"""Manual SSR test: pulse one zone's relay for a few seconds, by hand.

TEST WITH THE HEATING ELEMENTS UNPLUGGED FIRST. Watch each SSR's indicator
light; only plug the elements back in once both zones switch on and off
exactly as asked.

    sudo systemctl stop smoker      # main.py holds the relays; stop it first
    cd ~/smoker/control && .venv/bin/python ssr_test.py
    uv run python ssr_test.py --simulate    # on the Mac, no hardware

Zone 1 = GPIO22 = header pin 15 -> SSR 1 terminal 3 (+)
Zone 2 = GPIO23 = header pin 16 -> SSR 2 terminal 3 (+)
(the pins come from config.yaml; the physical pin is shown for checking)

It is deliberately hard to misuse:
- Only from a terminal: it has no network side, nothing in the UI or the
  live WebSocket can reach it, and it refuses to run without an interactive
  terminal, so it can't be scripted or piped into.
- One zone at a time: each pulse opens a driver for that zone's pin only
  and closes it again, so the other zone's pin is never touched.
- Each pulse is capped at ssr_test.max_pulse_seconds (config.yaml, itself
  capped by safety.ABSOLUTE_MAX_SSR_TEST_PULSE_S), and needs the zone number
  typed again to confirm it.
- The relay driver's failsafe stays on (relay.failsafe_timeout_s): the
  pulse re-arms it every ssr_test.refresh_seconds, so if this tool hangs
  the relay drops out by itself. When the pulse ends, or on Ctrl+C or any
  error, the driver is closed, which drives the pin low at once.
- It won't start while main.py (or preflight.py) holds the GPIO lock.
- It only simulates with --simulate. Without it, RPi.GPIO failing to load
  stops it before the first prompt, showing the import error; it never
  quietly pulses a fake relay. It says REAL GPIO or SIMULATED first, and
  the log's session start line records which.
- Every pulse is appended, timestamped, to paths.ssr_test_log.
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import hardware
import safety
from gpio_lock import GpioLock, LockHeld
from relay_driver import SSRDriver
from settings import ConfigError, load_config

ZONE_NUMBERS = {"1": "zone1", "2": "zone2"}
FULL_ON = 1.0

# BCM GPIO -> physical header pin, for the log and the prompts.
BCM_TO_PHYSICAL = {2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23,
                   12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38,
                   21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13}


class PulseLog:
    """Appends one timestamped line per event to paths.ssr_test_log."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text):
        line = f"{datetime.now().astimezone().isoformat(timespec='milliseconds')}  {text}"
        with open(self.path, "a") as f:
            f.write(line + "\n")
        return line


def describe(cfg, zone):
    pin = cfg["zones"][zone]["gpio_pin"]
    return f"{zone} GPIO{pin} (pin {BCM_TO_PHYSICAL.get(pin, '?')})"


def pulse(cfg, zone, seconds, simulate, log, gpio=None, clock=time.monotonic, sleep=time.sleep):
    """Turn one zone's relay fully on for `seconds`, then off. Returns seconds on."""
    r = cfg["relay"]
    t = cfg["ssr_test"]
    limit = min(t["max_pulse_seconds"], safety.ABSOLUTE_MAX_SSR_TEST_PULSE_S)
    seconds = max(0.0, min(float(seconds), limit))
    pin = cfg["zones"][zone]["gpio_pin"]
    driver = SSRDriver(pin, window_s=r["window_seconds"], min_on_s=r["min_on_seconds"],
                       failsafe_timeout_s=r["failsafe_timeout_s"], simulate=simulate,
                       gpio=gpio, clock=clock)
    on_at = None
    try:
        driver.set_duty(FULL_ON)
        on_at = clock()
        log.write(f"ON   {describe(cfg, zone)} for {seconds:g} s")
        while clock() - on_at < seconds:
            sleep(min(t["refresh_seconds"], max(0.0, seconds - (clock() - on_at))))
            driver.set_duty(FULL_ON)      # re-arm the failsafe
    finally:
        driver.close()                    # pin low at once
        held = 0.0 if on_at is None else clock() - on_at
        log.write(f"OFF  {describe(cfg, zone)} after {held:.2f} s")
    return held


def main(argv=None, stdin=sys.stdin, input_fn=input):
    ap = argparse.ArgumentParser(description="pulse one SSR by hand")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    print(hardware.banner(args.simulate))
    if not stdin.isatty():
        print("ssr_test.py only runs from an interactive terminal.")
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e)
        return 2
    if not args.simulate:
        try:
            hardware.load_gpio()
        except hardware.HardwareUnavailable as e:
            print(f"{e}\nNo pulses. (Use --simulate to try it without hardware.)")
            return 3
    log = PulseLog(cfg["paths"]["ssr_test_log"])
    lock = None
    if not args.simulate:
        try:
            lock = GpioLock(cfg["paths"]["gpio_lock_file"], "ssr_test.py").acquire()
        except LockHeld as e:
            print(e)
            return 4
    limit = min(cfg["ssr_test"]["max_pulse_seconds"], safety.ABSOLUTE_MAX_SSR_TEST_PULSE_S)
    log.write(f"session start ({hardware.mode_name(args.simulate)} GPIO)")
    print("ELEMENTS UNPLUGGED? Watch the SSR lights.")
    for n, z in ZONE_NUMBERS.items():
        print(f"  {n} = {describe(cfg, z)}")
    try:
        while True:
            choice = input_fn("\nZone to pulse (1/2, q to quit): ").strip().lower()
            if choice in ("q", "quit", ""):
                break
            zone = ZONE_NUMBERS.get(choice)
            if not zone:
                print("Type 1, 2 or q.")
                continue
            secs_text = input_fn(f"Seconds (up to {limit:g}) [2]: ").strip() or "2"
            try:
                secs = float(secs_text)
            except ValueError:
                print("Not a number.")
                continue
            if secs <= 0:
                print("Must be more than 0.")
                continue
            if secs > limit:
                print(f"Capped at {limit:g} s.")
                secs = limit
            confirm = input_fn(f"Type {choice} again to pulse {describe(cfg, zone)} "
                               f"for {secs:g} s: ").strip()
            if confirm != choice:
                print("Not confirmed; nothing pulsed.")
                continue
            print(f"{describe(cfg, zone)} ON ...", flush=True)
            held = pulse(cfg, zone, secs, args.simulate, log)
            print(f"{describe(cfg, zone)} OFF after {held:.1f} s")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        log.write("session end")
        if lock:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
