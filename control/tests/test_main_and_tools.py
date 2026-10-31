"""main.Controller, live_server, preflight and ssr_test."""
import asyncio
import io
import json
import sys

import pytest

import hardware
import main as main_mod
import preflight
import ssr_test
from cook import COOKING
from grill_probe import encode_max31855
from helpers import FakeGPIO, FakeSpiDev, ManualClock, config, write_config
import live_server
from live_server import LiveServer, load_connect, wait_listening


# ------------------------------------------------------------ Controller
def sim_controller(tmp_path, overrides=None, live=None):
    cfg = config(tmp_path, overrides)
    clk = ManualClock()
    ctl = main_mod.Controller(cfg, simulate=True, threaded=False, clock=clk, live=live)
    return ctl, clk


def run_steps(ctl, clk, n, dt=1.0):
    row = None
    for _ in range(n):
        clk.advance(dt)
        for z in ctl.zones:
            z.relay.update()
        row = ctl.step()
    return row


def test_idle_keeps_relays_off(tmp_path):
    ctl, clk = sim_controller(tmp_path)
    row = run_steps(ctl, clk, 5)
    assert row["zone1_relay_state"] == 0 and row["zone2_relay_state"] == 0
    assert row["cook_state"] == ""
    assert not list((tmp_path / "data").glob("cook_*.csv"))


def test_cook_heats_both_zones_toward_setpoint(tmp_path):
    ctl, clk = sim_controller(tmp_path, {"simulation": {"speedup": 5}})
    ctl.cook.apply("start", {"name": "t", "setpoints": {"zone1": 225, "zone2": 200}})
    rows = [run_steps(ctl, clk, 1) for _ in range(400)]
    assert rows[0]["zone1_relay_state"] > 0
    z1 = rows[-1]["zone1_temp_f"]
    z2 = rows[-1]["zone2_temp_f"]
    assert 200 < z1 < 250 and 180 < z2 < 225
    assert rows[-1]["probe1_food_f"] is not None
    csv_files = list((tmp_path / "data").glob("cook_*_t.csv"))
    assert len(csv_files) == 1


def test_fault_turns_that_zone_off(tmp_path):
    ctl, clk = sim_controller(tmp_path)
    ctl.cook.apply("start", {})
    run_steps(ctl, clk, 2)
    ctl.zones[1].probe.sim_fault = "open"
    row = run_steps(ctl, clk, 1)
    assert row["zone2_relay_state"] == 0 and row["zone2_fault"] == "open"
    assert row["zone2_temp_f"] is None
    assert row["zone1_relay_state"] > 0
    assert "zone2: thermocouple fault" in row["safety"]


def test_overtemp_turns_both_off_until_resumed(tmp_path):
    ctl, clk = sim_controller(tmp_path)
    ctl.cook.apply("start", {})
    ctl.sim.t["zone1"] = 310.0
    row = run_steps(ctl, clk, 1)
    assert row["zone1_relay_state"] == 0 and row["zone2_relay_state"] == 0
    ctl.sim.t["zone1"] = 150.0
    row = run_steps(ctl, clk, 1)
    assert row["zone2_relay_state"] == 0          # still latched
    ctl.cook.apply("pause", {})
    ctl.cook.apply("resume", {})
    row = run_steps(ctl, clk, 1)
    assert row["zone1_relay_state"] > 0


def test_power_cap(tmp_path):
    ctl, clk = sim_controller(tmp_path, {"power": {"max_combined_duty": 1.6}})
    ctl.cook.apply("start", {})
    row = run_steps(ctl, clk, 1)
    assert row["zone1_relay_state"] + row["zone2_relay_state"] <= 1.6 + 1e-9


def test_pause_logs_with_relays_off(tmp_path):
    ctl, clk = sim_controller(tmp_path)
    ctl.cook.apply("start", {})
    ctl.cook.apply("pause", {"reason": "spritz"})
    row = run_steps(ctl, clk, 1)
    assert row["cook_state"] == "paused" and row["zone1_relay_state"] == 0


