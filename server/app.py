"""HTTP / WebSocket application (Starlette + uvicorn)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import DASHBOARD_DIR, DATA_DIR
from .engine import Engine
from .hub import Hub
from .recorder import Recorder
from .remote import RemoteController
from .sources.base import Source
from .sync import parse_voyo_sample
from .track import TrackProvider
from .video import VideoMonitor

log = logging.getLogger("app")

MAX_WS_MESSAGE = 1024
MAX_CLOCK_BODY = 8192
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def build_source(cfg: dict[str, Any], tracks: TrackProvider) -> tuple[Source, bool]:
    mode = cfg["source"]["mode"]
    if mode == "test":
        from .sources.simulator import SimulatorSource
        geo = tracks.load_test(cfg["test"].get("circuit", "jp-1962"))
        return SimulatorSource(cfg["test"], geo), False
    if mode == "replay":
        from .sources.replay import ReplaySource
        return ReplaySource(cfg["replay"]), False
    if mode == "vod":
        from .openf1 import OpenF1Client
        from .sources.vod import VodSource
        vod = cfg.get("vod") or {}
        client = OpenF1Client(DATA_DIR / "openf1_cache", str(vod.get("openf1_url") or "https://api.openf1.org/v1"))
        return VodSource(vod, client, DATA_DIR / "archive_cache"), False
    from .sources.f1_live import F1LiveSource
    recorder = Recorder(DATA_DIR / "recordings") if cfg["live"].get("record") else None
    src = F1LiveSource(cfg["live"], recorder)
    return src, src.token_configured


def create_app(cfg: dict[str, Any]) -> Starlette:
    if cfg["source"]["mode"] == "vod":
        cfg.setdefault("voyo", {})["enabled"] = True     # a recording is always watched in the VOYO window
    hub = Hub()
    tracks = TrackProvider(DATA_DIR, cfg["tracks"])
    source, token_ok = build_source(cfg, tracks)
    engine = Engine(cfg, source, tracks, hub, token_configured=token_ok)

    async def publish_ui(msg: dict) -> None:
        engine.set_selected(msg.get("selected"))
        hub.set_ui(msg)

    remote = RemoteController(cfg["remote"], engine.order, publish_ui)
    remote.sync_hook = engine.sync_command
    remote.auto_cycle_seconds = int(cfg["dashboard"].get("auto_cycle_seconds", 20))
    voyo_cfg = cfg.get("voyo") or {}
    remote.set_default_tv_mode(str(voyo_cfg.get("default_tv_mode", "RACE_VIEW")).upper())
    video = VideoMonitor(voyo_cfg, remote, hub)
    hub.ui = remote.message()
    dash = cfg["dashboard"]
    hub.hello = {
        "type": "hello", "mode": source.mode,
        "config": {
            "interp_delay_ms": int(dash.get("interp_delay_ms", 1200)),
            "map_fps": max(5, min(60, int(dash.get("map_fps", 30)))),
            "animations": str(dash.get("animations", "full")),
            "pulse_period_ms": int(dash.get("pulse_period_ms", 2400)),
            "reorder_ms": int(dash.get("reorder_ms", 450)),
            "remote_enabled": remote.enabled,
            "sync_enabled": engine.sync.enabled,
        },
        "keymap": remote.keymap,
        "keymap_video": remote.keymap_video,
        "keymap_video_focus": remote.keymap_video_focus,
    }

    tasks: list[asyncio.Task] = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        log.info("Starting data source: %s", source.mode.upper())
        tasks.append(asyncio.create_task(source.run(engine), name="source"))
        tasks.append(asyncio.create_task(engine.publish_loop(), name="publish"))
        tasks.append(asyncio.create_task(remote.auto_cycle_loop(), name="autocycle"))
        tasks.append(asyncio.create_task(video.run(), name="video"))  # independent of the F1 data pipeline
        yield
        for t in tasks:
            t.cancel()
        rec = getattr(source, "recorder", None)
        if rec:
            rec.close()

    # ---------------------------------------------------------------- HTTP
    async def index(request: Request) -> Response:
        return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def remote_page(request: Request) -> Response:
        return FileResponse(DASHBOARD_DIR / "remote.html", headers={"Cache-Control": "no-cache"})

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "mode": source.mode, "status": engine._status,
                             "clients": len(hub.clients)})

    async def api_state(request: Request) -> Response:
        return JSONResponse(engine.snapshot())

    async def api_ui(request: Request) -> Response:
        # read-only UI state (TV mode, focus); polled by tools/tv_launcher.py
        return JSONResponse(remote.message(), headers={"Cache-Control": "no-cache"})

    def _token(request: Request) -> str | None:
        return request.headers.get("x-remote-token") or request.query_params.get("token")

    async def remote_key(request: Request) -> Response:
        if not remote.enabled:
            return JSONResponse({"ok": False, "error": "remote disabled"}, status_code=403)
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        if not remote.rate_ok():
            return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
        if request.method == "GET":
            if not remote.allow_get:
                return JSONResponse({"ok": False, "error": "GET disabled"}, status_code=405)
            key = request.query_params.get("key", "")
        else:
            try:
                body = json.loads((await request.body())[:MAX_WS_MESSAGE] or b"{}")
                key = str(body.get("key", ""))
            except (ValueError, AttributeError):
                return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        ok = await remote.handle_key(key, f"http:{request.client.host if request.client else '?'}")
        return JSONResponse({"ok": ok, "ui": remote.message()}, status_code=200 if ok else 400)

    async def remote_command(request: Request) -> Response:
        if not remote.enabled:
            return JSONResponse({"ok": False, "error": "remote disabled"}, status_code=403)
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        if not remote.rate_ok():
            return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
        try:
            body = json.loads((await request.body())[:MAX_WS_MESSAGE] or b"{}")
            ok = await remote.handle_command(str(body.get("command", "")), body.get("arg"),
                                             f"http:{request.client.host if request.client else '?'}")
        except (ValueError, AttributeError):
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        return JSONResponse({"ok": ok, "ui": remote.message()}, status_code=200 if ok else 400)

    # ---------------------------------------------------------------- sync (VOYO playback clock)
    sync_cfg = cfg.get("sync") or {}
    allow_remote_clock = bool(sync_cfg.get("allow_remote_clock", False))
    clock_rate: list[float] = []

    async def api_sync(request: Request) -> Response:
        return JSONResponse(engine.sync_status(), headers={"Cache-Control": "no-cache"})

    def _remote_guard(request: Request) -> Response | None:
        if not remote.enabled:
            return JSONResponse({"ok": False, "error": "remote disabled"}, status_code=403)
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        if not remote.rate_ok():
            return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
        return None

    async def api_sync_session(request: Request) -> Response:
        """Choose the VOD session manually: {"session_key": 11253}."""
        bad = _remote_guard(request)
        if bad:
            return bad
        try:
            body = json.loads((await request.body())[:MAX_WS_MESSAGE] or b"{}")
            key = int(body.get("session_key"))
            if not 1 <= key <= 10_000_000:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"ok": False, "error": "session_key (integer) expected"}, status_code=400)
        if source.mode != "vod":
            return JSONResponse({"ok": False, "error": "only in VOD mode (python main.py --vod)"}, status_code=409)
        engine.select_session(key)
        return JSONResponse({"ok": True})

    SYNC_ACTIONS = {"capture": None, "countdown": "countdown", "exact": "f1_time", "auto": None,
                    "estimate": "lead_seconds", "clear": None, "keep_old": None, "use_new": None,
                    "resync": None, "select_session": "session_key", "clock": "clock", "marker": "marker"}

    async def api_media_catalog(request: Request) -> Response:
        """SELECT SESSION: Grands Prix + sessions of a season (public data, read-only)."""
        if source.mode != "vod":
            return JSONResponse({"error": "only in VOD mode"}, status_code=409)
        try:
            year = int(request.query_params.get("year", ""))
            if not 2018 <= year <= 2100:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "year expected"}, status_code=400)
        return JSONResponse(await engine.media_catalog(year), headers={"Cache-Control": "no-cache"})

    async def api_sync_action(request: Request) -> Response:
        """SYNC menu: POST /api/sync/{capture|countdown|exact|auto|estimate|clear}.

        countdown {"countdown": "23:47"}   - VOYO countdown to the session start at the captured moment
        exact     {"f1_time": ISO UTC}     - what time the video shows (Manual Exact Time, = S concept)
        estimate  {"lead_seconds": 1427}   - video starts this long before the scheduled start (null = remove)
        clock     {"clock": "Q2|remaining|07:32"} - qualifying / practice session clock at the captured moment
        marker    {"marker": "Q2_END"}     - SYNC HERE: the video shows that moment now
        """
        bad = _remote_guard(request)
        if bad:
            return bad
        action = request.path_params["action"]
        if action not in SYNC_ACTIONS:
            return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)
        try:
            body = json.loads((await request.body())[:MAX_WS_MESSAGE] or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        field = SYNC_ACTIONS[action]
        value = body.get(field) if field else None
        if value is not None and not isinstance(value, (str, int, float)):
            return JSONResponse({"ok": False, "error": f"{field}: string or number expected"}, status_code=400)
        res = engine.sync_action(action, value)
        return JSONResponse(res, status_code=200 if res.get("ok") or "result" in res else 400)

    async def api_sync_voyo(request: Request) -> Response:
        """Playback clock samples from tools/tv_launcher.py (read-only values of VOYO's <video>)."""
        host = request.client.host if request.client else ""
        if host not in LOOPBACK and not allow_remote_clock:
            return JSONResponse({"ok": False, "error": "only accepted from this computer"}, status_code=403)
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        now = time.monotonic()
        clock_rate[:] = [t for t in clock_rate if now - t < 1.0]
        if len(clock_rate) >= 30:
            return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
        clock_rate.append(now)
        raw = await request.body()
        if len(raw) > MAX_CLOCK_BODY:
            return JSONResponse({"ok": False, "error": "too large"}, status_code=413)
        try:
            sample = parse_voyo_sample(json.loads(raw or b"{}"), time.time(), time.monotonic())
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:80]}, status_code=400)
        engine.voyo_sample(sample)
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------------- WebSocket
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        addr = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
        client = await hub.add(ws, addr)
        try:
            while True:
                text = await ws.receive_text()
                if len(text) > MAX_WS_MESSAGE:
                    log.warning("Oversized websocket message from %s dropped", addr)
                    continue
                try:
                    msg = json.loads(text)
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    log.warning("Malformed websocket message from %s dropped", addr)
                    continue
                kind = msg.get("type")
                if kind == "ping":
                    continue
                if not remote.enabled or not remote.rate_ok():
                    continue
                if kind == "key":
                    await remote.handle_key(str(msg.get("key", "")), f"ws:{addr}")
                elif kind == "command":
                    await remote.handle_command(str(msg.get("command", "")), msg.get("arg"), f"ws:{addr}")
                elif kind == "sync_action":
                    # SYNC menu of a dashboard: same validation as POST /api/sync/{action}
                    action = str(msg.get("action", ""))
                    value = msg.get("value")
                    if action in SYNC_ACTIONS and (value is None or isinstance(value, (str, int, float))):
                        res = engine.sync_action(action, value)
                        res.pop("state", None)
                        hub._send(client, json.dumps({"type": "sync_result", "action": action, **res}))
                    else:
                        log.warning("Rejected sync action from %s", addr)
                else:
                    log.warning("Unknown websocket message type from %s", addr)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("WebSocket error (%s)", addr)
        finally:
            hub.remove(client)

    routes = [
        Route("/", index),
        Route("/remote", remote_page),
        Route("/api/health", health),
        Route("/api/state", api_state),
        Route("/api/ui", api_ui),
        Route("/api/remote/key", remote_key, methods=["GET", "POST"]),
        Route("/api/remote/command", remote_command, methods=["POST"]),
        Route("/api/sync", api_sync),
        Route("/api/sync/voyo", api_sync_voyo, methods=["POST"]),
        Route("/api/sync/session", api_sync_session, methods=["POST"]),
        Route("/api/media/catalog", api_media_catalog),
        Route("/api/sync/{action}", api_sync_action, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.engine = engine
    return app
