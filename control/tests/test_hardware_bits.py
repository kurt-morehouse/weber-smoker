"""grill_probe, relay_driver, pid, safety, gpio_lock."""
import sys

import pytest

import hardware
from gpio_lock import GpioLock, LockHeld
from grill_probe import (ThermocoupleFault, ThermocoupleReader, decode_max31855,
                         encode_max31855)
from helpers import FakeGPIO, FakeSpiDev, ManualClock
from pid_controller import PID
from relay_driver import SSRDriver
from safety import ABSOLUTE_MAX_TEMP_F, SafetyMonitor, cap_combined_duty


# ------------------------------------------------------------ MAX31855
def test_decode_positive():
    probe_f, board_f = decode_max31855(encode_max31855(100.0, 25.0))
    assert probe_f == pytest.approx(212.0)
    assert board_f == pytest.approx(77.0)


def test_decode_datasheet_frame():
    # Datasheet: +100.00 C = 0b00_0110_0100_00 (0x0190) in the 14-bit field,
    # +25.0000 C = 0x190 in the 12-bit cold-junction field.
    probe_f, _ = decode_max31855(0x0190 << 18 | (0x190 << 4))
    assert probe_f == pytest.approx(212.0)


def test_decode_negative():
    probe_f, board_f = decode_max31855(encode_max31855(-10.0, -1.0))
    assert probe_f == pytest.approx(14.0)
    assert board_f == pytest.approx(30.2)


@pytest.mark.parametrize("bits,kind", [(1, "open"), (2, "short_gnd"), (4, "short_vcc")])
def test_faults(bits, kind):
    with pytest.raises(ThermocoupleFault) as e:
        decode_max31855(encode_max31855(20.0, 20.0, bits))
    assert e.value.kind == kind


@pytest.mark.parametrize("raw", [0x00000000, 0xFFFFFFFF])
def test_no_response(raw):
    with pytest.raises(ThermocoupleFault, match="no response"):
        decode_max31855(raw)


def test_reader_opens_its_own_bus(monkeypatch):
    FakeSpiDev.opened.clear()
    FakeSpiDev.frames = {(1, 0): encode_max31855(50.0, 22.0)}
    monkeypatch.setattr(hardware, "load_spidev",
                        lambda: type("M", (), {"SpiDev": FakeSpiDev}))
    r = ThermocoupleReader(bus=1, device=0, max_speed_hz=1_000_000)
    assert FakeSpiDev.opened == [(1, 0)]
    assert r.read()[0] == pytest.approx(122.0)
    assert r.device_path == "/dev/spidev1.0"


def test_reader_without_spidev_is_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "spidev", None)
    with pytest.raises(hardware.HardwareUnavailable, match="spidev won.t load"):
        ThermocoupleReader(bus=0, device=0, max_speed_hz=1)


# -------------------------------------------------------------- relays
def make_driver(gpio=None, clock=None, **kw):
    args = dict(window_s=10, min_on_s=0.5, failsafe_timeout_s=5)
    args.update(kw)
    return SSRDriver(17, gpio=gpio or FakeGPIO(), clock=clock or ManualClock(),
                     threaded=False, **args)


def test_starts_low_in_bcm_mode():
    g = FakeGPIO()
    make_driver(g)
    assert g.mode == "BCM"
    assert g.levels[17] == 0


def test_time_proportioning():
    clk, g = ManualClock(), FakeGPIO()
    d = make_driver(g, clk)
    d.set_duty(0.3)
    states = []
    for _ in range(100):           # 10 s in 0.1 s steps
        d.update()
        states.append(d.is_on)
        clk.advance(0.1)
        if int(clk.t * 10) % 10 == 0:
            d.set_duty(0.3)         # keep the failsafe armed
    assert sum(states) == pytest.approx(30, abs=1)


def test_min_on_skips_short_pulses_and_fills_short_gaps():
    clk = ManualClock()
    d = make_driver(clock=clk)
    d.set_duty(0.02)                # 0.2 s < min_on 0.5 s
    assert not d.is_on
    d.set_duty(0.98)                # 0.2 s off < 0.5 s: fill it
    clk.advance(4.9)
    d.set_duty(0.98)
    clk.advance(4.9)
    d.set_duty(0.98)                # 9.8 s into the window: would be off unfilled
    assert d.is_on


def test_failsafe_turns_off_when_not_refreshed():
    clk = ManualClock()
    d = make_driver(clock=clk)
    d.set_duty(1.0)
    assert d.is_on
    clk.advance(5.1)
    d.update()
    assert not d.is_on and d.failsafe_tripped


def test_close_drives_low_and_only_its_pin():
    g = FakeGPIO()
    d = make_driver(g)
    d.set_duty(1.0)
    d.close()
    assert g.levels[17] == 0
    assert g.touched() == {17}
    d.set_duty(1.0)                 # ignored after close
    assert g.levels[17] == 0


def test_simulated_driver_never_loads_gpio(monkeypatch):
    monkeypatch.setattr(hardware, "load_gpio", lambda: (_ for _ in ()).throw(AssertionError))
    d = SSRDriver(17, 10, 0.5, 5, simulate=True, threaded=False)
    d.set_duty(1.0)
    assert d.is_on


# ----------------------------------------------------------------- PID
def test_pid_output_clamped():
    pid = PID(0.05, 0.001, 0.5)
    assert pid.update(225, 70, 1.0) == 1.0
    assert pid.update(225, 400, 1.0) == 0.0


def test_pid_integral_limited():
    pid = PID(0.0, 0.1, 0.0, integral_limit=0.3)
    for _ in range(100):
        out = pid.update(225, 220, 1.0)
    assert out == pytest.approx(0.3)


def test_pid_no_derivative_kick_on_setpoint_change():
    pid = PID(0.0, 0.0, 1.0)
    pid.update(200, 200, 1.0)
    assert pid.update(250, 200, 1.0) == 0.0


# -------------------------------------------------------------- safety
def mon():
    return SafetyMonitor(max_temp_f=300, stale_seconds=5, max_board_temp_f=150)


def test_safety_good_reading():
    assert mon().evaluate_zone(225, None, 1.0, 80).allowed


def test_safety_fault_and_stale():
    assert not mon().evaluate_zone(225, "open", 1.0).allowed
    assert not mon().evaluate_zone(225, None, 6.0).allowed
    assert not mon().evaluate_zone(None, None, None).allowed


def test_overtemp_latches_both_zones():
    m = mon()
    assert not m.evaluate_zone(301, None, 1.0).allowed
    assert not m.evaluate_zone(200, None, 1.0).allowed       # the other zone too
    m.clear_latch()
    assert m.evaluate_zone(200, None, 1.0).allowed


def test_board_overtemp_latches():
    m = mon()
    assert not m.evaluate_zone(200, None, 1.0, board_f=160).allowed
    assert m.latched


def test_hard_temp_limit():
    assert SafetyMonitor(9999, 5, 150).max_temp_f == ABSOLUTE_MAX_TEMP_F


def test_cap_combined_duty():
    assert cap_combined_duty([1.0, 1.0], 1.6) == pytest.approx([0.8, 0.8])
    assert cap_combined_duty([0.5, 0.5], 1.6) == [0.5, 0.5]


# ---------------------------------------------------------------- lock
def test_gpio_lock_is_exclusive(tmp_path):
    p = str(tmp_path / "lock")
    with GpioLock(p, "main.py"):
        with pytest.raises(LockHeld, match="main.py"):
            GpioLock(p, "ssr_test.py").acquire()
    with GpioLock(p, "ssr_test.py"):
        pass
