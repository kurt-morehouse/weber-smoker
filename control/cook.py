"""Cook state: IDLE, COOKING or PAUSED, and the commands that move between them.

main.py starts IDLE: relays held off, readings streaming on the live
WebSocket, no CSV. A cook is started, paused and stopped from the UI:

    start         {"name": "brisket", "setpoints": {"zone1": 225, "zone2": 250}}
    pause         {"reason": "wrapping"}         relays off, logging continues
    resume        {}
    set_setpoint  {"zone": "zone1", "value_f": 240}
    stop          {}

Each cook writes data/cook_<YYYY-MM-DD>_<HHMMSS>_<name>.csv and a
..._meta.json next to it (name, times, setpoint changes, pauses and their
reasons, real or simulated hardware, the tuning used). The metadata is
rewritten on every change until the cook stops.

apply() returns (ok, message) and never raises for a bad command.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from data_logger import DataLogger

IDLE, COOKING, PAUSED = "idle", "cooking", "paused"
ZONES = ("zone1", "zone2")


def _now():
    return datetime.now().astimezone()


def safe_name(name):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return s[:40] or "cook"


class CookManager:
    def __init__(self, cfg, hardware_mode, clock_now=_now, on_change=None):
        self.cfg = cfg
        self.data_dir = Path(cfg["paths"]["data_dir"])
        self.hardware_mode = hardware_mode
        self.max_probes = cfg["chefiq"]["max_probes"]
        self.min_sp = cfg["safety"]["min_setpoint_f"]
        self.max_sp = cfg["safety"]["max_setpoint_f"]
        self._now = clock_now
        self.on_change = on_change        # called after start/resume (clears safety latch)
        self.state = IDLE
        self.setpoints = {z: float(cfg["zones"][z]["default_setpoint_f"]) for z in ZONES}
        self.logger = None
        self.meta = None
        self.meta_path = None
        self.csv_path = None

    # ------------------------------------------------------------ queries
    @property
    def relays_allowed(self):
        return self.state == COOKING

    def status(self):
        return {
            "state": self.state,
            "setpoints": dict(self.setpoints),
            "file": self.csv_path.name if self.csv_path else None,
            "name": self.meta["name"] if self.meta else None,
            "started": self.meta["started"] if self.meta else None,
        }

    # ----------------------------------------------------------- commands
    def apply(self, command, args=None):
        args = args or {}
        handler = {
            "start": self._start, "pause": self._pause, "resume": self._resume,
            "set_setpoint": self._set_setpoint, "stop": self._stop,
        }.get(command)
        if handler is None:
            return False, f"unknown command {command!r}"
        try:
            return handler(args)
        except (TypeError, ValueError, KeyError) as e:
            return False, f"bad {command} arguments: {e}"

    def _check_sp(self, value):
        v = float(value)
        if not self.min_sp <= v <= self.max_sp:
            raise ValueError(f"setpoint {v:g} F is outside {self.min_sp:g}..{self.max_sp:g} F")
        return v

    def _start(self, args):
        if self.state != IDLE:
            return False, f"a cook is already {self.state}; stop it first"
        sps = args.get("setpoints") or {}
        new_sp = dict(self.setpoints)
        for z in ZONES:
            if z in sps and sps[z] is not None:
                new_sp[z] = self._check_sp(sps[z])
        name = str(args.get("name") or "cook")[:80]
        t = self._now()
        stem = f"cook_{t:%Y-%m-%d_%H%M%S}_{safe_name(name)}"
        self.csv_path = self.data_dir / f"{stem}.csv"
        self.meta_path = self.data_dir / f"{stem}_meta.json"
        self.logger = DataLogger(self.csv_path, self.max_probes)
        self.setpoints = new_sp
        self.state = COOKING
        self.meta = {
            "name": name,
            "file": self.csv_path.name,
            "started": t.isoformat(timespec="seconds"),
            "stopped": None,
            "hardware": self.hardware_mode,
            "initial_setpoints_f": dict(new_sp),
            "setpoint_changes": [],
            "pauses": [],
            "tuning": {"pid": self.cfg["pid"], "relay": self.cfg["relay"],
                       "max_combined_duty": self.cfg["power"]["max_combined_duty"]},
            "rows": 0,
        }
        self._write_meta()
        if self.on_change:
            self.on_change()
        return True, f"cook started: {self.csv_path.name}"

    def _pause(self, args):
        if self.state != COOKING:
            return False, "nothing to pause"
        self.state = PAUSED
        self.meta["pauses"].append({"at": self._now().isoformat(timespec="seconds"),
                                    "reason": str(args.get("reason") or "")[:200],
                                    "resumed": None})
        self._write_meta()
        return True, "paused: relays off, still logging"

    def _resume(self, args):
        if self.state != PAUSED:
            return False, "not paused"
        self.state = COOKING
        if self.meta["pauses"]:
            self.meta["pauses"][-1]["resumed"] = self._now().isoformat(timespec="seconds")
        self._write_meta()
        if self.on_change:
            self.on_change()
        return True, "resumed"

    def _set_setpoint(self, args):
        zone = args["zone"]
        if zone not in ZONES:
            return False, f"unknown zone {zone!r}"
        v = self._check_sp(args["value_f"])
        old = self.setpoints[zone]
        self.setpoints[zone] = v
        if self.meta:
            self.meta["setpoint_changes"].append({
                "at": self._now().isoformat(timespec="seconds"),
                "zone": zone, "from_f": old, "to_f": v})
            self._write_meta()
        return True, f"{zone} setpoint {v:g} F"

    def _stop(self, args):
        if self.state == IDLE:
            return False, "no cook running"
        if self.meta["pauses"] and self.meta["pauses"][-1]["resumed"] is None:
            self.meta["pauses"][-1]["resumed"] = "stopped"
        self.meta["stopped"] = self._now().isoformat(timespec="seconds")
        self._write_meta()
        name = self.csv_path.name
        self.logger.close()
        self.logger = None
        self.state = IDLE
        self.meta = None
        self.csv_path = self.meta_path = None
        return True, f"cook stopped: {name}"

    # -------------------------------------------------------------- rows
    def log(self, row):
        """Write a row if a cook is running (cooking or paused)."""
        if self.logger is None:
            return
        self.logger.write(row)
        self.meta["rows"] = self.logger.rows
        if self.logger.rows % 60 == 0:
            self._write_meta()

    def close(self):
        if self.state != IDLE:
            self._stop({})

    def _write_meta(self):
        if self.meta_path:
            tmp = self.meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.meta, indent=2))
            tmp.replace(self.meta_path)
