import asyncio
import io
import json
import socket
import threading
import time

import pytest

import settings as settings_mod
from generate_sample_cook import generate
from live import LiveRelay
from replay import list_cooks, normalize_column, read_rows, safe_path
from replay_player import Player


# ------------------------------------------------------------- settings
def test_settings_defaults(tmp_path):
    s = settings_mod.load({"SMOKER_DATA_DIR": str(tmp_path)})
    assert s.pi_url == "ws://192.168.1.229:8765"
    assert s.data_dir == tmp_path.resolve() and s.live


def test_settings_overrides(tmp_path):
    s = settings_mod.load({"SMOKER_DATA_DIR": str(tmp_path), "SMOKER_PI_HOST": "pi.local",
                           "SMOKER_PI_PORT": "9000", "SMOKER_LIVE": "0",
                           "SMOKER_COMMAND_TOKEN": "abc"})
    assert s.pi_url == "ws://pi.local:9000" and not s.live and s.command_token == "abc"


def test_settings_bad_number(tmp_path):
    with pytest.raises(ValueError, match="SMOKER_PI_PORT"):
        settings_mod.load({"SMOKER_PI_PORT": "eighty"})


# --------------------------------------------------------------- replay
def write_csv(path, header, rows):
    path.write_text("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n")
    return path


def test_normalize_mac_logger_columns():
    assert normalize_column("probe2_food") == "probe2_food_f"
    assert normalize_column("probe1_t3") == "probe1_t3_f"
    assert normalize_column("probe1_battery") == "probe1_battery_pct"
    assert normalize_column("probe1_food_f") == "probe1_food_f"


def test_read_rows(tmp_path):
    p = write_csv(tmp_path / "cook_a.csv", ["timestamp", "zone1_temp_f", "cook_state"],
                  [["2026-10-01T10:00:00", "225.5", "cooking"], ["2026-10-01T10:00:01", "", "paused"]])
    cols, rows = read_rows(p)
    assert cols == ["timestamp", "zone1_temp_f", "cook_state"]
    assert rows[0]["zone1_temp_f"] == 225.5 and rows[1]["zone1_temp_f"] is None
    assert rows[1]["cook_state"] == "paused"


def test_list_cooks_with_meta(tmp_path):
    write_csv(tmp_path / "cook_2026-10-01_100000_ribs.csv", ["timestamp"], [["t"], ["t"]])
    (tmp_path / "cook_2026-10-01_100000_ribs_meta.json").write_text(
        json.dumps({"name": "Ribs", "hardware": "real", "started": "x"}))
    write_csv(tmp_path / "chefiq_mac_x.csv", ["timestamp"], [["t"]])
    cooks = {c["file"]: c for c in list_cooks(tmp_path)}
    ribs = cooks["cook_2026-10-01_100000_ribs.csv"]
    assert ribs["name"] == "Ribs" and ribs["rows"] == 2 and ribs["kind"] == "cook"
    assert cooks["chefiq_mac_x.csv"]["kind"] == "chefiq"


@pytest.mark.parametrize("name", ["../etc/passwd", ".hidden.csv", "a/b.csv", "missing.csv", ""])
def test_safe_path_rejects(tmp_path, name):
    with pytest.raises(ValueError):
        safe_path(tmp_path, name)


# --------------------------------------------------------------- player
def player_with(tmp_path, n=100):
    rows = [[f"t{i}", str(200 + i)] for i in range(n)]
    write_csv(tmp_path / "cook_x.csv", ["timestamp", "zone1_temp_f"], rows)
    sent = []

    async def send(m):
        sent.append(m)
    return Player(tmp_path, send), sent


