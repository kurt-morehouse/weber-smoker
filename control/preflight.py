"""Hardware checks, run by systemd before main.py (ExecStartPre) and by hand.

Prints PASS / FAIL / WARN for each check and exits 1 if anything FAILs, so
the service doesn't start on broken hardware (systemd retries every 15 s).

    Config         config.yaml loads and passes every check
    SPI            /dev/spidev<bus>.<cs> exists for each zone
    SPI1 overlay   boot config has dtoverlay=spi1-1cs, never spi1-2cs/3cs
    MAX31855 zoneN reads a sane temperature (no fault, no "no response")
    GPIO library   RPi.GPIO loads in this Python (and from where)
    GPIO           both relay pins can be set up as outputs
    Relays off     both relay pins read low
    Live stream    websockets loads, for the dashboard (WARN only)
    Bluetooth      Chef iQ probes heard (WARN only: probes never control)

    cd ~/smoker/control && .venv/bin/python preflight.py
    uv run python preflight.py --simulate
"""
import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import hardware
from cook import ZONES
from gpio_lock import GpioLock, LockHeld
from grill_probe import ThermocoupleFault, ThermocoupleReader
from settings import ConfigError, load_config

BOOT_CONFIGS = ("/boot/firmware/config.txt", "/boot/config.txt")
SANE_F = (-20.0, 700.0)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn_only: bool = False

    @property
    def label(self):
        if self.ok:
            return "PASS"
        return "WARN" if self.warn_only else "FAIL"


def check_spi(cfg, dev_dir="/dev"):
    devices = [Path(dev_dir) / f"spidev{cfg['zones'][z]['spi_bus']}."
               f"{cfg['zones'][z]['spi_chip_select']}" for z in ZONES]
    missing = [d for d in devices if not d.exists()]
    if missing:
        hints = []
        if any(d.name.startswith("spidev0.") for d in missing):
            hints.append("enable SPI0: sudo raspi-config nonint do_spi 0")
        if any(d.name.startswith("spidev1.") for d in missing):
            hints.append("enable SPI1: add dtoverlay=spi1-1cs to /boot/firmware/config.txt "
                         "and reboot")
        return Check("SPI", False, f"missing {', '.join(str(d) for d in missing)} "
                                   f"({'; '.join(hints)})")
    return Check("SPI", True, ", ".join(str(d) for d in devices))


def check_spi1_overlay(cfg, boot_configs=BOOT_CONFIGS):
    """A spi1-2cs/3cs overlay always fails (it takes GPIO17/16), even
    when no zone uses SPI1. spi1-1cs is only required if a zone is on SPI1."""
    spi1_used = any(cfg["zones"][z]["spi_bus"] == 1 for z in ZONES)
    for path in boot_configs:
        p = Path(path)
        if not p.exists():
            continue
        lines = [ln.split("#", 1)[0].strip() for ln in p.read_text().splitlines()]
        bad = [ln for ln in lines if ln.startswith("dtoverlay=spi1-2cs")
               or ln.startswith("dtoverlay=spi1-3cs")]
        if bad:
            return Check("SPI1 overlay", False,
                         f"{path} has {bad[0]}: its extra chip selects take GPIO17 "
                         f"and GPIO16. Use dtoverlay=spi1-1cs only")
        if not spi1_used:
            return None
        if any(ln.startswith("dtoverlay=spi1-1cs") for ln in lines):
            return Check("SPI1 overlay", True, f"dtoverlay=spi1-1cs in {path}")
        return Check("SPI1 overlay", False, f"no dtoverlay=spi1-1cs in {path}")
    if not spi1_used:
        return None
    return Check("SPI1 overlay", True, "no boot config found (not a Pi?)", warn_only=True)


def check_thermocouple(cfg, zone, simulate):
    zc = cfg["zones"][zone]
    name = f"MAX31855 {zone}"
    try:
        reader = ThermocoupleReader(bus=zc["spi_bus"], device=zc["spi_chip_select"],
                                    max_speed_hz=cfg["spi"]["max_speed_hz"],
                                    sim_temp_f=cfg["simulation"]["start_temp_f"],
                                    sim_board_f=cfg["simulation"]["board_temp_f"],
                                    simulate=simulate)
    except (OSError, hardware.HardwareUnavailable) as e:
        return Check(name, False, f"can't open /dev/spidev{zc['spi_bus']}."
                                  f"{zc['spi_chip_select']}: {e}")
    try:
        readings = []
        for _ in range(3):
            readings.append(reader.read())
            time.sleep(0.05 if not simulate else 0)
        probe_f, board_f = readings[-1]
        if not SANE_F[0] <= probe_f <= SANE_F[1]:
            return Check(name, False, f"implausible reading {probe_f:.1f} °F")
        return Check(name, True, f"{probe_f:.1f} °F (board {board_f:.1f} °F)")
    except ThermocoupleFault as e:
        return Check(name, False, f"fault: {e}")
    finally:
        reader.close()


