"""Security boundaries of /tv, /disk and /remote - tested against the server's HTTP / WebSocket API
directly (no UI): someone who knows the URLs must not get in.

Run (from main/):  python -m unittest tests.test_security
"""
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import load_config  # noqa: E402
from server.security import RateLimiter, Security, hash_password, verify_password  # noqa: E402

PW = "correct horse battery"


class Server:
    """A real app (create_app) on a temp data dir; one TestClient per "browser"."""

    def __init__(self, d: Path, protect_dashboard: bool = False, remote_token: str = ""):
        from starlette.testclient import TestClient
        import server.app as appmod
        self.d = d
        env = {"F1DASH_VOYO_SERVER_PLAYER_ENABLED": "true", "F1DASH_VOYO_RECORDING_PATH": str(d / "rec"),
               "F1DASH_VOYO_RECORDING_REQUIRE_MOUNT": "", "F1DASH_VOYO_RECORDING_MOUNT_MARKER": "",
               "F1DASH_VOYO_RECORDING_MIN_FREE_BYTES": "0", "F1DASH_REMOTE_TOKEN": remote_token,
               "F1DASH_SECURITY_PROTECT_DASHBOARD": "true" if protect_dashboard else "false"}
        self._patches = [mock.patch.dict(os.environ, env), mock.patch.object(appmod, "DATA_DIR", d)]
        for p in self._patches:
            p.start()
        cfg = load_config()
        cfg["source"]["mode"] = "test"
        self.app = appmod.create_app(cfg)
        self.sec = self.app.state.security
        self.TC = TestClient

    def browser(self):
        return self.TC(self.app)

    def close(self):
        for p in self._patches:
            p.stop()

    # helpers
    def setup_password(self, c, pw=PW):
        code = (self.d / "auth" / "disk-setup-code").read_text().strip()
        r = c.post("/api/disk/auth/setup", json={"code": code, "password": pw, "confirm": pw})
        assert r.status_code == 200, r.text
        return c.get("/api/disk/auth/state").json()["csrf"]

    def remote_device(self, c):
        """A phone: opens /remote (gets its device cookie) -> its device id."""
        assert c.get("/remote").status_code == 200
        tok = c.cookies.get("f1_dev")
        return self.sec.device(tok)[0], tok


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.s = Server(self.d)
        self.admin = self.s.browser()
        self.csrf = self.s.setup_password(self.admin)
        self.H = {"X-F1-CSRF": self.csrf}

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def trust(self, did):
        r = self.admin.post("/api/disk/security/trust", json={"device": did}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.text)

    def tv_request(self, tv):
        r = tv.post("/api/tv/auth/request")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json(), next(x for x in self.s.sec.requests.values() if x["status"] == "pending")

    def ws_decide(self, phone_tok, rid, approve=True, origin=None):
        tc = self.s.browser()
        headers = {"cookie": f"f1_dev={phone_tok}"}
        if origin:
            headers["origin"] = origin
        with tc.websocket_connect("/ws?client=remote", headers=headers) as ws:
            seen = []
            ws.send_text(json.dumps({"type": "tv_decide", "request": rid, "approve": approve}))
            for _ in range(30):
                m = json.loads(ws.receive_text())
                seen.append(m)
                if m.get("type") == "tv_decided":
                    return m, seen
        return None, seen

    # 1 2 ---------------------------------------------------------------------------------------
    def test_01_02_tv_without_session_gets_only_the_request_page(self):
        tv = self.s.browser()
        page = tv.get("/tv").text
        self.assertIn("ACCESS REQUEST", page)
        self.assertNotIn('id="dash"', page)                                 # not the TV app
        for path in ("/api/tv/status", "/tv/live/index.m3u8", "/tv/live/live_00001.ts"):
            self.assertEqual(tv.get(path).status_code, 401, path)

    # 3 4 5 6 -----------------------------------------------------------------------------------
    def test_03_to_06_approval_flow(self):
        phone_b = self.s.browser()                                          # untrusted remote
        did_b, tok_b = self.s.remote_device(phone_b)
        phone_c = self.s.browser()                                          # the trusted one
        did_c, tok_c = self.s.remote_device(phone_c)
        self.trust(did_c)
        tv = self.s.browser()
        info, req = self.tv_request(tv)
        self.assertTrue(info["approver_set"])
        # 4: an untrusted /remote device cannot approve - and is not even told about the request
        m, seen = self.ws_decide(tok_b, req["id"])
        self.assertFalse(m["ok"])
        self.assertIn("not the trusted approver", m["error"])
        self.assertFalse(any(x.get("type") == "tv_requests" for x in seen))
        self.assertEqual(tv.get("/api/tv/auth/status").json()["status"], "pending")
        # a random browser sending a forged decision over HTTP: there is no such public endpoint
        self.assertIn(self.s.browser().post("/api/disk/security/decide", json={"request": req["id"], "approve": True})
                      .status_code, (401, 403))
        # 5: the trusted device approves (it sees the request with its code first)
        m, seen = self.ws_decide(tok_c, req["id"])
        self.assertTrue(m["ok"], m)
        self.assertTrue(any(x.get("type") == "tv_requests" and x["requests"] for x in seen))
        st = tv.get("/api/tv/auth/status")
        self.assertEqual(st.json()["status"], "authenticated")
        set_cookie = st.headers.get("set-cookie", "")
        self.assertIn("f1_tv=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("samesite=lax", set_cookie.lower())
        self.assertIn('id="dash"', tv.get("/tv").text)
        self.assertEqual(tv.get("/api/tv/status").status_code, 200)
        # the approval is one-time: the same request cannot mint a second session
        self.assertEqual(self.s.browser().get("/api/tv/auth/status").json()["status"], "none")
        # 6: refresh keeps the session
        self.assertIn('id="dash"', tv.get("/tv").text)

    def test_03_tv_browser_cannot_approve_itself(self):
        phone = self.s.browser()
        did, tok = self.s.remote_device(phone)
        self.trust(did)
        # the trusted phone itself opens /tv -> its own request
        _info, req = self.tv_request(phone)
        m, _seen = self.ws_decide(tok, req["id"])
        self.assertFalse(m["ok"])
        self.assertIn("own", m["error"])
        # the /disk admin approving the request from the same browser: refused too
        _i2, req2 = self.tv_request(self.admin)
        self.assertEqual(self.admin.post("/api/tv/auth/request").json().get("authenticated"), True)   # admin has /disk
        tv = self.s.browser()
        _i3, req3 = self.tv_request(tv)
        r = tv.post("/api/disk/security/decide", json={"request": req3["id"], "approve": True})
        self.assertEqual(r.status_code, 401)                                 # the TV has no /disk session
        self.assertEqual(tv.get("/api/tv/auth/status").json()["status"], "pending")

    # 7 8 ---------------------------------------------------------------------------------------
    def _approved_tv(self):
        phone = self.s.browser()
        did, tok = self.s.remote_device(phone)
        self.trust(did)
        tv = self.s.browser()
        _info, req = self.tv_request(tv)
        self.ws_decide(tok, req["id"])
        tv.get("/api/tv/auth/status")
        self.assertEqual(tv.get("/api/tv/status").status_code, 200)
        return tv

    def test_07_tv_session_expires_and_is_revoked(self):
        tv = self._approved_tv()
        for rec in self.s.sec.data["tv_sessions"].values():
            rec["expires"] = time.time() - 1
        self.assertEqual(tv.get("/api/tv/status").status_code, 401)
        self.assertIn("ACCESS REQUEST", tv.get("/tv").text)
        tv2 = self._approved_tv()
        sid = self.admin.get("/api/disk/security").json()["tv_sessions"][0]["id"]
        self.assertEqual(self.admin.post("/api/disk/security/revoke_tv", json={"session": sid}, headers=self.H).status_code, 200)
        self.assertEqual(tv2.get("/api/tv/status").status_code, 401)

    def test_08_logout_invalidates_server_side(self):
        tv = self._approved_tv()
        stolen = tv.cookies.get("f1_tv")
        self.assertEqual(tv.post("/api/tv/logout").status_code, 200)
        other = self.s.browser()
        other.cookies.set("f1_tv", stolen)                                  # a copied cookie is dead too
        self.assertEqual(other.get("/api/tv/status").status_code, 401)
        # /disk logout
        d_tok = self.admin.cookies.get("f1_disk")
        self.assertEqual(self.admin.post("/api/disk/auth/logout", headers=self.H).status_code, 200)
        thief = self.s.browser()
        thief.cookies.set("f1_disk", d_tok)
        self.assertEqual(thief.get("/api/disk/status").status_code, 401)

    # 9 10 11 12 --------------------------------------------------------------------------------
    def test_09_to_12_disk_password(self):
        d2 = Path(tempfile.mkdtemp())
        s2 = Server(d2)
        try:
            c = s2.browser()
            self.assertIn("/DISK SETUP", c.get("/disk").text)               # first launch: setup page
            self.assertFalse(c.get("/api/disk/auth/state").json()["password_set"])
            code_file = d2 / "auth" / "disk-setup-code"
            self.assertEqual(stat.S_IMODE(code_file.stat().st_mode), 0o600)
            # knowing the URL is not enough: the setup code is needed
            r = c.post("/api/disk/auth/setup", json={"code": "AAA-BBBBBBB", "password": PW, "confirm": PW})
            self.assertEqual(r.status_code, 403)
            r = c.post("/api/disk/auth/setup", json={"code": code_file.read_text(), "password": "short", "confirm": "short"})
            self.assertEqual(r.status_code, 400)
            s2.setup_password(c)
            self.assertFalse(code_file.exists())                              # one-time
            self.assertEqual(c.post("/api/disk/auth/setup", json={"code": "x", "password": PW, "confirm": PW}).status_code, 409)
            # 10: no plaintext, Argon2id, file 600, the hash never leaves the server
            raw = (d2 / "auth" / "security.json").read_text()
            self.assertNotIn(PW, raw)
            self.assertIn("$argon2id$", raw)
            self.assertEqual(stat.S_IMODE((d2 / "auth" / "security.json").stat().st_mode), 0o600)
            self.assertNotIn("argon2", json.dumps(c.get("/api/disk/auth/state").json()))
            self.assertNotIn("argon2", c.get("/api/disk/status").text)
            # 11 12
            fresh = s2.browser()
            self.assertIn("/DISK LOGIN", fresh.get("/disk").text)
            self.assertEqual(fresh.post("/api/disk/auth/login", json={"password": "wrong password!"}).status_code, 401)
            self.assertEqual(fresh.get("/api/disk/status").status_code, 401)
            r = fresh.post("/api/disk/auth/login", json={"password": PW})
            self.assertEqual(r.status_code, 200)
            self.assertIn("samesite=strict", r.headers["set-cookie"].lower())
            self.assertIn("httponly", r.headers["set-cookie"].lower())
            self.assertEqual(fresh.get("/api/disk/status").status_code, 200)
            self.assertIn("RECORDER · DISK", fresh.get("/disk").text)
        finally:
            s2.close()
            shutil.rmtree(d2, ignore_errors=True)

    # 13 14 -------------------------------------------------------------------------------------
    def test_13_14_disk_api_needs_session_csrf_origin_and_reauth(self):
        anon = self.s.browser()
        for path in ("/api/disk/status", "/api/disk/log", "/api/disk/recordings", "/api/disk/voyo", "/api/disk/security",
                     "/api/voyo/recordings", "/api/voyo/recordings/abcd1234",
                     "/api/voyo/recordings/abcd1234/files/manifest.json"):
            self.assertEqual(anon.get(path).status_code, 401, path)
        destructive = [("/api/disk/recordings/abcd1234/delete?what=all", {}), ("/api/disk/settings", {"keep_race_days": 0}),
                       ("/api/disk/voyo", {"email": "a@b.si"}), ("/api/disk/voyo/forget", {}),
                       ("/api/disk/security/trust", {"device": "x"}), ("/api/disk/security/revoke_all_tv", {}),
                       ("/api/disk/auth/logout", {}), ("/api/disk/auth/password", {"current": PW, "password": "x" * 12})]
        for path, body in destructive:
            self.assertEqual(anon.post(path, json=body).status_code, 401, path)
            # a session alone is not enough: CSRF token header
            self.assertEqual(self.admin.post(path, json=body).status_code, 403, path)
            # wrong CSRF
            self.assertEqual(self.admin.post(path, json=body, headers={"X-F1-CSRF": "guess"}).status_code, 403, path)
            # right CSRF but another site
            self.assertEqual(self.admin.post(path, json=body, headers={**self.H, "origin": "http://evil.example"}).status_code,
                             403, path)
        # destructive with an old login: the password again
        for rec in self.s.sec.data["disk_sessions"].values():
            rec["auth_at"] = time.time() - 3600
        r = self.admin.post("/api/disk/recordings/abcd1234/delete?what=all", headers=self.H)
        self.assertEqual(r.json()["error"], "reauth_required")
        self.assertEqual(self.admin.post("/api/disk/auth/reauth", json={"password": "nope nope"}, headers=self.H).status_code, 401)
        self.assertEqual(self.admin.post("/api/disk/auth/reauth", json={"password": PW}, headers=self.H).status_code, 200)
        self.assertEqual(self.admin.post("/api/disk/recordings/abcd1234/delete?what=all", headers=self.H).status_code, 404)
        # the PC's capture upload keeps its own machine auth (remote token / local), not a browser session
        self.assertNotEqual(anon.put("/api/voyo/recordings/abcd1234/capture/a.mp4", content=b"x").status_code, 200)

    # 15 ----------------------------------------------------------------------------------------
    def test_15_client_supplied_flags_and_forged_tokens_mean_nothing(self):
        anon = self.s.browser()
        for hdr in ({"authorized": "true"}, {"x-role": "admin"}, {"x-device": "remote"}, {"x-remote-token": ""}):
            self.assertEqual(anon.get("/api/disk/status", headers=hdr).status_code, 401)
        self.assertEqual(anon.get("/api/disk/status?authorized=true&role=admin").status_code, 401)
        for name in ("f1_disk", "f1_tv"):
            forged = self.s.browser()
            forged.cookies.set(name, "A" * 43)
            self.assertEqual(forged.get("/api/disk/status").status_code, 401)
            self.assertEqual(forged.get("/api/tv/status").status_code, 401)
        # the TV's request cookie is not a session
        tv = self.s.browser()
        self.tv_request(tv)
        self.assertEqual(tv.get("/api/tv/status").status_code, 401)
        # tokens are random and only their hash is stored
        raw = (self.d / "auth" / "security.json").read_text()
        self.assertNotIn(self.admin.cookies.get("f1_disk"), raw)

    # 16 ----------------------------------------------------------------------------------------
    def test_16_websockets(self):
        c = self.s.browser()
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as cm:
            with c.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
                ws.receive_text()
        self.assertEqual(cm.exception.code, 4403)                          # cross-site WebSocket hijacking
        # a /remote socket without a device cookie: works for the remote, but has no device to approve with
        with c.websocket_connect("/ws?client=remote") as ws:
            ws.send_text(json.dumps({"type": "tv_decide", "request": "x", "approve": True}))
            ws.send_text(json.dumps({"type": "ping"}))

    def test_16_protect_dashboard_locks_page_api_and_websocket(self):
        d2 = Path(tempfile.mkdtemp())
        s2 = Server(d2, protect_dashboard=True)
        try:
            anon = s2.browser()
            r = anon.get("/", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/tv?next=/"))
            self.assertEqual(anon.get("/api/state").status_code, 401)
            self.assertEqual(anon.get("/api/sync").status_code, 401)
            from starlette.websockets import WebSocketDisconnect
            with self.assertRaises(WebSocketDisconnect) as cm:
                with anon.websocket_connect("/ws") as ws:
                    ws.receive_text()
            self.assertEqual(cm.exception.code, 4401)
            self.assertEqual(anon.get("/remote").status_code, 200)         # the remote stays reachable
            admin = s2.browser()
            s2.setup_password(admin)
            self.assertEqual(admin.get("/api/state").status_code, 200)
            with admin.websocket_connect("/ws") as ws:
                self.assertEqual(json.loads(ws.receive_text())["type"], "hello")
        finally:
            s2.close()
            shutil.rmtree(d2, ignore_errors=True)

    # 17 ----------------------------------------------------------------------------------------
    def test_17_login_rate_limit(self):
        c = self.s.browser()
        codes = [c.post("/api/disk/auth/login", json={"password": f"wrong {i} xxxxx"}).status_code for i in range(5)]
        self.assertEqual(codes, [401] * 5)
        r = c.post("/api/disk/auth/login", json={"password": PW})         # even the right one: locked out
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        rl = RateLimiter(2, 60, 60)
        rl.hit("a")
        rl.hit("a")
        self.assertGreater(rl.blocked("a"), 0)
        self.assertEqual(rl.blocked("b"), 0)

    def test_17_tv_request_rate_limit(self):
        tv = self.s.browser()
        codes = [tv.post("/api/tv/auth/request").status_code for _ in range(8)]
        self.assertIn(429, codes)

    # 18 ----------------------------------------------------------------------------------------
    def test_18_requests_expire(self):
        phone = self.s.browser()
        did, tok = self.s.remote_device(phone)
        self.trust(did)
        tv = self.s.browser()
        _info, req = self.tv_request(tv)
        req["expires"] = time.time() - 1
        self.assertEqual(tv.get("/api/tv/auth/status").json()["status"], "expired")
        m, _ = self.ws_decide(tok, req["id"])
        self.assertFalse(m["ok"])
        self.assertEqual(tv.get("/api/tv/status").status_code, 401)
        # denied
        tv2 = self.s.browser()
        _i, req2 = self.tv_request(tv2)
        self.ws_decide(tok, req2["id"], approve=False)
        self.assertEqual(tv2.get("/api/tv/auth/status").json()["status"], "denied")

    # 19 ----------------------------------------------------------------------------------------
    def test_19_restart_keeps_sessions_and_trust_drops_pending_requests(self):
        tv = self._approved_tv()
        tv_tok, disk_tok = tv.cookies.get("f1_tv"), self.admin.cookies.get("f1_disk")
        trusted = self.s.sec.data["trusted_device"]
        pending_tv = self.s.browser()
        self.tv_request(pending_tv)
        self.s.close()
        s2 = Server(self.d)                                                 # the server restarts
        try:
            a, b = s2.browser(), s2.browser()
            a.cookies.set("f1_tv", tv_tok)
            b.cookies.set("f1_disk", disk_tok)
            self.assertEqual(a.get("/api/tv/status").status_code, 200)
            self.assertEqual(b.get("/api/disk/status").status_code, 200)
            self.assertEqual(s2.sec.data["trusted_device"], trusted)
            self.assertEqual(s2.sec.pending(), [])                            # requests are memory-only
            self.assertFalse((self.d / "auth" / "disk-setup-code").exists())  # no new setup code
        finally:
            s2.close()
            self.s = Server(self.d)                                           # tearDown closes this one

    # 20 ----------------------------------------------------------------------------------------
    def test_20_remote_still_works(self):
        c = self.s.browser()
        self.assertEqual(c.get("/remote").status_code, 200)
        with c.websocket_connect("/ws?client=remote", headers={"cookie": f"f1_dev={c.cookies.get('f1_dev')}"}) as ws:
            types = [json.loads(ws.receive_text()).get("type") for _ in range(3)]
            self.assertIn("hello", types)
            ws.send_text(json.dumps({"type": "device_name", "name": "Nik's iPhone"}))
            for _ in range(20):
                m = json.loads(ws.receive_text())
                if m.get("type") == "device" and m.get("name") == "Nik's iPhone":
                    break
            else:
                self.fail("rename not confirmed")
            self.assertFalse(m["trusted"])
            ws.send_text(json.dumps({"type": "key", "key": "MOVE_DOWN"}))      # remote commands as before
        r = c.post("/api/remote/key", json={"key": "UP"})
        self.assertIn(r.status_code, (200, 202))

    # review findings ----------------------------------------------------------------------------
    def test_review_remote_token_not_leaked_and_cross_site_posts_refused(self):
        self.s.close()
        d2 = Path(tempfile.mkdtemp())
        s2 = Server(d2, remote_token="s3cret-remote")
        try:
            anon = s2.browser()
            info = anon.get("/api/remote/info").json()
            self.assertNotIn("s3cret-remote", json.dumps(info))             # was: the token for anyone
            self.assertTrue(info["token_hidden"])
            admin = s2.browser()
            s2.setup_password(admin)
            self.assertIn("s3cret-remote", json.dumps(admin.get("/api/remote/info").json()))
            # CSRF on the plain dashboard's state-changing endpoints (another website posting)
            r = anon.post("/api/mode", json={"mode": "LIVE"}, headers={"origin": "http://evil.example"})
            self.assertEqual(r.status_code, 403)
            r = anon.post("/api/sync/clear", headers={"origin": "null"})
            self.assertEqual(r.status_code, 403)
            # tools without an Origin header still work as before (the token decides)
            self.assertEqual(anon.post("/api/remote/key", json={"key": "UP"}).status_code, 401)        # no token
            self.assertEqual(anon.post("/api/remote/key?token=s3cret-remote", json={"key": "UP"}).status_code, 200)
        finally:
            s2.close()
            shutil.rmtree(d2, ignore_errors=True)
            self.s = Server(self.d)

    # logging ------------------------------------------------------------------------------------
    def test_secrets_never_logged(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            c = self.s.browser()
            c.post("/api/disk/auth/login", json={"password": "wrong secret password"})
            c.post("/api/disk/auth/login", json={"password": PW})
            phone = self.s.browser()
            did, tok = self.s.remote_device(phone)
            self.trust(did)
            tv = self.s.browser()
            _info, req = self.tv_request(tv)
            self.ws_decide(tok, req["id"])
            tv.get("/api/tv/auth/status")
        text = "\n".join(logs.output)
        for secret in (PW, "wrong secret password", c.cookies.get("f1_disk"), tok, tv.cookies.get("f1_tv")):
            self.assertNotIn(secret, text)
        self.assertIn("/tv authorization APPROVED", text)
        self.assertIn("/disk login FAILED", text)
        act = (self.d / "logs" / "activity.jsonl").read_text()
        self.assertNotIn(PW, act)
        self.assertNotIn(tok, act)


class UnitTest(unittest.TestCase):
    def test_password_hash(self):
        h = hash_password("a long password")
        self.assertTrue(h.startswith("$argon2id$"))
        self.assertTrue(verify_password(h, "a long password"))
        self.assertFalse(verify_password(h, "a long passworD"))
        self.assertNotEqual(hash_password("a long password"), h)              # random salt
        self.assertFalse(verify_password("", "x"))

    def test_disk_admin_cannot_approve_request_from_own_browser(self):
        d = Path(tempfile.mkdtemp())
        try:
            sec = Security(d)
            _poll, r = sec.create_request("TV", None, "abc")
            with self.assertRaises(PermissionError):
                sec.decide(r["id"], True, by_disk="abc")
            self.assertEqual(sec.decide(r["id"], True, by_disk="other"), "approved")
            with self.assertRaises(ValueError):
                sec.decide(r["id"], True, by_disk="other")                   # once
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
