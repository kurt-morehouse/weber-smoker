"""Entry point: runs both zone loops, the Chef iQ scanner and the live stream.

Every loop.interval_seconds it:
  1. runs any cook commands that arrived on the WebSocket (cook.py),
  2. reads both MAX31855 thermocouples,
  3. asks safety.py whether each relay may be on at all,
  4. runs each zone's PID (only while COOKING), caps combined duty,
  5. sets both relays (which also re-arms their failsafe),
  6. builds one row: logs it to the cook CSV (if a cook is running) and
     publishes it on the live WebSocket.

It starts IDLE: relays held off, readings streaming, no CSV. On exit (Ctrl+C,
SIGTERM from systemd, or any error) both relays are driven off and a running
cook is stopped, so restarting the service ends a cook.

It only simulates with --simulate. Without it, RPi.GPIO or spidev failing
to load stops it with the import error; it never quietly runs on fake
hardware. It prints REAL GPIO or SIMULATED first, and each cook's _meta.json
records "hardware": "real" or "simulated".

    sudo systemctl start smoker              # on the Pi, normally
    uv run python main.py --simulate         # anywhere, no hardware
"""
import argparse
import signal
import sys
import time
from datetime import datetime

import hardware
from chefiq_ble import BleScanner, FakeProbes, ProbeTracker
from cook import COOKING, ZONES, CookManager
from gpio_lock import GpioLock, LockHeld
from grill_probe import ThermocoupleFault, ThermocoupleReader
from live_server import LiveServer, read_token, wait_listening
from pid_controller import PID
from relay_driver import SSRDriver
from safety import SafetyMonitor, cap_combined_duty
from settings import ConfigError, load_config


class Zone:
    def __init__(self, zone_id, cfg, simulate, gpio=None, threaded=True, clock=time.monotonic):
        zc = cfg["zones"][zone_id]
        sim = cfg["simulation"]
        r = cfg["relay"]
        p = cfg["pid"]
        self.id = zone_id
        self.probe = ThermocoupleReader(bus=zc["spi_bus"], device=zc["spi_chip_select"],
                                        max_speed_hz=cfg["spi"]["max_speed_hz"],
                                        sim_temp_f=sim["start_temp_f"],
                                        sim_board_f=sim["board_temp_f"], simulate=simulate)
        self.relay = SSRDriver(zc["gpio_pin"], window_s=r["window_seconds"],
                               min_on_s=r["min_on_seconds"],
                               failsafe_timeout_s=r["failsafe_timeout_s"],
                               simulate=simulate, gpio=gpio, threaded=threaded,
                               clock=clock)
        self.pid = PID(p["kp"], p["ki"], p["kd"], integral_limit=p["integral_limit"])
        self.temp_f = None
        self.board_f = None
        self.fault = None
        self.fault_message = None
        self.last_good = None

    def read(self, now):
        try:
            self.temp_f, self.board_f = self.probe.read()
            self.fault = self.fault_message = None
            self.last_good = now
        except ThermocoupleFault as e:
            self.fault, self.fault_message = e.kind, str(e)

    def close(self):
        self.relay.close()
        self.probe.close()


class GrillSim:
    """A crude two-zone grill for --simulate: heat in from duty, loss to ambient,
    a little coupling between the zones."""

    def __init__(self, zones, sim_cfg):
        self.zones = zones
        self.c = sim_cfg
        self.t = {z.id: sim_cfg["start_temp_f"] for z in zones}

    def step(self, dt):
        dt *= self.c["speedup"]
        amb = self.c["ambient_f"]
        mean = sum(self.t.values()) / len(self.t)
        for z in self.zones:
            on = 1.0 if z.relay.is_on else 0.0
            t = self.t[z.id]
            t += self.c["heat_rate_f_per_s"] * on * dt
            t -= self.c["loss_per_s"] * (t - amb) * dt
            t += 0.002 * (mean - t) * dt
            self.t[z.id] = t
            z.probe.sim_temp_f = t

    def temps(self):
        return [self.t[z.id] for z in self.zones]


