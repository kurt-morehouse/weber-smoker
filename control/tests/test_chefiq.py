"""chefiq_decode and chefiq_ble.ProbeTracker. Real CQ60 captures from the
chefiq-ble project's tests (protocol 5.0.0)."""
import json

import pytest

import chefiq_decode as dec
from chefiq_ble import FakeProbes, ProbeTracker
from chefiq_logger import columns as mac_columns
from helpers import ManualClock

V3_TEMPERATURE = bytes.fromhex("015024012b0135012e012b0130012401315a")
V3_STATUS = bytes.fromhex("0350c91465ca079a64201e010301007ac4")
V3_NAME = bytes.fromhex("005050726f6265203100000000000a2b")   # "Probe 1"
ADDR = "C9:14:65:CA:07:9A"


def test_real_temperature_packet():
    p = dec.decode(V3_TEMPERATURE)
    assert p["type"] == "temperature"
    assert p["ambient_c"] == 29.2
    assert p["food_c"] == 29.9
    assert p["tips_c"] == [30.9, 30.2, 29.9, 30.4]


def test_real_status_packet():
    p = dec.decode(V3_STATUS)
    assert p == {"type": "status", "mac": ADDR, "battery_pct": 100}


def test_real_name_packet():
    assert dec.decode(V3_NAME) == {"type": "name", "name": "Probe 1"}
    assert dec.probe_slot_from_name("Probe 1") == 1
    assert dec.probe_slot_from_name("CQ60") is None


def test_sentinel_is_none():
    msg = bytearray(V3_TEMPERATURE)
    msg[6:8] = (0x7FFF).to_bytes(2, "little")
    assert dec.decode(bytes(msg))["tips_c"][0] is None


@pytest.mark.parametrize("payload", [b"", b"\x01", bytes(25), bytes.fromhex("0170") + bytes(14)])
def test_rejects_non_probe_payloads(payload):
    assert dec.decode(payload) is None


def tracker(tmp_path, clock=None):
    return ProbeTracker(3, 30, tmp_path / "slots.json", clock=clock or ManualClock())


def test_readings_wait_for_the_name_packet(tmp_path):
    t = tracker(tmp_path)
    t.feed(ADDR, V3_TEMPERATURE)
    assert t.snapshot()[1]["food"] is None
    t.feed(ADDR, V3_NAME)
    t.feed(ADDR, V3_TEMPERATURE)
    t.feed(ADDR, V3_STATUS)
    s = t.snapshot()[1]
    assert s["food"] == pytest.approx(85.8)
    assert s["ambient"] == pytest.approx(84.6)
    assert s["battery"] == 100


def test_slot_remembered_across_runs(tmp_path):
    t = tracker(tmp_path)
    t.feed(ADDR, V3_NAME)
    assert json.loads((tmp_path / "slots.json").read_text()) == {ADDR: 1}
    t2 = tracker(tmp_path)
    t2.feed(ADDR, V3_TEMPERATURE)
    assert t2.snapshot()[1]["food"] is not None


def test_stale_readings_blank(tmp_path):
    clk = ManualClock()
    t = tracker(tmp_path, clk)
    t.feed(ADDR, V3_NAME)
    t.feed(ADDR, V3_TEMPERATURE)
    clk.advance(31)
    assert t.snapshot()[1]["food"] is None


def test_tip_cap_blank(tmp_path):
    t = tracker(tmp_path)
    msg = bytearray(V3_TEMPERATURE)
    msg[6:8] = (1000).to_bytes(2, "little")          # t1 = 100.0 C (its cap)
    t.feed(ADDR, V3_NAME)
    t.feed(ADDR, bytes(msg))
    assert t.snapshot()[1]["t1"] is None
    assert t.snapshot()[1]["t2"] is not None


def test_fake_probes_follow_zones(tmp_path):
    t = tracker(tmp_path)
    fp = FakeProbes(t, 2, lambda: [225.0, 250.0])
    fp.step(1.0)
    s = t.snapshot()
    assert s[1]["ambient"] == pytest.approx(225.0, abs=0.2)
    assert s[2]["ambient"] == pytest.approx(250.0, abs=0.2)
    assert s[3]["food"] is None


def test_mac_logger_columns():
    cols = mac_columns(2)
    assert cols[:3] == ["timestamp", "probe1_food", "probe1_t1"]
    assert "probe2_battery" in cols
