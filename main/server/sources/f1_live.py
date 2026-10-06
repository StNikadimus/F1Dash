"""Official F1 live timing client (livetiming.formula1.com).

Two transports are implemented:

* SignalR Core  - ``wss://livetiming.formula1.com/signalrcore`` (what the
  official apps, FastF1 and boxbox use). Works anonymously - exactly the
  handshake boxbox uses (negotiate -> connectionToken -> ?id=). An F1 TV
  subscription token can optionally be sent as Bearer token; F1 only streams
  ``Position.z`` / ``CarData.z`` to entitled connections (see README).
* Legacy SignalR 1.5 - ``wss://livetiming.formula1.com/signalr``. Anonymous.
  Reported to answer 401 since F1 moved to SignalR Core; kept as fallback.

Both deliver identical topic payloads. The client reconnects with exponential
back-off, watches for silence, and re-subscribes after every reconnect (the
subscribe result is a complete snapshot, so state never goes stale).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..f1tv_auth import AuthManager, parse_token, token_info
from ..telemetry import parse_utc
from .archive_follow import ArchiveFollower
from .base import Sink, Source

log = logging.getLogger("f1_live")

BASE = "livetiming.formula1.com"
CORE_NEGOTIATE = f"https://{BASE}/signalrcore/negotiate"
CORE_WS = f"wss://{BASE}/signalrcore"
LEGACY_NEGOTIATE = f"https://{BASE}/signalr/negotiate"
LEGACY_CONNECT = f"wss://{BASE}/signalr/connect"
LEGACY_START = f"https://{BASE}/signalr/start"
INDEX_URL = f"https://{BASE}/static/{{year}}/Index.json"
RS = "\x1e"                         # SignalR Core record separator
UA_CORE = "f1-tv-dashboard/1.0"
UA_LEGACY = "BestHTTP"
ORIGIN = "https://www.formula1.com"
# second line-crossing signal of server/laps.py (the F1 archive / recordings carry it too)
REQUIRED_TOPICS = ("LapSeries",)
# what the dashboard needs at least - used alone if F1 refuses a subscription to the full list
CORE_TOPICS = [
    "Heartbeat", "SessionInfo", "SessionStatus", "SessionData", "ExtrapolatedClock",
    "LapCount", "TrackStatus", "DriverList", "TimingData", "TimingDataF1",
    "TimingAppData", "TimingStats", "RaceControlMessages", "WeatherData",
    "TeamRadio", "TopThree", "PitLaneTimeCollection", "CurrentTyres",
    "LapSeries", "Position.z", "CarData.z",
]


class FeedError(Exception):
    pass


class AuthRejected(FeedError):
    """F1 refused the F1 TV token (HTTP 401 / 403) - sign in again, use the public feed meanwhile."""


class SubscribeFailed(FeedError):
    pass


def token_expiry(token: str) -> Optional[datetime]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except Exception:  # noqa: BLE001
        return None


class F1LiveSource(Source):
    mode = "live"

    def __init__(self, cfg: dict[str, Any], recorder=None, auth: Optional[AuthManager] = None) -> None:
        self.cfg = cfg
        self.topics: list[str] = list(cfg.get("topics") or [])
        for t in REQUIRED_TOPICS:
            if t not in self.topics:
                # an older config.toml lists the topics without it: the live lap / sector state
                # would then differ from the same moment of a recording (which has it)
                log.info("Subscribing to %s as well (needed for the lap / sector tracking)", t)
                self.topics.append(t)
        self.transport_pref: str = (cfg.get("transport") or "auto").lower()
        self.reconnect_min = float(cfg.get("reconnect_min", 2.0))
        self.reconnect_max = float(cfg.get("reconnect_max", 60.0))
        self.silence_timeout = float(cfg.get("silence_timeout", 120.0))
        self.recorder = recorder
        # F1 TV sign-in ([f1_tv] subscription); without a manager: the legacy token setting only
        self.auth = auth if auth is not None else AuthManager(
            {"subscription": bool(cfg.get("f1tv_token"))}, _NullStore(), cfg.get("f1tv_token") or "",
            opener=lambda _url: False)
        self._conn_token: Optional[str] = None          # token of the current connection (None: anonymous)
        self.auth_mode = "ANONYMOUS"
        self._reduced = False                           # F1 refused the full topic list once
        self._fallback_logged: Optional[str] = None
        self._failures = 0
        self._core_failures = 0
        # Public-archive fallback for Position.z / CarData.z (see archive_follow.py)
        self.archive_follow = bool(cfg.get("archive_follow", True))
        self.archive_poll = max(1.0, float(cfg.get("archive_poll_seconds", 3.0)))
        self._session_path: Optional[str] = None
        self._session_status: Optional[str] = None
        self._last_ws_pos = 0.0
        self._last_ws_car = 0.0
        self._warned_no_pos = False
        self._live_since: Optional[float] = None

    @property
    def token_configured(self) -> bool:
        return self.auth.token() is not None

    @property
    def token(self) -> Optional[str]:
        """Token of the current connection (never logged / sent anywhere but to F1)."""
        return self._conn_token

    def connection_info(self) -> dict:
        """Safe for diagnostics / dashboards."""
        return {"auth_mode": self.auth_mode, "f1_tv": self.auth.public_info(),
                "topics_subscribed": list(self._topics_now())}

    def _topics_now(self) -> list[str]:
        if not self._reduced:
            return self.topics
        return [t for t in self.topics if t in CORE_TOPICS] or list(CORE_TOPICS)

    # ------------------------------------------------------------------
    async def run(self, sink: Sink) -> None:
        # helpers = false: a minimal second connection (the LIGHTS OUT fallback, server/lights_out_probe.py)
        helpers = [asyncio.create_task(self._schedule_loop(sink)), asyncio.create_task(self._archive_loop(sink))] \
            if self.cfg.get("helpers", True) else []
        try:
            await self._run(sink)
        finally:                    # stopped (LIVE -> VOD switch, shutdown): no helper keeps running
            for t in helpers:
                t.cancel()

    async def _run(self, sink: Sink) -> None:
        if self.auth.subscription:
            log.info("F1 TV subscription mode: ENABLED")
            if self.auth.needs_login():
                self.auth.request_login(self.auth.reason or "no stored sign-in")
        else:
            log.info("F1 TV subscription mode: DISABLED - anonymous public live timing")
        attempt = 0
        while True:
            token = self.auth.token()
            self._conn_token = token
            self.auth_mode = "AUTHENTICATED" if token else "ANONYMOUS"
            transport = self._pick_transport()
            if self.auth.subscription and token is None and self._fallback_logged != self.auth.state:
                self._fallback_logged = self.auth.state
                log.warning("F1 TV authentication unavailable (%s: %s) - falling back to anonymous public "
                            "live timing", self.auth.state, self.auth.reason)
            sink.set_status(state="connecting", transport=transport, attempt=attempt, auth=self.auth_mode,
                            detail=f"Connecting to F1 live timing ({transport}, {self.auth_mode.lower()})")
            started = time.monotonic()
            version = self.auth.version
            conn = asyncio.create_task(self._run_core(sink) if transport == "core" else self._run_legacy(sink))
            # a sign-in completed while connected anonymously: reconnect with it
            waiter = asyncio.create_task(self.auth.wait_change(version)) if self.auth.subscription else None
            quick = False
            try:
                done, _ = await asyncio.wait({conn} | ({waiter} if waiter else set()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if conn in done:
                    conn.result()
                    reason = "connection closed by server"
                else:
                    conn.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await conn
                    reason = "F1 TV sign-in completed - reconnecting authenticated"
                    log.info("F1 TV sign-in completed - reconnecting with authenticated live timing")
                    quick = True
            except asyncio.CancelledError:
                conn.cancel()
                raise
            except AuthRejected as exc:
                self.auth.reject(str(exc))
                reason = f"{exc} - falling back to anonymous public live timing"
                quick = True
            except InvalidStatus as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (401, 403) and token:
                    self.auth.reject(f"websocket HTTP {code}")
                    quick = True
                reason = f"InvalidStatus: HTTP {code}"
            except SubscribeFailed as exc:
                if not self._reduced:
                    self._reduced = True
                    log.warning("F1 refused the subscription (%s) - next attempt subscribes to the core "
                                "topics only (%d)", exc, len(self._topics_now()))
                    quick = True
                reason = str(exc)
            except (FeedError, ConnectionClosed, httpx.HTTPError, OSError, asyncio.TimeoutError, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error in live client")
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                if waiter is not None:
                    waiter.cancel()

            if time.monotonic() - started > 60:
                attempt = 0            # connection was healthy for a while
                self._core_failures = 0
            elif transport == "core" and not quick:
                self._core_failures += 1
            attempt += 1
            delay = min(1.0, self.reconnect_min) if quick else \
                min(self.reconnect_max, self.reconnect_min * (2 ** min(attempt - 1, 8)))
            delay *= random.uniform(0.85, 1.15)
            log.warning("F1 feed disconnected (%s). Reconnecting in %.1fs (attempt %d)", reason, delay, attempt)
            sink.set_status(state="reconnecting", transport=transport, attempt=attempt, auth=self.auth_mode,
                            retry_in=round(delay, 1), detail=reason)
            await asyncio.sleep(delay)

    def _pick_transport(self) -> str:
        if self.transport_pref in ("core", "legacy"):
            return self.transport_pref
        if self._conn_token:
            return "core"              # the legacy endpoint does not take the F1 TV token
        # auto: prefer core; after two consecutive core failures try legacy once
        if self._core_failures >= 2 and self._core_failures % 2 == 0:
            return "legacy"
        return "core"

    # ------------------------------------------------------------------
    async def _handle_snapshot(self, sink: Sink, result: Any) -> None:
        if not isinstance(result, dict):
            return
        await sink.begin_snapshot()
        now = datetime.now(timezone.utc)
        # F1's own clock in the snapshot: the time of its last heartbeat (the content is newer)
        hb = result.get("Heartbeat")
        hb_utc = parse_utc(hb.get("Utc")) if isinstance(hb, dict) else None
        # SessionInfo first: if the session changed while disconnected, the engine clears the
        # state on it - which must not wipe topics of this snapshot applied before it
        for topic, data in sorted(result.items(), key=lambda kv: kv[0] != "SessionInfo"):
            self._track(topic, data)
            if self.recorder:
                self.recorder.write(topic, data, now, True)
            await sink.feed(topic, data, hb_utc, snapshot=True)
        log.info("Received subscription snapshot with %d topics", len(result))

    async def _handle_feed(self, sink: Sink, args: list) -> None:
        if len(args) < 2 or not isinstance(args[0], str):
            log.debug("Malformed feed message: %r", args[:1])
            return
        topic, data = args[0], args[1]
        ts = parse_utc(args[2]) if len(args) > 2 else None
        ts = ts or datetime.now(timezone.utc)
        self._track(topic, data)
        if self.recorder:
            self.recorder.write(topic, data, ts, False)
        await sink.feed(topic, data, ts)

    # ------------------------------------------------------------------
    async def _run_core(self, sink: Sink) -> None:
        headers = {"User-Agent": UA_CORE, "Origin": ORIGIN}
        token = self._conn_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        ws_url = CORE_WS
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as http:
            try:  # pre-negotiate to obtain the load balancer cookie (AWSALBCORS)
                await http.options(CORE_NEGOTIATE)
            except httpx.HTTPError as exc:
                log.debug("OPTIONS negotiate failed: %s", exc)
            r = await http.post(f"{CORE_NEGOTIATE}?negotiateVersion=1")
            if r.status_code in (401, 403):
                if token:
                    raise AuthRejected(f"negotiate rejected the F1 TV sign-in (HTTP {r.status_code})")
                raise FeedError(f"negotiate rejected (HTTP {r.status_code})")
            r.raise_for_status()
            neg = r.json()
            if neg.get("url"):                       # redirect to another SignalR service
                ws_url = neg["url"].replace("https://", "wss://").replace("http://", "ws://")
                if neg.get("accessToken"):
                    headers["Authorization"] = f"Bearer {neg['accessToken']}"
            else:
                conn_token = neg.get("connectionToken") or neg.get("connectionId")
                if conn_token:
                    ws_url = f"{CORE_WS}?id={urllib.parse.quote(conn_token)}"
            cookies = "; ".join(f"{k}={v}" for k, v in http.cookies.items())
        ws_headers = dict(headers)
        if cookies:
            ws_headers["Cookie"] = cookies

        async with connect(ws_url, additional_headers=ws_headers, max_size=None, open_timeout=20,
                           ping_interval=20, ping_timeout=20, user_agent_header=None) as ws:
            await ws.send(json.dumps({"protocol": "json", "version": 1}) + RS)
            hs = await asyncio.wait_for(ws.recv(), 15)
            first = str(hs).split(RS)[0]
            if first and json.loads(first).get("error"):
                raise FeedError(f"handshake error: {json.loads(first)['error']}")
            topics = self._topics_now()
            await ws.send(json.dumps({"type": 1, "invocationId": "1", "target": "Subscribe",
                                      "arguments": [topics]}) + RS)
            if token:
                self.auth.confirm()
                info = token_info(token)
                log.info("F1 TV authentication successful (%s, %s) - authenticated live timing enabled",
                         info.get("subscribed_product") or "product not stated",
                         info.get("subscription_status") or "status not stated")
            log.info("Connected to F1 live timing (SignalR Core, %s), subscribed to %d topics",
                     "AUTHENTICATED" if token else "ANONYMOUS", len(topics))
            sink.set_status(state="connected", transport="core", attempt=0, auth=self.auth_mode, detail="Connected")
            pinger = asyncio.create_task(self._core_ping(ws))
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), self.silence_timeout)
                    for frame in str(raw).split(RS):
                        if not frame:
                            continue
                        try:
                            msg = json.loads(frame)
                        except ValueError:
                            log.warning("Malformed SignalR frame (%d bytes)", len(frame))
                            continue
                        t = msg.get("type")
                        if t == 1 and str(msg.get("target", "")).lower() == "feed":
                            await self._handle_feed(sink, msg.get("arguments") or [])
                        elif t == 3:
                            if msg.get("error"):
                                raise SubscribeFailed(f"Subscribe failed: {msg['error']}")
                            if msg.get("invocationId") == "1":
                                await self._handle_snapshot(sink, msg.get("result"))
                        elif t == 7:
                            raise FeedError(f"server closed connection: {msg.get('error') or 'no reason'}")
            finally:
                pinger.cancel()

    @staticmethod
    async def _core_ping(ws) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await ws.send(json.dumps({"type": 6}) + RS)
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    # ------------------------------------------------------------------
    async def _run_legacy(self, sink: Sink) -> None:
        conn_data = json.dumps([{"name": "Streaming"}], separators=(",", ":"))
        params = {"clientProtocol": "1.5", "connectionData": conn_data}
        headers = {"User-Agent": UA_LEGACY, "Accept-Encoding": "gzip,identity", "Origin": ORIGIN}
        async with httpx.AsyncClient(timeout=20, headers=headers) as http:
            r = await http.get(LEGACY_NEGOTIATE, params=params)
            r.raise_for_status()
            token = r.json().get("ConnectionToken")
            if not token:
                raise FeedError("negotiate returned no ConnectionToken")
            cookies = "; ".join(f"{k}={v}" for k, v in http.cookies.items())
            qs = urllib.parse.urlencode({**params, "transport": "webSockets", "connectionToken": token})
            ws_headers = dict(headers)
            if cookies:
                ws_headers["Cookie"] = cookies
            async with connect(f"{LEGACY_CONNECT}?{qs}", additional_headers=ws_headers, max_size=None,
                               open_timeout=20, user_agent_header=None) as ws:
                try:
                    await http.get(LEGACY_START, params={**params, "transport": "webSockets",
                                                         "connectionToken": token})
                except httpx.HTTPError as exc:
                    log.debug("legacy /start failed (ignored): %s", exc)
                topics = self._topics_now()
                await ws.send(json.dumps({"H": "Streaming", "M": "Subscribe", "A": [topics], "I": 1}))
                log.info("Connected to F1 live timing (legacy SignalR, ANONYMOUS), subscribed to %d topics",
                         len(topics))
                sink.set_status(state="connected", transport="legacy", attempt=0, auth="ANONYMOUS",
                                detail="Connected")
                while True:
                    raw = await asyncio.wait_for(ws.recv(), self.silence_timeout)
                    if not raw or len(raw) < 3:
                        continue            # keep-alive "{}"
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        log.warning("Malformed legacy frame (%d bytes)", len(raw))
                        continue
                    if "R" in msg and str(msg.get("I")) == "1":
                        await self._handle_snapshot(sink, msg["R"])
                    for m in msg.get("M") or []:
                        if isinstance(m, dict) and str(m.get("M", "")).lower() == "feed":
                            await self._handle_feed(sink, m.get("A") or [])

    # ------------------------------------------------------------------
    def _track(self, topic: str, data: Any) -> None:
        """Remember session path/status and whether the socket delivers positions."""
        if topic in ("Position.z", "Position") and data:
            self._last_ws_pos = time.monotonic()
        elif topic in ("CarData.z", "CarData") and data:
            self._last_ws_car = time.monotonic()
        elif topic == "SessionInfo" and isinstance(data, dict):
            self._session_path = data.get("Path") or self._session_path
            self._session_status = data.get("SessionStatus") or self._session_status
        elif topic == "SessionStatus" and isinstance(data, dict):
            self._session_status = data.get("Status") or self._session_status

    async def _archive_loop(self, sink: Sink) -> None:
        """While a session runs without Position.z on the socket, try the public archive."""
        follower = ArchiveFollower(poll_seconds=self.archive_poll)
        headers = {"User-Agent": UA_CORE}
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as http:
            while True:
                await asyncio.sleep(self.archive_poll)
                live = self._session_status in ("Started", "Aborted")
                if not live:
                    self._live_since = None
                    self._warned_no_pos = False
                    if follower.state != "idle":
                        follower.reset(None)
                    continue
                if self._live_since is None:
                    self._live_since = time.monotonic()
                ws_has_pos = time.monotonic() - self._last_ws_pos < 20
                if ws_has_pos:
                    if follower.state == "following":
                        log.info("Live socket delivers Position.z again - archive follower paused")
                    follower.reset(self._session_path)
                    continue
                # give the socket 30 s after the session went live before concluding
                if time.monotonic() - self._live_since < 30:
                    continue
                if not self._warned_no_pos:
                    self._warned_no_pos = True
                    log.warning("Session is running but the live socket sends no Position.z/CarData.z "
                                "(F1 withholds them from %s connections). %s",
                                "this F1 TV account's" if self._conn_token else "anonymous",
                                "Trying the public F1 archive stream." if self.archive_follow
                                else "Archive follower disabled (live.archive_follow = false).")
                if not self.archive_follow or not self._session_path:
                    continue
                if follower.path != self._session_path:
                    follower.reset(self._session_path)

                async def emit(topic: str, payload: Any) -> None:
                    await sink.feed(topic, payload, None, origin="archive")

                try:
                    await follower.poll(http, emit)
                except Exception:  # noqa: BLE001
                    log.exception("Archive follower error")

    # ------------------------------------------------------------------
    async def _schedule_loop(self, sink: Sink) -> None:
        """Fetch the season index to show the next session when nothing is live."""
        while True:
            try:
                sink.set_schedule(await self._next_session())
                await asyncio.sleep(3 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("Could not load session schedule: %s", exc)
                await asyncio.sleep(600)

    async def _next_session(self) -> Optional[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        best = None
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA_CORE}) as http:
            for year in (now.year, now.year + 1):
                r = await http.get(INDEX_URL.format(year=year))
                if r.status_code != 200:
                    continue
                data = json.loads(r.content.decode("utf-8-sig"))
                for m in data.get("Meetings") or []:
                    for s in m.get("Sessions") or []:
                        start = _local_to_utc(s.get("StartDate"), s.get("GmtOffset"))
                        if start and start > now - timedelta(hours=3) and (best is None or start < best["start"]):
                            end = _local_to_utc(s.get("EndDate"), s.get("GmtOffset"))
                            if end and end < now:
                                continue
                            best = {"start": start, "meeting": m.get("Name"), "session": s.get("Name"),
                                    "location": m.get("Location")}
                if best:
                    break
        if best:
            best = {**best, "start_utc": best.pop("start").isoformat()}
            log.info("Next session: %s - %s at %s", best["meeting"], best["session"], best["start_utc"])
        return best


def _local_to_utc(local: Optional[str], offset: Optional[str]) -> Optional[datetime]:
    if not local:
        return None
    try:
        dt = datetime.fromisoformat(local)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    sign = -1 if (offset or "").startswith("-") else 1
    parts = [int(p) for p in (offset or "0:0:0").lstrip("+-").split(":")[:3]] + [0, 0, 0]
    delta = timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2]) * sign
    return (dt - delta).replace(tzinfo=timezone.utc)


class _NullStore:
    """No stored sign-in (a source built without an AuthManager: tests / tools)."""

    def load(self):
        return None

    def save(self, token):
        pass

    def clear(self):
        return False


__all__ = ["F1LiveSource", "FeedError", "AuthRejected", "SubscribeFailed", "parse_token", "token_expiry"]