def test_player_load_play_speed_seek(tmp_path):
    p, sent = player_with(tmp_path)

    async def go():
        await p.handle({"type": "load", "file": "cook_x.csv"})
        assert sent[0]["type"] == "loaded" and sent[0]["count"] == 100
        assert sent[1] == {"type": "history", "rows": [{"timestamp": "t0", "zone1_temp_f": 200.0, "_i": 0}]}
        await p.handle({"type": "speed", "value": 10})
        await p.handle({"type": "play"})
        sent.clear()
        await p.tick(1.0)                       # 10 rows per second
        rows = [m for m in sent if m["type"] == "rows"][0]["rows"]
        assert [r["_i"] for r in rows] == list(range(1, 11))
        await p.handle({"type": "seek", "index": 50})
        hist = [m for m in sent if m["type"] == "history"][-1]["rows"]
        assert len(hist) == 51
        sent.clear()
        await p.tick(100.0)
        assert any(m["type"] == "end" for m in sent) and not p.playing
        assert p.index == 99
    asyncio.run(go())


def test_player_errors(tmp_path):
    p, sent = player_with(tmp_path)

    async def go():
        await p.handle({"type": "load", "file": "../x.csv"})
        await p.handle({"type": "speed", "value": "fast"})
        await p.handle({"type": "warp"})
    asyncio.run(go())
    assert [m["type"] for m in sent] == ["error", "error", "error"]


# --------------------------------------------------------------- server
@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    import server
    cfg = settings_mod.load({"SMOKER_DATA_DIR": str(tmp_path), "SMOKER_LIVE": "0",
                             "SMOKER_COMMAND_TOKEN": "tok"})
    generate(tmp_path / "cook_2026-10-03_063000_sample.csv", hours=0.05)
    with TestClient(server.create_app(cfg)) as c:
        yield c


def test_api_cooks_and_labels(client):
    cooks = client.get("/api/cooks").json()
    assert cooks[0]["file"] == "cook_2026-10-03_063000_sample.csv" and cooks[0]["rows"] == 180
    labels = client.get("/api/labels").json()
    assert labels["columns"]["zone1_temp_f"]["unit"] == "°F"


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text


def test_ws_replay(client):
    with client.websocket_connect("/ws/replay") as ws:
        ws.send_text(json.dumps({"type": "load", "file": "cook_2026-10-03_063000_sample.csv"}))
        loaded = json.loads(ws.receive_text())
        assert loaded["type"] == "loaded" and loaded["count"] == 180
        assert "probe1_food_f" in loaded["columns"]
        assert json.loads(ws.receive_text())["type"] == "history"


def test_journal_notes(client):
    stem = "cook_2026-10-03_063000_sample"
    assert client.get(f"/api/journal/{stem}").json()["notes"] == ""
    r = client.put(f"/api/journal/{stem}", json={"notes": "bark was great", "rating": 5,
                                                   "proteins": ["brisket"]})
    assert r.status_code == 200
    assert client.get(f"/api/journal/{stem}").json()["proteins"] == ["brisket"]
    assert client.put(f"/api/journal/{stem}", json={"rating": 9}).status_code == 400
    assert client.get("/api/journal/..%2Fescape").status_code in (400, 404)


def test_journal_photo_strips_metadata(client):
    from PIL import Image
    img = Image.new("RGB", (40, 30), "red")
    exif = Image.Exif()
    exif[306] = "2026:10:03 12:00:00"
    exif[0x8825] = {1: "N", 2: (40.0, 0.0, 0.0)}           # GPS info
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    stem = "cook_2026-10-03_063000_sample"
    r = client.post(f"/api/journal/{stem}/photos", files={"photo": ("p.jpg", buf.getvalue(), "image/jpeg")},
                    data={"caption": "the bark"})
    assert r.status_code == 200, r.text
    ph = r.json()["photos"][0]
    assert ph["caption"] == "the bark" and ph["taken_at"].startswith("2026-10-03T12:00:00")
    got = client.get(f"/api/journal/{stem}/photos/{ph['file']}")
    assert got.status_code == 200
    saved = Image.open(io.BytesIO(got.content))
    assert not saved.getexif()                               # no GPS, no metadata
    assert client.post(f"/api/journal/{stem}/photos",
                       files={"photo": ("x.jpg", b"not an image", "image/jpeg")}).status_code == 400


