"""Server VOYO player (tools/voyo_server_player.py): session windows from the F1 schedule, its own
recording channel on the server (never mixed with the VOYO window you watch), closing a package.

Run (from main/):  python -m unittest tests.test_voyo_server_player
"""
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

from server.config import load_config  # noqa: E402
from tools.voyo_server_player import PLAY_JS, session_windows  # noqa: E402

INDEX = {"Meetings": [{"Name": "Japanese Grand Prix", "Sessions": [
    {"Name": "Practice 1", "StartDate": "2026-03-27T11:30:00", "EndDate": "2026-03-27T12:30:00", "GmtOffset": "09:00:00"},
    {"Name": "Qualifying", "StartDate": "2026-03-28T15:00:00", "EndDate": "2026-03-28T16:00:00", "GmtOffset": "09:00:00"},
    {"Name": "Race", "StartDate": "2026-03-29T14:00:00", "EndDate": "2026-03-29T16:00:00", "GmtOffset": "09:00:00"}]}]}


class WindowsTest(unittest.TestCase):
    def test_windows_follow_the_schedule_and_the_session_filter(self):
        sp = {"record_sessions": ["qualifying", "race"], "lead_minutes": 15, "trail_minutes": 30}
        ws = session_windows(INDEX, sp)
        self.assertEqual([w["session_name"] for w in ws], ["Qualifying", "Race"])
        race = ws[1]
        self.assertEqual(race["start"], "2026-03-29T05:00:00+00:00")          # local 14:00 JST
        self.assertEqual(race["open_until"] - race["open_from"], 2 * 3600 + 45 * 60)
        self.assertEqual(race["kind"], "race")
        self.assertEqual(session_windows(None, sp), [])

    def test_config_defaults_off(self):
        sp = load_config()["voyo"]["server_player"]
        self.assertFalse(sp["enabled"])
        self.assertIn("race", sp["record_sessions"])
        self.assertIn("requestFullscreen", PLAY_JS)


