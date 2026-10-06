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


if __name__ == "__main__":
    unittest.main()
