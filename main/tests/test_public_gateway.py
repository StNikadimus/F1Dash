"""The public gateway for Tailscale Funnel (server/public_gateway.py + the public rules in server/app.py):
only /tv and /remote (and exactly what they need) are reachable from the internet; /disk and everything
else is not; /tv keeps its per-load phone approval; /remote control and approvals only for the trusted phone.

Everything here runs against a temporary data directory. Two kinds of tests:
* through the gateway with a TestClient (as a browser behind Funnel would talk to it);
* raw ASGI calls with hand-made request paths (encodings, dot segments ... that HTTP clients would
  normalise before sending).

Run (from main/):  python -m unittest tests.test_public_gateway
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.config import load_config  # noqa: E402
from server.public_gateway import PublicGateway, config_problem  # noqa: E402

HOST = "f1test.tail1234.ts.net"
PUBLIC = f"https://{HOST}:8443"
TOKEN = "lan-remote-token-xyz"
PW = "correct horse battery"


class Env:
    """create_app on a temp data dir with the gateway on; .lan = the LAN app, .pub = the gateway."""

    def __init__(self, d: Path, **extra):
        from starlette.testclient import TestClient
        import server.app as appmod
        self.d = d
        env = {"F1DASH_VOYO_SERVER_PLAYER_ENABLED": "true", "F1DASH_VOYO_RECORDING_PATH": str(d / "rec"),
               "F1DASH_VOYO_RECORDING_REQUIRE_MOUNT": "", "F1DASH_VOYO_RECORDING_MOUNT_MARKER": "",
               "F1DASH_VOYO_RECORDING_MIN_FREE_BYTES": "0", "F1DASH_REMOTE_TOKEN": TOKEN,
               "F1DASH_PUBLIC_ENABLED": "true", "F1DASH_PUBLIC_HOSTNAME": HOST, "F1DASH_PUBLIC_PORT": "8090", **extra}
        self._patches = [mock.patch.dict(os.environ, env), mock.patch.object(appmod, "DATA_DIR", d)]
        for p in self._patches:
            p.start()
        cfg = load_config()
        cfg["source"]["mode"] = "test"
        self.app = appmod.create_app(cfg)
        self.sec = self.app.state.security
        self.pub = self.app.state.public_app
        self.TC = TestClient

    def lan(self):
        return self.TC(self.app)

    def internet(self, ip="203.0.113.7", app=None):
        """A browser on the internet, through Funnel (tailscaled sets X-Forwarded-For)."""
        return internet_client(app or self.pub, ip)

    def close(self):
        for p in self._patches:
            p.stop()


def internet_client(app, ip):
    """TestClient sends "Host: testserver" and no cookies on WebSockets; a browser sends the real host and its cookies."""
    from starlette.testclient import TestClient

    class Browser(TestClient):
        def websocket_connect(self, url, subprotocols=None, **kwargs):
            jar = "; ".join(f"{k}={v}" for k, v in self.cookies.items())       # ... and its cookies
            kwargs["headers"] = {"host": f"{HOST}:8443", **({"cookie": jar} if jar else {}), **(kwargs.get("headers") or {})}
            return super().websocket_connect(url, subprotocols, **kwargs)
    return Browser(app, base_url=PUBLIC, headers={"x-forwarded-for": ip})


def asgi_call(app, path, method="GET", raw=None, host=HOST, query=b"", kind="http", headers=()):
    """One request with an exact raw path (no client-side normalisation) -> (status or ws close code, body)."""
    out = {"status": None, "body": b""}
    scope = {"type": kind, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": raw if raw is not None else path.encode("latin-1"), "query_string": query, "root_path": "",
             "headers": [(b"host", host.encode())] + list(headers), "client": ("127.0.0.1", 40000),
             "server": ("127.0.0.1", 8090)}
    if kind == "websocket":
        scope["subprotocols"] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False} if kind == "http" else {"type": "websocket.connect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
        elif msg["type"] == "websocket.close":
            out["status"] = msg.get("code")
        elif msg["type"] == "websocket.accept":
            out["status"] = "accepted"
            raise ConnectionAbortedError
    try:
        asyncio.run(app(scope, receive, send))
    except ConnectionAbortedError:
        pass
    return out["status"], out["body"]


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.e = Env(self.d)
        self.admin = self.e.lan()                                         # /disk at home
        code = (self.d / "auth" / "disk-setup-code").read_text().strip()
        self.admin.post("/api/disk/auth/setup", json={"code": code, "password": PW, "confirm": PW})
        self.H = {"X-F1-CSRF": self.admin.get("/api/disk/auth/state").json()["csrf"]}

    def tearDown(self):
        self.e.close()
        shutil.rmtree(self.d, ignore_errors=True)

    # helpers
    def trusted_public_phone(self):
        """The owner's phone opens the PUBLIC /remote; at home the owner trusts that device in /disk."""
        phone = self.e.internet("198.51.100.20")
        r = phone.get("/remote")
        self.assertEqual(r.status_code, 200)
        did = self.e.sec.device(phone.cookies.get("f1_dev"))[0]
        self.assertEqual(self.admin.post("/api/disk/security/trust", json={"device": did}, headers=self.H).status_code, 200)
        return phone, did

    def open_tv(self, tv):
        r = tv.get("/tv")
        self.assertIn("ACCESS REQUEST", r.text)
        b = tv.post("/api/tv/auth/request").json()
        req = next(x for x in self.e.sec.requests.values() if x["code"] == b["code"])
        return b["challenge"], req

    def status(self, tv, ch):
        return tv.post("/api/tv/auth/status", headers={"X-F1-TV-Challenge": ch})

    def approved_public_tv(self):
        _phone, did = self.trusted_public_phone()
        tv = self.e.internet("192.0.2.50")
        ch, req = self.open_tv(tv)
        self.e.sec.decide(req["id"], True, by_device=did)
        st = self.status(tv, ch)
        self.assertEqual(st.json()["status"], "authenticated")
        return tv, {"X-F1-TV-Page": st.json()["page"]}, ch, req

    # 0 configuration -------------------------------------------------------------------------------
    def test_00_off_by_default_and_fail_closed(self):
        self.assertIsNotNone(self.e.pub)
        self.assertIsNone(config_problem({"hostname": HOST, "port": 8090}, 8080, 443))
        for bad in ({"hostname": "", "port": 8090}, {"hostname": "not a host", "port": 8090}, {"hostname": HOST, "port": 80},
                    {"hostname": HOST, "port": 8080}, {"hostname": HOST, "port": 443}, {"hostname": HOST, "port": "x"}):
            self.assertIsNotNone(config_problem(bad, 8080, 443), bad)
        d2 = Path(tempfile.mkdtemp())
        try:
            self.e.close()
            for extra in ({"F1DASH_PUBLIC_ENABLED": "false"}, {"F1DASH_PUBLIC_HOSTNAME": ""},
                          {"F1DASH_PUBLIC_HOSTNAME": "evil host"}, {"F1DASH_PUBLIC_PORT": "8080"}):
                e2 = Env(d2, **extra)
                self.assertIsNone(e2.pub, extra)                              # no gateway at all
                e2.close()
        finally:
            shutil.rmtree(d2, ignore_errors=True)
            self.e = Env(self.d)

    # 1 /tv without approval ---------------------------------------------------------------------------
    def test_01_tv_without_approval(self):
        tv = self.e.internet()
        r = tv.get("/tv")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="locked"', r.text)
        self.assertNotIn("/?layout", r.text)
        csp = r.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline' 'self'; script", csp)
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")
        self.assertNotIn("server", r.headers)
        for path in ("/api/tv/status", "/tv/live/index.m3u8", "/tv/live/live_00001.ts"):
            self.assertEqual(tv.get(path).status_code, 401, path)
        ch, _req = self.open_tv(tv)
        self.assertEqual(self.status(tv, ch).json()["status"], "pending")
        self.assertEqual(tv.get("/api/tv/status", headers={"X-F1-TV-Page": ch}).status_code, 401)
        # the dashboard (the iframe source) is not served before the approval
        r = tv.get("/", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/tv?next=/"))
        self.assertEqual(tv.get("/api/track/layouts").status_code, 401)
        self.assertEqual(tv.get("/api/media/catalog?year=2026").status_code, 401)
        self.assertEqual(tv.get("/api/radio/audio/0123456789abcdef").status_code, 401)   # team radio playback

    # 2 3 stale cookie, forged / reused challenge and page secret --------------------------------------
    def test_02_03_stale_cookie_forged_and_reused_secrets(self):
        tv, page, ch, req = self.approved_public_tv()
        self.assertEqual(tv.get("/api/tv/status", headers=page).status_code, 200)
        cookie = tv.cookies.get("f1_tv")
        tv.get("/tv")                                                       # reload: a new approval needed
        self.assertEqual(tv.get("/api/tv/status", headers=page).status_code, 401)
        stale = self.e.internet("192.0.2.99")
        stale.cookies.set("f1_tv", cookie)
        self.assertEqual(stale.get("/api/tv/status", headers=page).status_code, 401)
        self.assertIn("ACCESS REQUEST", stale.get("/tv").text)
        # the used challenge again (with both of its secrets): nothing
        replay = self.e.internet("192.0.2.98")
        self.assertEqual(self.status(replay, ch).json()["status"], "none")
        for forged in ({"X-F1-TV-Page": "A" * 43}, {"X-F1-TV-Page": ch}, {}):
            f = self.e.internet("192.0.2.97")
            f.cookies.set("f1_tv", "B" * 43)
            self.assertEqual(f.get("/api/tv/status", headers=forged).status_code, 401)
        # a LAN /tv approval does not exist on the public host and vice versa (separate cookies per host)
        self.assertEqual(self.admin.get("/api/tv/status", headers=page).status_code, 401)

    # 4 TV API + video without authorization ----------------------------------------------------------
    def test_04_tv_api_and_video(self):
        live = self.d / "live"
        live.mkdir()
        (live / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\nlive_00001.ts\n")
        (live / "live_00001.ts").write_bytes(b"\x47" * 188)
        anon = self.e.internet()
        for q in ("", "?p=" + "A" * 43):
            self.assertEqual(anon.get("/tv/live/index.m3u8" + q).status_code, 401)
            self.assertEqual(anon.get("/tv/live/live_00001.ts" + q).status_code, 401)
        tv, page, _ch, _req = self.approved_public_tv()
        r = tv.get("/tv/live/index.m3u8", headers=page)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(tv.get("/tv/live/live_00001.ts", headers=page).status_code, 200)
        p = page["X-F1-TV-Page"]
        self.assertIn("live_00001.ts?p=", tv.get("/tv/live/index.m3u8", params={"p": p}).text)      # Safari
        self.assertEqual(tv.get("/tv/live/live_00001.ts", params={"p": p}).status_code, 200)
        self.assertEqual(tv.get("/tv/live/live_00002.ts", headers=page).status_code, 404)         # off air piece
        self.assertEqual(tv.get("/tv/live/other.ts", headers=page).status_code, 404)              # not allowlisted

    # 5 6 /remote from the internet --------------------------------------------------------------------
    def test_05_public_remote_without_trust_is_locked(self):
        visitor = self.e.internet("203.0.113.66")
        r = visitor.get("/remote")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)
        self.assertIn("'sha256-", r.headers["content-security-policy"])     # inline script by hash only
        self.assertIn("secure", r.headers["set-cookie"].lower())            # https through Funnel
        tv = self.e.internet("192.0.2.1")
        _ch, req = self.open_tv(tv)
        with visitor.websocket_connect("/ws?client=remote") as ws:
            got = [json.loads(ws.receive_text()) for _ in range(2)]
            self.assertEqual([m["type"] for m in got], ["device", "locked"])
            self.assertFalse(got[0]["trusted"])
            mode_before = self.e.app.state.mode.state()["selected_mode"]
            for m in ({"type": "tv_decide", "request": req["id"], "approve": True}, {"type": "key", "key": "UP"},
                      {"type": "mode", "value": "LIVE"}, {"type": "command", "command": "sync_reset"}):
                ws.send_text(json.dumps(m))
            ws.send_text(json.dumps({"type": "device_name", "name": "visitor"}))
            time.sleep(0.3)
        self.assertEqual(req["status"], "pending")                            # no approval
        self.assertEqual(self.e.app.state.mode.state()["selected_mode"], mode_before)
        # a remote token in a public URL is refused, the right one too (never in public URLs)
        self.assertEqual(visitor.get("/remote?token=" + TOKEN).status_code, 400)
        st, _ = asgi_call(self.e.pub, "/ws", kind="websocket", query=b"client=remote&token=" + TOKEN.encode())
        self.assertEqual(st, 4404)
        # /disk can't be reached to trust oneself
        self.assertEqual(visitor.post("/api/disk/security/trust", json={"device": "x"}).status_code, 404)

    def test_06_only_the_trusted_phone_approves_and_controls(self):
        phone, did = self.trusted_public_phone()
        other = self.e.internet("203.0.113.67")
        other.get("/remote")
        tv = self.e.internet("192.0.2.2")
        ch, req = self.open_tv(tv)
        with other.websocket_connect("/ws?client=remote") as ws:          # untrusted: told nothing
            self.assertEqual(json.loads(ws.receive_text())["type"], "device")
            self.assertEqual(json.loads(ws.receive_text())["type"], "locked")
            ws.send_text(json.dumps({"type": "tv_decide", "request": req["id"], "approve": True}))
        self.assertEqual(req["status"], "pending")
        with phone.websocket_connect("/ws?client=remote") as ws:          # the trusted phone, no token
            seen, decided = [], None
            for _ in range(40):
                m = json.loads(ws.receive_text())
                seen.append(m.get("type"))
                if m.get("type") == "tv_requests" and m["requests"] and decided is None:
                    self.assertEqual(m["requests"][0]["code"], req["code"])
                    ws.send_text(json.dumps({"type": "tv_decide", "request": req["id"], "approve": True}))
                    decided = True
                if m.get("type") == "tv_decided":
                    self.assertTrue(m["ok"])
                    break
            self.assertIn("hello", seen)                                     # the full remote
        self.assertEqual(self.status(tv, ch).json()["status"], "authenticated")
        # trust taken away at home: the public phone's control ends
        self.admin.post("/api/disk/security/untrust", headers=self.H)
        with phone.websocket_connect("/ws?client=remote") as ws:
            self.assertEqual(json.loads(ws.receive_text())["type"], "device")
            self.assertEqual(json.loads(ws.receive_text())["type"], "locked")

    # 7 /disk + admin / internal routes ----------------------------------------------------------------
    def test_07_private_routes_are_not_public(self):
        anon = self.e.internet()
        admin_pub = self.e.internet("198.51.100.1")
        admin_pub.cookies.set("f1_disk", self.admin.cookies.get("f1_disk"))   # even a real /disk session
        gets = ["/disk", "/disk/", "/disk-static/disk.js", "/disk-static/login.html", "/api/disk/status",
                "/api/disk/auth/state", "/api/disk/security", "/api/disk/log", "/api/disk/recordings", "/api/disk/voyo",
                "/api/voyo/recordings", "/api/voyo/recordings/abcd1234", "/api/voyo/recordings/abcd1234/files/manifest.json",
                "/api/health", "/api/diagnostics", "/api/state", "/api/sync", "/api/ui", "/api/mode", "/api/remote/info",
                "/f1tv/login", "/f1tv/status", "/tv/", "/static/remote.html", "/static/index.html", "/tv-static/index.html",
                "/tv-static/auth.html", "/tv-static/vendor/hls.js-LICENSE", "/api/remote/key", "/favicon.ico", "/robots.txt",
                "/api/radio/clips", "/api/radio/audio/0123456789ABCDEF", "/api/radio/audio/../clips"]
        posts = ["/api/disk/auth/login", "/api/disk/auth/setup", "/api/disk/security/decide", "/api/disk/security/trust",
                 "/api/disk/settings", "/api/disk/voyo", "/api/mode", "/api/remote/key", "/api/remote/command", "/api/sync/voyo",
                 "/api/voyo/player/status", "/api/voyo/player/log", "/api/track/choice", "/api/sync/session", "/api/sync/clear",
                 "/f1tv/callback", "/api/disk/auth/logout",
                 "/api/radio/transcript", "/api/radio/audio/0123456789abcdef"]
        for c in (anon, admin_pub):
            for path in gets:
                self.assertEqual(c.get(path).status_code, 404, path)
            for path in posts:
                self.assertEqual(c.post(path, json={"password": PW}).status_code, 404, path)
            self.assertEqual(c.put("/api/voyo/recordings/abcd1234/capture/a.mp4", content=b"x").status_code, 404)
        # and the bodies say nothing about why
        self.assertEqual(anon.get("/disk").json(), {"ok": False, "error": "not found"})

    def test_07_refusals_cannot_forge_log_lines(self):
        with self.assertLogs("security", level="WARNING") as logs:
            asgi_call(self.e.pub, "/x\nFAKE: admin logged in", raw=b"/x%0aFAKE:%20admin%20logged%20in",
                      headers=[(b"x-forwarded-for", b"203.0.113.77")])
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("\n", logs.output[0])
        self.assertIn("\\n", logs.output[0])                               # shown escaped

    # 8 path tricks, methods, hosts, queries -----------------------------------------------------------
    def test_08_ambiguous_paths_methods_hosts_queries(self):
        g = self.e.pub
        tricky = [("/tv", b"/%74v"), ("/tv/", b"/tv%2F"), ("/tv/live/../../auth", b"/tv/live/..%2F..%2Fauth"),
                  ("//tv", b"//tv"), ("/./tv", b"/./tv"), ("/tv/.", b"/tv/."), ("/static/../disk", b"/static/../disk"),
                  ("/disk", b"/%64isk"), ("/tv;x", b"/tv;x"), ("/tv\\", b"/tv\\"), ("/disk\x00", b"/disk%00"),
                  ("/../disk", b"/%2e%2e/disk"), ("/tv/live/index.m3u8/", b"/tv/live/index.m3u8/"),
                  ("/" + "a" * 300, ("/" + "a" * 300).encode()), ("/tv%20", b"/tv%20"), ("/tv", b"/t%76")]
        for path, raw in tricky:
            st, _ = asgi_call(g, path, raw=raw)
            self.assertIn(st, (400, 404), (path, raw))
        st, _ = asgi_call(g, "/tvé", raw="/tvé".encode("utf-8"))
        self.assertEqual(st, 400)
        self.assertEqual(asgi_call(g, "/TV")[0], 404)                       # case matters
        for m in ("HEAD", "PUT", "DELETE", "OPTIONS", "TRACE", "PATCH", "POST", "CONNECT"):
            self.assertEqual(asgi_call(g, "/tv", method=m)[0], 404, m)
        self.assertEqual(asgi_call(g, "/api/tv/auth/request", method="GET")[0], 404)
        self.assertEqual(asgi_call(g, "/tv/live/index.m3u8", method="POST")[0], 404)
        for host in ("evil.example", "127.0.0.1:8090", "192.168.10.140", HOST + ".evil.example", "", "x" + HOST,
                     HOST + ":8443:1"):
            self.assertEqual(asgi_call(g, "/tv", host=host)[0], 421, host)
        self.assertEqual(asgi_call(g, "/tv", host=HOST.upper() + ":8443")[0], 200)
        for q in (b"token=abc", b"next=/&token=x", b"debug=1", b"next=%2F&x=1", b"next=" + b"a" * 400, b"a=%ZZ",
                  b"next=/disk", b"next=https://evil.example"):
            self.assertEqual(asgi_call(g, "/tv", query=q)[0], 400, q)
        self.assertEqual(asgi_call(g, "/tv", query=b"next=/")[0], 200)

    # 9 WebSockets and streaming -----------------------------------------------------------------------
    def test_09_websockets_and_streaming_routes(self):
        g = self.e.pub
        for path in ("/tv", "/api/tv/status", "/tv/live/index.m3u8", "/disk", "/ws/", "/remote"):
            self.assertEqual(asgi_call(g, path, kind="websocket")[0], 4404, path)
        self.assertEqual(asgi_call(g, "/ws", kind="websocket", query=b"client=dashboard&x=1")[0], 4404)
        from starlette.websockets import WebSocketDisconnect
        anon = self.e.internet()
        with self.assertRaises(WebSocketDisconnect) as cm:                # the dashboard socket before approval
            with anon.websocket_connect("/ws") as ws:
                ws.receive_text()
        self.assertEqual(cm.exception.code, 4401)
        tv, page, _ch, _req = self.approved_public_tv()
        with tv.websocket_connect("/ws") as ws:                           # approved: the dashboard (read-only)
            self.assertEqual(json.loads(ws.receive_text())["type"], "hello")
            before = self.e.app.state.mode.state()["selected_mode"]
            ws.send_text(json.dumps({"type": "mode", "value": "LIVE" if before != "LIVE" else "VOD"}))
            time.sleep(0.3)
        self.assertEqual(self.e.app.state.mode.state()["selected_mode"], before)
        with self.assertRaises(WebSocketDisconnect) as cm:                # cross-site socket
            with tv.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_text()
        self.assertEqual(cm.exception.code, 4403)
        tv.get("/tv")                                                     # reloaded: the socket is closed again
        with self.assertRaises(WebSocketDisconnect):
            with tv.websocket_connect("/ws") as ws:
                ws.receive_text()

    def test_09_socket_caps(self):
        gw = PublicGateway(self.e.app, HOST, lambda s: True, Path("/nonexistent"), ws_per_client=1, ws_total=2)
        c = internet_client(gw, "203.0.113.5")
        self.admin.get("/remote")
        with c.websocket_connect("/ws?client=remote"):
            st, _ = asgi_call(gw, "/ws", kind="websocket", query=b"client=remote",
                              headers=[(b"x-forwarded-for", b"203.0.113.5")])
            self.assertEqual(st, 4404)                                     # the 2nd socket of that visitor
        gw2 = PublicGateway(self.e.app, HOST, lambda s: True, Path("/nonexistent"), ws_per_minute=3)
        xff = [(b"x-forwarded-for", b"203.0.113.6")]
        codes = [asgi_call(gw2, "/ws", kind="websocket", query=b"client=remote", headers=xff)[0] for _ in range(4)]
        self.assertEqual(codes[-1], 4404)                                 # reconnect churn is limited

    # 10 rate limits -----------------------------------------------------------------------------------
    def test_10_rate_limits(self):
        tv = self.e.internet("203.0.113.10")
        codes = [tv.post("/api/tv/auth/request").status_code for _ in range(8)]
        self.assertIn(429, codes)
        self.assertEqual(self.e.internet("203.0.113.11").post("/api/tv/auth/request").status_code, 200)   # per visitor
        # new /remote identities: at most 5 per visitor and hour
        made = 0
        for _ in range(7):
            c = self.e.internet("203.0.113.12")
            c.get("/remote")
            made += bool(c.cookies.get("f1_dev"))
        self.assertEqual(made, 5)
        # the gateway's own per-visitor request limit
        gw = PublicGateway(self.e.app, HOST, lambda s: True, Path("/nonexistent"), per_client=3)
        statuses = [asgi_call(gw, "/tv-static/tv.css", headers=[(b"x-forwarded-for", b"203.0.113.13")])[0] for _ in range(5)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(asgi_call(gw, "/tv-static/tv.css", headers=[(b"x-forwarded-for", b"203.0.113.14")])[0], 200)

    def test_10_visitors_never_look_local(self):
        self.assertEqual(PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"127.0.0.1")]}), "public")
        self.assertEqual(PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"::1")]}), "public")
        self.assertEqual(PublicGateway.client_ip({"headers": []}), "public")
        self.assertEqual(PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"bogus")]}), "public")
        self.assertEqual(PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"203.0.113.9")]}), "203.0.113.9")
        # Go prints IPv4 visitors over IPv6 as ::ffff:a.b.c.d - still that IPv4 visitor, and never "local"
        self.assertEqual(PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"::ffff:203.0.113.9")]}), "203.0.113.9")
        self.assertEqual(PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"::ffff:127.0.0.1")]}), "public")
        # IPv6: one visitor = its /64 (rotating addresses inside it does not reset the limits)
        a = PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"2001:db8:1:2::1")]})
        b = PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"2001:db8:1:2:ffff::9")]})
        self.assertEqual((a, b), ("2001:db8:1:2::/64", "2001:db8:1:2::/64"))
        self.assertNotEqual(a, PublicGateway.client_ip({"headers": [(b"x-forwarded-for", b"2001:db8:1:3::1")]}))
        # the LAN app gives this machine extras (the remote token, the recorder state) - never through the gateway
        local = self.e.internet("127.0.0.1")
        self.assertEqual(local.get("/api/remote/info").status_code, 404)
        self.assertEqual(local.get("/api/health").status_code, 404)
        with self.assertRaises(Exception):
            with local.websocket_connect("/ws?client=remote&token=" + TOKEN) as ws:
                ws.receive_text()

    # 11 expired and replayed challenges ---------------------------------------------------------------
    def test_11_expired_and_replayed(self):
        _phone, did = self.trusted_public_phone()
        tv = self.e.internet("192.0.2.3")
        ch, req = self.open_tv(tv)
        req["expires"] = time.time() - 1
        self.assertEqual(self.status(tv, ch).json()["status"], "expired")
        with self.assertRaises(ValueError):
            self.e.sec.decide(req["id"], True, by_device=did)
        tv2 = self.e.internet("192.0.2.4")
        ch2, req2 = self.open_tv(tv2)
        saved = tv2.cookies.get("f1_tvreq")
        self.e.sec.decide(req2["id"], True, by_device=did)
        self.assertEqual(self.status(tv2, ch2).json()["status"], "authenticated")
        thief = self.e.internet("192.0.2.5")
        thief.cookies.set("f1_tvreq", saved)
        r = self.status(thief, ch2)
        self.assertEqual(r.json()["status"], "consumed")
        self.assertNotIn("page", r.json())

    # 12 HTTPS / browser behaviour through the Funnel hostname -----------------------------------------
    def test_12_cookies_origin_and_headers_through_funnel(self):
        tv = self.e.internet("192.0.2.6")
        tv.get("/tv")
        r = tv.post("/api/tv/auth/request")
        sc = r.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(flag, sc)
        self.assertNotIn("domain=", sc)                                    # host-only cookie
        self.assertEqual(r.headers["cache-control"], "no-store")
        # cross-site POST / socket refused (Origin is another site)
        self.assertEqual(tv.post("/api/tv/auth/request", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(tv.post("/api/tv/auth/request", headers={"origin": "null"}).status_code, 403)
        # same site through Funnel (Origin = the public host with its port) works
        self.assertEqual(tv.post("/api/tv/auth/request", headers={"origin": PUBLIC}).status_code, 200)
        r = tv.get("/static/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["strict-transport-security"], "max-age=15552000")
        _tv2, page, _c, _r = self.approved_public_tv()
        dash = _tv2.get("/?layout=RACE_VIEW")
        self.assertEqual(dash.status_code, 200)
        csp = dash.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("wss://" + HOST + ":8443", csp)
        self.assertEqual(dash.headers["x-frame-options"], "SAMEORIGIN")
        # nothing secret in anything the public side serves
        for c, path in ((_tv2, "/"), (_tv2, "/static/app.js"), (tv, "/tv"), (tv, "/tv-static/tv.js"), (tv, "/remote")):
            self.assertNotIn(TOKEN, c.get(path).text, path)

    # 13 LAN keeps working --------------------------------------------------------------------------
    def test_13_lan_unchanged(self):
        lan = self.e.lan()
        self.assertEqual(lan.get("/").status_code, 200)                    # the plain dashboard as before
        self.assertEqual(lan.get("/api/state").status_code, 200)
        self.assertEqual(lan.get("/disk").status_code, 200)
        self.assertNotIn("content-security-policy", lan.get("/tv").headers)
        self.assertEqual(lan.post("/api/remote/key?token=" + TOKEN, json={"key": "UP"}).status_code, 200)
        with lan.websocket_connect("/ws?client=remote&token=" + TOKEN) as ws:   # LAN remote with its token
            self.assertEqual(json.loads(ws.receive_text())["type"], "hello")
        import server.app as appmod
        with mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}):   # the player runs on this machine
            self.assertEqual(lan.post("/api/voyo/player/status", content=json.dumps({"state": "idle"})).status_code, 200)


if __name__ == "__main__":
    unittest.main()