def test_real_mode_uses_configured_buses_and_pins(tmp_path, monkeypatch):
    FakeSpiDev.opened.clear()
    FakeSpiDev.frames = {(0, 0): encode_max31855(100, 25), (1, 0): encode_max31855(110, 25)}
    monkeypatch.setattr(hardware, "load_spidev", lambda: type("M", (), {"SpiDev": FakeSpiDev}))
    gpio = FakeGPIO()
    cfg = config(tmp_path)
    ctl = main_mod.Controller(cfg, simulate=False, gpio=gpio, threaded=False, clock=ManualClock())
    assert FakeSpiDev.opened == [(0, 0), (1, 0)]       # zone1 SPI0, zone2 SPI1
    assert set(gpio.setup_pins) == {17, 27}            # zone1 GPIO17, zone2 GPIO27
    row = ctl.step()
    assert row["zone1_temp_f"] == pytest.approx(212) and row["zone2_temp_f"] == pytest.approx(230)
    ctl.close()
    assert gpio.levels == {17: 0, 27: 0}


def test_main_refuses_without_hardware(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "RPi", None)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", None)
    rc = main_mod.main(["--config", str(write_config(tmp_path))])
    assert rc == 3
    out = capsys.readouterr().out
    assert "REAL GPIO" in out and "RPi.GPIO won't load" in out


def test_main_bad_config(tmp_path, capsys):
    rc = main_mod.main(["--simulate", "--config", str(write_config(tmp_path, {"pid": {"kp": "x"}}))])
    assert rc == 2 and "problem" in capsys.readouterr().out


# ------------------------------------------------------------ live server
def ws_session(port, messages, expect):
    """Connect, send messages, collect `expect` replies."""
    connect = load_connect()

    async def go():
        got = []
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            for m in messages:
                await ws.send(json.dumps(m))
            while len(got) < expect:
                got.append(json.loads(await asyncio.wait_for(ws.recv(), 5)))
        return got
    return asyncio.run(go())


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_live_history_resume_and_commands(tmp_path):
    port = free_port()
    live = LiveServer("127.0.0.1", port, 3, 5, token="secret").start()
    assert wait_listening(live), live.error
    try:
        for i in range(5):
            live.publish({"i": i}, {"state": "idle"})
        import time
        time.sleep(0.2)
        got = ws_session(port, [{"type": "hello", "resume_from": None}], 3)
        assert [m["seq"] for m in got] == [3, 4, 5]               # history is 3 deep
        got = ws_session(port, [{"type": "hello", "resume_from": 4, "boot": live.boot}], 1)
        assert got[0]["seq"] == 5
        got = ws_session(port, [{"type": "hello", "resume_from": 5},
                                {"type": "command", "id": "a", "token": "nope",
                                 "command": "start"}], 1)
        assert got[0] == {"type": "command_result", "id": "a", "ok": False,
                          "message": "wrong command token"}
    finally:
        live.stop()


def test_live_commands_reach_the_controller(tmp_path):
    port = free_port()
    live = LiveServer("127.0.0.1", port, 50, 5, token="secret").start()
    assert wait_listening(live), live.error
    ctl, clk = sim_controller(tmp_path, live=live)
    try:
        connect = load_connect()

        async def go():
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"type": "hello", "resume_from": None}))
                await ws.send(json.dumps({"type": "command", "id": "c1", "token": "secret",
                                          "command": "start",
                                          "args": {"name": "ws", "setpoints": {"zone1": 230}}}))
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    run_steps(ctl, clk, 1)
                    try:
                        while True:
                            m = json.loads(await asyncio.wait_for(ws.recv(), 0.05))
                            if m["type"] == "command_result":
                                return m
                    except asyncio.TimeoutError:
                        pass
        result = asyncio.run(go())
        assert result["ok"] and result["id"] == "c1"
        assert ctl.cook.state == COOKING and ctl.cook.setpoints["zone1"] == 230
    finally:
        live.stop()
        ctl.close()


