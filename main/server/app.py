"""HTTP / WebSocket application (Starlette + uvicorn)."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import time
from urllib.parse import quote, urlparse
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
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
from .voyo_account import VoyoAccount
from . import security as secmod
from .security import RateLimiter, Security
from .public_gateway import PublicGateway, config_problem as public_config_problem

log = logging.getLogger("app")

MAX_WS_MESSAGE = 1024
MAX_CLOCK_BODY = 8192
DISK_PAGE_DIR = REPO_ROOT / "server" / "disk"      # the /disk page (Linux server deployment folder)
TV_PAGE_DIR = REPO_ROOT / "server" / "tv"          # the /tv page: live stream + dashboard
LIVE_NAME = re.compile(r"^(index\.m3u8|live_\d{5}\.ts)$")
VIEWER_S = 12.0                                     # a /tv viewer counts while it fetched the playlist this recently
# cookies (server/security.py): HttpOnly, SameSite, Secure over https; only their SHA-256 is stored
COOKIE_DISK, COOKIE_TV, COOKIE_DEV, COOKIE_TVREQ = "f1_disk", "f1_tv", "f1_dev", "f1_tvreq"
seclog = logging.getLogger("security")
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


# paths that stay reachable without a /tv or /disk session when [security] protect_dashboard is on:
# the auth pages themselves, the phone remote (own token), the PC launcher's machine endpoints (token /
# local only), static code (no data) - everything else needs a session
GATE_OPEN = ("/tv", "/api/tv/", "/disk", "/api/disk/", "/disk-static/", "/tv-static/", "/static/", "/remote",
             "/api/remote/", "/api/sync/voyo", "/api/voyo/player/", "/api/health", "/f1tv/", "/ws")


class OriginGuard:
    """CSRF for every endpoint: a browser always sends Origin with a cross-site POST / PUT / DELETE - if it
    is not this server, refuse. Tools without an Origin (launcher, clock bridge, curl) are unaffected."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") in ("POST", "PUT", "PATCH", "DELETE"):
            conn = HTTPConnection(scope)
            origin = conn.headers.get("origin")
            if origin and origin != "null" and urlparse(origin).netloc.lower() != (conn.headers.get("host") or "").lower():
                logging.getLogger("security").warning("Cross-site %s %s refused (Origin %s)", scope["method"],
                                                      scope.get("path"), origin[:80])
                return await JSONResponse({"ok": False, "error": "cross-site request refused"},
                                          status_code=403)(scope, receive, send)
            if origin == "null":
                return await JSONResponse({"ok": False, "error": "cross-site request refused"},
                                          status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)


