"""Standalone Chef iQ probe logger, for the Mac (or the Pi) as a backup.

The Pi's cook CSV already logs every Chef iQ reading. This runs on its own,
for example on the Mac when the Pi is out of Bluetooth range, and writes
data/chefiq_<source>_<date>_<time>.csv: one row per second, with values blank
once a probe has been silent for --stale-after seconds. Its columns use the
short names probeN_food, probeN_t1..t4, probeN_ambient, probeN_battery; the
backend reads both these and the Pi's names.

    caffeinate -i uv run python chefiq_logger.py     # Mac: keep it awake
    uv run python chefiq_logger.py --raw              # raw packets, for decoding

The first time, macOS asks Terminal for Bluetooth permission. Ctrl+C stops.
After the cook, line the file up with the Pi's CSV using
analysis/merge_probes.py.
"""
import argparse
import csv
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from chefiq_ble import FIELDS, BleScanner, ProbeTracker
from settings import ConfigError, load_config


def columns(max_probes):
    cols = ["timestamp"]
    for n in range(1, max_probes + 1):
        cols += [f"probe{n}_{f}" for f in FIELDS]
    return cols


def row_from_snapshot(snap, max_probes, now=None):
    row = {"timestamp": (now or datetime.now().astimezone()).isoformat(timespec="seconds")}
    for n in range(1, max_probes + 1):
        for f in FIELDS:
            row[f"probe{n}_{f}"] = snap.get(n, {}).get(f)
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=["pi", "mac"],
                    default="mac" if platform.system() == "Darwin" else "pi")
    ap.add_argument("--out", help="CSV file or folder (default: data/)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--stale-after", type=float, default=None,
                    help="seconds before a silent probe logs blank (default: config)")
    ap.add_argument("--raw", action="store_true",
                    help="also log every raw advertisement to paths.chefiq_raw_log")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    cq = cfg["chefiq"]
    stale = args.stale_after if args.stale_after is not None else cq["stale_seconds"]
    slots_file = Path(cfg["paths"]["data_dir"]) / f"chefiq_{args.source}_slots.json"
    tracker = ProbeTracker(cq["max_probes"], stale, slots_file)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = Path(args.out) if args.out else Path(cfg["paths"]["data_dir"])
    if out.suffix.lower() != ".csv":
        out = out / f"chefiq_{args.source}_{stamp}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    raw_writer = None
    raw_file = None
    if args.raw:
        raw_path = Path(cfg["paths"]["chefiq_raw_log"])
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        new = not raw_path.exists()
        raw_file = open(raw_path, "a", newline="")
        raw_writer = csv.writer(raw_file)
        if new:
            raw_writer.writerow(["timestamp", "address", "name", "rssi", "payload_hex"])

    def raw_log(address, name, rssi, payload):
        if raw_writer:
            raw_writer.writerow([datetime.now().astimezone().isoformat(timespec="milliseconds"),
                                 address, name, rssi, payload.hex()])
            raw_file.flush()

    scanner = BleScanner(tracker, raw_log if args.raw else None).start()
    print(f"Logging Chef iQ probes to {out} every {args.interval:g} s. Ctrl+C to stop.")
    print("A probe appears once its name packet arrives (up to ~30 s).")
    cols = columns(cq["max_probes"])
    try:
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            next_t = time.monotonic()
            while True:
                snap = tracker.snapshot()
                w.writerow(row_from_snapshot(snap, cq["max_probes"]))
                fh.flush()
                live = [f"P{n} {v['food']:.1f}F" for n, v in snap.items() if v["food"] is not None]
                status = "  ".join(live) or "waiting for probes"
                if scanner.error:
                    status += f"  [{scanner.error}]"
                print(f"\r{datetime.now():%H:%M:%S}  {status}      ", end="", flush=True)
                next_t += args.interval
                time.sleep(max(0.0, next_t - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        if raw_file:
            raw_file.close()
        print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