class ServerChannelTest(unittest.TestCase):
    """POST /api/sync/voyo with channel=server_player: own instance + package, the dashboard's
    VOYO sync untouched; "close" finalizes the package."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_routing(self):
        from starlette.testclient import TestClient
        import server.app as appmod
        env = {"F1DASH_VOYO_SERVER_PLAYER_ENABLED": "true", "F1DASH_VOYO_RECORDING_PATH": str(self.d / "rec"),
               "F1DASH_VOYO_RECORDING_REQUIRE_MOUNT": "", "F1DASH_VOYO_RECORDING_MIN_FREE_BYTES": "0"}
        with mock.patch.dict(os.environ, env), mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}), \
                mock.patch.object(appmod, "DATA_DIR", self.d):
            cfg = load_config()
            cfg["source"]["mode"] = "test"
            app = appmod.create_app(cfg)
            c = TestClient(app)
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from authhelp import disk_login
            disk_login(c, self.d)                                # the recordings list is /disk-only

            def post(body):
                body = {"paused": False, "playback_rate": 1.0, "timestamp_local": time.time(), **body}
                r = c.post("/api/sync/voyo", content=json.dumps(body), headers={"Content-Type": "application/json"})
                self.assertEqual(r.status_code, 200, r.text)
                return r.json()["recording"]
            hint = {"meeting": "Japanese Grand Prix", "session_name": "Race"}
            a = post({"playback_time": 5, "asset": "voyo.si/live/f1|", "channel": "server_player",
                      "session_hint": hint})
            self.assertEqual(a["channel"], "server_player")
            self.assertTrue(a["capture"])                           # record_video default true
            v = post({"playback_time": 900, "asset": "voyo.si/vod/x|7200", "duration": 7200})
            self.assertEqual(v["channel"], "viewer")
            self.assertFalse(v["capture"])                          # the PC window capture stays opt-in
            self.assertNotEqual(a["instance"], v["instance"])
            a2 = post({"playback_time": 6, "asset": "voyo.si/live/f1|", "channel": "server_player",
                       "session_hint": hint})
            self.assertEqual(a2["instance"], a["instance"])        # the viewer never splits the player's stream
            lst = c.get("/api/voyo/recordings").json()
            chans = sorted(r["channel"] for r in lst["recordings"])
            self.assertEqual(chans, ["server_player", "viewer"])
            sp = next(r for r in lst["recordings"] if r["channel"] == "server_player")
            self.assertEqual((sp["session_name"], sp["session_kind"]), ("Race", "race"))
            post({"playback_time": 0, "channel": "server_player", "close": True, "why": "session window over"})
            m = json.loads((self.d / "rec" / a["instance"] / "manifest.json").read_text())
            self.assertEqual((m["status"], m["close_reason"], m["channel"]), ("closed", "session window over",
                                                                              "server_player"))
            r = c.put(f"/api/voyo/recordings/{a['instance']}/capture/run1_seg_00000.mp4", content=b"x" * 5,
                      headers={"Content-Length": "5"})
            self.assertEqual(r.status_code, 200, r.text)            # the player's package accepts its video
            self.assertTrue((self.d / "rec" / a["instance"] / "capture" / "run1_seg_00000.mp4").exists())


# ---------------------------------------------------------------------------------------------
# login: the page that is open at Ctrl+C becomes the stream page only with a usable video player
# ---------------------------------------------------------------------------------------------
LANDING = "https://voyo.si/vsebina/some-f1-event"                  # a content page: text + a play button
STREAM = "https://voyo.si/predvajaj/some-f1-event/live"            # the page with the player
OLD = "https://voyo.si/predvajaj/last-race/live"                   # a page learned before (worked)


def page(url=STREAM, video=True, ready=4, paused=False, visible=True, error=None, t=12.0, title="F1"):
    if not video:
        return {"url": url, "title": title, "videos": 0, "video": False}
    return {"url": url, "title": title, "videos": 1, "video": True, "visible": visible, "w": 1920 if visible else 0,
            "h": 1080 if visible else 0, "paused": paused, "ended": False, "ready": ready, "t": t,
            "duration": None, "live": True, "muted": False, "error": error}


class PlayerCheckTest(unittest.TestCase):
    def test_what_counts_as_a_usable_player(self):
        from tools.voyo_server_player import player_check
        cases = [(None, False), (page(LANDING, video=False), False), (page(ready=0), False), (page(ready=1), False),
                 (page(ready=2, paused=True), True), (page(ready=4, paused=True), True), (page(ready=4), True),
                 (page(ready=4, visible=False), False), (page(ready=4, error=3), False)]
        for st, usable in cases:
            with self.subTest(st=st):
                self.assertIs(player_check(st)[0], usable)
        self.assertIn("paused", player_check(page(ready=2, paused=True))[1])
        self.assertIn("playing", player_check(page(ready=4))[1])
        self.assertIn("not ready", player_check(page(ready=1))[1])
        self.assertIn("no video", player_check(page(LANDING, video=False))[1])

    def test_inspection_reads_the_page_not_the_media(self):
        from tools.voyo_server_player import INSPECT_JS
        for word in ("currentSrc", ".src", "srcObject", "play(", "muted =", "requestFullscreen"):
            self.assertNotIn(word, INSPECT_JS)                    # read only, never a media address
        self.assertIn("location.href", INSPECT_JS)
        self.assertIn("readyState", INSPECT_JS)


class FakeProc:
    def __init__(self, name, events):
        self.name, self.events = name, events

    def start(self, *a, **k):
        self.events.append(f"{self.name}.start")

    def stop(self, *a, **k):
        self.events.append(f"{self.name}.stop")

    def alive(self):
        return True


class FakePlayer:
    """Player for cmd_login: no Xvfb / Chrome / VNC; inspect_page() answers from a script. A
    KeyboardInterrupt in the script = Ctrl+C at that moment of the login loop."""

    def __init__(self, script, stream_url="", events=None):
        self.events = events if events is not None else []
        self.sp = {"stream_url": stream_url, "vnc_port": 5999}
        self.display = ":99"
        self.vnc, self.chrome, self.pulse, self.xvfb = (FakeProc(n, self.events) for n in ("vnc", "chrome", "pulse", "xvfb"))
        self.script = list(script)
        self.opened = None

    def env(self):
        return {}

    def start_display(self):
        self.events.append("xvfb.start")

    def start_audio(self):
        pass

    def start_chrome(self, url):
        self.opened = url
        self.events.append("chrome.start")

    def inspect_page(self):
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            raise step
        return step

    def page_url(self):
        return None


class LoginTest(unittest.TestCase):
    def setUp(self):
        import tools.voyo_server_player as vsp
        self.vsp = vsp
        self.d = Path(tempfile.mkdtemp())
        self.state = self.d / "voyo_server_player.json"
        self.events = []
        real_save = vsp.save_state

        def save(d):
            self.events.append("save")
            real_save(d)
        self._p = [mock.patch.object(vsp, "STATE", self.state), mock.patch.object(vsp, "save_state", save),
                   mock.patch.object(vsp.shutil, "which", lambda n: "/usr/bin/" + n)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def saved(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def login(self, script, answer=False, stream_url="", force=False, before=None):
        if before is not None:
            self.state.write_text(json.dumps(before))
        asked = []

        def confirm(q):
            asked.append(q)
            self.events.append("confirm")
            return answer
        player = FakePlayer(script, stream_url, self.events)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.vsp.cmd_login(player, save_unverified=force, confirm=confirm, poll_s=0)
        return buf.getvalue(), asked, player

    def assert_cleanup_after(self, step):
        i = self.events.index(step)
        for proc in ("vnc", "chrome", "pulse", "xvfb"):
            self.assertIn(f"{proc}.stop", self.events)
            self.assertGreater(self.events.index(f"{proc}.stop"), i, f"{proc} stopped before {step}")

    def test_landing_page_without_video_does_not_replace_a_working_page(self):
        out, asked, _ = self.login([page(LANDING, video=False), KeyboardInterrupt(), page(LANDING, video=False)],
                                   answer=False, before={"last_url": OLD, "learned_at": "x"})
        self.assertEqual(self.saved(), {"last_url": OLD, "learned_at": "x"})     # unchanged
        self.assertNotIn("save", self.events)
        self.assertIn("no usable video player was confirmed", out)
        self.assertIn("no video element", out)
        self.assertIn(f"stays {OLD}", out)
        self.assertEqual(len(asked), 1)
        self.assertIn(f"replace the saved stream page {OLD}", asked[0])           # the question says what it replaces
        self.assert_cleanup_after("confirm")                                    # Chrome still up while deciding

    def test_video_not_ready_is_not_a_usable_player(self):
        out, asked, _ = self.login([page(ready=1, paused=True), KeyboardInterrupt(), page(ready=1, paused=True)],
                                   answer=False, before={"last_url": OLD})
        self.assertEqual(self.saved()["last_url"], OLD)
        self.assertIn("not ready to play yet (readyState 1", out)
        self.assertEqual(len(asked), 1)

    def test_paused_but_ready_player_counts(self):
        out, asked, _ = self.login([page(ready=2, paused=True), KeyboardInterrupt(), page(ready=2, paused=True)],
                                   before={"last_url": OLD})
        st = self.saved()
        self.assertEqual((st["last_url"], st["last_url_verified"]), (STREAM, True))
        self.assertEqual(asked, [])                                             # no question for a verified page
        self.assertIn("Saved as the stream page: " + STREAM, out)
        self.assertIn("paused", out)
        self.assert_cleanup_after("save")                                       # saved before Chrome / Xvfb stop

    def test_playing_player_counts(self):
        out, asked, _ = self.login([page(ready=4), KeyboardInterrupt(), page(ready=4, t=15)])
        self.assertEqual(self.saved()["last_url"], STREAM)
        self.assertIn("playing", out)
        self.assertEqual(asked, [])

    def test_navigation_from_landing_page_to_the_player(self):
        script = [None, page(LANDING, video=False), page(LANDING, video=False), page(STREAM, video=False),
                  page(STREAM, ready=0), page(STREAM, ready=1), page(STREAM, ready=3, paused=True),
                  page(STREAM, ready=4), KeyboardInterrupt(), page(STREAM, ready=4)]
        out, asked, _ = self.login(script)
        self.assertEqual(self.saved()["last_url"], STREAM)                       # the player page, not the landing page
        self.assertIn(LANDING, out)
        self.assertEqual(out.count(f"page: {LANDING}"), 1)                      # no repeated lines for the same state
        self.assertIn("video player ready", out)
        self.assertEqual(asked, [])

    def test_ctrl_c_before_the_player_is_ready(self):
        """Ctrl+C right after the page opened, before its player initialised: nothing replaced silently."""
        out, asked, _ = self.login([page(STREAM, video=False), KeyboardInterrupt(), page(STREAM, video=False)],
                                   answer=False, before={"last_url": OLD})
        self.assertEqual(self.saved()["last_url"], OLD)
        self.assertIn("login --save-unverified", out)                          # how to do it on purpose
        # ... the explicit ways: yes to the question, or --save-unverified
        out, asked, _ = self.login([page(STREAM, video=False), KeyboardInterrupt(), page(STREAM, video=False)],
                                   answer=True, before={"last_url": OLD})
        self.assertEqual((self.saved()["last_url"], self.saved()["last_url_verified"]), (STREAM, False))
        self.assertIn("UNVERIFIED", out)
        self.state.write_text(json.dumps({"last_url": OLD}))
        out, asked, _ = self.login([page(STREAM, video=False), KeyboardInterrupt(), page(STREAM, video=False)],
                                   force=True)
        self.assertEqual(asked, [])
        self.assertEqual((self.saved()["last_url"], self.saved()["last_url_verified"]), (STREAM, False))

    def test_player_seen_then_buffering_at_ctrl_c_still_counts(self):
        out, _asked, _ = self.login([page(ready=4), page(ready=1), KeyboardInterrupt(), None])
        self.assertEqual(self.saved()["last_url"], STREAM)

    def test_player_seen_but_then_left_does_not_count_for_the_new_page(self):
        out, asked, _ = self.login([page(ready=4), page(LANDING, video=False), KeyboardInterrupt(),
                                    page(LANDING, video=False)], answer=False, before={"last_url": OLD})
        self.assertEqual(self.saved()["last_url"], OLD)
        self.assertIn(f"A usable player was seen earlier on {STREAM}", out)

    def test_configured_stream_url_is_never_touched(self):
        cfg_url = "https://voyo.si/predvajaj/configured"
        out, asked, player = self.login([page(ready=4), KeyboardInterrupt(), page(ready=4)], stream_url=cfg_url,
                                        before={"last_url": OLD})
        self.assertEqual(player.opened, cfg_url)
        self.assertEqual(self.saved(), {"last_url": OLD})
        self.assertNotIn("save", self.events)
        self.assertIn("stream_url is set", out)
        self.assertIn("Nothing saved", out)
        self.assertNotIn("Saved as the stream page", out)
        self.assertEqual(asked, [])

    def test_disk_page_url_still_wins_and_is_said(self):
        out, _asked, _ = self.login([page(ready=4), KeyboardInterrupt(), page(ready=4)],
                                    before={"page_url": "https://voyo.si/from-disk"})
        st = self.saved()
        self.assertEqual((st["page_url"], st["last_url"]), ("https://voyo.si/from-disk", STREAM))
        self.assertIn("/disk page", out)

    def test_no_page_at_all(self):
        out, asked, _ = self.login([None, KeyboardInterrupt(), None], before={"last_url": OLD})
        self.assertEqual(self.saved(), {"last_url": OLD})
        self.assertIn("nothing saved", out.lower())
        self.assertEqual(asked, [])

    def test_unreadable_page_moment_keeps_state(self):
        out, _asked, _ = self.login([page(ready=4), None, None, KeyboardInterrupt(), page(ready=4)])
        self.assertEqual(self.saved()["last_url"], STREAM)

    def test_terminal_answer(self):
        from tools.voyo_server_player import _ask_tty
        with mock.patch.object(sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                self.assertFalse(_ask_tty("Save? "))                    # no terminal (service / pipe): no
            stdin.isatty.return_value = True
            for typed, want in (("y", True), ("yes", True), ("", False), ("n", False)):
                with mock.patch("builtins.input", return_value=typed):
                    self.assertIs(_ask_tty("Save? "), want)
            for exc in (EOFError(), KeyboardInterrupt()):                   # a second Ctrl+C at the question = no
                with mock.patch("builtins.input", side_effect=exc), redirect_stdout(io.StringIO()):
                    self.assertFalse(_ask_tty("Save? "))

    def test_cleanup_even_when_the_decision_fails(self):
        def boom(q):
            raise RuntimeError("terminal gone")
        player = FakePlayer([page(LANDING, video=False), KeyboardInterrupt(), page(LANDING, video=False)],
                            events=self.events)
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            self.vsp.cmd_login(player, confirm=boom, poll_s=0)
        for proc in ("vnc", "chrome", "pulse", "xvfb"):
            self.assertIn(f"{proc}.stop", self.events)
        self.assertFalse(self.state.exists())

    def test_save_error_is_reported_and_cleanup_still_runs(self):
        import io
        from contextlib import redirect_stdout
        player = FakePlayer([page(ready=4), KeyboardInterrupt(), page(ready=4)], events=self.events)
        buf = io.StringIO()
        with mock.patch.object(self.vsp, "save_state", side_effect=PermissionError("read-only")), \
                redirect_stdout(buf), self.assertRaises(OSError):
            self.vsp.cmd_login(player, confirm=lambda q: False, poll_s=0)
        self.assertIn("nothing was saved", buf.getvalue())
        self.assertIn("chrome.stop", self.events)

    def test_cli_flag(self):
        import tools.voyo_server_player as vsp
        with mock.patch.object(sys, "argv", ["voyo_server_player.py", "login", "--save-unverified"]), \
                mock.patch.object(vsp, "Player"), mock.patch.object(vsp, "load_config", return_value={}), \
                mock.patch.object(vsp, "cmd_login") as cl:
            vsp.main()
        self.assertTrue(cl.call_args.kwargs["save_unverified"])
        with mock.patch.object(sys, "argv", ["voyo_server_player.py", "login"]), \
                mock.patch.object(vsp, "Player"), mock.patch.object(vsp, "load_config", return_value={}), \
                mock.patch.object(vsp, "cmd_login") as cl:
            vsp.main()
        self.assertFalse(cl.call_args.kwargs["save_unverified"])


class FakeDashboard:
    """A real HTTP server on 127.0.0.1 answering like the dashboard server would: {path: (status, body)}.
    Records the headers it got (the status command must not send credentials)."""

    def __init__(self, routes):
        import http.server
        import threading
        self.routes, self.seen = routes, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                status, body = outer.routes.get(self.path, (404, {"ok": False, "error": "not found"}))
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DISK_401 = (401, {"ok": False, "error": "login required", "auth": "disk"})


def health(state="RECORDING", busy=True, recorder=True):
    return (200, {"ok": True, "mode": "live", **({"recorder": {"state": state, "busy": busy}} if recorder else {})})


class StatusReachabilityTest(unittest.TestCase):
    """status / test: an HTTP 401 from /api/voyo/recordings is a running server that wants a /disk login -
    not "not reachable"; the recording state comes from /api/health (this machine only)."""

    def setUp(self):
        import tools.voyo_server_player as vsp
        self.vsp = vsp
        self.servers = []
        self._p = [mock.patch.object(vsp, "Schedule"), mock.patch.object(vsp, "heartbeat")]
        for p in self._p:
            p.start()
        vsp.Schedule.return_value.windows.return_value = []

    def tearDown(self):
        for p in self._p:
            p.stop()
        for srv in self.servers:
            srv.close()

    def dash(self, routes):
        srv = FakeDashboard(routes)
        self.servers.append(srv)
        return srv

    def player(self, server):
        from types import SimpleNamespace
        return SimpleNamespace(sp={"enabled": True, "when": "schedule"}, server=server, stream_url=lambda: "https://voyo.si/x",
                               profile=Path(tempfile.gettempdir()), open=lambda *a: None, tick=lambda: None,
                               close=lambda *a: None, episode_mode=lambda: True, event_url=lambda: "https://voyo.si/x")

    def status(self, server):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.vsp.cmd_status(self.player(server))
        lines = {ln.split()[0]: ln for ln in buf.getvalue().splitlines() if ln.strip()}
        return buf.getvalue(), lines

    def run_test_cmd(self, server):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(self.vsp, "_post"):
            self.vsp.cmd_test(self.player(server), 0, None)
        return buf.getvalue()

    def test_401_means_reachable_and_the_recorder_state_comes_from_health(self):
        d = self.dash({"/api/voyo/recordings": DISK_401, "/api/health": health("RECORDING", True)})
        out, ln = self.status(d.url)
        self.assertNotIn("not reachable", out)
        self.assertIn("reachable - the details need a /disk login (HTTP 401)", ln["recordings"])
        self.assertIn("RECORDING", ln["recorder"])
        self.assertIn("busy: yes", ln["recorder"])
        for path, headers in d.seen:                          # no credentials sent, none printed
            self.assertFalse({"Cookie", "X-Remote-Token", "Authorization"} & set(headers), path)

    def test_403_too(self):
        d = self.dash({"/api/voyo/recordings": (403, {"ok": False}), "/api/health": health("REST", False)})
        out, ln = self.status(d.url)
        self.assertIn("(HTTP 403)", ln["recordings"])
        self.assertIn("REST  (busy: no)", ln["recorder"])

    def test_recorder_states_are_shown_as_they_are(self):
        for state, busy in (("OFF", False), ("DISK ERROR", False), ("WAITING FOR DISK", False),
                            ("PLAYER OFF", False), ("OPENING", True)):
            d = self.dash({"/api/voyo/recordings": DISK_401, "/api/health": health(state, busy)})
            _out, ln = self.status(d.url)
            self.assertIn(f"{state}  (busy: {'yes' if busy else 'no'})", ln["recorder"], state)

    def test_server_not_running(self):
        url = f"http://127.0.0.1:{free_port()}"
        out, ln = self.status(url)
        self.assertIn("dashboard server not reachable (", ln["recordings"])
        self.assertIn("UNKNOWN - dashboard server not reachable", ln["recorder"])
        self.assertNotIn("busy", ln["recorder"])                # nothing claimed about recording

    def test_health_without_recorder_state_is_unknown(self):
        for h in (health(recorder=False), (500, {"ok": False}), (200, b"<html>not json</html>"),
                  (200, {"ok": True, "recorder": {"busy": True}})):
            d = self.dash({"/api/voyo/recordings": DISK_401, "/api/health": h})
            _out, ln = self.status(d.url)
            self.assertIn("UNKNOWN", ln["recorder"], h)
            self.assertNotIn("busy: yes", ln["recorder"], h)

    def test_200_still_shows_the_recorder_of_the_list(self):
        rec = {"status": {"path": "/mnt/x/viewer", "ok": True}, "server_player": {"path": "/mnt/x", "ok": True,
               "error": None}, "recordings": []}
        d = self.dash({"/api/voyo/recordings": (200, rec), "/api/health": health("REST", False)})
        _out, ln = self.status(d.url)
        self.assertIn("/mnt/x  ok=True", ln["recordings"])
        self.assertIn("REST", ln["recorder"])

    def test_other_http_errors_are_said_as_such(self):
        d = self.dash({"/api/voyo/recordings": (500, {"ok": False}), "/api/health": health("REST", False)})
        _out, ln = self.status(d.url)
        self.assertIn("answered HTTP 500", ln["recordings"])
        self.assertNotIn("not reachable", ln["recordings"])

    def test_cmd_test_401_is_not_a_dead_server(self):
        d = self.dash({"/api/voyo/recordings": DISK_401, "/api/health": health("RECORDING", True)})
        out = self.run_test_cmd(d.url)
        self.assertNotIn("not reachable", out)
        self.assertIn("needs a /disk login (HTTP 401)", out)
        self.assertIn("Recorder: RECORDING  (busy: yes)", out)

    def test_cmd_test_server_not_running(self):
        out = self.run_test_cmd(f"http://127.0.0.1:{free_port()}")
        self.assertIn("dashboard server not reachable - is it running?", out)

    def test_cmd_test_200_lists_the_recording(self):
        rec = {"recordings": [{"channel": "server_player", "stream_instance_id": "abc123", "status": "closed",
                               "watched_seconds": 60, "capture_segments": 1, "capture_bytes": 2e6}]}
        d = self.dash({"/api/voyo/recordings": (200, rec)})
        out = self.run_test_cmd(d.url)
        self.assertIn("recording abc123: closed, 60 s, 1 video segment(s), 2.0 MB", out)


class HealthRecorderEndToEndTest(unittest.TestCase):
    """The real /api/health of create_app: the recorder state only for this machine (what status reads)."""

    def test_real_health_answer_parses(self):
        import server.app as appmod
        import tools.voyo_server_player as vsp
        from starlette.testclient import TestClient
        d = Path(tempfile.mkdtemp())
        env = {"F1DASH_VOYO_SERVER_PLAYER_ENABLED": "true", "F1DASH_VOYO_RECORDING_PATH": str(d / "rec"),
               "F1DASH_VOYO_RECORDING_REQUIRE_MOUNT": "", "F1DASH_VOYO_RECORDING_MOUNT_MARKER": "",
               "F1DASH_VOYO_RECORDING_MIN_FREE_BYTES": "0"}
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(appmod, "DATA_DIR", d):
                cfg = load_config()
                cfg["source"]["mode"] = "test"
                c = TestClient(appmod.create_app(cfg))
                self.assertEqual(c.get("/api/voyo/recordings").status_code, 401)        # unchanged: /disk only
                lan = c.get("/api/health").json()
                with mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}):
                    local = c.get("/api/health").json()
            self.assertNotIn("recorder", lan)                                           # unchanged: LAN gets nothing
            routes = {"/api/voyo/recordings": DISK_401, "/api/health": (200, local)}
            srv = FakeDashboard(routes)
            try:
                rec, why = vsp.recorder_state(srv.url)
            finally:
                srv.close()
            self.assertEqual(rec, {"state": "PLAYER OFF", "busy": False}, why)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class EpisodeRunTest(unittest.TestCase):
    """cmd_run in episode mode: a session window opens the session recorder with the window's session; a
    finished session is not opened again in the same window; a running recording outlives its window
    (live: until the recorder ends it); stopping the service closes it."""

    def run_loop(self, windows, ticks, session_states, feed_live=False):
        import tools.voyo_server_player as vsp
        from tools.voyo_session import Target
        opened, closed = [], []
        player = mock.Mock()
        player.sp = {"enabled": True, "when": "schedule", "record_sessions": ["sprint"]}
        player.session = None
        player.is_open = False
        player.episode_mode.return_value = True
        player.event_url.return_value = "https://voyo.si/f1/vn-kitajske"
        player.stream_url.return_value = "https://voyo.si/f1/vn-kitajske"
        player.feed_live.return_value = feed_live
        player._flushed, player.done_window = -1e9, None
        states = iter(session_states)

        def open_session(target, url):
            self.assertIsInstance(target, Target)
            opened.append((target.kind, target.meeting, url))
            player.session = mock.Mock(state="DISCOVERING", target=target, end_reason="")

        def session_tick():
            player.session.state = next(states, player.session.state)
            if player.session.state == "DONE":
                player.session.end_reason = "the recording ended"

        def close_session(why):
            closed.append(why)
            player.session = None
        player.open_session.side_effect = open_session
        player.session_tick.side_effect = session_tick
        player.close_session.side_effect = close_session
        player.flush_pending.return_value = None
        win_iter = iter(windows)
        loops = {"n": 0}

        def fake_sleep(_s):
            pass

        def current(_now):
            loops["n"] += 1
            if loops["n"] > ticks:
                raise KeyboardInterrupt
            return next(win_iter, None)
        with mock.patch.object(vsp, "Schedule") as S, mock.patch.object(vsp, "heartbeat", return_value=[]), \
                mock.patch.object(vsp.time, "sleep", fake_sleep), mock.patch.object(vsp, "find_browser", return_value="chrome"), \
                mock.patch.object(vsp.signal, "signal"):
            S.return_value.windows.return_value = []
            S.return_value.current.side_effect = current
            vsp.cmd_run(player)
        return opened, closed

    WIN = {"kind": "sprint", "meeting": "Chinese Grand Prix", "session_name": "Sprint",
           "start": "2026-03-21T03:00:00+00:00", "open_until": 1e12}

    def test_window_opens_the_sessions_recorder_once(self):
        opened, closed = self.run_loop([self.WIN] * 8, 8, ["RECORDING", "RECORDING", "DONE"])
        self.assertEqual(opened, [("sprint", "Chinese Grand Prix", "https://voyo.si/f1/vn-kitajske")])
        self.assertEqual(closed, ["the recording ended"])           # DONE in the window: not opened again

    def test_recording_outlives_its_window_and_service_stop_closes_it(self):
        opened, closed = self.run_loop([self.WIN, None, None, None], 4, ["RECORDING"] * 10)
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, ["the server VOYO player was stopped"])

    def test_no_window_no_session(self):
        opened, closed = self.run_loop([None] * 3, 3, [])
        self.assertEqual((opened, closed), ([], []))
