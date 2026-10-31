"""Replays a saved cook over a WebSocket: play, pause, speed and scrub.

Row message format (shared with the live view, /ws/live). The backend and
control/ share no code; this and the CSV columns are their contract.

  browser -> server
    {"type": "load", "file": "cook_2026-10-12_101500_brisket.csv"}
    {"type": "play"}   {"type": "pause"}
    {"type": "speed", "value": 60}        rows per second of real time
    {"type": "seek", "index": 1234}       jump; playback continues from there

  server -> browser
    {"type": "loaded", "file": ..., "columns": [...], "count": N}
    {"type": "history", "rows": [...]}    every row up to the current one:
                                          sent after load and after seek, so
                                          the charts redraw from scratch
    {"type": "rows", "rows": [...]}       the next rows during playback
    {"type": "position", "index": i, "count": N, "playing": bool, "speed": s}
    {"type": "end"}                        reached the last row
    {"type": "error", "message": "..."}

Rows are the CSV's rows as JSON (numbers, null for blanks, text), plus
"_i", their index in the file.
"""
import asyncio
import time

from replay import read_rows, safe_path

TICK_S = 0.05
MAX_SPEED = 3600.0


class Player:
    def __init__(self, data_dir, send):
        self.data_dir = data_dir
        self.send = send                 # async callable(dict)
        self.file = None
        self.columns = []
        self.rows = []
        self.index = -1                  # last row sent
        self.playing = False
        self.speed = 1.0
        self._carry = 0.0

    async def handle(self, msg):
        t = msg.get("type")
        if t == "load":
            await self.load(msg.get("file"))
        elif t == "play":
            if self.rows:
                if self.index >= len(self.rows) - 1:
                    await self.seek(0)
                self.playing = True
                await self.position()
        elif t == "pause":
            self.playing = False
            await self.position()
        elif t == "speed":
            try:
                v = float(msg.get("value"))
            except (TypeError, ValueError):
                await self.send({"type": "error", "message": "speed must be a number"})
                return
            self.speed = max(0.1, min(MAX_SPEED, v))
            await self.position()
        elif t == "seek":
            try:
                await self.seek(int(msg.get("index")))
            except (TypeError, ValueError):
                await self.send({"type": "error", "message": "seek needs an index"})
        else:
            await self.send({"type": "error", "message": f"unknown message type {t!r}"})

    async def load(self, name):
        try:
            path = safe_path(self.data_dir, name)
        except ValueError as e:
            await self.send({"type": "error", "message": f"{name}: {e}"})
            return
        cols, rows = read_rows(path)
        for i, r in enumerate(rows):
            r["_i"] = i
        self.file, self.columns, self.rows = path.name, cols, rows
        self.playing = False
        self.index = -1
        await self.send({"type": "loaded", "file": self.file, "columns": cols, "count": len(rows)})
        await self.seek(0)

    async def seek(self, index):
        if not self.rows:
            return
        self.index = max(0, min(len(self.rows) - 1, index))
        self._carry = 0.0
        await self.send({"type": "history", "rows": self.rows[: self.index + 1]})
        await self.position()

    async def position(self):
        await self.send({"type": "position", "index": self.index, "count": len(self.rows),
                         "playing": self.playing, "speed": self.speed, "file": self.file})

    async def tick(self, dt):
        """Advance playback by dt seconds of real time."""
        if not self.playing or not self.rows:
            return
        self._carry += self.speed * dt
        n = int(self._carry)
        if n <= 0:
            return
        self._carry -= n
        start = self.index + 1
        end = min(len(self.rows), start + n)
        if start < end:
            self.index = end - 1
            await self.send({"type": "rows", "rows": self.rows[start:end]})
        if self.index >= len(self.rows) - 1:
            self.playing = False
            await self.send({"type": "end"})
        await self.position()

    async def run(self, stop):
        """Drive playback until stop (an asyncio.Event) is set."""
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            await self.tick(now - last)
            last = now
