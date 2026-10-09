"""The public gateway: the ONLY way the internet (Tailscale Funnel) reaches F1Dash.

Why a gateway and not Funnel's own path mounts: /tv embeds the dashboard, which lives at "/", and a "/"
mount in ``tailscale serve`` / ``funnel`` is a catch-all prefix (it matches every path that no longer
mount matches). So Funnel forwards everything to this gateway on 127.0.0.1:<port>, and the gateway:

* forwards only the exact routes /tv and /remote need (ROUTES: path, method, HTTP or WebSocket) - all
  else is a plain 404 and never reaches the application;
* refuses ambiguous request paths instead of normalising them: any %-encoding, "//", "." / ".."
  segments, backslashes, control or non-ASCII characters, absurd lengths;
* answers only for the configured public hostname (the Host header tailscaled passes on), else 421;
* refuses a ``token`` query parameter - secrets never travel in public URLs;
* marks the request public (``scope["f1_public"]``): the application then applies the public rules -
  dashboard data only for an approved /tv page or the trusted phone, /remote control only for the
  trusted phone (no remote token), a read-only dashboard socket (server/app.py);
* never lets a visitor look like this machine: the client address comes from X-Forwarded-For
  (tailscaled sets it), a loopback / missing / invalid one becomes "public";
* sets the scheme to https (Funnel terminated TLS) so cookies get ``Secure``;
* rate-limits per client and caps public WebSockets;
* adds security headers and a Content-Security-Policy to everything it returns.

It is fail-closed: an invalid [public] configuration means no gateway at all (``config_problem``).
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl

from .security import RateLimiter

log = logging.getLogger("security")

HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
# a public request path: plain ASCII path characters only (no %, no ;, no \, no spaces, no controls)
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
RADAR_TILES = "https://tilecache.rainviewer.com"

# (pattern, methods, kind, needs) - needs: "" (anyone; the app decides), "view" (an approved /tv page or the
# trusted phone - checked here AND in the app), "page" (an HTML page: page CSP)
STATIC_DASH = r"(style\.css|tv\.css|app\.js|components/(voyo_player\.js|voyo_player\.css|f1time\.js|qrcode\.js|pitlane\.js|team_radio\.js))"
ROUTES: list[tuple[re.Pattern, frozenset, str, str]] = [
    # /tv: the page (approval screen first), its code, the approval API, the page's own API and the video
    (re.compile(r"^/tv$"), frozenset({"GET"}), "http", "page"),
    (re.compile(r"^/tv-static/(tv\.css|tv\.js|vendor/hls\.light\.min\.js)$"), frozenset({"GET"}), "http", ""),
    (re.compile(r"^/api/tv/auth/(request|status)$"), frozenset({"POST"}), "http", ""),
    (re.compile(r"^/api/tv/logout$"), frozenset({"POST"}), "http", ""),
    (re.compile(r"^/api/tv/status$"), frozenset({"GET"}), "http", ""),
    (re.compile(r"^/tv/live/(index\.m3u8|live_[0-9]{5}\.ts)$"), frozenset({"GET"}), "http", ""),
    # the dashboard inside /tv's iframe (and what it reads) - only for an approved /tv page / the trusted phone
    (re.compile(r"^/$"), frozenset({"GET"}), "http", "view"),
    (re.compile(r"^/static/" + STATIC_DASH + r"$"), frozenset({"GET"}), "http", ""),
    (re.compile(r"^/api/track/layouts$"), frozenset({"GET"}), "http", "view"),
    (re.compile(r"^/api/media/catalog$"), frozenset({"GET"}), "http", "view"),
    # TEAM RADIO playback in the dashboard (a clip of the session shown now, fetched from the F1 archive)
    (re.compile(r"^/api/radio/audio/[0-9a-f]{16}$"), frozenset({"GET"}), "http", "view"),
    # /remote: the page and its socket (the app allows control / approvals only to the trusted phone)
    (re.compile(r"^/remote$"), frozenset({"GET"}), "http", "page"),
    (re.compile(r"^/ws$"), frozenset({"GET"}), "websocket", ""),
]
# the only query parameters a public route accepts (exact path; "/tv/live/" as a prefix)
QUERY_KEYS = {"/": {"layout"}, "/tv": {"next"}, "/api/media/catalog": {"year"}, "/ws": {"client"}}
QUERY_PREFIX_KEYS = {"/tv/live/": {"p"}}


def config_problem(pub: dict, http_port: int, https_port: int) -> Optional[str]:
    """None when [public] is usable, else why the gateway stays off."""
    host = str(pub.get("hostname") or "").strip().lower().rstrip(".")
    if not host:
        return "[public] hostname is empty (e.g. f1server.tail1234.ts.net)"
    if not HOSTNAME_RE.match(host):
        return f"[public] hostname {host!r} is not a valid DNS name"
    try:
        port = int(pub.get("port"))
    except (TypeError, ValueError):
        return "[public] port is not a number"
    if not 1024 <= port <= 65535:
        return "[public] port must be 1024-65535"
    if port in (int(http_port or 0), int(https_port or 0)):
        return f"[public] port {port} is already used by the dashboard itself"
    return None


def inline_script_hashes(html_file: Path) -> list[str]:
    """CSP hashes of the inline <script> blocks of a page (so no 'unsafe-inline' for scripts)."""
    try:
        text = html_file.read_text(encoding="utf-8")
    except OSError:
        return []
    return ["'sha256-" + base64.b64encode(hashlib.sha256(m.encode("utf-8")).digest()).decode() + "'"
            for m in re.findall(r"<script>(.*?)</script>", text, flags=re.S)]


class PublicGateway:
    def __init__(self, app, hostname: str, view_check: Callable, remote_html: Path,
                 per_client: int = 600, ws_per_client: int = 6, ws_total: int = 40, ws_per_minute: int = 30) -> None:
        self.app = app
        self.host = hostname.strip().lower().rstrip(".")
        self.view_check = view_check                     # (scope) -> bool: approved /tv page or trusted phone
        self.remote_hashes = " ".join(inline_script_hashes(remote_html))
        self.rl = RateLimiter(per_client, 60)
        self.ws_per_client, self.ws_total = ws_per_client, ws_total
        self.ws_rl = RateLimiter(ws_per_minute, 60)      # socket (re)connects per visitor - no churn
        self.ws_open: dict[str, int] = {}
        self.denied = RateLimiter(20, 60)                # log at most 20 refusals per client and minute

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _header(scope, name: bytes) -> str:
        for k, v in scope.get("headers") or []:
            if k.lower() == name:
                return v.decode("latin-1")
        return ""

    def _host_ok(self, host: str) -> bool:
        h = host.strip().lower()
        if h == self.host:
            return True
        name, sep, port = h.rpartition(":")
        return bool(sep) and name == self.host and port.isdigit()

    @staticmethod
    def client_ip(scope) -> str:
        """The visitor (X-Forwarded-For from tailscaled: the original client, not the Funnel relay) - never
        loopback, never this machine. IPv4 written as IPv6 (::ffff:a.b.c.d, as Go prints it) counts as the IPv4
        address; an IPv6 visitor counts as its /64, so rotating addresses inside it does not dodge the limits."""
        raw = PublicGateway._header(scope, b"x-forwarded-for").split(",")[0].strip()
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return "public"
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_unspecified:
            return "public"
        if ip.version == 6:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
        return str(ip)

    def _route(self, path: str, method: str, kind: str):
        for pat, methods, rkind, needs in ROUTES:
            if pat.match(path):
                if rkind != kind or method not in methods:
                    return None
                return needs
        return None

    @staticmethod
    def _query_ok(path: str, qs: bytes) -> bool:
        if not qs:
            return True
        if len(qs) > 300:
            return False
        try:
            text = qs.decode("ascii")
        except UnicodeDecodeError:
            return False
        if any(ord(c) < 0x21 or ord(c) > 0x7e for c in text):
            return False
        allowed = QUERY_KEYS.get(path) or next((v for k, v in QUERY_PREFIX_KEYS.items() if path.startswith(k)), set())
        try:
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return False
        if not all(k in allowed for k, _v in pairs):
            return False
        # /tv?next= may only lead back to the dashboard (nothing else is public anyway)
        return all(v in ("/", "/tv") for k, v in pairs if k == "next")

    def _csp(self, path: str, scope) -> str:
        wss = "wss://" + self._header(scope, b"host").strip().lower()
        base = "base-uri 'none'; object-src 'none'; form-action 'self'"
        if path == "/tv":
            return (f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    f"media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; frame-src 'self'; "
                    f"frame-ancestors 'none'; {base}")
        if path == "/":
            return (f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    f"img-src 'self' data: blob: {RADAR_TILES}; media-src 'self' blob:; worker-src 'self' blob:; "
                    f"connect-src 'self' {wss}; frame-src 'none'; frame-ancestors 'self'; {base}")
        if path == "/remote":
            return (f"default-src 'self'; script-src 'self' {self.remote_hashes}; style-src 'self' 'unsafe-inline'; "
                    f"img-src 'self' data:; connect-src 'self' {wss}; frame-src 'none'; frame-ancestors 'none'; {base}")
        return f"default-src 'none'; frame-ancestors 'none'; {base}"

    async def _deny(self, scope, receive, send, status: int, why: str, client: str) -> None:
        if not self.denied.blocked(client):
            self.denied.hit(client)
            # the path only - never the query string (it may hold a page secret or a token)
            log.warning("public gateway refused %s %s from %s: %s", ascii(str(scope.get("method", "WS"))[:10]),
                        ascii((scope.get("path") or "")[:80]), client, why)     # ascii(): no forged log lines
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return
        body = json.dumps({"ok": False, "error": {400: "bad request", 401: "not authorized", 404: "not found", 421: "wrong host",
                                                   429: "too many requests"}.get(status, "refused")}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                   (b"cache-control", b"no-store")] + self._sec_headers("", scope)
        if status == 429:
            headers.append((b"retry-after", b"60"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    def _sec_headers(self, path: str, scope) -> list[tuple[bytes, bytes]]:
        return [(b"content-security-policy", self._csp(path, scope).encode()),
                (b"x-content-type-options", b"nosniff"),
                (b"referrer-policy", b"no-referrer"),
                (b"x-frame-options", b"SAMEORIGIN" if path == "/" else b"DENY"),
                (b"strict-transport-security", b"max-age=15552000"),
                (b"cross-origin-opener-policy", b"same-origin"),
                (b"cross-origin-resource-policy", b"same-origin"),
                (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()")]

    # ------------------------------------------------------------------ ASGI
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return                                       # no lifespan through the public listener
        client = self.client_ip(scope)
        raw = scope.get("raw_path")
        path = scope.get("path") or ""
        try:
            raw_s = raw.decode("ascii") if isinstance(raw, (bytes, bytearray)) else path
        except UnicodeDecodeError:
            return await self._deny(scope, receive, send, 400, "non-ASCII path", client)
        if (raw_s != path or not SAFE_PATH.match(path) or "//" in path
                or any(seg in (".", "..") for seg in path.split("/"))):
            return await self._deny(scope, receive, send, 400, "ambiguous path", client)
        if not self._host_ok(self._header(scope, b"host")):
            return await self._deny(scope, receive, send, 421, "wrong host", client)
        wait = self.rl.blocked(client)
        if wait:
            return await self._deny(scope, receive, send, 429, "rate limit", client)
        self.rl.hit(client)
        method = scope.get("method", "GET") if scope["type"] == "http" else "GET"
        needs = self._route(path, method, scope["type"])
        if needs is None:
            return await self._deny(scope, receive, send, 404, "not on the public allowlist", client)
        if not self._query_ok(path, scope.get("query_string") or b""):
            return await self._deny(scope, receive, send, 400, "query parameters not allowed", client)

        inner = dict(scope)
        inner["scheme"] = "https" if scope["type"] == "http" else "wss"
        inner["client"] = (client, 0)
        inner["f1_public"] = True
        if needs == "view" and not self.view_check(inner):
            if path == "/":                                # the dashboard: get approved first
                await send({"type": "http.response.start", "status": 303,
                            "headers": [(b"location", b"/tv?next=/"), (b"content-length", b"0"),
                                        (b"cache-control", b"no-store")] + self._sec_headers("", scope)})
                await send({"type": "http.response.body", "body": b""})
                return
            return await self._deny(scope, receive, send, 401 if scope["type"] == "http" else 404, "not approved", client)

        if scope["type"] == "websocket":
            if (self.ws_open.get(client, 0) >= self.ws_per_client or sum(self.ws_open.values()) >= self.ws_total
                    or self.ws_rl.blocked(client)):
                return await self._deny(scope, receive, send, 429, "too many sockets", client)
            self.ws_rl.hit(client)
            self.ws_open[client] = self.ws_open.get(client, 0) + 1
            try:
                return await self.app(inner, receive, send)
            finally:
                self.ws_open[client] -= 1
                if self.ws_open[client] <= 0:
                    self.ws_open.pop(client, None)

        extra = self._sec_headers(path, scope)
        own = {k for k, _v in extra}                       # the gateway's headers replace the app's
        is_static = path.startswith(("/static/", "/tv-static/", "/tv/live/", "/api/radio/audio/"))

        async def send_secured(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"server" and k.lower() not in own]
                if not is_static and not any(k.lower() == b"cache-control" for k, _v in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers + extra}
            await send(message)

        return await self.app(inner, receive, send_secured)
