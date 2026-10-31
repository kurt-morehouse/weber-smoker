import csv
import json

import pytest

from cook import COOKING, IDLE, PAUSED, CookManager
from data_logger import DataLogger, columns, fmt
from helpers import config


def test_columns_contract():
    cols = columns(3)
    assert cols[:7] == ["timestamp", "zone1_temp_f", "zone2_temp_f", "zone1_relay_state",
                        "zone2_relay_state", "zone1_target_f", "zone2_target_f"]
    assert "probe3_battery_pct" in cols and "cook_state" in cols


def test_fmt():
    assert fmt(None) == "" and fmt(225.0) == "225" and fmt(0.125) == "0.12" and fmt("x") == "x"


def test_logger_writes_header_once(tmp_path):
    p = tmp_path / "c.csv"
    lg = DataLogger(p)
    lg.write({"timestamp": "t1", "zone1_temp_f": 200.5})
    lg.close()
    lg = DataLogger(p)
    lg.write({"timestamp": "t2"})
    lg.close()
    rows = list(csv.DictReader(open(p)))
    assert [r["timestamp"] for r in rows] == ["t1", "t2"]
    assert rows[0]["zone1_temp_f"] == "200.5" and rows[1]["zone1_temp_f"] == ""


@pytest.fixture
def cook(tmp_path):
    return CookManager(config(tmp_path), "simulated")


def test_lifecycle(cook, tmp_path):
    assert cook.state == IDLE and not cook.relays_allowed
    ok, msg = cook.apply("start", {"name": "Brisket #1", "setpoints": {"zone1": 230}})
    assert ok and cook.state == COOKING and cook.relays_allowed
    assert cook.csv_path.name.endswith("_brisket-1.csv")
    cook.log({"timestamp": "t", "cook_state": "cooking"})
    assert cook.apply("pause", {"reason": "wrapping"})[0]
    assert cook.state == PAUSED and not cook.relays_allowed
    assert cook.apply("resume")[0]
    assert cook.apply("set_setpoint", {"zone": "zone2", "value_f": 250})[0]
    meta_path = cook.meta_path
    assert cook.apply("stop")[0] and cook.state == IDLE
    meta = json.loads(meta_path.read_text())
    assert meta["name"] == "Brisket #1" and meta["hardware"] == "simulated"
    assert meta["initial_setpoints_f"]["zone1"] == 230
    assert meta["pauses"][0]["reason"] == "wrapping" and meta["pauses"][0]["resumed"]
    assert meta["setpoint_changes"][0]["to_f"] == 250
    assert meta["stopped"] and meta["rows"] == 1


def test_bad_commands(cook):
    assert not cook.apply("explode")[0]
    assert not cook.apply("pause")[0]
    assert not cook.apply("stop")[0]
    assert not cook.apply("start", {"setpoints": {"zone1": 900}})[0]
    assert cook.state == IDLE
    assert not cook.apply("set_setpoint", {"zone": "zone9", "value_f": 200})[0]
    assert not cook.apply("set_setpoint", {"zone": "zone1"})[0]
    cook.apply("start", {})
    assert not cook.apply("start", {})[0]


def test_start_and_resume_clear_the_safety_latch(tmp_path):
    calls = []
    c = CookManager(config(tmp_path), "real", on_change=lambda: calls.append(1))
    c.apply("start", {})
    c.apply("pause", {})
    c.apply("resume", {})
    assert len(calls) == 2