class DashboardGate:
    """ASGI middleware - server-side, for HTTP and WebSocket alike (the /ws endpoint checks itself)."""

    def __init__(self, app, check) -> None:
        self.app, self.check = app, check

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        capture_put = path.startswith("/api/voyo/recordings/") and "/capture/" in path
        if path == "/" or not (path.startswith(GATE_OPEN) or capture_put):
            if not self.check(HTTPConnection(scope)):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4401})
                    return
                resp = (Response(status_code=303, headers={"Location": "/tv?next=/"}) if path == "/" else
                        JSONResponse({"ok": False, "error": "not authorized", "auth": "tv"}, status_code=401))
                return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


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
    # server-side authentication: /disk password, trusted /remote device, approved /tv browsers
    sec_cfg = cfg.get("security") or {}
    security = Security(DATA_DIR / "auth", sec_cfg)
    security.ensure_setup_code()
    rl_login = RateLimiter(int(sec_cfg.get("login_max_failures", 5)), 300, float(sec_cfg.get("lockout_minutes", 5)) * 60)
    rl_login_all = RateLimiter(30, 600, 60)            # all addresses together (spread-out guessing)
    rl_setup = RateLimiter(5, 900, 900)
    rl_tvreq = RateLimiter(6, 60)
    rl_decide = RateLimiter(30, 60)
    # public gateway (Tailscale Funnel): no visitor may flood the device list, the phone or the disk
    rl_pub_tvreq = RateLimiter(30, 600)                 # TV challenges from the internet, all visitors together
    rl_pub_newdev = RateLimiter(5, 3600)                # new /remote identities per visitor and hour ...
    rl_pub_newdev_all = RateLimiter(40, 3600)           # ... and for all visitors together
    rl_pub_rename = RateLimiter(5, 600)
    public_limited: dict = {}                           # untrusted public /remote sockets -> device id (or None)
    # [security] protect_dashboard = true: the plain dashboard ("/", its APIs and WebSocket) also needs an
    # approved /tv session (or a /disk session) - off by default, the PC / WD TV clients use it as before
    protect_dashboard = bool(sec_cfg.get("protect_dashboard", False))
    live_dir = DATA_DIR / "live"                      # the server VOYO player's live HLS (tools/voyo_capture.py)
    viewers: dict = {}
    voyo_account = VoyoAccount(DATA_DIR / "auth" / "voyo_credentials.json", DATA_DIR / "voyo_server_player.json",
                               str(((cfg.get("voyo") or {}).get("server_player") or {}).get("stream_url") or ""))
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
                with contextlib.suppress(Exception):
                    security._expire_requests()        # expired /tv requests disappear from the phone too
                    push_remote_state()
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
        """The phone remote. Every browser gets a device identity (random secret in an HttpOnly cookie) -
        that alone grants nothing: only the device chosen in /disk may approve /tv."""
        resp = FileResponse(DASHBOARD_DIR / "remote.html", headers={"Cache-Control": "no-cache"})
        if security.device(request.cookies.get(COOKIE_DEV)) is None:
            if _public(request):
                # from the internet: a new identity is free to get, but not in bulk (the list and the disk)
                ip = _ip(request)
                if rl_pub_newdev.blocked(ip) or rl_pub_newdev_all.blocked("all"):
                    seclog.warning("Public /remote: no new device identity for %s (rate limit)", ip)
                    return resp
                rl_pub_newdev.hit(ip)
                rl_pub_newdev_all.hit("all")
            tok, _did, _rec = security.new_device(request.headers.get("user-agent", ""))
            _set_cookie(resp, request, COOKIE_DEV, tok, 400 * 86400, "lax")
        return resp

    async def health(request: Request) -> Response:
        st = mode.state()
        out = {"ok": True, "mode": rt.source.mode if rt.source else None,
               "selected_mode": st["selected_mode"], "detected_mode": st["detected_mode"],
               "effective_mode": st["effective_mode"],
               "status": rt.engine._status if rt.engine else {}, "clients": len(hub.clients)}
        if _ip(request) in LOOPBACK:
            # for server/update-f1dash.sh on this machine: is a recording running (do not restart now)?
            with contextlib.suppress(Exception):
                rs = admin.current_state(stream_rec, player_rec, player_rec is not None, player_hb["data"], _hb_age())
                out["recorder"] = {"state": rs.get("state"),
                                   "busy": rs.get("state") in ("RECORDING", "OPENING", "WATCHING (PC)")}
        return JSONResponse(out)

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
        """URLs of the phone remote (LAN / Tailscale) for the QR code on the dashboard. The remote token is
        only put into them for this server itself, a browser with an approved /tv page or a /disk login."""
        info = phone_remote()
        if remote.token and _ip(request) not in LOOPBACK and dash_access(request) is None:
            strip = lambda u: u.split("?")[0] if isinstance(u, str) else u      # noqa: E731
            info = {**info, "url": strip(info.get("url")), "lan": [strip(u) for u in info.get("lan") or []],
                    "tailscale": [strip(u) for u in info.get("tailscale") or []], "token_hidden": True}
        return JSONResponse(info, headers={"Cache-Control": "no-store"})

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
        hint = {k: str(v)[:120] for k, v in hint.items()
                if k in ("meeting", "session_name", "start", "end") and v not in (None, "")}
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
            if isinstance(body.get("login"), dict):
                voyo_account.last_login = body["login"]
        return JSONResponse({"ok": True, "commands": voyo_account.take_commands()})

    async def api_disk_voyo(request: Request) -> Response:
        """GET: the VOYO account (never the password). POST {email, password, stream_url}: save (token)."""
        if request.method == "GET":
            _s, deny = require_disk(request)
            return deny or JSONResponse(voyo_account.public())
        _s, deny = require_disk(request, write=True)
        if deny:
            return deny
        try:
            body = json.loads((await request.body())[:8192] or b"{}")
            if not isinstance(body, dict):
                raise ValueError("object expected")
            changed = voyo_account.save(body.get("email"), body.get("password") or None, body.get("stream_url"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": f"not saved: {exc}"}, status_code=500)
        logging.getLogger("disk").info("VOYO account changed on the /disk page: %s", ", ".join(changed) or "nothing")
        return JSONResponse({"ok": True, "changed": changed, "account": voyo_account.public()})

    async def api_disk_voyo_action(request: Request) -> Response:
        action = request.path_params["action"]
        _s, deny = require_disk(request, write=True, recent=action == "forget")
        if deny:
            return deny
        if action == "forget":
            voyo_account.forget()
            logging.getLogger("disk").info("VOYO e-mail / password deleted on the /disk page")
            return JSONResponse({"ok": True, "account": voyo_account.public()})
        if action == "login":
            if player_rec is None:
                return JSONResponse({"ok": False, "error": "the server VOYO player is not enabled"}, status_code=409)
            if _hb_age() is None or _hb_age() > 90:
                return JSONResponse({"ok": False, "error": "the server VOYO player is not running"}, status_code=409)
            if "login" not in voyo_account.commands:
                voyo_account.commands.append("login")
            logging.getLogger("disk").info("LOGIN NOW requested on the /disk page")
            return JSONResponse({"ok": True, "message": "the player signs in within ~15 s - see the log"})
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)

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
        """The dashboard only with a /disk session - else the login / first-setup page."""
        page = "index.html" if _disk(request)[1] is not None else "login.html"
        return FileResponse(DISK_PAGE_DIR / page, headers={"Cache-Control": "no-store"})

    async def api_disk_status(request: Request) -> Response:
        _s, deny = require_disk(request)
        if deny:
            return deny
        sizes = rec_sizes.get(stream_rec.root if stream_rec.ok else None)
        rc = stream_rec.rc
        return JSONResponse({
            "now": time.time(),
            "state": admin.current_state(stream_rec, player_rec, player_rec is not None, player_hb["data"], _hb_age()),
            "disk": admin.disk_info(stream_rec, sizes),
            "player": {"enabled": player_rec is not None, "heartbeat": player_hb["data"],
                       "heartbeat_age_s": None if _hb_age() is None else round(_hb_age(), 1)},
            "retention": admin.retention_table(rc, rec_settings.values),
            "voyo": voyo_account.public(),
            "live": live_info(),
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
        _s, deny = require_disk(request)
        if deny:
            return deny
        try:
            limit = min(2000, int(request.query_params.get("limit") or 400))
        except ValueError:
            limit = 400
        return JSONResponse({"entries": activity.entries(limit, request.query_params.get("level") or "INFO"),
                             "keep_hours": 48})

    async def api_disk_recordings(request: Request) -> Response:
        _s, deny = require_disk(request)
        if deny:
            return deny
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
        _s, deny = require_disk(request, write=True)
        if deny:
            return deny
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
        _s, deny = require_disk(request, write=True, recent=True)       # destructive: password in the last minutes
        if deny:
            return deny
        try:
            msg = admin.delete_package(stream_rec, (player_rec,), request.path_params["iid"],
                                       request.query_params.get("what") or "video")
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc).strip("'")}, status_code=404)
        except (PermissionError, ValueError, OSError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        rec_sizes.get(stream_rec.root if stream_rec.ok else None, force=True)
        return JSONResponse({"ok": True, "message": msg})

    # ---------------------------------------------------------------- the /tv page (live stream + dashboard)
    def live_info() -> dict:
        idx = live_dir / "index.m3u8"
        now_m = time.monotonic()
        for ip in [k for k, v in viewers.items() if now_m - v > VIEWER_S]:
            viewers.pop(ip, None)
        out = {"on_air": False, "segments": 0, "lag_s": None, "viewers": len(viewers), "age_s": None}
        try:
            st = idx.stat()
            text = idx.read_text(encoding="utf-8")
        except OSError:
            return out
        age = time.time() - st.st_mtime
        segs = text.count("#EXTINF")
        out.update(age_s=round(age, 1), segments=segs, on_air=age < 10 and segs > 0)
        pdts = re.findall(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", text)
        if pdts:
            with contextlib.suppress(ValueError):
                from datetime import datetime
                last = datetime.fromisoformat(pdts[-1].replace("Z", "+00:00").replace("+0000", "+00:00"))
                out["lag_s"] = round(time.time() - last.timestamp(), 1)
        return out

    async def tv_page(request: Request) -> Response:
        """Every load of /tv starts from zero: whatever this browser was authorized for before (its TV page
        session, its open challenge) ends here, and the page that is served holds no TV data - it asks the
        trusted phone for a fresh approval first (server/tv/tv.js). No cookie, device id, IP or /disk login
        lets a load of /tv skip that."""
        tok = request.cookies.get(COOKIE_TV)
        if tok and security.revoke("tv", token=tok):
            seclog.info("/tv loaded again in a browser: its previous TV authorization ended")
        if security.cancel_request(request.cookies.get(COOKIE_TVREQ)):
            push_remote_state()
        resp = FileResponse(TV_PAGE_DIR / "index.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        _clear_cookie(resp, request, COOKIE_TV)
        _clear_cookie(resp, request, COOKIE_TVREQ)
        return resp

    def tv_auth(request, allow_query: bool = False) -> Optional[dict]:
        """An approved /tv page: its HttpOnly session cookie AND its page secret (X-F1-TV-Page header; for the
        browser's own HLS player, which cannot send headers, ?p= on the live files)."""
        page = request.headers.get("x-f1-tv-page") or (request.query_params.get("p") if allow_query else None)
        return security.tv_page(request.cookies.get(COOKIE_TV), page)

    def _tv_denied() -> Response:
        return JSONResponse({"ok": False, "error": "not authorized", "auth": "tv"}, status_code=401,
                            headers={"Cache-Control": "no-store"})

    async def tv_live_file(request: Request) -> Response:
        """The live HLS of the server VOYO player (only while it records) - an approved /tv page only."""
        if tv_auth(request, allow_query=True) is None:
            return _tv_denied()
        name = request.path_params["name"]
        path = live_dir / name
        if not LIVE_NAME.match(name) or not path.is_file():
            return JSONResponse({"ok": False, "error": "off air"}, status_code=404)
        if name == "index.m3u8":
            viewers[request.client.host if request.client else "?"] = time.monotonic()
            nohdr = {"Cache-Control": "no-cache, no-store"}
            q = request.query_params.get("p")
            if q and not request.headers.get("x-f1-tv-page"):
                # native HLS (Safari): the pieces must carry the page secret too
                text = await run_in_threadpool(path.read_text, errors="replace")
                suffix = "?p=" + quote(q, safe="")
                text = "\n".join(ln + suffix if ln.strip() and not ln.startswith("#") else ln
                                 for ln in text.splitlines()) + "\n"
                return Response(text, media_type="application/vnd.apple.mpegurl", headers=nohdr)
            return FileResponse(path, media_type="application/vnd.apple.mpegurl", headers=nohdr)
        return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=60"})

    async def api_tv_status(request: Request) -> Response:
        if tv_auth(request) is None:
            return _tv_denied()
        cur = player_rec.cur if player_rec is not None else None
        sess = (cur or {}).get("session") or {}
        hb = player_hb["data"] or {}
        return JSONResponse({
            "now": time.time(), "live": live_info(),
            "state": admin.current_state(stream_rec, player_rec, player_rec is not None, player_hb["data"], _hb_age()),
            "session": {"meeting": sess.get("meeting"), "session_name": sess.get("session_name")} if cur else None,
            "next": hb.get("next") if _hb_age() is not None and _hb_age() < 90 else None})

    # ---------------------------------------------------------------- authentication helpers
    def _ip(request) -> str:
        return request.client.host if request.client else "?"

    def _set_cookie(resp: Response, request, name: str, value: str, max_age: Optional[float],
                    samesite: str = "strict") -> None:
        """max_age None: a browser-session cookie (gone when the browser closes)."""
        resp.set_cookie(name, value, max_age=None if max_age is None else int(max_age), path="/", httponly=True,
                        samesite=samesite, secure=request.url.scheme in ("https", "wss"))

    def _clear_cookie(resp: Response, request, name: str) -> None:
        if name in request.cookies:
            _set_cookie(resp, request, name, "", 0)

    def _same_origin(request) -> bool:
        """Browsers always send Origin on cross-site POSTs and WebSockets: it must be this server."""
        origin = request.headers.get("origin")
        if not origin:
            return True
        return urlparse(origin).netloc.lower() == (request.headers.get("host") or "").lower()

    def _disk(request) -> tuple[Optional[str], Optional[dict]]:
        tok = request.cookies.get(COOKIE_DISK)
        return tok, security.session("disk", tok)

    def require_disk(request, write: bool = False, recent: bool = False) -> tuple[Optional[dict], Optional[Response]]:
        """The /disk session (server-side). write: same origin + the session's CSRF token in X-F1-CSRF.
        recent: password entered in the last reauth_minutes (destructive actions)."""
        _tok, s = _disk(request)
        if s is None:
            return None, JSONResponse({"ok": False, "error": "login required", "auth": "disk"}, status_code=401)
        if write:
            if not _same_origin(request):
                seclog.warning("/disk request from another site refused (%s)", request.url.path)
                return None, JSONResponse({"ok": False, "error": "cross-site request refused"}, status_code=403)
            if not hmac.compare_digest(str(request.headers.get("x-f1-csrf") or ""), str(s.get("csrf") or "")):
                return None, JSONResponse({"ok": False, "error": "csrf token missing or wrong"}, status_code=403)
        if recent and not security.recent(s):
            return None, JSONResponse({"ok": False, "error": "reauth_required"}, status_code=403)
        return s, None

    def _public(conn) -> bool:
        """Came through the public gateway (Tailscale Funnel) - server/public_gateway.py sets it, a client can't."""
        return bool(conn.scope.get("f1_public"))

    def public_view(scope_or_conn) -> bool:
        """From the internet, the dashboard's data is for an approved /tv page (its session cookie - the iframe
        cannot send the page header) or the trusted phone. Not a /disk login (/disk is not public)."""
        conn = scope_or_conn if hasattr(scope_or_conn, "cookies") else HTTPConnection(scope_or_conn)
        if security.session("tv", conn.cookies.get(COOKIE_TV)) is not None:
            return True
        dev = security.device(conn.cookies.get(COOKIE_DEV))
        return dev is not None and security.trusted(dev[0])

    def dash_access(conn) -> Optional[dict]:
        """The plain dashboard when [security] protect_dashboard is on (and the remote token in the QR code):
        the cookie of a currently approved /tv page (its iframe cannot send the page header) or a /disk
        login. Never used for /tv itself - the TV endpoints need tv_auth()."""
        s = security.session("tv", conn.cookies.get(COOKIE_TV))
        return s if s is not None else _disk(conn)[1]

    async def _json(request, limit: int = 4096) -> dict:
        try:
            body = json.loads((await request.body())[:limit] or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _too_many(seconds: float) -> Response:
        return JSONResponse({"ok": False, "error": f"too many attempts - wait {int(seconds) + 1} s"}, status_code=429,
                            headers={"Retry-After": str(int(seconds) + 1)})

    # ---------------------------------------------------------------- /disk: password, sessions
    async def api_disk_auth_state(request: Request) -> Response:
        _tok, s = _disk(request)
        return JSONResponse({"password_set": security.password_set, "authenticated": s is not None,
                             "csrf": s["csrf"] if s else None,
                             "recent": bool(s and security.recent(s)), "https": request.url.scheme == "https",
                             "setup_code_file": None if security.password_set else str(security.code_path)})

    async def api_disk_auth_setup(request: Request) -> Response:
        """First password - only with the one-time setup code from the server's disk-setup-code file."""
        if not _same_origin(request):
            return JSONResponse({"ok": False, "error": "cross-site request refused"}, status_code=403)
        if security.password_set:
            return JSONResponse({"ok": False, "error": "a password is already set"}, status_code=409)
        wait = rl_setup.blocked(_ip(request))
        if wait:
            return _too_many(wait)
        body = await _json(request)
        rl_setup.hit(_ip(request))
        if not security.setup_code_ok(str(body.get("code") or "")):
            seclog.warning("/disk password setup with a wrong setup code from %s", _ip(request))
            return JSONResponse({"ok": False, "error": "wrong setup code"}, status_code=403)
        prob = secmod.password_problem(body.get("password"), body.get("confirm"))
        if prob:
            return JSONResponse({"ok": False, "error": prob}, status_code=400)
        await run_in_threadpool(security.set_password, body["password"])
        seclog.warning("/disk password set (first setup) from %s", _ip(request))
        tok, _rec = security.new_session("disk", secmod.ua_summary(request.headers.get("user-agent", "")))
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, request, COOKIE_DISK, tok, security.disk_ttl)
        return resp

    async def api_disk_auth_login(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse({"ok": False, "error": "cross-site request refused"}, status_code=403)
        if not security.password_set:
            return JSONResponse({"ok": False, "error": "no password set yet"}, status_code=409)
        ip = _ip(request)
        wait = max(rl_login.blocked(ip), rl_login_all.blocked("all"))
        if wait:
            return _too_many(wait)
        body = await _json(request)
        ok = await run_in_threadpool(security.check_password, str(body.get("password") or "")[:secmod.MAX_PASSWORD])
        if not ok:
            rl_login.hit(ip)
            rl_login_all.hit("all")
            seclog.warning("/disk login FAILED from %s", ip)
            return JSONResponse({"ok": False, "error": "wrong password"}, status_code=401)
        rl_login.reset(ip)
        old = request.cookies.get(COOKIE_DISK)
        if old:
            security.revoke("disk", token=old)              # never keep a pre-login session id (fixation)
        tok, rec = security.new_session("disk", secmod.ua_summary(request.headers.get("user-agent", "")))
        seclog.info("/disk login from %s (session %s)", ip, rec["id"])
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, request, COOKIE_DISK, tok, security.disk_ttl)
        return resp

    async def api_disk_auth_reauth(request: Request) -> Response:
        s, deny = require_disk(request, write=True)
        if deny:
            return deny
        ip = _ip(request)
        wait = max(rl_login.blocked(ip), rl_login_all.blocked("all"))
        if wait:
            return _too_many(wait)
        body = await _json(request)
        if not await run_in_threadpool(security.check_password, str(body.get("password") or "")[:secmod.MAX_PASSWORD]):
            rl_login.hit(ip)
            rl_login_all.hit("all")
            seclog.warning("/disk re-authentication FAILED (session %s, %s)", s["id"], ip)
            return JSONResponse({"ok": False, "error": "wrong password"}, status_code=401)
        security.reauth(request.cookies.get(COOKIE_DISK))
        return JSONResponse({"ok": True})

    async def api_disk_auth_logout(request: Request) -> Response:
        s, deny = require_disk(request, write=True)
        if deny:
            return deny
        security.revoke("disk", token=request.cookies.get(COOKIE_DISK))
        seclog.info("/disk logout (session %s)", s["id"])
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_DISK, path="/")
        return resp

    async def api_disk_auth_password(request: Request) -> Response:
        s, deny = require_disk(request, write=True, recent=True)
        if deny:
            return deny
        body = await _json(request)
        if not await run_in_threadpool(security.check_password, str(body.get("current") or "")[:secmod.MAX_PASSWORD]):
            rl_login.hit(_ip(request))
            return JSONResponse({"ok": False, "error": "the current password is wrong"}, status_code=401)
        prob = secmod.password_problem(body.get("password"), body.get("confirm"))
        if prob:
            return JSONResponse({"ok": False, "error": prob}, status_code=400)
        await run_in_threadpool(security.set_password, body["password"])          # ends every disk session
        seclog.warning("/disk password changed - all /disk sessions ended")
        tok, _rec = security.new_session("disk", secmod.ua_summary(request.headers.get("user-agent", "")))
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, request, COOKIE_DISK, tok, security.disk_ttl)
        return resp

    # ---------------------------------------------------------------- /disk: devices, TV sessions, requests
    def push_remote_state() -> None:
        """Each /remote socket: its own device (name, code, trusted) and - only the trusted one - the
        pending /tv requests."""
        pend = security.pending()
        for ws_l, did in list(public_limited.items()):      # untrusted public /remote pages
            if did and security.trusted(did):
                asyncio.ensure_future(_ws_send_close(ws_l, {"type": "reload"}))   # trusted now: start again
            elif did:
                d = security.data["devices"].get(did) or {}
                asyncio.ensure_future(_ws_send(ws_l, {"type": "device", "name": d.get("name"), "code": d.get("code"),
                                                      "trusted": False}))
        for c in list(hub.clients):
            if c.kind != "remote" or not c.device_id:
                continue
            if c.public and not security.trusted(c.device_id):   # trust taken away: no public control any more
                asyncio.ensure_future(_ws_send_close(c.ws, {"type": "reload"}))
                continue
            d = security.data["devices"].get(c.device_id) or {}
            hub._send(c, json.dumps({"type": "device", "name": d.get("name"), "code": d.get("code"),
                                     "trusted": security.trusted(c.device_id)}))
            if security.trusted(c.device_id):
                hub._send(c, json.dumps({"type": "tv_requests", "requests": pend}))

    async def _ws_send(ws, msg: dict) -> None:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps(msg))

    async def _ws_send_close(ws, msg: dict) -> None:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps(msg))
            await ws.close(code=4401)

    async def public_remote_limited(ws: WebSocket, dev) -> None:
        """A /remote page from the internet that is NOT the trusted phone: it learns its own name and code
        (so the owner can choose it in /disk at home) and nothing else - no state, no control, no approvals."""
        did = dev[0] if dev else None
        await ws.accept()
        public_limited[ws] = did
        if did:
            security.device_connected(did, True)
            d = dev[1]
            await _ws_send(ws, {"type": "device", "name": d.get("name"), "code": d.get("code"), "trusted": False})
        await _ws_send(ws, {"type": "locked", "reason": "untrusted" if did else "no_identity"})
        try:
            while True:
                text = await ws.receive_text()
                if len(text) > 512 or not did:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "device_name" and not rl_pub_rename.blocked(did):
                    rl_pub_rename.hit(did)
                    if security.rename_device(did, str(msg.get("name") or "")):
                        push_remote_state()
        except WebSocketDisconnect:
            pass
        finally:
            public_limited.pop(ws, None)
            if did:
                security.device_connected(did, False)

    async def api_disk_security(request: Request) -> Response:
        _s, deny = require_disk(request)
        if deny:
            return deny
        return JSONResponse({"devices": security.devices_public(), "trusted_device": security.data.get("trusted_device"),
                             "tv_sessions": security.tv_sessions_public(), "requests": security.pending(),
                             "disk_sessions": len(security.data["disk_sessions"])})

    async def api_disk_security_action(request: Request) -> Response:
        action = request.path_params["action"]
        destructive = action in ("trust", "untrust", "forget", "revoke_all_tv", "logout_others")
        s, deny = require_disk(request, write=True, recent=destructive)
        if deny:
            return deny
        body = await _json(request)
        try:
            if action == "trust":
                security.set_trusted(str(body.get("device") or ""))
            elif action == "untrust":
                security.set_trusted(None)
            elif action == "forget":
                if not security.forget_device(str(body.get("device") or "")):
                    raise KeyError("unknown device")
            elif action == "rename":
                if not security.rename_device(str(body.get("device") or ""), str(body.get("name") or "")):
                    raise KeyError("unknown device or empty name")
            elif action == "revoke_tv":
                if not security.revoke("tv", sid=str(body.get("session") or "")):
                    raise KeyError("unknown session")
            elif action == "revoke_all_tv":
                security.revoke_all("tv")
            elif action == "logout_others":
                security.revoke_all("disk", except_token=request.cookies.get(COOKIE_DISK))
            elif action == "decide":
                # /tv is approved only on the trusted phone (/remote), never from /disk
                return JSONResponse({"ok": False, "error": "approve /tv on the trusted phone (/remote)"}, status_code=403)
            else:
                return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc).strip("'")}, status_code=404)
        except (PermissionError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        push_remote_state()
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------------- /tv: approval by the trusted remote device
    async def api_tv_auth_request(request: Request) -> Response:
        """A loaded /tv page asks for access: a new server-side challenge (random id, expires). The browser
        gets a random secret in an HttpOnly cookie, the page a second one it keeps only in memory; the
        trusted /remote device is notified. Any earlier authorization of this browser ends."""
        if not _same_origin(request):
            return JSONResponse({"ok": False, "error": "cross-site request refused"}, status_code=403)
        ip = _ip(request)
        wait = rl_tvreq.blocked(ip) or (_public(request) and rl_pub_tvreq.blocked("all"))
        if wait:
            return _too_many(wait)
        rl_tvreq.hit(ip)
        if _public(request):
            rl_pub_tvreq.hit("all")
        tok = request.cookies.get(COOKIE_TV)
        if tok:
            security.revoke("tv", token=tok)
        dev = security.device(request.cookies.get(COOKIE_DEV))
        try:
            browser, page, req = security.create_request(request.headers.get("user-agent", ""), dev[0] if dev else None,
                                                         supersede=request.cookies.get(COOKIE_TVREQ))
        except OverflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
        push_remote_state()
        trusted = security.data.get("trusted_device")
        resp = JSONResponse({"ok": True, "code": req["code"], "challenge": page,
                             "expires_in": round(req["expires"] - time.time()),
                             "approver_set": bool(trusted), "approver_online": bool(trusted and trusted in security.connected)},
                            headers={"Cache-Control": "no-store"})
        _set_cookie(resp, request, COOKIE_TVREQ, browser, security.request_ttl + 30, "strict")
        _clear_cookie(resp, request, COOKIE_TV)
        return resp

    async def api_tv_auth_status(request: Request) -> Response:
        """The page that created the challenge asks for its state (its cookie + X-F1-TV-Challenge). Once
        approved, the first such call - and only it - turns the approval into this page's TV session."""
        if not _same_origin(request):
            return JSONResponse({"ok": False, "error": "cross-site request refused"}, status_code=403)
        nostore = {"Cache-Control": "no-store"}
        r = security.request_for(request.cookies.get(COOKIE_TVREQ), request.headers.get("x-f1-tv-challenge"))
        if r is None:
            return JSONResponse({"status": "none"}, headers=nostore)
        if r["status"] == "approved":
            got = security.consume(r)
            if got is None:
                return JSONResponse({"status": r["status"]}, headers=nostore)
            cookie, page, rec = got
            resp = JSONResponse({"status": "authenticated", "page": page, "expires_in": round(rec["expires"] - time.time())},
                                headers=nostore)
            _set_cookie(resp, request, COOKIE_TV, cookie, None, "strict")      # browser-session cookie
            _clear_cookie(resp, request, COOKIE_TVREQ)
            return resp
        return JSONResponse({"status": r["status"], "code": r["code"], "expires_in": max(0, round(r["expires"] - time.time()))},
                            headers=nostore)

    async def api_tv_logout(request: Request) -> Response:
        """The TV page ends its authorization (LOG OUT button, or the page being left - sendBeacon with the
        page secret as the body). Only the page itself can: cookie + its page secret."""
        if not _same_origin(request):
            return JSONResponse({"ok": False, "error": "cross-site request refused"}, status_code=403)
        page = request.headers.get("x-f1-tv-page")
        if not page:
            if int(request.headers.get("content-length") or 0) > 1024:
                return _tv_denied()
            page = (await request.body())[:200].decode("ascii", "replace").strip()
        tok = request.cookies.get(COOKIE_TV)
        if security.tv_page(tok, page) is None:
            return _tv_denied()
        security.revoke("tv", token=tok)
        resp = JSONResponse({"ok": True})
        _clear_cookie(resp, request, COOKIE_TV)
        return resp

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
        _s, deny = require_disk(request)
        if deny:
            return deny
        return JSONResponse({"status": stream_rec.status(),
                             "server_player": player_rec.status() if player_rec is not None else None,
                             "recordings": stream_rec.list(also=(player_rec,))})

    async def api_voyo_recording(request: Request) -> Response:
        """One package: manifest + meta + anchors + the AUTO SYNC calibration re-run from its anchors
        (?full=1 also returns the timeline and observations)."""
        _s, deny = require_disk(request)
        if deny:
            return deny
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
        """A file of a package (timeline.jsonl, ..., capture/<segment>) - /disk session."""
        _s, deny = require_disk(request)
        if deny:
            return deny
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
        if not _same_origin(ws):                       # cross-site WebSocket hijacking: another site's page
            seclog.warning("WebSocket from another site refused (Origin %s)", ws.headers.get("origin", "")[:80])
            await ws.close(code=4403)
            return
        public = _public(ws)
        dev = security.device(ws.cookies.get(COOKIE_DEV)) if kind == "remote" else None
        if public:
            # from the internet (Tailscale Funnel): no remote token at all (never in a public URL) - /remote
            # control and /tv approvals only for the trusted phone, the dashboard only for an approved /tv page
            if kind == "remote" and not (dev is not None and security.trusted(dev[0])):
                await public_remote_limited(ws, dev)
                return
            if kind == "dashboard" and not public_view(ws):
                await ws.close(code=4401)
                return
        else:
            if kind == "remote" and not remote.check_token(ws.query_params.get("token")):
                await ws.close(code=4401)
                return
            if kind == "dashboard" and protect_dashboard and dash_access(ws) is None:
                await ws.close(code=4401)
                return
        await ws.accept()
        addr = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
        try:
            client = await hub.add(ws, addr, kind)
        except Exception:  # noqa: BLE001 - log it once in full, then keep the dashboard connected
            log.exception("Dashboard connection setup failed for %s", addr)
            raise
        client.public = public
        if dev is not None:
            client.device_id = dev[0]
            security.device_connected(dev[0], True)
            push_remote_state()
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
                if public and client.kind != "remote":
                    continue                             # a public dashboard socket only listens
                if public and not security.trusted(client.device_id):
                    break                                # trust was taken away meanwhile
                if kind == "device_name" and client.device_id:
                    # a remote device names itself (that is all it can change about itself)
                    if security.rename_device(client.device_id, str(msg.get("name") or "")):
                        push_remote_state()
                    continue
                if kind == "tv_decide":
                    # /tv approval: the server checks that THIS socket's device is the trusted approver
                    if not client.device_id or rl_decide.blocked(client.device_id):
                        continue
                    rl_decide.hit(client.device_id)
                    try:
                        res = security.decide(str(msg.get("request") or ""), bool(msg.get("approve")),
                                              by_device=client.device_id)
                        hub._send(client, json.dumps({"type": "tv_decided", "ok": True, "status": res}))
                    except (KeyError, PermissionError, ValueError) as exc:
                        hub._send(client, json.dumps({"type": "tv_decided", "ok": False, "error": str(exc).strip("'")}))
                    push_remote_state()
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
            if getattr(client, "device_id", None):
                security.device_connected(client.device_id, False)

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
        Route("/tv", tv_page),
        Route("/tv/", tv_page),
        Route("/tv/live/{name}", tv_live_file),
        Route("/api/tv/status", api_tv_status),
        Route("/api/tv/auth/request", api_tv_auth_request, methods=["POST"]),
        Route("/api/tv/auth/status", api_tv_auth_status, methods=["POST"]),
        Route("/api/tv/logout", api_tv_logout, methods=["POST"]),
        Route("/api/disk/auth/state", api_disk_auth_state),
        Route("/api/disk/auth/setup", api_disk_auth_setup, methods=["POST"]),
        Route("/api/disk/auth/login", api_disk_auth_login, methods=["POST"]),
        Route("/api/disk/auth/reauth", api_disk_auth_reauth, methods=["POST"]),
        Route("/api/disk/auth/logout", api_disk_auth_logout, methods=["POST"]),
        Route("/api/disk/auth/password", api_disk_auth_password, methods=["POST"]),
        Route("/api/disk/security", api_disk_security),
        Route("/api/disk/security/{action}", api_disk_security_action, methods=["POST"]),
        Route("/disk", disk_page),
        Route("/disk/", disk_page),
        Route("/api/disk/status", api_disk_status),
        Route("/api/disk/log", api_disk_log),
        Route("/api/disk/recordings", api_disk_recordings),
        Route("/api/disk/settings", api_disk_settings, methods=["POST"]),
        Route("/api/disk/voyo", api_disk_voyo, methods=["GET", "POST"]),
        Route("/api/disk/voyo/{action}", api_disk_voyo_action, methods=["POST"]),
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
        Mount("/tv-static", StaticFiles(directory=str(TV_PAGE_DIR), check_dir=False), name="tv-static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.security = security                     # tests / diagnostics (never sent to a client)
    app.add_middleware(OriginGuard)
    if protect_dashboard:
        app.add_middleware(DashboardGate, check=lambda conn: dash_access(conn) is not None)
        seclog.warning("protect_dashboard is ON: the dashboard needs an approved /tv or a /disk session")
    app.state.runtime = rt
    app.state.mode = mode
    # the public gateway (Tailscale Funnel) - only with a valid [public] configuration (fail closed)
    app.state.public_app = None
    pub_cfg = cfg.get("public") or {}
    if pub_cfg.get("enabled"):
        problem = public_config_problem(pub_cfg, port, int(cfg["server"].get("https_port") or 0))
        if problem:
            seclog.error("PUBLIC GATEWAY OFF: %s - nothing is reachable from the internet", problem)
        else:
            app.state.public_app = PublicGateway(app, str(pub_cfg["hostname"]), public_view,
                                                 DASHBOARD_DIR / "remote.html")
            app.state.public_port = int(pub_cfg["port"])
            seclog.warning("Public gateway for https://%s (Tailscale Funnel -> 127.0.0.1:%d): only /tv and /remote "
                           "and what they need", pub_cfg["hostname"], int(pub_cfg["port"]))
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
