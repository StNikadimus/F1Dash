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
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import DASHBOARD_DIR, DATA_DIR, resolve_path
from .engine import Engine
from .f1tv_auth import AuthManager, AuthStore, login_page, result_page
from .hub import Hub
from .mode import SELECTABLE, ModeController
from .netinfo import remote_info
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


def build_source(cfg: dict[str, Any], tracks: TrackProvider,
                 auth: AuthManager | None = None) -> tuple[Source, bool]:
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
    src = F1LiveSource(cfg["live"], recorder, auth=auth if auth is not None else make_auth(cfg))
    return src, src.token_configured


def make_auth(cfg: dict[str, Any]) -> AuthManager:
    """F1 TV sign-in of the live source ([f1_tv]); the legacy live.f1tv_token / F1TV_TOKEN still wins."""
    f1 = cfg.get("f1_tv") or {}
    store = AuthStore(resolve_path(f1.get("auth_file") or "data/auth/f1tv_auth.json"))
    return AuthManager(f1, store, override=(cfg.get("live") or {}).get("f1tv_token") or "")


class Runtime:
    """The data source + engine that run now. Switching LIVE <-> VOD (the mode selector,
    server/mode.py) stops both and starts the other pair in the same process - the HTTP server,
    the dashboards' WebSockets, the remote, the VOYO monitor and the F1 TV sign-in stay."""

    def __init__(self, cfg: dict[str, Any], hub: Hub, tracks: TrackProvider, auth: AuthManager) -> None:
        self.cfg = cfg
        self.hub = hub
        self.tracks = tracks
        self.auth = auth
        self.source: Source | None = None
        self.engine: Engine | None = None
        self.tasks: list[asyncio.Task] = []

    def mode_cfg(self, mode: str) -> dict[str, Any]:
        c = dict(self.cfg)
        c["source"] = {**self.cfg["source"], "mode": mode}
        if mode == "vod":
            c["voyo"] = {**(self.cfg.get("voyo") or {}), "enabled": True}   # a recording is watched in VOYO
        return c

    async def stop(self) -> None:
        tasks, self.tasks = self.tasks, []
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.engine is not None:
            self.engine.close()
        rec = getattr(self.source, "recorder", None)
        if rec:
            rec.close()

    async def switch(self, mode: str) -> None:
        """Run ``mode`` (live / vod / test / replay) from now on."""
        old = self.source.mode if self.tasks and self.source is not None else None
        await self.stop()
        source, engine = self.prepare(mode)
        if old is None:
            log.info("Starting data source: %s", source.mode.upper())
        else:
            log.info("Data source switched: %s -> %s (no restart)", old.upper(), source.mode.upper())
        self.tasks = [asyncio.create_task(source.run(engine), name="source"),
                      asyncio.create_task(engine.publish_loop(), name="publish")]

    def prepare(self, mode: str) -> tuple[Source, Engine]:
        """Build the source + engine of ``mode`` (not started) and reset the dashboards."""
        c = self.mode_cfg(mode)
        source, token_ok = build_source(c, self.tracks, self.auth)
        engine = Engine(c, source, self.tracks, self.hub, token_configured=token_ok)
        self.source, self.engine = source, engine
        hello = dict(self.hub.hello)
        hello["mode"] = source.mode
        hello["config"] = {**(hello.get("config") or {}), "sync_enabled": engine.sync.enabled}
        self.hub.reset_data(hello)
        return source, engine


