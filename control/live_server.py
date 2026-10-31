"""Live stream: every row out, cook commands in, on ws://<pi>:8765.

Runs in its own thread with its own asyncio loop, so the control loop never
waits on the network. Keeps the last websocket.history_rows rows so a client
that drops off can resume where it left off.

Protocol (JSON text messages). backend/live.py is the other side.

  client -> server
    {"type": "hello", "resume_from": <seq or null>}
        Sent first. The server replies with every kept row after resume_from
        (all kept rows if null), then streams new ones.
    {"type": "command", "id": "<any>", "token": "<command token>",
     "command": "start|pause|resume|set_setpoint|stop", "args": {...}}
        See cook.py for the commands. Refused without the right token, and
        refused for everyone if the Pi has no token file.

  server -> client
    {"type": "row", "seq": <int>, "row": {<CSV columns>}, "status": {...}}
    {"type": "command_result", "id": "<same>", "ok": true|false, "message": "..."}
    {"type": "error", "message": "..."}

seq increases by one per row and restarts from 1 when main.py restarts; the
"boot" field in each row message changes then too, so a client can tell.
"""
import asyncio
import collections
import json
import queue
import threading
import time
import uuid

MIN_WEBSOCKETS = (10, 1)       # 13+ preferred; 10.1-12.x run on the legacy API


def websockets_version():
    """websockets' version as a tuple, or None if it isn't installed."""
    try:
        import websockets
    except ImportError:
        return None
    parts = []
    for p in str(getattr(websockets, "__version__", "0")).split(".")[:2]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def load_serve():
    """websockets' serve(), new API if it has one, else the legacy one.

    Raises ImportError with a clear message if websockets is missing or too old.
    Debian Bookworm's apt python3-websockets is 10.4 (legacy API only), and a
    venv made with --system-site-packages can pick that up.
    """
    try:
        from websockets.asyncio.server import serve       # websockets >= 13
        return serve, False
    except ImportError:
        pass
    ver = websockets_version()
    if ver is None:
        raise ImportError("the websockets package isn't installed in this Python "
                          "(run: cd ~/smoker/control && ~/.local/bin/uv sync)")
    if ver < MIN_WEBSOCKETS:
        raise ImportError(f"websockets {ver[0]}.{ver[1]} is too old (need 13+, or at least 10.1); "
                          f"run: cd ~/smoker/control && ~/.local/bin/uv sync")
    from websockets.legacy.server import serve                # 10.1 - 12.x
    return serve, True


def load_connect():
    """websockets' client connect(), new API if available (used by tests)."""
    try:
        from websockets.asyncio.client import connect
        return connect
    except ImportError:
        from websockets.legacy.client import connect
        return connect


class LiveServer:
    def __init__(self, host, port, history_rows, max_pending, token=None, retry_seconds=5.0):
        self.host, self.port = host, port
        self.token = token
        self.retry_seconds = retry_seconds
        self.boot = uuid.uuid4().hex[:8]
        self.history = collections.deque(maxlen=history_rows)
        self.commands = queue.Queue(maxsize=max_pending)
        self.seq = 0
        self.clients = set()
        self.error = None
        self.listening = threading.Event()
        self.failed = threading.Event()        # set if the server thread gave up
        self._loop = None
        self._stop = None
        self._thread = None

    # ------------------------------------------------------ control thread
    def start(self):
        self._thread = threading.Thread(target=self._run, name="live-server", daemon=True)
        self._thread.start()
        return self

    def publish(self, row, status):
        """Queue one row for every client. Never blocks."""
        self.seq += 1
        msg = {"type": "row", "seq": self.seq, "boot": self.boot, "row": row, "status": status}
        text = json.dumps(msg, default=str)
        if self._loop is None:
            self.history.append((self.seq, text))
            return
        try:
            self._loop.call_soon_threadsafe(self._broadcast, self.seq, text)
        except RuntimeError:       # loop closed during shutdown
            pass

    def pending_commands(self):
        """Drain queued commands: list of (client, id, command, args)."""
        out = []
        while True:
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                return out

    def reply(self, client, cmd_id, ok, message):
        text = json.dumps({"type": "command_result", "id": cmd_id, "ok": ok, "message": message})
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._send_one, client, text)
            except RuntimeError:
                pass

    def stop(self):
        if self._loop and self._stop:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    # ---------------------------------------------------------- loop side
    def _broadcast(self, seq, text):
        self.history.append((seq, text))
        for ws in list(self.clients):
            self._send_one(ws, text)

    def _send_one(self, ws, text):
        async def send():
            try:
                await ws.send(text)
            except Exception:
                self.clients.discard(ws)
        asyncio.ensure_future(send())

    async def _handler(self, ws):
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except Exception:
            first = {}
        resume = first.get("resume_from") if first.get("type") == "hello" else None
        if first.get("boot") not in (None, self.boot):
            resume = None           # client last saw an earlier run of main.py
        for seq, text in list(self.history):
            if resume is None or seq > resume:
                await ws.send(text)
        self.clients.add(ws)
        try:
            async for raw in ws:
                await self._on_message(ws, raw)
        except Exception:
            pass
        finally:
            self.clients.discard(ws)

    async def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            await ws.send(json.dumps({"type": "error", "message": "not JSON"}))
            return
        if msg.get("type") != "command":
            return
        cmd_id = msg.get("id")
        if not self.token:
            ok, why = False, "the Pi has no command token file, so it refuses every command"
        elif msg.get("token") != self.token:
            ok, why = False, "wrong command token"
        else:
            try:
                self.commands.put_nowait((ws, cmd_id, msg.get("command"), msg.get("args") or {}))
                return
            except queue.Full:
                ok, why = False, "busy: too many commands queued"
        await ws.send(json.dumps({"type": "command_result", "id": cmd_id, "ok": ok, "message": why}))

    def _run(self):
        try:
            serve, legacy = load_serve()
        except ImportError as e:
            self.error = f"live stream off: {e}"
            self.failed.set()
            return
        handler = (lambda ws, *path: self._handler(ws)) if legacy else self._handler
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()

        async def main():
            while not self._stop.is_set():
                try:
                    async with serve(handler, self.host, self.port, ping_interval=20):
                        self.error = None
                        self.listening.set()
                        await self._stop.wait()
                except OSError as e:
                    self.listening.clear()
                    self.error = f"can't listen on {self.host}:{self.port}: {e}"
                    try:
                        await asyncio.wait_for(self._stop.wait(), self.retry_seconds)
                    except asyncio.TimeoutError:
                        pass

        try:
            self._loop.run_until_complete(main())
        except Exception as e:                  # never die without saying why
            self.error = f"live stream stopped: {type(e).__name__}: {e}"
            self.listening.clear()
            self.failed.set()
        finally:
            self._loop.close()


def read_token(path):
    try:
        t = open(path).read().strip()
    except OSError:
        return None
    return t or None


def wait_listening(server, timeout=10.0):
    """True once the server listens; False if it failed or timed out
    (server.error then says why, if it knows)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if server.listening.is_set():
            return True
        if server.failed.is_set():
            return False
        time.sleep(0.02)
    if server.error is None:
        server.error = f"not listening on {server.host}:{server.port} after {timeout:g} s"
    return False