def check_gpio(cfg, simulate, gpio=None):
    """GPIO library, pin setup, and both relay pins low."""
    pins = {z: cfg["zones"][z]["gpio_pin"] for z in ZONES}
    if simulate:
        return [Check("GPIO library", True, "simulated"),
                Check("GPIO", True, "simulated: " + ", ".join(f"{z} GPIO{p}" for z, p in pins.items())),
                Check("Relays off", True, "simulated")]
    try:
        gpio = gpio or hardware.load_gpio()
    except hardware.HardwareUnavailable as e:
        return [Check("GPIO library", False, str(e))]
    version = getattr(gpio, "VERSION", "?")
    out = [Check("GPIO library", True, f"RPi.GPIO {version} from {getattr(gpio, '__file__', '?')} "
                                       f"in {sys.executable}")]
    try:
        gpio.setwarnings(False)
        gpio.setmode(gpio.BCM)
        for p in pins.values():
            gpio.setup(p, gpio.OUT, initial=gpio.LOW)
        out.append(Check("GPIO", True, ", ".join(f"{z} GPIO{p}" for z, p in pins.items())))
        high = [f"GPIO{p}" for p in pins.values() if gpio.input(p)]
        out.append(Check("Relays off", not high,
                         "both relay pins read low" if not high else f"{', '.join(high)} read HIGH"))
    except Exception as e:
        out.append(Check("GPIO", False, f"{type(e).__name__}: {e}"))
    finally:
        try:
            for p in pins.values():
                gpio.output(p, gpio.LOW)
            gpio.cleanup(list(pins.values()))
        except Exception:
            pass
    return out


def check_websockets(cfg):
    """The live stream's library. WARN only: the grill runs without the stream."""
    if not cfg["websocket"]["enabled"]:
        return Check("Live stream", True, "disabled in config")
    import live_server
    try:
        _, legacy = live_server.load_serve()
    except ImportError as e:
        return Check("Live stream", False, str(e), warn_only=True)
    v = live_server.websockets_version()
    note = " (legacy API; uv sync to update)" if legacy else ""
    return Check("Live stream", True, f"websockets {v[0]}.{v[1]}{note} in {sys.executable}")


def check_bluetooth(cfg, simulate, seconds=8.0):
    if not cfg["chefiq"]["enabled"]:
        return Check("Bluetooth", True, "Chef iQ disabled in config")
    if simulate:
        return Check("Bluetooth", True, "simulated")
    from chefiq_ble import BleScanner, ProbeTracker
    tracker = ProbeTracker(cfg["chefiq"]["max_probes"], 60, None)
    scanner = BleScanner(tracker).start()
    time.sleep(seconds)
    scanner.stop()
    if scanner.error:
        return Check("Bluetooth", False, scanner.error, warn_only=True)
    names = tracker.heard_names()
    if not names:
        return Check("Bluetooth", False, "no Chef iQ probes heard (out of the charger?)",
                     warn_only=True)
    return Check("Bluetooth", True, f"{len(tracker.heard)} heard: {', '.join(names)}")


def run(cfg, simulate, dev_dir="/dev", boot_configs=BOOT_CONFIGS, gpio=None, bluetooth=True):
    checks = []
    if simulate:
        checks.append(Check("SPI", True, "simulated"))
    else:
        checks.append(check_spi(cfg, dev_dir))
        ov = check_spi1_overlay(cfg, boot_configs)
        if ov:
            checks.append(ov)
    for z in ZONES:
        checks.append(check_thermocouple(cfg, z, simulate))
    checks.extend(check_gpio(cfg, simulate, gpio))
    checks.append(check_websockets(cfg))
    if bluetooth:
        checks.append(check_bluetooth(cfg, simulate))
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(description="smoker hardware checks")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--no-bluetooth", action="store_true")
    args = ap.parse_args(argv)
    print(hardware.banner(args.simulate), flush=True)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"FAIL  Config  {e}")
        print("preflight FAILED: Config")
        return 1
    lock = None
    if not args.simulate:
        try:
            lock = GpioLock(cfg["paths"]["gpio_lock_file"], "preflight.py").acquire()
        except LockHeld as e:
            print(f"FAIL  GPIO lock  {e}")
            print("preflight FAILED: GPIO lock")
            return 1
    try:
        checks = run(cfg, args.simulate, bluetooth=not args.no_bluetooth)
    finally:
        if lock:
            lock.release()
    for c in checks:
        print(f"{c.label:20} {c.name:15} {c.detail}", flush=True)
    failed = [c.name for c in checks if not c.ok and not c.warn_only]
    if failed:
        print(f"preflight FAILED: {', '.join(failed)}", flush=True)
        return 1
    print("preflight passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