# ------------------------------------------------------------ live relay
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakePi:
    """Speaks control/live_server.py's protocol."""

    def __init__(self, port):
        self.port = port
        self.hellos = []
        self.commands = []
        self.seq = 0
        self.history = []
        self.clients = set()

    async def handler(self, ws):
        hello = json.loads(await ws.recv())
        self.hellos.append(hello)
        for seq, text in self.history:
            if hello.get("resume_from") is None or seq > hello["resume_from"]:
                await ws.send(text)
        self.clients.add(ws)
        try:
            async for raw in ws:
                m = json.loads(raw)
                self.commands.append(m)
                await ws.send(json.dumps({"type": "command_result", "id": m["id"],
                                          "ok": m["token"] == "tok", "message": "done"}))
        finally:
            self.clients.discard(ws)

    async def publish(self, row):
        self.seq += 1
        text = json.dumps({"type": "row", "seq": self.seq, "boot": "b1", "row": row,
                           "status": {"state": "idle"}})
        self.history.append((self.seq, text))
        for c in list(self.clients):
            await c.send(text)


def test_live_relay_end_to_end(tmp_path):
    from websockets.asyncio.server import serve
    port = free_port()
    cfg = settings_mod.load({"SMOKER_DATA_DIR": str(tmp_path), "SMOKER_PI_HOST": "127.0.0.1",
                             "SMOKER_PI_PORT": str(port), "SMOKER_COMMAND_TOKEN": "tok",
                             "SMOKER_RETRY_MAX_S": "1"})

    async def go():
        pi = FakePi(port)
        relay = LiveRelay(cfg)
        got = []

        async def browser(m):
            got.append(m)

        async with serve(pi.handler, "127.0.0.1", port) as srv:
            relay.start()
            await relay.attach(browser)
            for _ in range(100):
                if relay.connected and pi.clients:
                    break
                await asyncio.sleep(0.02)
            assert relay.connected
            await pi.publish({"timestamp": "t1", "zone1_temp_f": "225.5"})
            await pi.publish({"timestamp": "t2", "zone1_temp_f": 226.0})
            await asyncio.sleep(0.2)
            rows = [m["row"] for m in got if m["type"] == "row"]
            assert [r["_seq"] for r in rows] == [1, 2] and rows[0]["zone1_temp_f"] == 225.5
            await relay.command(browser, {"type": "command", "id": "x", "command": "pause",
                                          "args": {"reason": "r"}})
            await asyncio.sleep(0.2)
            assert pi.commands[0]["token"] == "tok" and pi.commands[0]["command"] == "pause"
            res = [m for m in got if m["type"] == "command_result"][0]
            assert res["id"] == "x" and res["ok"]
            # drop the connection: the relay reconnects and resumes after seq 2
            for c in list(pi.clients):
                await c.close()
            await pi.publish({"timestamp": "t3", "zone1_temp_f": 227.0})
            for _ in range(200):
                if len(pi.hellos) >= 2 and relay.last_seq == 3:
                    break
                await asyncio.sleep(0.02)
            assert pi.hellos[1]["resume_from"] == 2
            rows = [m["row"] for m in got if m["type"] == "row"]
            assert [r["_seq"] for r in rows] == [1, 2, 3]
        await relay.stop()
        recorded = list(tmp_path.glob("live_*.csv"))
        assert len(recorded) == 1
        lines = recorded[0].read_text().splitlines()
        assert lines[0] == "timestamp,zone1_temp_f" and len(lines) == 4

    asyncio.run(go())


def test_live_command_without_pi(tmp_path):
    cfg = settings_mod.load({"SMOKER_DATA_DIR": str(tmp_path), "SMOKER_COMMAND_TOKEN": "tok"})
    relay = LiveRelay(cfg, record=False)
    got = []

    async def browser(m):
        got.append(m)
    asyncio.run(relay.command(browser, {"id": 1, "command": "stop"}))
    assert got[0]["ok"] is False and "not connected" in got[0]["message"]


def test_sample_generator(tmp_path):
    p = generate(tmp_path / "s.csv", hours=0.1)
    cols, rows = read_rows(p)
    assert len(rows) == 360 and rows[0]["probe1_food_f"] is not None