def create_app(cfg: dict[str, Any]) -> Starlette:
    start_mode = str(cfg["source"].get("mode") or "auto").lower()
    # --test / --replay: developer sources, AUTO means "that source"; live / vod: a manual
    # selection at start (config / --live / --vod); auto: the detection decides
    fixed = start_mode.upper() if start_mode in ("test", "replay") else None
    selected = {"live": "LIVE", "vod": "VOD"}.get(start_mode, "AUTO")
    from . import chase
    chase.CHASE_GAP_S = max(0.1, float(cfg["dashboard"].get("chase_gap_seconds", 1.5)))
    hub = Hub()
    tracks = TrackProvider(DATA_DIR, cfg["tracks"])
    auth = make_auth(cfg)                     # one F1 TV sign-in for every LIVE period of this process
    rt = Runtime(cfg, hub, tracks, auth)

    async def publish_ui(msg: dict) -> None:
        if rt.engine is not None:
            rt.engine.set_selected(msg.get("selected"))
        hub.set_ui(msg)

    remote = RemoteController(cfg["remote"], lambda: rt.engine.order() if rt.engine else [], publish_ui)
    remote.sync_hook = lambda name, arg: rt.engine.sync_command(name, arg) if rt.engine else None
    remote.track_hook = lambda: rt.engine.track_report() if rt.engine else "No data source running"
    remote.weather_hook = lambda arg: rt.engine.request_weather_report(arg) if rt.engine else "No data source running"
    # TEST mode: simulated race events (the animations come from the resulting feed messages)
    remote.sim_hook = lambda arg: (rt.source.inject(arg) if rt.source is not None and hasattr(rt.source, "inject")
                                   else "Simulated events only in TEST mode")
    remote.auto_cycle_seconds = int(cfg["dashboard"].get("auto_cycle_seconds", 20))
    voyo_cfg = cfg.get("voyo") or {}
    if start_mode == "vod":
        voyo_cfg = {**voyo_cfg, "enabled": True}
    remote.set_default_tv_mode(str(voyo_cfg.get("default_tv_mode", "RACE_VIEW")).upper())
    video = VideoMonitor(voyo_cfg, remote, hub)
    hub.ui = remote.message()
    dash = cfg["dashboard"]
    hub.hello = {
        "type": "hello", "mode": None,
        "config": {
            "interp_delay_ms": int(dash.get("interp_delay_ms", 1200)),
            "map_fps": max(5, min(60, int(dash.get("map_fps", 30)))),
            "animations": str(dash.get("animations", "full")),
            "pulse_period_ms": int(dash.get("pulse_period_ms", 2400)),
            "reorder_ms": int(dash.get("reorder_ms", 450)),
            "remote_enabled": remote.enabled,
            "sync_enabled": bool((cfg.get("sync") or {}).get("enabled", True)),
        },
        "keymap": remote.keymap,
        "keymap_video": remote.keymap_video,
        "keymap_video_focus": remote.keymap_video_focus,
    }

    port = int(cfg["server"]["port"])
    callback_url = f"http://127.0.0.1:{port}/f1tv/callback"
    if auth.subscription:
        auth.login_url = f"http://127.0.0.1:{port}/f1tv/login"
        if cfg.get("_force_login"):
            auth.logout()

    tasks: list[asyncio.Task] = []

    def restart_video() -> None:
        # a recording is watched in the VOYO window: switching to VOD turns the video layer on
        if not video.enabled:
            video.enabled = True
            vt = next((t for t in tasks if t.get_name() == "video"), None)
            if vt is not None:
                vt.cancel()
                tasks.remove(vt)
            tasks.append(asyncio.create_task(video.run(), name="video"))

    async def switch(mode: str) -> None:
        await rt.switch(mode)
        if rt.engine is not None:
            rt.engine.set_selected(remote.ui.selected)
        if mode == "vod":
            restart_video()

    def feed_status():
        if rt.source is None or rt.source.mode != "live" or rt.engine is None:
            return None
        return (rt.engine.feed_state.get("SessionStatus") or {}).get("Status")

    mode = ModeController(selected, fixed=fixed, switch=switch, publish=hub.set_mode, feed_status=feed_status)
    # an engine exists from the start (API requests); the lifespan starts the mode actually wanted
    rt.prepare(fixed.lower() if fixed else "vod" if selected == "VOD" else "live")
    remote.mode_hook = mode.select

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if fixed is None:
            try:                                # the first detection decides what AUTO starts with
                await asyncio.wait_for(mode.detect(), 12)
            except asyncio.TimeoutError:
                log.info("Mode detection: F1 schedule did not answer within 12 s - assuming a recording")
        st = mode.state()
        log.info("Mode: selected %s, detected %s (%s) -> %s", st["selected_mode"], st["detected_mode"],
                 st["detected_reason"], st["effective_mode"])
        await mode.apply()
        tasks.append(asyncio.create_task(mode.run(), name="mode"))
        try:
            pr = phone_remote()
            log.info("PHONE REMOTE: %s", pr["url"].split("?")[0] + (" (+ ?token=...)" if pr["token_required"] else ""))
            for u in pr["tailscale"]:
                log.info("PHONE REMOTE (Tailscale): %s", u.split("?")[0])
            if pr["local_only"]:
                log.warning("PHONE REMOTE: %s", pr["note"])
        except Exception:  # noqa: BLE001 - only a hint
            log.debug("phone remote address not determined", exc_info=True)
        tasks.append(asyncio.create_task(remote.auto_cycle_loop(), name="autocycle"))
        tasks.append(asyncio.create_task(video.run(), name="video"))  # independent of the F1 data pipeline
        yield
        for t in tasks:
            t.cancel()
        await rt.stop()

    # ---------------------------------------------------------------- HTTP
    async def index(request: Request) -> Response:
        return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def remote_page(request: Request) -> Response:
        return FileResponse(DASHBOARD_DIR / "remote.html", headers={"Cache-Control": "no-cache"})

    async def health(request: Request) -> Response:
        st = mode.state()
        return JSONResponse({"ok": True, "mode": rt.source.mode if rt.source else None,
                             "selected_mode": st["selected_mode"], "detected_mode": st["detected_mode"],
                             "effective_mode": st["effective_mode"],
                             "status": rt.engine._status if rt.engine else {}, "clients": len(hub.clients)})

    async def api_mode(request: Request) -> Response:
        """GET: the mode selector state. POST {"mode": "AUTO" | "LIVE" | "VOD"}: select it
        (switches the data source in the running server, no restart)."""
        if request.method == "GET":
            return JSONResponse(mode.state(), headers={"Cache-Control": "no-store"})
        bad = _remote_guard(request)
        if bad:
            return bad
        try:
            body = json.loads((await request.body())[:MAX_WS_MESSAGE] or b"{}")
            value = str(body.get("mode", "")).upper() if isinstance(body, dict) else ""
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if value not in SELECTABLE:
            return JSONResponse({"ok": False, "error": "mode AUTO, LIVE or VOD expected"}, status_code=400)
        text = await mode.select(value)
        st = mode.state()
        return JSONResponse({"ok": not st["error"], "result": text, **st}, status_code=200 if not st["error"] else 500)

    # ---------------------------------------------------------------- F1 TV sign-in (this computer only)
    def _local(request: Request) -> bool:
        return (request.client.host if request.client else "") in LOOPBACK

    async def f1tv_login(request: Request) -> Response:
        if not auth.subscription:
            return HTMLResponse(result_page(False, "F1 TV sign-in is off ([f1_tv] subscription = false)."),
                                status_code=409)
        if not _local(request):
            return HTMLResponse(result_page(False, "Open this page on the computer that runs the dashboard."),
                                status_code=403)
        return HTMLResponse(login_page(callback_url, auth.store.key(), auth.public_info()),
                            headers={"Cache-Control": "no-store"})

    async def f1tv_callback(request: Request) -> Response:
        if not auth.subscription:
            return HTMLResponse(result_page(False, "F1 TV sign-in is off."), status_code=409)
        if not _local(request):
            return HTMLResponse(result_page(False, "Only accepted from this computer."), status_code=403)
        body = await request.body()
        if len(body) > 16384:
            return HTMLResponse(result_page(False, "Too large."), status_code=413)
        from urllib.parse import parse_qs
        form = parse_qs(body.decode("utf-8", "replace"))
        key = (form.get("key") or [""])[0]
        import secrets as _secrets
        if not _secrets.compare_digest(key, auth.store.key()):
            log.warning("F1 TV sign-in with a wrong bookmark key refused (re-create the bookmark on /f1tv/login)")
            return HTMLResponse(result_page(False, "This bookmark belongs to another installation - drag the "
                                                   "button on the sign-in page to your bookmarks again."),
                                status_code=403)
        ok, msg = auth.complete((form.get("session") or [""])[0])
        return HTMLResponse(result_page(ok, msg), status_code=200 if ok else 400,
                            headers={"Cache-Control": "no-store"})

    async def f1tv_status(request: Request) -> Response:
        info = auth.public_info()
        return JSONResponse(info, headers={"Cache-Control": "no-store"})

    async def api_diagnostics(request: Request) -> Response:
        if request.query_params.get("format") == "text":
            from .diagnostics import format_report
            st = mode.state()
            pr = phone_remote()
            head = (f"Mode: selected {st['selected_mode']} · detected {st['detected_mode']} "
                    f"({st['detected_reason']}) · effective {st['effective_mode']}\n"
                    f"PHONE REMOTE: {', '.join(u.split('?')[0] for u in pr['lan'] + pr['tailscale']) or pr['note']}\n")
            return Response(head + format_report(rt.engine.diagnostics()), media_type="text/plain; charset=utf-8")
        return JSONResponse({**rt.engine.diagnostics(), "mode": mode.state()}, headers={"Cache-Control": "no-store"})

    async def api_track_layouts(request: Request) -> Response:
        return JSONResponse(rt.engine.track_choices(), headers={"Cache-Control": "no-store"})

    async def api_track_choice(request: Request) -> Response:
        if not remote.enabled:
            return JSONResponse({"ok": False, "error": "remote disabled"}, status_code=403)
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        try:
            body = json.loads((await request.body())[:2048] or b"{}")
            ref_id = body.get("layout") if isinstance(body, dict) else None
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if ref_id is not None and not isinstance(ref_id, str):
            return JSONResponse({"ok": False, "error": "layout id expected"}, status_code=400)
        return JSONResponse({"ok": True, "result": rt.engine.set_track_choice(ref_id)})

    async def api_state(request: Request) -> Response:
        return JSONResponse(rt.engine.snapshot())

    def phone_remote() -> dict:
        return remote_info(str(cfg["server"]["host"]), port, remote.token)

    async def api_remote_info(request: Request) -> Response:
        """URLs of the phone remote (LAN / Tailscale) for the QR code on the dashboard."""
        return JSONResponse(phone_remote(), headers={"Cache-Control": "no-store"})

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
        return JSONResponse(rt.engine.sync_status(), headers={"Cache-Control": "no-cache"})

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
        if rt.source.mode != "vod":
            return JSONResponse({"ok": False, "error": "only in VOD mode (python main.py --vod)"}, status_code=409)
        rt.engine.select_session(key)
        return JSONResponse({"ok": True})

    SYNC_ACTIONS = {"capture": None, "countdown": "countdown", "exact": "f1_time", "auto": None,
                    "estimate": "lead_seconds", "clear": None, "keep_old": None, "use_new": None,
                    "resync": None, "select_session": "session_key", "clock": "clock", "marker": "marker",
                    "stream_start": None, "stream_reset": None}

    async def api_media_catalog(request: Request) -> Response:
        """SELECT SESSION: Grands Prix + sessions of a season (public data, read-only)."""
        if rt.source.mode != "vod":
            return JSONResponse({"error": "only in VOD mode"}, status_code=409)
        try:
            year = int(request.query_params.get("year", ""))
            if not 2018 <= year <= 2100:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "year expected"}, status_code=400)
        return JSONResponse(await rt.engine.media_catalog(year), headers={"Cache-Control": "no-cache"})

    async def api_sync_action(request: Request) -> Response:
        """SYNC menu: POST /api/sync/{capture|countdown|exact|auto|estimate|clear}.

        countdown {"countdown": "23:47"}   - VOYO countdown to the session start at the captured moment
                  {"countdown": "4:00|actual"} - ... counting to: auto | scheduled | announced | actual start
        stream_start {}                    - MARK STREAM START: the video shows the ACTUAL session start now
        stream_reset {}                    - remove the stream start mark
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
        res = rt.engine.sync_action(action, value)
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
        rt.engine.voyo_sample(sample)
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------------- WebSocket
    async def ws_endpoint(ws: WebSocket) -> None:
        # ?client=remote: a phone remote (served by /remote) - the same endpoint and commands, but only
        # the small remote state is sent to it; the remote token ([remote] token) is required when set
        kind = "remote" if ws.query_params.get("client") == "remote" else "dashboard"
        if kind == "remote" and not remote.check_token(ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        await ws.accept()
        addr = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
        try:
            client = await hub.add(ws, addr, kind)
        except Exception:  # noqa: BLE001 - log it once in full, then keep the dashboard connected
            log.exception("Dashboard connection setup failed for %s", addr)
            raise
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
                elif kind == "mode":
                    # mode selector of a dashboard: AUTO / LIVE / VOD (switches in the background)
                    value = str(msg.get("value", "")).upper()
                    if value in SELECTABLE:
                        await remote.handle_command("SET_MODE", value, f"ws:{addr}")
                    else:
                        log.warning("Rejected mode %r from %s", value[:10], addr)
                elif kind == "track_choice":
                    # "choose the circuit" menu of a dashboard: a known layout id or "auto"
                    value = msg.get("value")
                    if value is None or (isinstance(value, str) and len(value) <= 40):
                        text = rt.engine.set_track_choice(value)
                        remote._toast(text)
                        await publish_ui(remote.message())
                elif kind == "sync_action":
                    # SYNC menu of a dashboard: same validation as POST /api/sync/{action}
                    action = str(msg.get("action", ""))
                    value = msg.get("value")
                    if action in SYNC_ACTIONS and (value is None or isinstance(value, (str, int, float))):
                        res = rt.engine.sync_action(action, value)
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
        Route("/api/diagnostics", api_diagnostics),
        Route("/api/mode", api_mode, methods=["GET", "POST"]),
        Route("/api/track/layouts", api_track_layouts),
        Route("/api/track/choice", api_track_choice, methods=["POST"]),
        Route("/f1tv/login", f1tv_login),
        Route("/f1tv/callback", f1tv_callback, methods=["POST"]),
        Route("/f1tv/status", f1tv_status),
        Route("/api/ui", api_ui),
        Route("/api/remote/info", api_remote_info),
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
    app.state.runtime = rt
    app.state.mode = mode
    return app


async def diagnose(cfg: dict[str, Any], seconds: float) -> None:
    """``python main.py --diagnose``: live connection for ``seconds`` without the HTTP server,
    then the report of what F1 actually delivered. Uses the stored F1 TV sign-in; when there is
    none it reports ANONYMOUS (sign in with a normal start or --f1-login first)."""
    from .diagnostics import format_report
    cfg = dict(cfg)
    cfg["live"] = {**cfg["live"], "record": False}
    cfg["f1_tv"] = {**(cfg.get("f1_tv") or {}), "open_browser": False}
    hub = Hub()
    tracks = TrackProvider(DATA_DIR, cfg["tracks"])
    source, token_ok = build_source(cfg, tracks)
    engine = Engine(cfg, source, tracks, hub, token_configured=token_ok)
    print(f"Connecting to F1 live timing for {seconds:.0f} s ...", flush=True)
    tasks = [asyncio.create_task(source.run(engine)), asyncio.create_task(engine.publish_loop())]
    try:
        await asyncio.sleep(seconds)
    finally:
        print(format_report(engine.diagnostics()), flush=True)
        for t in tasks:
            t.cancel()
