"""Scheduled vs ACTUAL session start, delayed starts and MARK STREAM START (server/sync.py).

Live scenarios drive the SyncManager like the engine does: F1 feed messages (clock A, the
feed's "now" = src_now) and VOYO playback samples (clock C) of a stream that is behind live.
The VOD scenario uses the real 2026 Japanese GP race (tests/fixtures): the start was delayed
("FORMATION LAP WILL START AT 14:10"), lights out at 05:14:02.078 UTC, 14 min after the
scheduled 05:00.

Run:  python -m unittest tests.test_sync_start
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sync import SyncManager, parse_voyo_sample  # noqa: E402

SCHED = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc).timestamp() * 1000      # 15:00 CEST race
MIN = 60_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LiveRace:
    """A live race on the feed + a VOYO live stream ``stream_delay`` seconds behind it."""

    def __init__(self, test, stream_delay_s, start_ms, notices=(), reaction=0.0, start_via="SessionStatus"):
        self.start_via = start_via            # SessionStatus | StatusSeries | clock (ExtrapolatedClock only)
        self.t = test
        self.sync = SyncManager({"enabled": True, "mode": "AUTO", "broadcast_delay_seconds": 5.0,
                                 "mark_reaction_seconds": reaction},
                                voyo_enabled=True, legacy_delay=0.0, source_speed=1.0, store_path=None)
        self.delay = stream_delay_s * 1000
        self.start = start_ms                 # actual start (lights out) on F1's clock
        self.notices = list(notices)          # [(ms, text)]
        self.now = SCHED - 30 * MIN           # F1 clock (the feed's now)
        self.mono = 1000.0
        self.pb0 = 1800.0                     # video position at the beginning of the test
        self.started = False
        self.laps = 0
        self.sync.observe_feed("SessionInfo", {"Key": 9999, "Name": "Race", "Type": "Race",
                                               "StartDate": iso(SCHED)[:19], "GmtOffset": "00:00:00",
                                               "Meeting": {"Name": "Test Grand Prix"}}, self.now, True)
        self.sync.observe_feed("TimingData", {"Lines": {"1": {"NumberOfLaps": 0}}}, self.now, True)
        self.sync.observe_feed("ExtrapolatedClock", {"Utc": iso(self.now), "Remaining": "02:00:00",
                                                     "Extrapolating": False}, self.now, True)
        self.step(0)

    def pb(self):
        """Video position: the stream shows F1 time now - delay; 1 video s = 1 F1 s."""
        return self.pb0 + (self.now - self.delay - (SCHED - 30 * MIN)) / 1000

    def step(self, seconds, paused=False):
        end = self.now + seconds * 1000
        while True:
            for ms, text in list(self.notices):
                if ms <= self.now:
                    self.sync.observe_feed("RaceControlMessages", {"Messages": [{"Utc": iso(ms), "Message": text,
                                                                                 "Category": "Other"}]}, ms, False)
                    self.notices.remove((ms, text))
            if not self.started and self.start is not None and self.now >= self.start:
                self.started = True
                if self.start_via == "SessionStatus":
                    self.sync.observe_feed("SessionStatus", {"Status": "Started"}, self.start, False)
                elif self.start_via == "StatusSeries":
                    self.sync.observe_feed("SessionData", {"StatusSeries": {"2": {"Utc": iso(self.start),
                                                                                  "SessionStatus": "Started"}}},
                                           self.start, False)
                else:                         # the session clock starts running (posted 2 s later)
                    self.sync.observe_feed("ExtrapolatedClock", {"Utc": iso(self.start + 2000),
                                                                 "Remaining": "01:59:58"}, self.start + 2000, False)
            if self.started and self.now >= self.start + (self.laps + 1) * 90_000:
                self.laps += 1                # lap completed (line crossing of car 1)
                self.sync.observe_feed("TimingData", {"Lines": {"1": {"NumberOfLaps": self.laps}}},
                                       self.start + self.laps * 90_000, False)
            body = {"playback_time": self.pb(), "paused": paused, "playback_rate": 1.0, "ready_state": 4,
                    "asset": "voyo-live|f1"}
            self.sync.update(parse_voyo_sample(body, 0.0, self.mono), self.now)
            self.sync.target(self.mono, self.now)
            if self.now >= end:
                return
            step = min(500.0, end - self.now)
            self.now += step
            self.mono += step / 1000

    def to_video_showing(self, f1_ms, paused=True):
        """Run until the video shows F1 time f1_ms (then pause on it)."""
        self.step((f1_ms + self.delay - self.now) / 1000)
        self.step(0, paused=paused)

    def shown(self):
        return self.sync.target(self.mono, self.now).ms

    def state(self):
        return self.sync.get_state(self.mono, self.now, None)


class StartStateTest(unittest.TestCase):
    def test_1_normal_gp_no_delay(self):
        r = LiveRace(self, stream_delay_s=30, start_ms=SCHED)
        r.step(10 * 60)
        self.assertEqual(r.state()["startInfo"]["state"], "PRE_START")
        r.to_video_showing(SCHED)
        msg = r.sync.add_event_anchor("start", r.mono, r.now, None, "START")             # lights out
        self.assertIn("STREAM DELAY +0:30", msg)
        st = r.state()
        self.assertEqual(st["confidence"], "HIGH")
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 0.0)
        self.assertFalse(st["startInfo"]["delayed"])
        self.assertAlmostEqual(st["streamDelaySeconds"], 30.0, delta=0.1)
        self.assertAlmostEqual(r.shown(), SCHED, delta=50)

    def test_2_gp_delayed_by_4_minutes(self):
        r = LiveRace(self, stream_delay_s=10, start_ms=SCHED + 4 * MIN,
                     notices=[(SCHED - 2 * MIN, "RACE START DELAYED")])
        with self.assertLogs("sync", "INFO") as logs:
            r.step(31 * 60)                                      # 15:01: scheduled start passed, no start
        st = r.state()["startInfo"]
        self.assertEqual(st["state"], "DELAYED")
        self.assertIsNone(st["actualUtc"])
        self.assertTrue(st["scheduledIgnored"])
        text = "\n".join(logs.output)
        self.assertIn("[SYNC] Delayed start detected: RACE START DELAYED", text)
        self.assertIn("IGNORED as the race-time anchor", text)
        # the broadcast says "the race starts in 4 minutes" - never counted against the 15:00 schedule
        msg = r.sync.add_countdown_anchor(4 * 60, r.mono, r.now, "4:00")
        self.assertIn("start was delayed", msg)
        self.assertEqual(r.sync.mapping.confidence, "LOW")       # unchanged: still the estimate
        r.step(5 * 60)                                           # the actual start happens at 15:04
        st = r.state()["startInfo"]
        self.assertIn(st["state"], ("STARTED", "RUNNING"))
        self.assertEqual(st["actualUtc"], iso(SCHED + 4 * MIN)[11:23])
        self.assertEqual(st["f1DelaySeconds"], 240.0)

    def test_2b_late_start_without_announcement_after_grace(self):
        r = LiveRace(self, stream_delay_s=10, start_ms=None)
        r.step(30 * 60 + 3 * 60)                                 # 15:03 - still inside the formation-lap grace
        self.assertEqual(r.state()["startInfo"]["state"], "PRE_START")
        r.step(4 * 60)                                           # 15:07, no start: delayed
        st = r.state()["startInfo"]
        self.assertEqual(st["state"], "DELAYED")
        self.assertEqual(st["waitingSeconds"], 420)

    def test_3_stream_delayed_f1_on_time(self):
        r = LiveRace(self, stream_delay_s=4 * 60, start_ms=SCHED)
        r.to_video_showing(SCHED)
        self.assertIn("STREAM DELAY +4:00", r.sync.add_event_anchor("start", r.mono, r.now, None, "START"))
        st = r.state()
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 0.0)
        self.assertAlmostEqual(st["lightsOut"]["last"]["streamDelaySeconds"], 240.0, delta=0.1)
        self.assertAlmostEqual(r.shown(), SCHED, delta=50)

    def test_4_both_f1_and_voyo_delayed_are_not_combined(self):
        # scheduled 15:00, actual race 15:04, the stream reaches the start at 15:08
        r = LiveRace(self, stream_delay_s=4 * 60, start_ms=SCHED + 4 * MIN,
                     notices=[(SCHED - MIN, "FORMATION LAP WILL START AT 13:04")])
        r.to_video_showing(SCHED + 4 * MIN)
        self.assertAlmostEqual(r.now, SCHED + 8 * MIN, delta=1)
        with self.assertLogs("sync", "INFO") as logs:
            msg = r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        self.assertIn("STREAM DELAY +4:00", msg)                 # NOT +8:00 (vs the 15:00 schedule)
        st = r.state()
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 240.0)
        self.assertAlmostEqual(st["lightsOut"]["last"]["streamDelaySeconds"], 240.0, delta=0.1)
        self.assertAlmostEqual(st["streamDelaySeconds"], 240.0, delta=0.1)
        self.assertAlmostEqual(r.shown(), SCHED + 4 * MIN, delta=50)   # the video maps to the ACTUAL start
        text = "\n".join(logs.output)
        for line in ("[SYNC] Looking for Lights Out event", "[SYNC] VOYO reference time:",
                     "[SYNC] Lights Out F1 timestamp: 13:04:00.000 UTC (scheduled 13:00:00.000 UTC, race delay +4:00)",
                     "(stream delay +4:00)", "[SYNC] Sync confidence: HIGH"):
            self.assertIn(line, text)

    def test_5b_reaction_time_while_playing(self):
        r = LiveRace(self, stream_delay_s=60, start_ms=SCHED, reaction=0.2)
        r.to_video_showing(SCHED + 200, paused=False)            # L pressed 0.2 s after lights out was shown
        r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        self.assertAlmostEqual(r.state()["lightsOut"]["last"]["streamDelaySeconds"], 60.0, delta=0.1)
        self.assertAlmostEqual(r.shown(), SCHED + 200, delta=60)

    def test_8_sync_after_a_delayed_start(self):
        r = LiveRace(self, stream_delay_s=150, start_ms=SCHED + 7 * MIN,
                     notices=[(SCHED + MIN, "RACE START DELAYED")])
        r.to_video_showing(SCHED + 7 * MIN)
        r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        # the race runs; the video keeps following F1 time from the ACTUAL start
        r.step(60)
        self.assertAlmostEqual(r.now - r.shown(), 150_000, delta=600)
        # a line crossing marked when the video shows it agrees -> two independent anchors
        cross = SCHED + 7 * MIN + 2 * 90_000
        r.to_video_showing(cross)
        msg = r.sync.add_event_anchor("lap", r.mono, r.now, "1", "CAR 1")
        self.assertIn("LAP", msg)
        st = r.state()
        self.assertEqual(st["confidence"], "HIGH")
        self.assertIn("independent anchors agree", st["reason"])
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 420.0)
        self.assertAlmostEqual(st["streamDelaySeconds"], 150.0, delta=0.6)
        r.step(5 * 60)
        self.assertAlmostEqual(r.now - r.shown(), 150_000, delta=600)

    def test_aborted_start_l_key_uses_the_actual_start(self):
        r = LiveRace(self, stream_delay_s=20, start_ms=None)
        aborted, real = SCHED + MIN, SCHED + 12 * MIN
        r.step(30 * 60 + 60)
        r.sync.observe_feed("SessionStatus", {"Status": "Started"}, aborted, False)
        r.sync.observe_feed("SessionStatus", {"Status": "Aborted"}, aborted + 20_000, False)
        r.step(11 * 60)
        r.sync.observe_feed("SessionStatus", {"Status": "Started"}, real, False)
        r.step(0)
        r.sync.observe_feed("TimingData", {"Lines": {"1": {"NumberOfLaps": 1}}}, real + 90_000, False)
        self.assertEqual(r.sync.start_info(None)["actual"], real)        # not the aborted one, not 15:00
        r.to_video_showing(real)
        r.sync.clear_anchor()
        r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        self.assertAlmostEqual(r.shown(), real, delta=50)


class VodDelayedJapanTest(unittest.TestCase):
    """Real data: 2026 Japanese GP race - start delayed, lights out 14 min after the schedule."""

    def setUp(self):
        from server.openf1 import ref_events_from_openf1
        from server.telemetry import parse_utc
        fix = json.loads((Path(__file__).resolve().parent / "fixtures" / "openf1_2026_japan.json").read_text())
        self.session = {**next(x for x in fix["sessions"] if x["session_key"] == 11253), "gmt_offset": "09:00:00"}
        self.ref = ref_events_from_openf1(fix["laps"], fix["race_control"])
        self.sched = parse_utc(self.session["date_start"]).timestamp() * 1000
        self.start = parse_utc("2026-03-29T05:14:02.078Z").timestamp() * 1000
        self.K = (self.sched - (28 * 60 + 50) * 1000) / 1000     # video 0:00 = 04:31:10 UTC
        self.s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0,
                             store_path=None, vod=True)
        self.s.initialize(self.session, self.ref)
        self.mono = 0.0

    def at(self, f1_ms):
        self.mono += 1.0
        self.s.update(parse_voyo_sample({"playback_time": f1_ms / 1000 - self.K, "paused": True,
                                         "asset": "voyo-media:jp"}, 0.0, self.mono), 0.0)
        self.s.target(self.mono, 0.0)

    def test_notice_and_actual_start_from_the_data(self):
        info = self.s.start_info(None)
        self.assertEqual(info["actual"], self.start)
        self.assertAlmostEqual(info["f1_delay"], 14 * 60 + 2.078, places=2)
        self.assertTrue(info["delayed"])
        self.assertEqual(info["notice"], "FORMATION LAP WILL START AT 14:10")
        self.assertEqual(info["announced"], self.sched + 10 * MIN)      # 14:10 JST = 05:10 UTC

    def test_countdown_auto_refused_explicit_targets(self):
        self.at(self.sched - 10 * MIN)
        self.assertIn("start was delayed", self.s.add_countdown_anchor(600, self.mono, 0.0, "10:00"))
        # the countdown counted to the announced 14:10: video shows 05:00 UTC
        self.assertIn("COUNTDOWN", self.s.add_countdown_anchor(600, self.mono, 0.0, "10:00", target="announced"))
        self.assertAlmostEqual(self.s.mapping.offset, self.K + 600, places=2)
        self.assertIn("announced start", self.s.mapping.anchor)

    def test_lights_out_is_high_and_the_start_is_in_the_video(self):
        self.at(self.start)
        msg = self.s.add_event_anchor("start", self.mono, 0.0, None, "START")
        self.assertIn("HIGH", msg)
        st = self.s.get_state(self.mono, 0.0, None)
        self.assertAlmostEqual(st["offsetSeconds"], self.K, places=2)
        self.assertIsNone(st["streamDelaySeconds"])                     # a recording has no live delay
        self.assertAlmostEqual(st["startInfo"]["actualStartVideo"], self.start / 1000 - self.K, places=1)
        self.assertAlmostEqual(st["startInfo"]["f1DelaySeconds"], 842.1, places=1)
        self.assertEqual(st["offsetDisplayLabel"], "scheduled start at video")


class ApiTest(unittest.TestCase):
    def test_remote_and_menu_actions(self):
        import tempfile
        from starlette.testclient import TestClient
        from server.app import create_app
        from server.config import load_config
        tmp = tempfile.mkdtemp()
        cfg = load_config(Path(tmp) / "none.toml")
        cfg["source"]["mode"] = "test"
        cfg["voyo"]["check_reachability"] = False
        cfg["f1_tv"]["auth_file"] = str(Path(tmp) / "auth.json")
        cfg["f1_tv"]["open_browser"] = False
        with TestClient(create_app(cfg)) as c:
            r = c.post("/api/sync/stream_start", json={}).json()
            self.assertIn("result", r)
            self.assertIn("startInfo", r["state"])
            self.assertIn("streamDelaySeconds", r["state"])
            r = c.post("/api/sync/stream_reset", json={}).json()
            self.assertIn("result", r)
            bad = c.post("/api/sync/countdown", json={"countdown": "4:00|tomorrow"}).json()
            self.assertFalse(bad["ok"])
            with c.websocket_connect("/ws?client=remote") as phone:
                phone.receive_json()
                phone.send_json({"type": "command", "command": "SYNC_STREAM_START"})
                phone.send_json({"type": "command", "command": "SYNC_STREAM_RESET"})
                phone.send_json({"type": "sync_action", "action": "stream_start"})
                for _ in range(200):
                    m = phone.receive_json()
                    if m["type"] == "sync_result":
                        self.assertEqual(m["action"], "stream_start")
                        break
                else:
                    self.fail("no sync_result")


if __name__ == "__main__":
    unittest.main()