class Controller:
    def __init__(self, cfg, simulate, gpio=None, threaded=True, clock=time.monotonic,
                 live=None, tracker=None):
        self.cfg = cfg
        self.simulate = simulate
        self.clock = clock
        self.zones = [Zone(z, cfg, simulate, gpio=gpio, threaded=threaded, clock=clock)
                      for z in ZONES]
        s = cfg["safety"]
        self.safety = SafetyMonitor(s["max_temp_f"], s["stale_seconds"], s["max_board_temp_f"])
        self.cook = CookManager(cfg, hardware.mode_name(simulate),
                                on_change=self.safety.clear_latch)
        cq = cfg["chefiq"]
        self.tracker = tracker or ProbeTracker(cq["max_probes"], cq["stale_seconds"],
                                               cfg["paths"]["chefiq_slots_file"], clock=clock)
        self.live = live
        self.sim = GrillSim(self.zones, cfg["simulation"]) if simulate else None
        self.fake_probes = None
        if simulate and cq["enabled"] and cfg["simulation"]["fake_probes"]:
            self.fake_probes = FakeProbes(self.tracker, cfg["simulation"]["fake_probes"],
                                          self.sim.temps, cfg["simulation"]["start_temp_f"])
        self._last = None
        self.last_row = None
        self.verdicts = {}

    # ---------------------------------------------------------------- loop
    def handle_commands(self):
        if not self.live:
            return
        for client, cmd_id, command, args in self.live.pending_commands():
            ok, message = self.cook.apply(command, args)
            if command in ("set_setpoint", "start") and ok:
                for z in self.zones:
                    z.pid.reset()
            self.live.reply(client, cmd_id, ok, message)

    def step(self):
        now = self.clock()
        dt = self.cfg["loop"]["interval_seconds"] if self._last is None else now - self._last
        self._last = now
        if self.sim:
            self.sim.step(dt)
        if self.fake_probes:
            self.fake_probes.step(dt * self.cfg["simulation"]["speedup"])
        self.handle_commands()

        duties = []
        for z in self.zones:
            z.read(now)
            age = None if z.last_good is None else now - z.last_good
            v = self.safety.evaluate_zone(z.temp_f, z.fault, age, z.board_f)
            self.verdicts[z.id] = v
            if v.allowed and self.cook.state == COOKING:
                duties.append(z.pid.update(self.cook.setpoints[z.id], z.temp_f, dt))
            else:
                if not v.allowed:
                    z.pid.reset()
                duties.append(0.0)
        duties = cap_combined_duty(duties, self.cfg["power"]["max_combined_duty"])
        for z, d in zip(self.zones, duties):
            z.relay.set_duty(d)

        row = self.build_row(duties)
        self.cook.log(row)
        if self.live:
            self.live.publish(row, self.status())
        self.last_row = row
        return row

    def build_row(self, duties):
        row = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds")}
        for z, d in zip(self.zones, duties):
            n = z.id[-1]
            fresh = z.fault is None and z.temp_f is not None
            row[f"zone{n}_temp_f"] = round(z.temp_f, 2) if fresh else None
            row[f"zone{n}_relay_state"] = round(d, 3)
            row[f"zone{n}_target_f"] = self.cook.setpoints[z.id]
            row[f"zone{n}_board_f"] = round(z.board_f, 2) if z.board_f is not None else None
            row[f"zone{n}_fault"] = z.fault or ""
        for n, vals in self.tracker.snapshot().items():
            row[f"probe{n}_food_f"] = vals["food"]
            for i in range(1, 5):
                row[f"probe{n}_t{i}_f"] = vals[f"t{i}"]
            row[f"probe{n}_ambient_f"] = vals["ambient"]
            row[f"probe{n}_battery_pct"] = vals["battery"]
        row["cook_state"] = self.cook.state if self.cook.state != "idle" else ""
        row["safety"] = "; ".join(f"{zid}: {v.reason}" for zid, v in self.verdicts.items()
                                  if not v.allowed and v.reason != "no reading yet") or ""
        return row

    def status(self):
        st = self.cook.status()
        st["hardware"] = hardware.mode_name(self.simulate)
        st["safety_latched"] = self.safety.latched
        st["faults"] = {z.id: z.fault_message for z in self.zones if z.fault}
        return st

    def close(self):
        for z in self.zones:
            try:
                z.relay.close()
            except Exception:
                pass
        try:
            self.cook.close()
        except Exception:
            pass
        for z in self.zones:
            try:
                z.probe.close()
            except Exception:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weber smoker controller")
    ap.add_argument("--simulate", action="store_true", help="fake probes, relays and grill")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args(argv)

    print(hardware.banner(args.simulate), flush=True)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, flush=True)
        return 2
    if not args.simulate:
        try:
            hardware.load_gpio()
            hardware.load_spidev()
        except hardware.HardwareUnavailable as e:
            print(f"{e}\nRefusing to start. (Use --simulate for a run without hardware.)",
                  flush=True)
            return 3

    lock = None
    if not args.simulate:
        try:
            lock = GpioLock(cfg["paths"]["gpio_lock_file"], "main.py").acquire()
        except LockHeld as e:
            print(e, flush=True)
            return 4

    live = None
    ws = cfg["websocket"]
    if ws["enabled"]:
        token = read_token(cfg["paths"]["command_token_file"])
        if not token:
            print(f"no command token at {cfg['paths']['command_token_file']}: "
                  f"streaming only, every command will be refused", flush=True)
        live = LiveServer(ws["host"], ws["port"], ws["history_rows"],
                          ws["max_pending_commands"], token, ws["retry_seconds"]).start()
        if not wait_listening(live, timeout=5.0):
            # Keep controlling the grill (the relays don't depend on the stream),
            # but say loudly why the dashboard will see nothing.
            print(f"WARNING: {live.error}", flush=True)

    ctl = None
    scanner = None
    stop = {"flag": False}

    def on_signal(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        ctl = Controller(cfg, args.simulate, live=live)
        if cfg["chefiq"]["enabled"] and not args.simulate:
            scanner = BleScanner(ctl.tracker).start()
        interval = cfg["loop"]["interval_seconds"]
        stream = (f"live stream on port {ws['port']}" if live and live.listening.is_set()
                  else "NO live stream" if ws["enabled"] else "live stream disabled")
        print(f"running: IDLE, relays off; {stream}", flush=True)
        next_t = time.monotonic()
        last_print = 0.0
        while not stop["flag"]:
            row = ctl.step()
            if time.monotonic() - last_print >= 30:
                last_print = time.monotonic()
                print(f"{ctl.cook.state:8} z1 {row['zone1_temp_f']} F ({row['zone1_relay_state']})"
                      f"  z2 {row['zone2_temp_f']} F ({row['zone2_relay_state']})"
                      + (f"  [{row['safety']}]" if row["safety"] else ""), flush=True)
            next_t += interval
            delay = next_t - time.monotonic()
            if delay < -interval:            # fell far behind: don't try to catch up
                next_t = time.monotonic()
            else:
                time.sleep(max(0.0, delay))
    finally:
        if ctl:
            ctl.close()
        if scanner:
            scanner.stop()
        if live:
            live.stop()
        if lock:
            lock.release()
        print("stopped: relays off", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
