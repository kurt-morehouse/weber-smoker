"""Reads cook CSVs: lists them and parses them into rows.

Understands three kinds of file in the data folder:
  cook_*.csv        a cook logged by the Pi (with cook_*_meta.json beside it)
  live_*.csv        the backend's own recording of the Pi's live stream
  chefiq_*.csv      the standalone Chef iQ logger (short column names)

Rows come back as dicts: numbers as floats, blanks as None, text as text.
The Chef iQ logger's short names (probe1_food, probe1_battery) are renamed
to the Pi's (probe1_food_f, probe1_battery_pct) so the UI sees one set.
"""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

TEXT_COLUMNS = {"timestamp", "cook_state", "zone1_fault", "zone2_fault", "safety"}
KINDS = (("cook_", "cook"), ("live_", "live"), ("chefiq_", "chefiq"))
_SHORT = re.compile(r"^(probe\d+)_(food|t[1-4]|ambient|battery)$")


def normalize_column(name):
    m = _SHORT.match(name)
    if not m:
        return name
    probe, field = m.groups()
    return f"{probe}_{field}_pct" if field == "battery" else f"{probe}_{field}_f"


def parse_value(col, text):
    if text is None or text == "":
        return None
    if col in TEXT_COLUMNS:
        return text
    try:
        return float(text)
    except ValueError:
        return text


def read_rows(path):
    """(columns, rows) for one CSV."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return [], []
        cols = [normalize_column(c) for c in header]
        rows = []
        for rec in reader:
            if not rec:
                continue
            rows.append({c: parse_value(c, v) for c, v in zip(cols, rec)})
    return cols, rows


def kind_of(name):
    for prefix, kind in KINDS:
        if name.startswith(prefix):
            return kind
    return "other"


def _count_rows(path):
    with open(path, "rb") as f:
        return max(0, sum(1 for line in f if line.strip()) - 1)


def describe(path):
    path = Path(path)
    meta_path = path.with_name(path.stem + "_meta.json")
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
    stat = path.stat()
    return {
        "file": path.name,
        "kind": kind_of(path.name),
        "name": meta.get("name") or path.stem,
        "started": meta.get("started"),
        "stopped": meta.get("stopped"),
        "hardware": meta.get("hardware"),
        "rows": _count_rows(path),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def list_cooks(data_dir):
    d = Path(data_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.glob("*.csv") if p.is_file()]
    out = [describe(p) for p in files]
    out.sort(key=lambda c: c["modified"], reverse=True)
    return out


def safe_path(data_dir, name):
    """Resolve a file name inside data_dir, or raise ValueError."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("bad file name")
    p = (Path(data_dir) / name).resolve()
    if p.parent != Path(data_dir).resolve() or not p.is_file() or p.suffix != ".csv":
        raise ValueError("no such cook")
    return p
