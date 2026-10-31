"""Line up a Pi cook CSV with a Chef iQ logger CSV by nearest timestamp.

The Mac logger's columns come in with a _mac suffix (probe1_food_f_mac ...),
so both copies of a probe can be compared side by side. Both machines' clocks
must be NTP-synced (the default on macOS and Raspberry Pi OS).

    uv run --no-project --with pandas python merge_probes.py \
        ../data/cook_<...>.csv ../data/chefiq_mac_<...>.csv -o merged.csv

In a notebook: from merge_probes import merge_cook_with_probes
"""
import argparse
import re
import sys

import pandas as pd

_SHORT = re.compile(r"^(probe\d+)_(food|t[1-4]|ambient|battery)$")


def _normalize(col):
    m = _SHORT.match(col)
    if not m:
        return col
    probe, field = m.groups()
    return f"{probe}_{field}_pct" if field == "battery" else f"{probe}_{field}_f"


def _read(path):
    df = pd.read_csv(path)
    df.columns = [_normalize(c) for c in df.columns]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    return df.sort_values("timestamp").reset_index(drop=True)


def merge_cook_with_probes(cook_csv, probes_csv, tolerance_s=2.0, suffix="_mac"):
    """Every cook row, with the nearest probe-log row within tolerance_s."""
    cook = _read(cook_csv)
    probes = _read(probes_csv)
    probes = probes.rename(columns={c: c + suffix for c in probes.columns if c != "timestamp"})
    return pd.merge_asof(cook, probes, on="timestamp", direction="nearest",
                         tolerance=pd.Timedelta(seconds=tolerance_s))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cook_csv")
    ap.add_argument("probes_csv")
    ap.add_argument("-o", "--out", default="merged.csv")
    ap.add_argument("--tolerance", type=float, default=2.0, help="seconds")
    a = ap.parse_args(argv)
    df = merge_cook_with_probes(a.cook_csv, a.probes_csv, a.tolerance)
    df.to_csv(a.out, index=False)
    print(f"wrote {a.out}: {len(df)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
