"""Writes backend/data/sample_cook.csv: a made-up 10-hour two-zone cook with
two Chef iQ probes, for trying the replay without a real cook.

    python generate_sample_cook.py [out.csv]
"""
import csv
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

COLUMNS = (["timestamp", "zone1_temp_f", "zone2_temp_f", "zone1_relay_state",
            "zone2_relay_state", "zone1_target_f", "zone2_target_f"]
           + [f"probe{n}_{s}" for n in (1, 2, 3)
              for s in ("food_f", "t1_f", "t2_f", "t3_f", "t4_f", "ambient_f", "battery_pct")]
           + ["cook_state", "zone1_board_f", "zone2_board_f", "zone1_fault", "zone2_fault", "safety"])


def generate(path, hours=10.0, seed=7):
    rnd = random.Random(seed)
    start = datetime(2026, 10, 3, 6, 30).astimezone()
    targets = [225.0, 250.0]
    temps = [62.0, 62.0]
    food = [40.0, 42.0]
    battery = [100.0, 96.0]
    rows = int(hours * 3600)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for i in range(rows):
            t = start + timedelta(seconds=i)
            paused = 5 * 3600 <= i < 5 * 3600 + 600          # a 10-minute wrap
            if i == 7 * 3600:
                targets[0] = 240.0
            duty = []
            for z in range(2):
                err = targets[z] - temps[z]
                hold = 0.004 * (temps[z] - 55) / 1.1          # duty that balances heat loss
                d = 0.0 if paused else max(0.0, min(1.0, 0.08 * err + hold))
                temps[z] += 1.1 * d - 0.004 * (temps[z] - 55) + rnd.gauss(0, 0.08)
                duty.append(d)
            row = {"timestamp": t.isoformat(timespec="seconds"),
                   "zone1_temp_f": temps[0], "zone2_temp_f": temps[1],
                   "zone1_relay_state": duty[0], "zone2_relay_state": duty[1],
                   "zone1_target_f": targets[0], "zone2_target_f": targets[1],
                   "cook_state": "paused" if paused else "cooking",
                   "zone1_board_f": 80 + 0.02 * temps[0], "zone2_board_f": 80 + 0.02 * temps[1],
                   "zone1_fault": "", "zone2_fault": "", "safety": ""}
            for n in range(2):
                amb = temps[n]
                stall = 1.0 - 0.75 * math.exp(-((food[n] - 160) / 8) ** 2)
                food[n] += (amb - food[n]) * 0.00011 * stall
                battery[n] = max(5.0, battery[n] - 0.0022)
                p = f"probe{n + 1}_"
                row[p + "food_f"] = food[n]
                for k in range(1, 5):
                    v = food[n] + (k - 1) * (amb - food[n]) * 0.12
                    row[p + f"t{k}_f"] = v if v < 212 else None
                row[p + "ambient_f"] = amb - 4
                row[p + "battery_pct"] = round(battery[n])
            w.writerow(["" if row.get(c) is None else
                        (f"{row[c]:.2f}" if isinstance(row[c], float) else row[c])
                        for c in COLUMNS])
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("data") / "sample_cook.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    generate(out)
    print(f"wrote {out}")
