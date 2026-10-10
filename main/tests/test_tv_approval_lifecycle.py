"""/tv approval-request lifecycle: a closed page stops asking and withdraws its open request; a denial ends
the asking for that page instance only; pages / devices stay independent.

* the page side (server/tv/tv.js) runs in node: tests/tv_page_lifecycle.js (fake DOM, timers and fetch);
* the server side (withdraw of a page's own open request, POST /api/tv/logout "challenge:<secret>") runs
  against the real app.

Run (from main/):  python -m unittest tests.test_tv_approval_lifecycle
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_security import Server  # noqa: E402

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class TvPageLifecycleTest(unittest.TestCase):
    """server/tv/tv.js: timers, polls, retries and late answers around pagehide, DENIED and new loads."""

    def test_scenarios(self):
        out = subprocess.run([NODE, str(HERE / "tv_page_lifecycle.js")], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
        self.assertGreaterEqual(len(results), 9, out.stdout + out.stderr)
        for r in results:
            with self.subTest(r["name"]):
                self.assertTrue(r["ok"], r["why"])


class WithdrawTest(unittest.TestCase):
    """The server end: only the page that asked can withdraw its request; nothing else is touched."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.s = Server(self.d)
        admin = self.s.browser()
        csrf = self.s.setup_password(admin)
        phone = self.s.browser()
        self.did, _tok = self.s.remote_device(phone)
        r = admin.post("/api/disk/security/trust", json={"device": self.did}, headers={"X-F1-CSRF": csrf})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def open_tv(self, tv=None):
        """One load of /tv: GET /tv + the page's request -> (browser, challenge, request record)."""
        tv = tv or self.s.browser()
        self.assertEqual(tv.get("/tv").status_code, 200)
        b = tv.post("/api/tv/auth/request").json()
        req = next(x for x in self.s.sec.requests.values() if x["code"] == b["code"])
        return tv, b["challenge"], req

    def status(self, tv, ch):
        return tv.post("/api/tv/auth/status", headers={"X-F1-TV-Challenge": ch}).json()["status"]

    def beacon(self, tv, ch, **kw):
        """What navigator.sendBeacon("/api/tv/logout", "challenge:" + ch) sends on pagehide."""
        return tv.post("/api/tv/logout", content=("challenge:" + ch).encode(),
                       headers={"content-type": "text/plain;charset=UTF-8", **kw.get("headers", {})})

    def pending_ids(self):
        return {r["id"] for r in self.s.sec.pending()}

    def test_closed_page_withdraws_its_open_request(self):
        tv, ch, req = self.open_tv()
        self.assertIn(req["id"], self.pending_ids())
        r = self.beacon(tv, ch)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(req["status"], "withdrawn")
        self.assertNotIn(req["id"], self.pending_ids())                      # gone from the phone
        with self.assertRaises(ValueError):                                  # and cannot be approved any more
            self.s.sec.decide(req["id"], True, by_device=self.did)
        self.assertEqual(self.status(tv, ch), "withdrawn")
        self.assertEqual(self.beacon(tv, ch).status_code, 200)               # a repeated beacon changes nothing

    def test_approved_but_unused_approval_dies_with_its_page(self):
        tv, ch, req = self.open_tv()
        self.s.sec.decide(req["id"], True, by_device=self.did)
        self.assertEqual(self.beacon(tv, ch).status_code, 200)
        self.assertEqual(self.status(tv, ch), "withdrawn")                   # no TV session for a gone page
        self.assertEqual(tv.get("/api/tv/status").status_code, 401)

    def test_devices_are_independent(self):
        a, ch_a, req_a = self.open_tv()
        b, ch_b, req_b = self.open_tv()
        self.assertEqual(self.beacon(a, ch_a).status_code, 200)              # A closes its tab
        self.assertEqual(req_b["status"], "pending")
        self.assertIn(req_b["id"], self.pending_ids())
        # nobody withdraws someone else's request: B's challenge from A, from a stranger, a forged one
        self.assertEqual(self.beacon(a, ch_b).status_code, 401)
        self.assertEqual(self.beacon(self.s.browser(), ch_b).status_code, 401)
        self.assertEqual(self.beacon(b, "x" * 43).status_code, 401)
        self.assertEqual(self.beacon(b, "").status_code, 401)
        self.assertEqual(self.beacon(b, ch_b, headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(req_b["status"], "pending")
        # B is still approvable and gets its TV
        self.s.sec.decide(req_b["id"], True, by_device=self.did)
        self.assertEqual(self.status(b, ch_b), "authenticated")

    def test_denial_is_final_for_that_request_only(self):
        tv, ch, req = self.open_tv()
        other, ch_o, req_o = self.open_tv()
        self.assertEqual(self.s.sec.decide(req["id"], False, by_device=self.did), "denied")
        self.assertEqual(self.status(tv, ch), "denied")
        self.assertEqual(self.status(tv, ch), "denied")                       # polling again does not revive it
        self.assertEqual(self.beacon(tv, ch).status_code, 200)               # the page closing changes nothing
        self.assertEqual(req["status"], "denied")
        self.assertEqual(req_o["status"], "pending")                         # another device is not affected
        self.assertEqual(self.status(other, ch_o), "pending")
        # no denial is remembered for the browser: a new load of /tv may ask again
        tv, ch2, req2 = self.open_tv(tv)
        self.assertNotEqual(req2["id"], req["id"])
        self.assertEqual(self.status(tv, ch2), "pending")
        self.assertEqual(req["status"], "denied")

    def test_reload_beacon_race_cannot_touch_the_new_request(self):
        """pagehide of the old load may reach the server after the new load already asked."""
        tv, ch_old, req_old = self.open_tv()
        tv, ch_new, req_new = self.open_tv(tv)                               # the reload (GET /tv + new request)
        self.assertEqual(req_old["status"], "superseded")
        self.assertEqual(self.beacon(tv, ch_old).status_code, 401)           # new cookie, old challenge: no match
        self.assertEqual(req_new["status"], "pending")

    def test_approved_page_logout_unchanged(self):
        tv, ch, req = self.open_tv()
        self.s.sec.decide(req["id"], True, by_device=self.did)
        st = tv.post("/api/tv/auth/status", headers={"X-F1-TV-Challenge": ch}).json()
        page = st["page"]
        self.assertEqual(tv.get("/api/tv/status", headers={"X-F1-TV-Page": page}).status_code, 200)
        self.assertEqual(tv.post("/api/tv/logout", content=page.encode()).status_code, 200)   # the pagehide beacon
        self.assertEqual(tv.get("/api/tv/status", headers={"X-F1-TV-Page": page}).status_code, 401)


class PublicGatewayWithdrawTest(unittest.TestCase):
    """Through Tailscale Funnel (the existing allowlisted POST /api/tv/logout): the same rules, no new route."""

    def test_withdraw_through_the_gateway(self):
        from test_public_gateway import Env
        d = Path(tempfile.mkdtemp())
        e = Env(d)
        try:
            a, b = e.internet("198.51.100.30"), e.internet("198.51.100.31")
            chs = {}
            for name, tv in (("a", a), ("b", b)):
                self.assertEqual(tv.get("/tv").status_code, 200)
                r = tv.post("/api/tv/auth/request")
                self.assertEqual(r.status_code, 200, r.text)
                chs[name] = r.json()["challenge"]
            self.assertEqual(len(e.sec.pending()), 2)
            r = a.post("/api/tv/logout", content=("challenge:" + chs["a"]).encode(), headers={"content-type": "text/plain"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(len(e.sec.pending()), 1)
            r = a.post("/api/tv/logout", content=("challenge:" + chs["b"]).encode(), headers={"content-type": "text/plain"})
            self.assertEqual(r.status_code, 401)
            self.assertEqual(len(e.sec.pending()), 1)                         # B untouched
        finally:
            e.close()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
