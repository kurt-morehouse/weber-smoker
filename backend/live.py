"""Relays the Pi's live stream to browsers, and browsers' cook commands to the Pi.

On startup the backend connects to ws://<SMOKER_PI_HOST>:<SMOKER_PI_PORT>
and stays connected: it retries with backoff (1 s doubling to
SMOKER_RETRY_MAX_S) while the Pi is off, resumes by sequence number after a
dropout, records the stream to data/live_<date>_<time>.csv, and passes it to
browsers on /ws/live.

Browser protocol (/ws/live):
  server -> browser
    {"type": "state", "connected": bool, "pi": "ws://...", "last_row_age_s": s|null,
     "file": "live_....csv"|null, "error": "..."|null, "commands": bool}
        on connect, then every second
    {"type": "history", "rows": [...]}       the session so far, on connect
    {"type": "row", "row": {...}, "status": {...}}
    {"type": "command_result", "id": ..., "ok": bool, "message": "..."}
  browser -> server
    {"type": "command", "id": "<any>", "command": "start|pause|resume|set_setpoint|stop",
     "args": {...}}
        The backend adds the command token and forwards it to the Pi.

The Pi's side of the protocol is described in control/live_server.py.
"""
import asyncio
import csv
import itertools
import json
import time
from datetime import datetime
from pathlib import Path

from replay import parse_value

MAX_KEPT_ROWS = 50_000          # ~14 hours at one row a second


class Recorder:
    """Appends live rows to data/live_<date>_<time>.csv, one file per backend run."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = None
        self._fh = None
        self._w = None
        self._cols = None

    def write(self, row):
        cols = [c for c in row.keys() if not c.startswith("_")]
        if self._fh is None or cols != self._cols:
            self.close()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.path = self.data_dir / f"live_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
            self._fh = open(self.path, "w", newline="")
            self._w = csv.writer(self._fh)
            self._w.writerow(cols)
            self._cols = cols
        self._w.writerow(["" if row.get(c) is None else row.get(c) for c in cols])
        self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
        self._fh = self._w = None


class LiveRelay:
    def __init__(self, settings, record=True):
        self.s = settings
        self.connected = False
        self.error = None
        self.last_row_at = None
        self.last_seq = None
        self.boot = None
        self.rows = []
        self.browsers = set()
        self.recorder = Recorder(settings.data_dir) if record else None
        self._pi = None
        self._pending = {}             # pi command id -> (browser send, browser id)
        self._ids = itertools.count(1)
        self._task = None

    # ------------------------------------------------------------ state
    def state(self):
        return {
            "type": "state",
            "connected": self.connected,
            "pi": self.s.pi_url,
            "last_row_age_s": (None if self.last_row_at is None
                               else round(time.monotonic() - self.last_row_at, 1)),
            "file": self.recorder.path.name if self.recorder and self.recorder.path else None,
            "error": self.error,
            "commands": bool(self.s.command_token),
        }

    # -------------------------------------------------------- lifecycle
    def start(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.recorder:
            self.recorder.close()

    async def _run(self):
        from websockets.asyncio.client import connect
        backoff = 1.0
        while True:
            try:
                async with connect(self.s.pi_url, open_timeout=5, ping_interval=20) as ws:
                    self._pi = ws
                    self.connected, self.error = True, None
                    backoff = 1.0
                    await ws.send(json.dumps({"type": "hello", "resume_from": self.last_seq,
                                              "boot": self.boot}))
                    async for raw in ws:
                        await self.on_pi_message(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self._pi = None
                if self.connected:
                    self.connected = False
                    await self.broadcast(self.state())
                for send, bid in list(self._pending.values()):
                    await self._safe(send, {"type": "command_result", "id": bid, "ok": False,
                                            "message": "lost the connection to the Pi"})
                self._pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.s.retry_max_s)

    # ------------------------------------------------------- pi messages
    async def on_pi_message(self, msg):
        t = msg.get("type")
        if t == "row":
            if msg.get("boot") != self.boot:
                self.boot = msg.get("boot")       # main.py restarted: seq starts over
                self.last_seq = None
            seq = msg.get("seq")
            if self.last_seq is not None and seq is not None and seq <= self.last_seq:
                return                             # already have it
            self.last_seq = seq
            self.last_row_at = time.monotonic()
            row = {k: (parse_value(k, v) if isinstance(v, str) else v)
                   for k, v in (msg.get("row") or {}).items()}
            row["_seq"] = seq
            self.rows.append(row)
            if len(self.rows) > MAX_KEPT_ROWS:
                del self.rows[: len(self.rows) - MAX_KEPT_ROWS]
            if self.recorder:
                self.recorder.write(row)
            await self.broadcast({"type": "row", "row": row, "status": msg.get("status") or {}})
        elif t == "command_result":
            pending = self._pending.pop(msg.get("id"), None)
            if pending:
                send, bid = pending
                await self._safe(send, {**msg, "id": bid})

    # --------------------------------------------------------- browsers
    async def attach(self, send):
        self.browsers.add(send)
        await send(self.state())
        await send({"type": "history", "rows": self.rows})

    def detach(self, send):
        self.browsers.discard(send)

    async def command(self, send, msg):
        bid = msg.get("id")
        if not self.s.command_token:
            await send({"type": "command_result", "id": bid, "ok": False,
                        "message": "no command token on the Mac (control/command_token)"})
            return
        if self._pi is None:
            await send({"type": "command_result", "id": bid, "ok": False,
                        "message": "not connected to the Pi"})
            return
        pid = f"b{next(self._ids)}"
        self._pending[pid] = (send, bid)
        await self._pi.send(json.dumps({"type": "command", "id": pid,
                                        "token": self.s.command_token,
                                        "command": msg.get("command"),
                                        "args": msg.get("args") or {}}))

    async def broadcast(self, msg):
        for send in list(self.browsers):
            await self._safe(send, msg)

    async def _safe(self, send, msg):
        try:
            await send(msg)
        except Exception:
            self.browsers.discard(send)