def test_live_without_token_refuses_everything():
    port = free_port()
    live = LiveServer("127.0.0.1", port, 5, 5, token=None).start()
    assert wait_listening(live), live.error
    try:
        got = ws_session(port, [{"type": "hello"}, {"type": "command", "id": 1,
                                                    "token": "", "command": "stop"}], 1)
        assert not got[0]["ok"] and "refuses every command" in got[0]["message"]
    finally:
        live.stop()


# --------------------------------------------------------------- preflight
def test_preflight_simulated_passes(tmp_path):
    checks = preflight.run(config(tmp_path), simulate=True)
    assert all(c.ok for c in checks)


def test_check_spi_finds_both_buses(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "spidev0.0").touch()
    (dev / "spidev1.0").touch()
    c = preflight.check_spi(config(tmp_path), str(dev))
    assert c.ok and "spidev0.0" in c.detail and "spidev1.0" in c.detail


def test_check_spi_hints_at_the_spi1_overlay(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "spidev0.0").touch()
    (dev / "spidev0.1").touch()                  # the old shared-bus layout
    c = preflight.check_spi(config(tmp_path), str(dev))
    assert not c.ok and "spidev1.0" in c.detail and "spi1-1cs" in c.detail


@pytest.mark.parametrize("line,ok", [("dtoverlay=spi1-1cs", True), ("dtoverlay=spi1-2cs", False),
                                     ("dtoverlay=spi1-3cs", False), ("# dtoverlay=spi1-1cs", False)])
def test_overlay_check(tmp_path, line, ok):
    boot = tmp_path / "config.txt"
    boot.write_text(f"dtparam=spi=on\n{line}\n")
    c = preflight.check_spi1_overlay(config(tmp_path), [str(boot)])
    assert c.ok == ok
    if line.endswith(("2cs", "3cs")):
        assert "GPIO17" in c.detail


def test_preflight_gpio_checks_only_relay_pins(tmp_path):
    g = FakeGPIO()
    checks = preflight.check_gpio(config(tmp_path), simulate=False, gpio=g)
    assert [c.ok for c in checks] == [True, True, True]
    assert g.touched() == {17, 27} and g.mode == "BCM"


def test_preflight_reports_dead_zone2(tmp_path, monkeypatch):
    FakeSpiDev.frames = {(0, 0): encode_max31855(21, 20), (1, 0): 0}   # zone2 silent
    monkeypatch.setattr(hardware, "load_spidev", lambda: type("M", (), {"SpiDev": FakeSpiDev}))
    cfg = config(tmp_path)
    assert preflight.check_thermocouple(cfg, "zone1", False).ok
    c = preflight.check_thermocouple(cfg, "zone2", False)
    assert not c.ok and "no response" in c.detail


# ---------------------------------------------------------------- ssr_test
class Log:
    def __init__(self):
        self.lines = []

    def write(self, t):
        self.lines.append(t)


@pytest.mark.parametrize("zone,pin,physical", [("zone1", 17, 11), ("zone2", 27, 13)])
def test_pulse_drives_only_that_zones_pin(tmp_path, zone, pin, physical):
    g, clk, log = FakeGPIO(), ManualClock(), Log()
    held = ssr_test.pulse(config(tmp_path), zone, 2.0, False, log, gpio=g, clock=clk,
                          sleep=clk.advance)
    assert held == pytest.approx(2.0)
    assert g.touched() == {pin}
    assert (pin, 1) in g.writes and g.levels[pin] == 0
    assert f"GPIO{pin} (pin {physical})" in log.lines[0] and log.lines[-1].startswith("OFF")


def test_pulse_is_capped(tmp_path):
    g, clk, log = FakeGPIO(), ManualClock(), Log()
    held = ssr_test.pulse(config(tmp_path), "zone1", 999, False, log, gpio=g, clock=clk,
                          sleep=clk.advance)
    assert held == pytest.approx(5.0)                 # ssr_test.max_pulse_seconds


