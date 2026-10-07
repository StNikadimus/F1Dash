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

from .config import DASHBOARD_DIR, DATA_DIR, REPO_ROOT, resolve_path
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
from .voyo_recording import CAPTURE_NAME as VOYO_CAPTURE_NAME, VoyoStreamRecorder, load_package, recalibrate
from . import activity as activity_mod
from . import recordings_admin as admin

log = logging.getLogger("app")

MAX_WS_MESSAGE = 1024
MAX_CLOCK_BODY = 8192
DISK_PAGE_DIR = REPO_ROOT / "server" / "disk"      # the /disk page (Linux server deployment folder)
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

    def __init__(self, cfg: dict[str, Any], hub: Hub, tracks: TrackProvider, auth: AuthManager,
                 stream_recorder: VoyoStreamRecorder | None = None) -> None:
        self.cfg = cfg
        self.stream_recorder = stream_recorder
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
        engine = Engine(c, source, self.tracks, self.hub, token_configured=token_ok,
                        stream_recorder=self.stream_recorder)
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
    # VOYO stream recordings: written on this server under [voyo.recording] path (checked now)
    # the /disk page: activity log (48 h), retention settings changed on the page
    activity = activity_mod.install(DATA_DIR / "logs" / "activity.jsonl", 48.0)
    rec_settings = admin.Settings(DATA_DIR / "voyo_recording_settings.json")
    rec_cfg = rec_settings.apply((cfg.get("voyo") or {}).get("recording") or {})
    rec_sizes = admin.Sizes()
    player_hb: dict = {"data": None, "at": None}
    stream_rec = VoyoStreamRecorder(rec_cfg, DATA_DIR / "recordings").start()
    # the server's own VOYO player (tools/voyo_server_player.py): its own stream instances + packages
    sp_cfg = (cfg.get("voyo") or {}).get("server_player") or {}
    player_rec = player_tracker = None
    if sp_cfg.get("enabled"):
        from .autosync import StreamTracker
        player_rec = VoyoStreamRecorder(rec_cfg, DATA_DIR / "recordings", channel="server_player",
                                        capture=bool(sp_cfg.get("record_video", True))).start()
        player_store_path = DATA_DIR / "voyo_server_player_instances.json"
        try:
            player_store = json.loads(player_store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            player_store = {}
        player_tracker = StreamTracker(player_store)
    rt = Runtime(cfg, hub, tracks, auth, stream_rec)

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
    stream_rec.mode_info = mode.state
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
        log.info("Server started - dashboard on port %s, recordings: %s", cfg["server"].get("port"),
                 stream_rec.root if stream_rec.ok else (stream_rec.error or "off"))

        async def recorder_housekeeping():
            while True:
                await asyncio.sleep(60)
                for r in (stream_rec, player_rec):
                    if r is not None:
                        with contextlib.suppress(Exception):
                            r.housekeeping()
                with contextlib.suppress(Exception):
                    activity.prune()
        tasks.append(asyncio.create_task(recorder_housekeeping(), name="rec-housekeeping"))
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
        log.info("Server stopping")
        stream_rec.close()
        if player_rec is not None:
            player_rec.close()

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
                    "stream_start": None, "stream_reset": None,
                    "event_set": "id", "event_remove": "id", "event_clear": None}

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
            body = json.loads(raw or b"{}")
            sample = parse_voyo_sample(body, time.time(), time.monotonic())
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:80]}, status_code=400)
        if isinstance(body, dict) and body.get("channel") == "server_player":
            return server_player_sample(request, body, sample)
        rt.engine.voyo_sample(sample)
        # the open VOYO stream recording + whether the PC should run the opt-in window capture for it
        return JSONResponse({"ok": True, "recording": stream_rec.clock_reply()})

    # ---------------------------------------------------------------- VOYO stream recordings
    class PlayerView:
        """What the recorder reads for a server-player sample: the F1 session (the live engine's,
        else the player's schedule hint), LIVE DATA DELAY of the live feed - never the dashboard's
        sync of the VOYO window you watch (that is another stream instance)."""

        def __init__(self, inst, hint: dict) -> None:
            eng = rt.engine
            live_eng = eng is not None and not eng.vod and rt.source is not None and rt.source.mode == "live"
            sess = (eng.sync.session if live_eng else None) or {}
            if not sess.get("session_key") and hint.get("session_name"):
                sess = {"session_key": None, "meeting_name": hint.get("meeting"),
                        "session_name": hint.get("session_name"), "date_start": hint.get("start"),
                        "source": "server player schedule"}
            self.session = sess or None
            self.vod = not (inst is not None and inst.live)
            self.live_delay = eng.sync.live_delay if live_eng else None
            self.auto_state, self.mapping, self.K, self.page = {}, None, None, None

    def server_player_sample(request: Request, body: dict, sample) -> Response:
        if player_rec is None:
            return JSONResponse({"ok": False, "error": "[voyo.server_player] enabled = false"}, status_code=409)
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            return JSONResponse({"ok": False, "error": "the server player runs on this server only"},
                                status_code=403)
        if body.get("close"):
            # the player closed the stream (session window over): its package is complete
            player_rec.finalize(str(body.get("why") or "server player closed the stream")[:80])
            player_tracker.current = None
            return JSONResponse({"ok": True, "recording": player_rec.clock_reply()})
        reason = player_tracker.observe(sample, {}, time.time())
        inst = player_tracker.current
        if reason and inst is not None:
            player_tracker.store.setdefault("instances", {})[inst.id] = inst.to_json()
            with contextlib.suppress(OSError):
                (DATA_DIR / "voyo_server_player_instances.json").write_text(
                    json.dumps(player_tracker.store, default=str), encoding="utf-8")
        hint = body.get("session_hint") if isinstance(body.get("session_hint"), dict) else {}
        hint = {k: str(v)[:120] for k, v in hint.items() if k in ("meeting", "session_name", "start", "end")}
        try:
            player_rec.observe(sample, inst, reason, PlayerView(inst, hint))
        except Exception:  # noqa: BLE001
            log.exception("VOYO server player recording failed")
        return JSONResponse({"ok": True, "recording": player_rec.clock_reply()})

    # ---------------------------------------------------------------- the /disk page
    def _loopback(request: Request) -> bool:
        return (request.client.host if request.client else "") in LOOPBACK

    async def api_player_status(request: Request) -> Response:
        """Heartbeat of the server VOYO player (every 15 s): idle / open, next session, problems."""
        if not _loopback(request):
            return JSONResponse({"ok": False, "error": "local only"}, status_code=403)
        try:
            body = json.loads((await request.body())[:8192] or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": "json"}, status_code=400)
        if isinstance(body, dict):
            player_hb.update(data=body, at=time.monotonic())
        return JSONResponse({"ok": True})

    async def api_player_log(request: Request) -> Response:
        """A line of the server VOYO player's own log -> the activity log."""
        if not _loopback(request):
            return JSONResponse({"ok": False, "error": "local only"}, status_code=403)
        try:
            body = json.loads((await request.body())[:4096] or b"{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": "json"}, status_code=400)
        lvl = str(body.get("level") or "INFO").upper()
        activity.add(str(body.get("text") or "")[:600], lvl if lvl in ("INFO", "WARNING", "ERROR") else "INFO",
                     "voyo-player")
        return JSONResponse({"ok": True})

    def _hb_age():
        return None if player_hb["at"] is None else time.monotonic() - player_hb["at"]

    async def disk_page(request: Request) -> Response:
        return FileResponse(DISK_PAGE_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def api_disk_status(request: Request) -> Response:
        sizes = rec_sizes.get(stream_rec.root if stream_rec.ok else None)
        rc = stream_rec.rc
        return JSONResponse({
            "now": time.time(), "token_required": bool(remote.token),
            "state": admin.current_state(stream_rec, player_rec, player_rec is not None, player_hb["data"], _hb_age()),
            "disk": admin.disk_info(stream_rec, sizes),
            "player": {"enabled": player_rec is not None, "heartbeat": player_hb["data"],
                       "heartbeat_age_s": None if _hb_age() is None else round(_hb_age(), 1)},
            "retention": admin.retention_table(rc, rec_settings.values),
            "config": {"path": rc.get("path"), "require_mount": rc.get("require_mount"),
                       "mount_marker": rc.get("mount_marker"), "capture_encoder": rc.get("capture_encoder"),
                       "capture_fps": rc.get("capture_fps"), "capture_segment_seconds": rc.get("capture_segment_seconds"),
                       "record_video_pc": bool(rc.get("record_video_capture")),
                       "record_video_server": bool(sp_cfg.get("record_video", True)) if player_rec is not None else None,
                       "record_sessions": sp_cfg.get("record_sessions") if player_rec is not None else None,
                       "lead_minutes": sp_cfg.get("lead_minutes"), "trail_minutes": sp_cfg.get("trail_minutes"),
                       "resolution": sp_cfg.get("resolution"), "min_free_bytes": stream_rec.min_free,
                       "log_keep_hours": 48}})

    async def api_disk_log(request: Request) -> Response:
        try:
            limit = min(2000, int(request.query_params.get("limit") or 400))
        except ValueError:
            limit = 400
        return JSONResponse({"entries": activity.entries(limit, request.query_params.get("level") or "INFO"),
                             "keep_hours": 48})

    async def api_disk_recordings(request: Request) -> Response:
        fresh = bool(request.query_params.get("fresh"))
        if fresh and stream_rec.ok:
            with contextlib.suppress(OSError):
                stream_rec.rebuild_index()              # also packages copied onto the disk by hand
        sizes = rec_sizes.get(stream_rec.root if stream_rec.ok else None, force=fresh)
        recs = stream_rec.list(also=(player_rec,))
        for r in recs:
            r.update(sizes.get(r["stream_instance_id"]) or {"video_bytes": 0, "data_bytes": 0, "segments": 0})
            r["keep_days"] = stream_rec.keep_days(r.get("session_kind") or "other")
        return JSONResponse({"recordings": recs, "ok": stream_rec.ok, "error": stream_rec.error})

    async def api_disk_settings(request: Request) -> Response:
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        try:
            body = json.loads((await request.body())[:8192] or b"{}")
            changed = rec_settings.update(body if isinstance(body, dict) else {}, (stream_rec, player_rec))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        removed = 0
        for r in (stream_rec, player_rec):
            if r is not None and r.ok:
                removed += r.apply_retention()
        rec_sizes.get(stream_rec.root if stream_rec.ok else None, force=True)
        return JSONResponse({"ok": True, "changed": changed, "video_files_deleted": removed})

    async def api_disk_delete(request: Request) -> Response:
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        try:
            msg = admin.delete_package(stream_rec, (player_rec,), request.path_params["iid"],
                                       request.query_params.get("what") or "video")
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc).strip("'")}, status_code=404)
        except (PermissionError, ValueError, OSError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        rec_sizes.get(stream_rec.root if stream_rec.ok else None, force=True)
        return JSONResponse({"ok": True, "message": msg})

    def _rec_for(iid: str) -> VoyoStreamRecorder:
        if player_rec is not None:
            pkg = player_rec.package_dir(iid)
            if pkg is not None:
                with contextlib.suppress(OSError, ValueError):
                    if json.loads((pkg / "manifest.json").read_text(encoding="utf-8")).get("channel") == \
                            "server_player":
                        return player_rec
        return stream_rec

    def _voyo_poster_ok(request: Request) -> JSONResponse | None:
        host = request.client.host if request.client else ""
        if host not in LOOPBACK and not allow_remote_clock:
            return JSONResponse({"ok": False, "error": "only accepted from this computer"}, status_code=403)
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        return None

    async def api_voyo_recordings(request: Request) -> Response:
        """List of the VOYO stream recordings on this server (index.json of [voyo.recording] path)."""
        return JSONResponse({"status": stream_rec.status(),
                             "server_player": player_rec.status() if player_rec is not None else None,
                             "recordings": stream_rec.list(also=(player_rec,))})

    async def api_voyo_recording(request: Request) -> Response:
        """One package: manifest + meta + anchors + the AUTO SYNC calibration re-run from its anchors
        (?full=1 also returns the timeline and observations)."""
        pkg = stream_rec.package_dir(request.path_params["iid"])
        if pkg is None:
            return JSONResponse({"ok": False, "error": "unknown stream instance"}, status_code=404)
        data = load_package(pkg)
        out = {k: data[k] for k in ("manifest", "meta", "anchors", "capture")}
        out["calibration"] = recalibrate(data)
        if request.query_params.get("full"):
            out["timeline"], out["observations"] = data["timeline"], data["observations"]
        return JSONResponse(out)

    async def api_voyo_recording_file(request: Request) -> Response:
        """A file of a package (timeline.jsonl, ..., capture/<segment>) - remote token required when set."""
        if not remote.check_token(_token(request)):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        pkg = stream_rec.package_dir(request.path_params["iid"])
        name = request.path_params["name"]
        if pkg is None:
            return JSONResponse({"ok": False, "error": "unknown stream instance"}, status_code=404)
        parts = name.split("/")
        allowed = (len(parts) == 1 and parts[0] in ("manifest.json", "meta.json", "timeline.jsonl", "anchors.json",
                                                     "sync_observations.jsonl")) or \
            (len(parts) == 2 and parts[0] == "capture" and (parts[1] == "capture.jsonl" or
                                                            VOYO_CAPTURE_NAME.match(parts[1])))
        path = pkg.joinpath(*parts)
        if not allowed or not path.is_file():
            return JSONResponse({"ok": False, "error": "no such file"}, status_code=404)
        return FileResponse(path)

    async def api_voyo_capture_upload(request: Request) -> Response:
        """PUT one finished window-capture segment from the PC (tools/voyo_capture.py) into the package."""
        denied = _voyo_poster_ok(request)
        if denied is not None:
            return denied
        iid, name = request.path_params["iid"], request.path_params["name"]
        try:
            size = int(request.headers.get("content-length") or 0) or None
        except ValueError:
            size = None
        rec = _rec_for(iid)
        target, err = rec.capture_target(iid, name, size)
        if target is None:
            return JSONResponse({"ok": False, "error": err}, status_code=409 if "off" in (err or "") else 400)
        if target.exists():
            return JSONResponse({"ok": True, "stored": target.name, "duplicate": True})
        limit = int(((cfg.get("voyo") or {}).get("recording") or {}).get("capture_max_segment_bytes", 4 * 1024 ** 3))
        tmp = target.with_name(target.name + ".part")
        n = 0
        try:
            with open(tmp, "wb") as fh:
                async for chunk in request.stream():
                    n += len(chunk)
                    if n > limit:
                        raise ValueError("segment too large")
                    fh.write(chunk)
            if size is not None and n != size:
                raise ValueError(f"incomplete upload ({n} of {size} bytes)")
            tmp.replace(target)
        except (OSError, ValueError) as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            log.warning("VOYO capture upload %s/%s failed: %s", iid, name, exc)
            return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=400)
        info = {}
        for h, k in (("x-capture-start", "pc_start_epoch"), ("x-capture-end", "pc_end_epoch"),
                     ("x-pc-now", "pc_now_epoch")):
            with contextlib.suppress(TypeError, ValueError):
                info[k] = float(request.headers.get(h))
        if "pc_now_epoch" in info:              # PC clock - server clock (upload time ignored): for alignment
            info["pc_clock_minus_server_s"] = round(info["pc_now_epoch"] - time.time(), 3)
        try:
            rec.capture_stored(iid, target, info)
        except OSError as exc:
            log.warning("VOYO capture index %s: %s", iid, exc)
        return JSONResponse({"ok": True, "stored": target.name, "bytes": n})

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
        Route("/api/voyo/recordings", api_voyo_recordings),
        Route("/api/voyo/player/status", api_player_status, methods=["POST"]),
        Route("/api/voyo/player/log", api_player_log, methods=["POST"]),
        Route("/disk", disk_page),
        Route("/disk/", disk_page),
        Route("/api/disk/status", api_disk_status),
        Route("/api/disk/log", api_disk_log),
        Route("/api/disk/recordings", api_disk_recordings),
        Route("/api/disk/settings", api_disk_settings, methods=["POST"]),
        Route("/api/disk/recordings/{iid}/delete", api_disk_delete, methods=["POST"]),
        Route("/api/voyo/recordings/{iid}", api_voyo_recording),
        Route("/api/voyo/recordings/{iid}/capture/{name}", api_voyo_capture_upload, methods=["PUT"]),
        Route("/api/voyo/recordings/{iid}/files/{name:path}", api_voyo_recording_file),
        Route("/api/sync/session", api_sync_session, methods=["POST"]),
        Route("/api/media/catalog", api_media_catalog),
        Route("/api/sync/{action}", api_sync_action, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static"),
        Mount("/disk-static", StaticFiles(directory=str(DISK_PAGE_DIR), check_dir=False), name="disk-static"),
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
