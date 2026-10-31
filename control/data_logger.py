"""Writes synchronized rows to one CSV per cook, at a fixed interval.

The column list here is the contract with the Mac backend (backend/replay.py)
and the analysis notebooks. Add new columns at the end; never rename one.

Blank cells mean no recent reading. Each cook also gets
cook_<...>_meta.json (see cook.py), rewritten until the cook stops.
"""
import csv
from pathlib import Path

ZONE_COLUMNS = [
    "zone1_temp_f", "zone2_temp_f",
    "zone1_relay_state", "zone2_relay_state",
    "zone1_target_f", "zone2_target_f",
]
PROBE_FIELDS = [("food", "food_f"), ("t1", "t1_f"), ("t2", "t2_f"), ("t3", "t3_f"),
                ("t4", "t4_f"), ("ambient", "ambient_f"), ("battery", "battery_pct")]
EXTRA_COLUMNS = ["zone1_board_f", "zone2_board_f", "zone1_fault", "zone2_fault", "safety"]


def columns(max_probes=3):
    cols = ["timestamp"] + ZONE_COLUMNS
    for n in range(1, max_probes + 1):
        cols += [f"probe{n}_{suffix}" for _, suffix in PROBE_FIELDS]
    cols += ["cook_state"] + EXTRA_COLUMNS
    return cols


def fmt(value):
    """CSV cell text: '' for None, floats rounded to 2 places."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value == value else ""
    return str(value)


class DataLogger:
    def __init__(self, path, max_probes=3):
        self.path = Path(path)
        self.columns = columns(max_probes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self._fh = open(self.path, "a", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=self.columns, extrasaction="ignore")
        if new:
            self._w.writeheader()
        self.rows = 0

    def write(self, row):
        self._w.writerow({c: fmt(row.get(c)) for c in self.columns})
        self._fh.flush()
        self.rows += 1

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
