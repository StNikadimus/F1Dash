"""Server side of a reliable server-player recording: one package per session (the session recorder's
key), its close with the player's report, the completeness check of the video (a one-minute file can never
pass as a full session), late segments, the segment checks sent with each upload, the server telling the
recorder to stop (disk full), and the /disk + /tv status of the session recorder.

Run (from main/):  python -m unittest tests.test_voyo_recording_health
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import recordings_admin as admin  # noqa: E402
from server.config import load_config  # noqa: E402
from server.voyo_recording import capture_check, clean_report  # noqa: E402

KEY = "sprint-63661233-20260321"
T = 1_774_090_000.0


def rows(spans, **kw):
    return [{"name": f"run1_seg_{i:05d}.mp4", "pc_start_epoch": T + a, "pc_end_epoch": T + b, **kw}
            for i, (a, b) in enumerate(spans)]


class CaptureCheckTest(unittest.TestCase):
    def test_complete(self):
        c = capture_check(rows([(0, 60), (60, 120), (120, 180)], audio=True, max_db=-12.0), {"expected_s": 180})
        self.assertEqual((c["state"], c["recorded_s"], c["sound"], c["gaps"]), ("COMPLETE", 180, "ok", []))

    def test_one_minute_of_a_long_session_is_incomplete(self):
        c = capture_check(rows([(0, 60)]), {"expected_s": 3600})
        self.assertEqual(c["state"], "INCOMPLETE")
        self.assertIn("1.0 of 60.0 min", c["issues"][0])

    def test_holes_are_found_even_without_a_report(self):
        c = capture_check(rows([(0, 60), (60, 120), (300, 360)]))
        self.assertEqual(c["state"], "INCOMPLETE")
        self.assertEqual(c["gaps"], [[int(T + 120), 180.0]])

    def test_sound(self):
        self.assertEqual(capture_check(rows([(0, 60)], audio=False), {"expected_s": 60})["sound"], "missing")
        c = capture_check(rows([(0, 60), (60, 120)], audio=True, max_db=-91.0), {"expected_s": 120})
        self.assertEqual(c["sound"], "silent")
        self.assertTrue(any("silence" in i for i in c["issues"]))
        self.assertEqual(capture_check(rows([(0, 60)]), {"expected_s": 60})["sound"], "unknown")

    def test_no_video(self):
        self.assertEqual(capture_check([], {"expected_s": 600})["state"], "NO VIDEO")
        self.assertEqual(capture_check([{"name": "x", "pc_start_epoch": "bad"}])["state"], "NO VIDEO")

    def test_real_length_beats_the_clock_span(self):
        # the segment says 60 s on the clock but holds only 5 s of video (an encoder that hung)
        c = capture_check(rows([(0, 60), (60, 120)], duration=5.0), {"expected_s": 120})
        self.assertEqual((c["state"], c["recorded_s"]), ("INCOMPLETE", 10))

    def test_a_finished_episode_not_recorded_from_start_to_end(self):
        r = rows([(0, 60), (60, 120)])
        ok = capture_check(r, {"expected_s": 120, "live": False, "duration": 120, "played_from": 0, "played_to": 120})
        self.assertEqual(ok["state"], "COMPLETE")
        late = capture_check(r, {"expected_s": 120, "live": False, "duration": 3600, "played_from": 0, "played_to": 120})
        self.assertEqual(late["state"], "INCOMPLETE")
        self.assertIn("recorded from 0.0 to 2.0 of its 60.0 min", late["issues"][-1])
        live = capture_check(r, {"expected_s": 120, "live": True, "duration": None, "played_from": 900, "played_to": 1020})
        self.assertEqual(live["state"], "COMPLETE")                 # a live stream has no start / end to compare

    def test_report_is_cleaned(self):
        rep = clean_report({"key": KEY, "episode_id": "63661233", "expected_s": 3600, "live": True,
                            "target": {"kind": "sprint", "label": "Sprint", "password": "x"},
                            "issues": [{"at": T, "text": "stall"}] * 50, "password": "secret", "recoveries": "lots"})
        self.assertNotIn("password", json.dumps(rep))
        self.assertEqual(len(rep["issues"]), 30)
        self.assertNotIn("recoveries", rep)
        self.assertEqual((rep["live"], rep["expected_s"]), (True, 3600.0))
        self.assertIsNone(clean_report("nope"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient
        import server.app as appmod
        self.d = Path(tempfile.mkdtemp())
        env = {"F1DASH_VOYO_SERVER_PLAYER_ENABLED": "true", "F1DASH_VOYO_RECORDING_PATH": str(self.d / "rec"),
               "F1DASH_VOYO_RECORDING_REQUIRE_MOUNT": "", "F1DASH_VOYO_RECORDING_MIN_FREE_BYTES": "0"}
        self._p = [mock.patch.dict(os.environ, env), mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}),
                   mock.patch.object(appmod, "DATA_DIR", self.d)]
        for p in self._p:
            p.start()
        cfg = load_config()
        cfg["source"]["mode"] = "test"
        self.app = appmod.create_app(cfg)
        self.c = TestClient(self.app)
        self.rec = self.d / "rec"

    def tearDown(self):
        for p in self._p:
            p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def post(self, pb, key=KEY, **kw):
        body = {"playback_time": pb, "paused": False, "playback_rate": 1.0, "timestamp_local": time.time(),
                "channel": "server_player", "asset": "voyo-media:63661233", "page": {"media_id": "63661233"},
                "session_hint": {"meeting": "Chinese Grand Prix", "session_name": "Sprint"},
                **({"recording": {"key": key, "episode_id": "63661233", "title": "Sprint", "kind": "sprint",
                                  "format": "hls", "verified": "player mediaId 63661233", "live": True}} if key else {}),
                **kw}
        r = self.c.post("/api/sync/voyo", content=json.dumps(body), headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["recording"]

    def put(self, iid, name, start, end, **headers):
        h = {"Content-Length": "4", "X-Capture-Start": f"{start:.3f}", "X-Capture-End": f"{end:.3f}", **headers}
        r = self.c.put(f"/api/voyo/recordings/{iid}/capture/{name}", content=b"abcd", headers=h)
        self.assertEqual(r.status_code, 200, r.text)

    def manifest(self, iid=KEY):
        return json.loads((self.rec / iid / "manifest.json").read_text())

    def test_one_package_per_session_whatever_the_video_does(self):
        a = self.post(5)
        self.assertEqual(a["instance"], KEY)
        # a page reload, the live window's length changing, another media id on the page in between:
        # the old heuristics would have opened new packages - the key keeps the one package
        self.post(1, page={"media_id": "63661233", "load_id": "reload2"})
        self.post(30, duration=3000.0)
        self.post(40, duration=2990.0)
        self.post(41, page={"media_id": "99999999"}, asset="voyo-media:99999999")
        self.assertEqual(sorted(p.name for p in self.rec.iterdir() if p.is_dir()), [KEY])
        m = self.manifest()
        self.assertEqual((m["episode"]["id"], m["episode"]["format"], m["channel"]), ("63661233", "hls", "server_player"))
        self.assertIsNone(self.post(42)["blocked"])

    def test_bad_keys_are_not_package_ids(self):
        for key in ("../../etc", "sprint-63661233", "SPRINT-1-2", "x" * 80, "sprint-63661233-2026032"):
            a = self.post(5, key=None, recording={"key": key})
            self.assertNotEqual(a["instance"], key)

    def test_close_with_report_checks_the_video(self):
        self.post(5)
        self.put(KEY, "run1_seg_00000.mp4", T, T + 60, **{"X-Capture-Duration": "60.0", "X-Capture-Audio": "1",
                                                            "X-Capture-Video": "1", "X-Capture-Audio-Max-Db": "-9.5"})
        rep = {"key": KEY, "episode_id": "63661233", "expected_s": 3600, "recording_s": 3600, "state": "DONE",
               "end_reason": "the session window is over", "issues": [{"at": T, "text": "the video stalled"}]}
        self.post(0, close=True, why="the session window is over", report=rep)
        m = self.manifest()
        self.assertEqual(m["status"], "closed")
        self.assertEqual(m["player_report"]["end_reason"], "the session window is over")
        chk = m["capture"]["check"]
        self.assertEqual((chk["state"], chk["recorded_s"], chk["expected_s"], chk["sound"]),
                         ("INCOMPLETE", 60, 3600, "ok"))
        line = json.loads((self.rec / KEY / "capture" / "capture.jsonl").read_text().splitlines()[0])
        self.assertEqual((line["duration"], line["audio"], line["video"], line["max_db"]), (60.0, True, True, -9.5))
        idx = json.loads((self.rec / "index.json").read_text())["recordings"][KEY]
        self.assertEqual((idx["capture_check"], idx["capture_recorded_s"], idx["episode_id"]), ("INCOMPLETE", 60, "63661233"))

    def test_late_segments_complete_a_closed_recording(self):
        self.post(5)
        self.put(KEY, "run1_seg_00000.mp4", T, T + 60)
        self.post(0, close=True, why="done", report={"key": KEY, "expected_s": 120})
        self.assertEqual(self.manifest()["capture"]["check"]["state"], "INCOMPLETE")
        self.put(KEY, "run1_seg_00001.mp4", T + 60, T + 120)          # left in the spool, uploaded after
        self.assertEqual(self.manifest()["capture"]["check"]["state"], "COMPLETE")

    def test_close_after_a_server_restart_closes_that_package(self):
        from starlette.testclient import TestClient
        import server.app as appmod
        self.post(5)
        cfg = load_config()                                   # the dashboard server restarts mid-recording
        cfg["source"]["mode"] = "test"
        self.c = TestClient(appmod.create_app(cfg))
        self.assertEqual(self.manifest()["status"], "interrupted")
        self.assertEqual(self.post(20)["instance"], KEY)           # the player goes on: the same package
        self.assertEqual(self.manifest()["resumes"][-1]["reason"], "resumed")
        self.c = TestClient(appmod.create_app(cfg))           # ... and restarts once more before the end
        a = self.post(0, close=True, why="the recording ended", report={"key": KEY, "expected_s": 60})
        self.assertIsNone(a["instance"])
        m = self.manifest()
        self.assertEqual((m["status"], m["player_report"]["key"]), ("closed", KEY))
        self.assertEqual(m["capture"]["check"]["state"], "NO VIDEO")

    def test_server_says_when_it_takes_no_video(self):
        a = self.post(5)
        self.assertIsNone(a["blocked"])
        with mock.patch("server.voyo_recording.VoyoStreamRecorder.capture_space_ok", return_value=False):
            b = self.post(6)
        self.assertEqual((b["capture"], b["blocked"]), (False, "disk full (min_free_bytes)"))


class StatusTest(unittest.TestCase):
    def state(self, hb, cur=None):
        rec = mock.Mock()
        rec.status.return_value = {"enabled": True, "error": None, "space_ok": True, "path": "/x"}
        rec.cur = None
        prec = mock.Mock()
        prec.cur = cur
        prec.last_sample_wall = time.time() if cur else 0
        return admin.current_state(rec, prec, True, hb, 5.0)

    def test_session_states_are_said(self):
        for st, name, level in (("NOT_FOUND", "NOT FOUND", "bad"), ("AMBIGUOUS", "AMBIGUOUS", "bad"),
                                ("FAILED", "FAILED", "bad"), ("VERIFYING", "OPENING", "ok")):
            s = self.state({"state": "open", "recording": {"state": st, "target": {"label": "Chinese GP Sprint"},
                                                           "problem": "no Sprint recording on the page",
                                                           "candidates": [{"id": "1", "title": "Sprint"}]}})
            self.assertEqual((s["state"], s["level"]), (name, level), st)
            self.assertIn("Chinese GP Sprint", s["detail"])
        self.assertEqual(s["candidates"][0]["id"], "1")

    def test_recording_shows_episode_and_problem(self):
        cur = {"stream_instance_id": KEY, "session": {}, "capture": {"segments": ["a"], "bytes": 10},
               "episode": {"id": "63661233", "format": "hls"}, "detected_at_epoch": time.time() - 60}
        s = self.state({"state": "open", "recording": {"state": "RECORDING", "target": {"label": "Sprint"},
                                                       "problem": "no sound from the player for 95 s", "recording_s": 60}},
                       cur)
        self.assertEqual((s["state"], s["level"], s["episode_id"]), ("RECORDING", "warn", "63661233"))
        self.assertIn("VOYO episode 63661233 (HLS)", s["detail"])
        self.assertIn("no sound", s["detail"])


if __name__ == "__main__":
    unittest.main()
