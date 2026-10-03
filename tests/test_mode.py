"""LIVE / VOD mode selector: AUTO detection + manual override, switched in the running server.

* ModeController: selected / detected / effective for every transition and situation;
* the real app (create_app + lifespan): a selection really replaces the data source + engine
  (LIVE -> F1LiveSource, VOD -> VodSource with the VOYO sync in VOD mode), the dashboards get the
  new hello + mode state over the WebSocket, nothing is restarted.

Network: the F1 schedule is replaced by a fixed index, the sources' run() by an idle loop
(the real sources are built; only their network loop is not started).

Run:  python -m unittest tests.test_mode
"""
import asyncio
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import mode as M  # noqa: E402


def at(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


INDEX = {"Meetings": [{"Name": "Singapore Grand Prix", "Sessions": [
    {"Name": "Race", "StartDate": "2026-10-04T20:00:00", "EndDate": "2026-10-04T22:00:00", "GmtOffset": "08:00:00"}]}]}
DURING = at("2026-10-04T12:30:00")          # the race (12:00-14:00 UTC) is running
LATER = at("2026-10-06T12:00:00")           # nothing on


def controller(now: float, selected="AUTO", fixed=None, feed=None):
    calls = []

    async def switch(m):
        calls.append(m)

    async def loader(year):
        return INDEX
    c = M.ModeController(selected, fixed=fixed, switch=switch, index_loader=loader, clock=lambda: now,
                         feed_status=(lambda: feed) if feed is not None else None)
    return c, calls


class DetectionTest(unittest.TestCase):
    def test_schedule(self):
        self.assertEqual(M.detect_from_schedule(INDEX, DURING)[0], "LIVE")
        self.assertEqual(M.detect_from_schedule(INDEX, at("2026-10-04T10:45:00"))[0], "LIVE")   # 75 min before
        self.assertEqual(M.detect_from_schedule(INDEX, LATER)[0], "VOD")
        det, sess, why = M.detect_from_schedule(None, DURING)
        self.assertEqual((det, sess), ("VOD", None))
        self.assertIn("not reachable", why)

    def test_launcher_uses_the_same_rule(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import tv_launcher
        self.assertEqual(tv_launcher.choose_mode(DURING, INDEX)[0], "live")
        self.assertEqual(tv_launcher.choose_mode(LATER, INDEX)[0], "vod")


class ControllerTest(unittest.TestCase):
    def run_(self, coro):
        return asyncio.run(coro)

    def test_auto_with_live_session(self):
        async def go():
            c, calls = controller(DURING)
            await c.detect()
            await c.apply()
            return c.state(), calls
        st, calls = self.run_(go())
        self.assertEqual((st["selected_mode"], st["detected_mode"], st["effective_mode"]), ("AUTO", "LIVE", "LIVE"))
        self.assertEqual(calls, ["live"])
        self.assertIsNone(st["notice"])
        self.assertIn("Singapore Grand Prix Race", st["detected_reason"])

    def test_auto_without_live_session(self):
        async def go():
            c, calls = controller(LATER)
            await c.detect()
            await c.apply()
            return c.state(), calls
        st, calls = self.run_(go())
        self.assertEqual((st["selected_mode"], st["detected_mode"], st["effective_mode"]), ("AUTO", "VOD", "VOD"))
        self.assertEqual(calls, ["vod"])

    def test_live_selected_without_live_session(self):
        async def go():
            c, calls = controller(LATER)
            await c.detect()
            text = await c.select("LIVE")
            return c.state(), calls, text
        st, calls, text = self.run_(go())
        self.assertEqual((st["selected_mode"], st["detected_mode"], st["effective_mode"]), ("LIVE", "VOD", "LIVE"))
        self.assertEqual(calls, ["live"])                              # really runs the live feed
        self.assertEqual(st["notice"], "No active live F1 session")    # ... and says so, selection kept
        self.assertEqual(st["label"], "LIVE (MANUAL)")
        self.assertIn("no active live F1 session", text)

    def test_vod_selected_while_a_live_session_exists(self):
        async def go():
            c, calls = controller(DURING)
            await c.detect()
            await c.apply()
            await c.select("VOD")
            # the detection keeps running and still says LIVE - it must not switch back
            for _ in range(3):
                await c.detect()
                if c.selected == "AUTO" and c.wanted_source() != c.running:
                    await c.apply()
            return c.state(), calls
        st, calls = self.run_(go())
        self.assertEqual((st["selected_mode"], st["detected_mode"], st["effective_mode"]), ("VOD", "LIVE", "VOD"))
        self.assertEqual(calls, ["live", "vod"])
        self.assertEqual(st["running"], "vod")

    def test_all_transitions(self):
        async def go():
            c, calls = controller(DURING)                # detected LIVE
            await c.detect()
            await c.apply()
            seen = []
            for m in ("LIVE", "AUTO", "VOD", "AUTO", "VOD", "LIVE", "VOD", "LIVE", "AUTO"):
                await c.select(m)
                seen.append((c.selected, c.effective, c.running))
            return seen, calls
        seen, calls = self.run_(go())
        self.assertEqual(seen, [
            ("LIVE", "LIVE", "live"),        # AUTO -> LIVE (already live: no switch)
            ("AUTO", "LIVE", "live"),        # LIVE -> AUTO
            ("VOD", "VOD", "vod"),           # AUTO -> VOD
            ("AUTO", "LIVE", "live"),        # VOD -> AUTO (back to the detection: LIVE)
            ("VOD", "VOD", "vod"),
            ("LIVE", "LIVE", "live"),        # VOD -> LIVE
            ("VOD", "VOD", "vod"),           # LIVE -> VOD
            ("LIVE", "LIVE", "live"),
            ("AUTO", "LIVE", "live"),
        ])
        self.assertEqual(calls, ["live", "vod", "live", "vod", "live", "vod", "live"])

    def test_auto_follows_the_detection(self):
        async def go():
            now = [LATER - 3 * 86400]                    # days before the race: VOD
            calls = []

            async def switch(m):
                calls.append(m)

            async def loader(year):
                return INDEX
            c = M.ModeController("AUTO", switch=switch, index_loader=loader, clock=lambda: now[0])
            await c.detect()
            await c.apply()
            now[0] = DURING                               # the race starts
            c._index_at = -1e18
            await c.detect()
            if c.wanted_source() != c.running:
                await c.apply()
            return calls, c.state()
        calls, st = self.run_(go())
        self.assertEqual(calls, ["vod", "live"])
        self.assertEqual(st["effective_mode"], "LIVE")

    def test_feed_status_counts_as_live(self):
        async def go():
            c, _ = controller(LATER, feed="Started")      # schedule says nothing, the feed says running
            await c.detect()
            return c.state()
        st = self.run_(go())
        self.assertEqual(st["detected_mode"], "LIVE")

    def test_test_source_is_auto_and_overridable(self):
        async def go():
            c, calls = controller(DURING, fixed="TEST")
            await c.detect()
            await c.apply()
            await c.select("VOD")
            await c.select("AUTO")
            return calls, c.state()
        calls, st = self.run_(go())
        self.assertEqual(calls, ["test", "vod", "test"])
        self.assertEqual(st["detected_mode"], "TEST")

    def test_remote_next_is_settled_before_switching(self):
        async def go():
            M.CYCLE_SETTLE_S, old = 0.05, M.CYCLE_SETTLE_S
            try:
                c, calls = controller(LATER)              # AUTO -> VOD running
                await c.detect()
                await c.apply()
                await c.select("NEXT")                    # LIVE
                await c.select("NEXT")                    # VOD (pressed again at once)
                await asyncio.sleep(0.2)
                return calls, c.state()
            finally:
                M.CYCLE_SETTLE_S = old
        calls, st = self.run_(go())
        self.assertEqual(st["selected_mode"], "VOD")
        self.assertEqual(calls, ["vod"])                  # LIVE on the way was never started

    def test_failed_switch_is_reported(self):
        async def go():
            async def boom(m):
                raise RuntimeError("no network")

            async def loader(year):
                return INDEX
            c = M.ModeController("AUTO", switch=boom, index_loader=loader, clock=lambda: LATER)
            await c.detect()
            text = await c.select("LIVE")
            return c.state(), text
        st, text = self.run_(go())
        self.assertIn("failed", st["error"])
        self.assertIn("failed", text)


class AppSwitchTest(unittest.TestCase):
    """The real app: the selection replaces source + engine in the running server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import server.app as A
        self.A = A
        self._orig = (A.build_source, M.fetch_index)
        real = A.build_source
        self.built = []

        def build(cfg, tracks, auth=None):
            src, tok = real(cfg, tracks, auth)

            async def idle(sink):
                sink.set_status(state="test-idle", detail=f"{src.mode} source (network loop not started)")
                await asyncio.Event().wait()
            src.run = idle
            self.built.append(src.mode)
            return src, tok

        async def index(year):
            return INDEX
        A.build_source = build
        M.fetch_index = index

    def tearDown(self):
        self.A.build_source, M.fetch_index = self._orig
        self.tmp.cleanup()

    def cfg(self, mode="auto", project_config=False):
        from server.config import load_config
        cfg = load_config(None if project_config else Path(self.tmp.name) / "none.toml")
        cfg["source"]["mode"] = mode
        cfg["live"]["record"] = False
        cfg["f1_tv"]["auth_file"] = str(Path(self.tmp.name) / "auth" / "f1tv_auth.json")
        cfg["f1_tv"]["open_browser"] = False
        cfg["f1_tv"]["subscription"] = False
        cfg["voyo"]["check_reachability"] = False
        return cfg

    def wait(self, cond, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_dashboard_selector_switches_the_running_server(self):
        from starlette.testclient import TestClient
        from server.sources.f1_live import F1LiveSource
        from server.sources.vod import VodSource
        app = self.A.create_app(self.cfg("auto"))
        rt, mode = app.state.runtime, app.state.mode
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            # AUTO: the detection (fixed schedule; real clock -> nothing on now, or the race) decides
            first = mode.state()
            self.assertEqual(first["selected_mode"], "AUTO")
            self.assertEqual(rt.source.mode, first["effective_mode"].lower())
            with client.websocket_connect("/ws") as ws:
                msgs = [ws.receive_json() for _ in range(3)]
                self.assertEqual(msgs[0]["type"], "hello")
                # the dashboard's MODE buttons
                cls_of = {"VOD": VodSource, "LIVE": F1LiveSource}
                seq = ("LIVE", "VOD", "LIVE") if first["effective_mode"] == "VOD" else ("VOD", "LIVE", "VOD")
                for want in seq:
                    cls = cls_of[want]
                    old_engine = rt.engine
                    ws.send_json({"type": "mode", "value": want})
                    got_hello = got_mode = False
                    for _ in range(400):
                        m = ws.receive_json()
                        if m["type"] == "hello" and m["mode"] == want.lower():
                            got_hello = True
                        if m["type"] == "mode" and m["selected_mode"] == want and not m["switching"] \
                                and m["running"] == want.lower():
                            got_mode = True
                        if got_hello and got_mode:
                            break
                    self.assertTrue(got_hello and got_mode, want)
                    self.assertIsInstance(rt.source, cls)                         # really another source ...
                    self.assertIsNot(rt.engine, old_engine)                       # ... and engine
                    self.assertEqual(rt.engine.vod, want == "VOD")                # VOD: archive + VOYO sync
                    self.assertEqual(rt.engine.sync.vod, want == "VOD")
                    self.assertEqual(rt.engine.timeline.track_latency, want == "LIVE")
                    st = mode.state()
                    self.assertEqual((st["selected_mode"], st["effective_mode"]), (want, want))
                # back to AUTO over HTTP
                r = client.post("/api/mode", json={"mode": "AUTO"})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["selected_mode"], "AUTO")
                self.assertEqual(rt.source.mode, r.json()["detected_mode"].lower())
            self.assertEqual(client.get("/api/mode").json()["effective_mode"], mode.effective)
            h = client.get("/api/health").json()
            self.assertEqual((h["selected_mode"], h["mode"]), ("AUTO", rt.source.mode))
            self.assertEqual(client.post("/api/mode", json={"mode": "BOGUS"}).status_code, 400)
        # every switch built a fresh source; the old ones were stopped (no task left running)
        self.assertGreaterEqual(len(self.built), 5)

    def test_manual_live_at_start_keeps_detection(self):
        from starlette.testclient import TestClient
        app = self.A.create_app(self.cfg("live"))
        rt, mode = app.state.runtime, app.state.mode
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            st = client.get("/api/mode").json()
            self.assertEqual((st["selected_mode"], st["effective_mode"]), ("LIVE", "LIVE"))
            self.assertIn(st["detected_mode"], ("LIVE", "VOD"))
            self.assertEqual(rt.source.mode, "live")

    def test_remote_key_cycles_the_mode(self):
        from starlette.testclient import TestClient
        app = self.A.create_app(self.cfg("test", project_config=True))        # with the shipped keymap
        rt, mode = app.state.runtime, app.state.mode
        M.CYCLE_SETTLE_S, old = 0.05, M.CYCLE_SETTLE_S
        try:
            with TestClient(app, client=("127.0.0.1", 50000)) as client:
                self.assertEqual(rt.source.mode, "test")                   # --test: AUTO = the simulator
                r = client.post("/api/remote/key", json={"key": "KEY_E"})  # E / IR MENU: next mode
                self.assertEqual(r.status_code, 200)
                self.assertTrue(self.wait(lambda: rt.source.mode == "live" and not mode.switching))
                self.assertEqual(mode.state()["selected_mode"], "LIVE")
                r = client.post("/api/remote/command", json={"command": "MODE_AUTO"})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(self.wait(lambda: rt.source.mode == "test" and not mode.switching))
        finally:
            M.CYCLE_SETTLE_S = old


if __name__ == "__main__":
    unittest.main()