def test_pulse_turns_off_on_error(tmp_path):
    g, clk, log = FakeGPIO(), ManualClock(), Log()

    def boom(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ssr_test.pulse(config(tmp_path), "zone2", 3, False, log, gpio=g, clock=clk, sleep=boom)
    assert g.levels[27] == 0 and log.lines[-1].startswith("OFF")


def test_ssr_test_refuses_without_a_terminal(tmp_path, capsys):
    rc = ssr_test.main(["--simulate", "--config", str(write_config(tmp_path))],
                       stdin=io.StringIO(""))
    assert rc == 2 and "interactive terminal" in capsys.readouterr().out


class TTY(io.StringIO):
    def isatty(self):
        return True


def test_ssr_test_needs_confirmation(tmp_path, capsys):
    answers = iter(["1", "1", "2", "q"])             # zone 1, 1 s, wrong confirm, quit
    rc = ssr_test.main(["--simulate", "--config", str(write_config(tmp_path))],
                       stdin=TTY(), input_fn=lambda _: next(answers))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Not confirmed" in out
    log = (tmp_path / "data" / "ssr_test.log").read_text()
    assert "session start (simulated GPIO)" in log and "ON " not in log


def test_ssr_test_refuses_while_main_holds_the_lock(tmp_path, monkeypatch, capsys):
    from gpio_lock import GpioLock
    monkeypatch.setattr(hardware, "load_gpio", lambda: FakeGPIO())
    cfgp = write_config(tmp_path)
    with GpioLock(str(tmp_path / "gpio.lock"), "main.py"):
        rc = ssr_test.main(["--config", str(cfgp)], stdin=TTY(), input_fn=lambda _: "q")
    assert rc == 4 and "main.py" in capsys.readouterr().out


# --------------------------------------------------------------- relays_off
def test_relays_off_drives_both_relay_pins_low(tmp_path):
    sys.path.insert(0, str(main_mod.__file__).rsplit("/", 1)[0] + "/tools")
    import relays_off
    g = FakeGPIO()
    g.levels = {17: 1, 27: 1}
    assert relays_off.main([str(write_config(tmp_path))], gpio=g) == 0
    assert g.levels == {17: 0, 27: 0} and g.mode == "BCM" and g.touched() == {17, 27}


def test_relays_off_without_config_uses_wired_pins(tmp_path):
    import relays_off
    assert relays_off.relay_pins(str(tmp_path / "missing.yaml")) == [17, 27]


def test_live_server_says_why_when_websockets_is_missing(monkeypatch):
    def broken():
        raise ImportError("the websockets package isn't installed in this Python")
    monkeypatch.setattr(live_server, "load_serve", broken)
    live = LiveServer("127.0.0.1", free_port(), 5, 5).start()
    assert not wait_listening(live, timeout=3)
    assert "isn't installed" in live.error
    live.stop()


def test_live_server_on_the_legacy_api(monkeypatch):
    legacy = pytest.importorskip("websockets.legacy.server")
    monkeypatch.setattr(live_server, "load_serve", lambda: (legacy.serve, True))
    port = free_port()
    live = LiveServer("127.0.0.1", port, 5, 5, token="t").start()
    try:
        assert wait_listening(live), live.error
        live.publish({"i": 1}, {})
        import time
        time.sleep(0.2)
        got = ws_session(port, [{"type": "hello"}], 1)
        assert got[0]["seq"] == 1
    finally:
        live.stop()


def test_preflight_live_stream_check(tmp_path, monkeypatch):
    c = preflight.check_websockets(config(tmp_path))
    assert c.ok and "websockets" in c.detail
    monkeypatch.setattr(live_server, "load_serve",
                        lambda: (_ for _ in ()).throw(ImportError("too old")))
    c = preflight.check_websockets(config(tmp_path))
    assert not c.ok and c.warn_only and "too old" in c.detail
