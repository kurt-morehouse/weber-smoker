import pandas as pd

from merge_probes import merge_cook_with_probes


def test_nearest_match(tmp_path):
    cook = tmp_path / "cook.csv"
    cook.write_text("timestamp,zone1_temp_f,probe1_food_f\n"
                    "2026-10-03T10:00:00-04:00,225,150\n"
                    "2026-10-03T10:00:01-04:00,226,151\n"
                    "2026-10-03T10:00:10-04:00,227,152\n")
    mac = tmp_path / "mac.csv"
    mac.write_text("timestamp,probe1_food,probe1_battery\n"
                   "2026-10-03T14:00:00.4+00:00,149.5,80\n"
                   "2026-10-03T14:00:01.2+00:00,150.5,80\n")
    df = merge_cook_with_probes(cook, mac)
    assert list(df["probe1_food_f_mac"][:2]) == [149.5, 150.5]
    assert pd.isna(df["probe1_food_f_mac"][2])          # nothing within 2 s
    assert "probe1_battery_pct_mac" in df.columns
