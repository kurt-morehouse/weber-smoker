"""FastAPI server: replays saved cooks, relays the Pi's live stream, keeps the
cook journal, and serves the dashboard.

    cd backend
    .venv/bin/uvicorn server:app --reload          # http://localhost:8000

Routes
    GET  /                                  the dashboard (frontend/)
    GET  /api/cooks                         cook files in the data folder
    GET  /api/labels                        column names, labels and units
    GET  /api/journal-config                what the journal accepts
    GET  /api/journal/{stem}                one cook's journal
    PUT  /api/journal/{stem}                save its notes
    POST /api/journal/{stem}/photos         add a photo (multipart "photo")
    GET  /api/journal/{stem}/photos/{name}  a photo
    DELETE /api/journal/{stem}/photos/{name}
    WS   /ws/replay                         replay player (replay_player.py)
    WS   /ws/live                           live view (live.py)
"""
import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import journal as journal_mod
import settings as settings_mod
from labels import LABELS
from live import LiveRelay
from replay import list_cooks
from replay_player import Player


def create_app(cfg=None, start_live=None):
    cfg = cfg or settings_mod.load()
    start_live = cfg.live if start_live is None else start_live
    journal = journal_mod.Journal(cfg.journal_dir)

    @asynccontextmanager
    async def lifespan(app):
        app.state.live = LiveRelay(cfg)
        if start_live:
            app.state.live.start()
        yield
        await app.state.live.stop()

    app = FastAPI(title="Weber smoker", lifespan=lifespan)
    app.state.settings = cfg

    @app.get("/api/cooks")
    def cooks():
        return list_cooks(cfg.data_dir)

    @app.get("/api/labels")
    def labels():
        return LABELS

    @app.get("/api/journal-config")
    def journal_config():
        return journal_mod.config()

    def _bad(e):
        raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/journal/{stem}")
    def get_journal(stem: str):
        try:
            return journal.get(stem)
        except ValueError as e:
            _bad(e)

    @app.put("/api/journal/{stem}")
    async def put_journal(stem: str, body: dict):
        try:
            return journal.save(stem, body)
        except ValueError as e:
            _bad(e)

    @app.post("/api/journal/{stem}/photos")
    async def add_photo(stem: str, photo: UploadFile = File(...), caption: str = Form("")):
        raw = await photo.read()
        try:
            return journal.add_photo(stem, photo.filename or "photo.jpg", raw, caption)
        except ValueError as e:
            _bad(e)

    @app.get("/api/journal/{stem}/photos/{name}")
    def get_photo(stem: str, name: str):
        try:
            return FileResponse(journal.photo_path(stem, name), media_type="image/jpeg")
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.delete("/api/journal/{stem}/photos/{name}")
    def delete_photo(stem: str, name: str):
        try:
            return journal.delete_photo(stem, name)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.websocket("/ws/replay")
    async def ws_replay(ws: WebSocket):
        await ws.accept()

        async def send(msg):
            await ws.send_text(json.dumps(msg))

        player = Player(cfg.data_dir, send)
        stop = asyncio.Event()
        runner = asyncio.create_task(player.run(stop))
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    await send({"type": "error", "message": "not JSON"})
                    continue
                await player.handle(msg)
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            await runner

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        live = app.state.live

        async def send(msg):
            await ws.send_text(json.dumps(msg))

        await live.attach(send)

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                await send(live.state())

        hb = asyncio.create_task(heartbeat())
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                if msg.get("type") == "command":
                    await live.command(send, msg)
        except WebSocketDisconnect:
            pass
        finally:
            hb.cancel()
            live.detach(send)

    if cfg.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=cfg.frontend_dir, html=True), name="frontend")
    else:
        @app.get("/")
        def no_frontend():
            return JSONResponse({"error": f"no dashboard at {cfg.frontend_dir}"}, status_code=404)

    return app


app = create_app()
