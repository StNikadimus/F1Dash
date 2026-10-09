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
sys.path.insert(0, str(Path(__file__).resolve().parent))

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

    # ---- one /tv page load = GET /tv + a new challenge (what server/tv/tv.js does)
    def tv_open(self, tv):
        page = tv.get("/tv")
        self.assertEqual(page.status_code, 200)
        self.assertIn("ACCESS REQUEST", page.text)
        self.assertIn("no-store", page.headers["cache-control"])
        r = tv.post("/api/tv/auth/request")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        req = next(x for x in self.s.sec.requests.values() if x["code"] == b["code"])
        return b["challenge"], req

    def tv_status(self, tv, challenge):
        return tv.post("/api/tv/auth/status", headers={"X-F1-TV-Challenge": challenge})

    def phone(self, trusted=True):
        c = self.s.browser()
        did, tok = self.s.remote_device(c)
        if trusted:
            self.trust(did)
        return did, tok

    def approved_tv(self, tv=None):
        """-> (browser, its page headers) - one approved /tv page load."""
        if not self.s.sec.data.get("trusted_device"):
            self.phone()
        tv = tv or self.s.browser()
        ch, req = self.tv_open(tv)
        self.assertEqual(self.s.sec.decide(req["id"], True, by_device=self.s.sec.data["trusted_device"]), "approved")
        st = self.tv_status(tv, ch)
        self.assertEqual(st.json()["status"], "authenticated", st.text)
        return tv, {"X-F1-TV-Page": st.json()["page"]}

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

    def assert_tv_denied(self, client, headers=None):
        for path in ("/api/tv/status", "/tv/live/index.m3u8", "/tv/live/live_00001.ts"):
            self.assertEqual(client.get(path, headers=headers or {}).status_code, 401, path)

    # 1 2 ---------------------------------------------------------------------------------------
    def test_01_02_tv_load_serves_no_tv_content_and_apis_are_closed(self):
        tv = self.s.browser()
        page = tv.get("/tv").text
        self.assertIn("ACCESS REQUEST", page)
        self.assertIn('class="locked"', page)                              # the app is not started
        self.assertNotIn("/?layout", page)                                 # no dashboard iframe source
        self.assertNotIn("index.m3u8", page)
        self.assert_tv_denied(tv)
        # a pending challenge is not access either
        ch, _req = self.tv_open(tv)
        self.assert_tv_denied(tv)
        self.assert_tv_denied(tv, {"X-F1-TV-Page": ch})
        self.assertEqual(self.tv_status(tv, ch).json()["status"], "pending")

    # 3 4 5 -------------------------------------------------------------------------------------
    def test_03_to_05_only_the_designated_phone_approves_this_exact_challenge(self):
        did_b, tok_b = self.phone(trusted=False)                            # connected, not designated
        did_c, tok_c = self.phone()                                        # USE FOR AUTH in /disk
        tv = self.s.browser()
        ch, req = self.tv_open(tv)
        # an untrusted /remote device cannot approve - and is not even told about the request
        m, seen = self.ws_decide(tok_b, req["id"])
        self.assertFalse(m["ok"])
        self.assertIn("not the trusted approver", m["error"])
        self.assertFalse(any(x.get("type") == "tv_requests" for x in seen))
        self.assertEqual(self.tv_status(tv, ch).json()["status"], "pending")
        # /disk can not approve (not the phone), nor can an anonymous browser
        r = self.admin.post("/api/disk/security/decide", json={"request": req["id"], "approve": True}, headers=self.H)
        self.assertEqual(r.status_code, 403)
        self.assertIn(self.s.browser().post("/api/disk/security/decide", json={"request": req["id"], "approve": True})
                      .status_code, (401, 403))
        self.assertEqual(self.tv_status(tv, ch).json()["status"], "pending")
        # the trusted phone sees it (with its code) and approves it
        m, seen = self.ws_decide(tok_c, req["id"])
        self.assertTrue(m["ok"], m)
        self.assertTrue(any(x.get("type") == "tv_requests" and x["requests"][0]["code"] == req["code"] for x in seen))
        saved_req_cookie = tv.cookies.get("f1_tvreq")
        st = self.tv_status(tv, ch)
        self.assertEqual(st.json()["status"], "authenticated")
        page = {"X-F1-TV-Page": st.json()["page"]}
        sc = st.headers.get("set-cookie", "").lower()
        self.assertIn("f1_tv=", sc)
        self.assertIn("httponly", sc)
        self.assertIn("samesite=strict", sc)
        self.assertNotIn("max-age", sc.split("f1_tv=")[1].split(",")[0])  # a browser-session cookie
        self.assertNotIn("expires", sc.split("f1_tv=")[1].split(",")[0])
        self.assertEqual(tv.get("/api/tv/status", headers=page).status_code, 200)
        # the cookie alone, the page secret alone: nothing
        self.assertEqual(tv.get("/api/tv/status").status_code, 401)
        other = self.s.browser()
        self.assertEqual(other.get("/api/tv/status", headers=page).status_code, 401)
        # the approval is used up: the same challenge cannot mint a second session (replay)
        self.assertEqual(self.tv_status(tv, ch).json()["status"], "none")             # its cookie was cleared
        self.assertEqual(self.tv_status(other, ch).json()["status"], "none")          # without the browser cookie
        replay = self.s.browser()                                                      # both secrets copied
        replay.cookies.set("f1_tvreq", saved_req_cookie)
        r = self.tv_status(replay, ch)
        self.assertEqual(r.json()["status"], "consumed")
        self.assertNotIn("page", r.json())
        self.assertNotIn("f1_tv=", r.headers.get("set-cookie", ""))
        self.assertEqual(len(self.s.sec.tv_pages), 1)
        m, _ = self.ws_decide(tok_c, req["id"])                                        # approving it again
        self.assertFalse(m["ok"])

    def test_03_tv_browser_cannot_approve_itself(self):
        did, tok = self.phone()
        # the trusted phone itself opens /tv -> its own request
        phone_browser = self.s.browser()
        phone_browser.cookies.set("f1_dev", tok)
        _ch, req = self.tv_open(phone_browser)
        m, _seen = self.ws_decide(tok, req["id"])
        self.assertFalse(m["ok"])
        self.assertIn("own", m["error"])

    # 6: reload / new tab / new browser start from zero ------------------------------------------
    def test_06_every_load_of_tv_needs_a_new_approval(self):
        tv, page = self.approved_tv()
        old_cookie = tv.cookies.get("f1_tv")
        self.assertEqual(tv.get("/api/tv/status", headers=page).status_code, 200)
        # reload: the server serves the request screen again and ends the previous authorization
        r = tv.get("/tv")
        self.assertIn("ACCESS REQUEST", r.text)
        self.assertNotIn("/?layout", r.text)
        self.assert_tv_denied(tv, page)
        stale = self.s.browser()                                            # the old cookie + page secret anywhere
        stale.cookies.set("f1_tv", old_cookie)
        self.assert_tv_denied(stale, page)
        self.assertIn("ACCESS REQUEST", stale.get("/tv").text)
        # a "closed and reopened" browser (the cookie jar survives, the page memory does not) - same story
        tv2, page2 = self.approved_tv(tv)
        restored = self.s.browser()
        restored.cookies.set("f1_tv", tv2.cookies.get("f1_tv"))
        self.assertEqual(restored.get("/api/tv/status").status_code, 401)  # no page secret
        self.assertIn("ACCESS REQUEST", restored.get("/tv").text)          # and loading /tv ends it
        self.assert_tv_denied(tv2, page2)
        # the URL in another browser / private window: nothing carries over
        self.assertIn("ACCESS REQUEST", self.s.browser().get("/tv").text)

    def test_06_two_tabs_one_browser_and_concurrent_challenges_are_isolated(self):
        self.phone()
        trusted = self.s.sec.data["trusted_device"]
        # tab A approved; tab B (same browser = same cookies) asks -> A's authorization ends
        browser = self.s.browser()
        _b, page_a = self.approved_tv(browser)
        ch_b, req_b = self.tv_open(browser)
        self.assert_tv_denied(browser, page_a)
        self.s.sec.decide(req_b["id"], True, by_device=trusted)
        page_b = {"X-F1-TV-Page": self.tv_status(browser, ch_b).json()["page"]}
        self.assertEqual(browser.get("/api/tv/status", headers=page_b).status_code, 200)
        self.assertEqual(browser.get("/api/tv/status", headers=page_a).status_code, 401)   # B's cookie, A's page
        # three TVs at once: approving the middle one authorizes only it
        tvs = [self.s.browser() for _ in range(3)]
        opened = [self.tv_open(t) for t in tvs]
        self.assertEqual(len({o[1]["id"] for o in opened}), 3)
        self.assertEqual(len({o[1]["code"] for o in opened}), 3)
        self.s.sec.decide(opened[1][1]["id"], True, by_device=trusted)
        states = [self.tv_status(t, o[0]).json()["status"] for t, o in zip(tvs, opened)]
        self.assertEqual(states, ["pending", "authenticated", "pending"])
        # one TV's challenge secret does not work for another TV
        self.assertEqual(self.tv_status(tvs[0], opened[2][0]).json()["status"], "none")
        # ASK AGAIN in the same page supersedes the earlier challenge, which can then not be approved
        ch_new, req_new = self.tv_open(tvs[0])
        self.assertEqual(opened[0][1]["status"], "superseded")
        with self.assertRaises(ValueError):
            self.s.sec.decide(opened[0][1]["id"], True, by_device=trusted)
        self.assertEqual(self.tv_status(tvs[0], ch_new).json()["status"], "pending")

    # 7 8 ---------------------------------------------------------------------------------------
    def test_07_tv_page_expires_and_is_revoked(self):
        tv, page = self.approved_tv()
        for rec in self.s.sec.tv_pages.values():
            rec["expires"] = time.time() - 1
        self.assertEqual(tv.get("/api/tv/status", headers=page).status_code, 401)
        tv2, page2 = self.approved_tv()
        sid = self.admin.get("/api/disk/security").json()["tv_sessions"][0]["id"]
        self.assertEqual(self.admin.post("/api/disk/security/revoke_tv", json={"session": sid}, headers=self.H).status_code, 200)
        self.assertEqual(tv2.get("/api/tv/status", headers=page2).status_code, 401)
        tv3, page3 = self.approved_tv()
        r = self.admin.post("/api/disk/security/revoke_all_tv", headers=self.H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(tv3.get("/api/tv/status", headers=page3).status_code, 401)

    def test_08_logout_invalidates_server_side(self):
        tv, page = self.approved_tv()
        stolen = tv.cookies.get("f1_tv")
        self.assertEqual(tv.post("/api/tv/logout").status_code, 401)                 # needs the page secret
        self.assertEqual(tv.post("/api/tv/logout", headers=page).status_code, 200)
        other = self.s.browser()
        other.cookies.set("f1_tv", stolen)                                  # a copied cookie is dead too
        self.assertEqual(other.get("/api/tv/status", headers=page).status_code, 401)
        # the page's beacon on leaving (body = the page secret)
        tv2, page2 = self.approved_tv()
        self.assertEqual(tv2.post("/api/tv/logout", content=page2["X-F1-TV-Page"]).status_code, 200)
        self.assertEqual(tv2.get("/api/tv/status", headers=page2).status_code, 401)
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
    def test_15_client_supplied_flags_stale_ids_and_forged_tokens_mean_nothing(self):
        anon = self.s.browser()
        for hdr in ({"authorized": "true"}, {"x-role": "admin"}, {"x-device": "remote"}, {"x-remote-token": ""}):
            self.assertEqual(anon.get("/api/disk/status", headers=hdr).status_code, 401)
            self.assertEqual(anon.get("/api/tv/status", headers=hdr).status_code, 401)
        self.assertEqual(anon.get("/api/disk/status?authorized=true&role=admin").status_code, 401)
        for name in ("f1_disk", "f1_tv", "f1_tvreq"):
            forged = self.s.browser()
            forged.cookies.set(name, "A" * 43)
            self.assertEqual(forged.get("/api/disk/status").status_code, 401)
            self.assert_tv_denied(forged, {"X-F1-TV-Page": "A" * 43})
            self.assertIn("ACCESS REQUEST", forged.get("/tv").text)
        # the trusted phone's own device cookie in a TV browser: still only the request screen
        did, tok = self.phone()
        tv = self.s.browser()
        tv.cookies.set("f1_dev", tok)
        self.assertIn("ACCESS REQUEST", tv.get("/tv").text)
        self.assert_tv_denied(tv)
        # a /disk login does not open /tv either (no shortcut past the phone)
        self.assertIn("ACCESS REQUEST", self.admin.get("/tv").text)
        self.assert_tv_denied(self.admin)
        self.assertNotIn("authenticated", self.admin.post("/api/tv/auth/request").text)
        # the TV's challenge cookie is not a session
        ch, _ = self.tv_open(tv)
        self.assert_tv_denied(tv, {"X-F1-TV-Page": ch})
        # the page keeps its secrets in memory only: tv.js stores nothing but the chosen layout
        import re
        js = (Path(__file__).resolve().parents[2] / "server" / "tv" / "tv.js").read_text()
        self.assertEqual(set(re.findall(r"(?:local|session)Storage\.\w+\(\"([^\"]+)\"", js)), {"f1tv-layout"})
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("document.cookie", js)
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
            from starlette.websockets import WebSocketDisconnect
            from authhelp import tv_approve

            def ws_code(client):
                try:
                    with client.websocket_connect("/ws") as ws:
                        return json.loads(ws.receive_text())["type"]
                except WebSocketDisconnect as exc:
                    return exc.code
            anon = s2.browser()
            r = anon.get("/", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/tv?next=/"))
            self.assertEqual(anon.get("/api/state").status_code, 401)
            self.assertEqual(anon.get("/api/sync").status_code, 401)
            self.assertEqual(ws_code(anon), 4401)
            self.assertEqual(anon.get("/remote").status_code, 200)         # the remote stays reachable
            admin = s2.browser()
            csrf = s2.setup_password(admin)
            self.assertEqual(admin.get("/api/state").status_code, 200)
            self.assertEqual(ws_code(admin), "hello")
            # a TV with a pending challenge: no; approved: yes; loaded /tv again: no
            tv = s2.browser()
            tv.get("/tv")
            tv.post("/api/tv/auth/request")
            self.assertEqual(ws_code(tv), 4401)
            tv_approve(s2.app, admin, {"X-F1-CSRF": csrf}, tv)
            self.assertEqual(ws_code(tv), "hello")
            self.assertEqual(tv.get("/api/state").status_code, 200)
            tv.get("/tv")
            self.assertEqual(ws_code(tv), 4401)
            self.assertEqual(tv.get("/api/state").status_code, 401)
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
    def test_18_challenges_expire(self):
        did, tok = self.phone()
        tv = self.s.browser()
        ch, req = self.tv_open(tv)
        req["expires"] = time.time() - 1
        self.assertEqual(self.tv_status(tv, ch).json()["status"], "expired")
        m, _ = self.ws_decide(tok, req["id"])
        self.assertFalse(m["ok"])
        self.assert_tv_denied(tv, {"X-F1-TV-Page": ch})
        # approved, but picked up too late: expired, no session
        tv2 = self.s.browser()
        ch2, req2 = self.tv_open(tv2)
        self.s.sec.decide(req2["id"], True, by_device=did)
        req2["expires"] = time.time() - 1
        self.assertEqual(self.tv_status(tv2, ch2).json()["status"], "expired")
        self.assertEqual(self.s.sec.tv_pages, {})
        # denied
        tv3 = self.s.browser()
        ch3, req3 = self.tv_open(tv3)
        self.ws_decide(tok, req3["id"], approve=False)
        self.assertEqual(self.tv_status(tv3, ch3).json()["status"], "denied")
        self.assert_tv_denied(tv3)

    # 19 ----------------------------------------------------------------------------------------
    def test_19_restart_keeps_disk_sessions_and_trust_but_ends_tv_pages(self):
        tv, page = self.approved_tv()
        tv_tok, disk_tok = tv.cookies.get("f1_tv"), self.admin.cookies.get("f1_disk")
        trusted = self.s.sec.data["trusted_device"]
        pending_tv = self.s.browser()
        self.tv_open(pending_tv)
        self.s.close()
        s2 = Server(self.d)                                                 # the server restarts
        try:
            a, b = s2.browser(), s2.browser()
            a.cookies.set("f1_tv", tv_tok)
            b.cookies.set("f1_disk", disk_tok)
            self.assertEqual(a.get("/api/tv/status", headers=page).status_code, 401)   # TV pages are memory-only
            self.assertEqual(b.get("/api/disk/status").status_code, 200)
            self.assertEqual(s2.sec.data["trusted_device"], trusted)
            self.assertEqual(s2.sec.pending(), [])                            # challenges are memory-only
            self.assertFalse((self.d / "auth" / "disk-setup-code").exists())  # no new setup code
        finally:
            s2.close()
            self.s = Server(self.d)                                           # tearDown closes this one

    def test_19_old_30_day_tv_sessions_are_dropped(self):
        f = self.d / "auth" / "security.json"
        data = json.loads(f.read_text())
        data["tv_sessions"] = {"a" * 64: {"id": "old", "expires": time.time() + 86400 * 20, "label": "old TV"}}
        f.write_text(json.dumps(data))
        self.s.close()
        self.s = Server(self.d)
        self.assertNotIn("tv_sessions", json.loads(f.read_text()))
        self.assertEqual(self.s.sec.tv_sessions_public(), [])
        self.assertEqual(self.s.sec.data["disk"]["hash"][:9], "$argon2id")             # nothing else lost

    def test_19_native_hls_gets_the_page_secret_in_the_playlist(self):
        tv, page = self.approved_tv()
        live = self.d / "live"
        live.mkdir()
        (live / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\nlive_00001.ts\n")
        (live / "live_00001.ts").write_bytes(b"\x47" * 188)
        p = page["X-F1-TV-Page"]
        r = tv.get("/tv/live/index.m3u8", params={"p": p})
        self.assertEqual(r.status_code, 200)
        from urllib.parse import quote
        self.assertIn("live_00001.ts?p=" + quote(p, safe=""), r.text)
        self.assertEqual(tv.get("/tv/live/live_00001.ts", params={"p": p}).status_code, 200)
        self.assertEqual(tv.get("/tv/live/live_00001.ts").status_code, 401)
        self.assertEqual(tv.get("/tv/live/live_00001.ts", headers=page).status_code, 200)    # hls.js: header
        self.assertEqual(self.s.browser().get("/tv/live/live_00001.ts", params={"p": p}).status_code, 401)
        self.assertEqual(tv.get("/api/tv/status", params={"p": p}).status_code, 401)          # ?p only for the video

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

    def test_health_tells_only_this_machine_whether_a_recording_runs(self):
        import server.app as appmod
        self.assertNotIn("recorder", self.s.browser().get("/api/health").json())      # LAN: nothing new
        with mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}):
            rec = self.s.browser().get("/api/health").json()["recorder"]           # the update script
        self.assertEqual((rec["state"], rec["busy"]), ("PLAYER OFF", False))

    # logging ------------------------------------------------------------------------------------
    def test_secrets_never_logged(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            c = self.s.browser()
            c.post("/api/disk/auth/login", json={"password": "wrong secret password"})
            c.post("/api/disk/auth/login", json={"password": PW})
            did, tok = self.phone()
            tv = self.s.browser()
            ch, req = self.tv_open(tv)
            self.ws_decide(tok, req["id"])
            page = self.tv_status(tv, ch).json()["page"]
            tv.get("/tv")
        text = "\n".join(logs.output)
        for secret in (PW, "wrong secret password", c.cookies.get("f1_disk"), tok, ch, page):
            self.assertNotIn(secret, text)
        self.assertIn("/tv authorization APPROVED", text)
        self.assertIn("previous TV authorization ended", text)
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

    def test_challenge_rules(self):
        d = Path(tempfile.mkdtemp())
        try:
            sec = Security(d, {"tv_request_seconds": 60})
            dev_tok, did, _ = sec.new_device("Phone")
            _t2, other, _ = sec.new_device("Other phone")
            sec.set_trusted(did)
            b1, p1, r1 = sec.create_request("TV", None)
            b2, p2, r2 = sec.create_request("TV 2", other)
            self.assertNotEqual(p1, p2)
            self.assertIsNone(sec.request_for(b1, p2))                        # both secrets of the same page
            self.assertIs(sec.request_for(b1, p1), r1)
            with self.assertRaises(PermissionError):
                sec.decide(r1["id"], True, by_device=other)                   # not the trusted device
            with self.assertRaises(PermissionError):
                sec.decide(r1["id"], True)                                    # no device at all (e.g. /disk)
            self.assertEqual(sec.decide(r1["id"], True, by_device=did), "approved")
            with self.assertRaises(ValueError):
                sec.decide(r1["id"], True, by_device=did)                     # once
            self.assertEqual(r2["status"], "pending")                         # the other challenge untouched
            got = sec.consume(r1)
            self.assertIsNotNone(got)
            self.assertIsNone(sec.consume(r1))                                # consumed once (atomic)
            cookie, page, _rec = got
            self.assertIsNotNone(sec.tv_page(cookie, page))
            self.assertIsNone(sec.tv_page(cookie, p1))                        # not the challenge secret
            self.assertIsNone(sec.tv_page(cookie, None))
            # superseded by the same browser asking again
            b3, p3, r3 = sec.create_request("TV 2", None, supersede=b2)
            self.assertEqual(r2["status"], "superseded")
            with self.assertRaises(ValueError):
                sec.decide(r2["id"], True, by_device=did)
            # TV page sessions are never written to disk
            self.assertNotIn(cookie, (d / "security.json").read_text())
            self.assertNotIn("tv_sessions", json.loads((d / "security.json").read_text()))
            with self.assertRaises(ValueError):
                sec.new_session("tv", "x")                                    # only from an approved challenge
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
