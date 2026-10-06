"""START OF STREAM (MARK STREAM START): VOYO 0:00 = the beginning of THIS VOYO broadcast.

Not the race start / lights out / the schedule. No F1 topic contains the broadcast origin, so its
F1 time comes from an F1 reference of the same video (lights out is the best) and is saved per
session + video; LIVE estimates it from the moment 0:00 airs. A recording never uses today's
wall clock.

Run:  python -m unittest tests.test_stream_origin
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.openf1 import RefEvents  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from tests.test_sync_start import MIN, SCHED, LiveRace, iso  # noqa: E402

ORIGIN = SCHED - 30 * MIN                 # VOYO broadcast begins 30 min before the scheduled start


def session(key=9001, name="Race", meeting="Singapore Grand Prix", start=SCHED):
    return {"session_key": key, "session_name": name, "session_type": "Race" if name == "Race" else "Qualifying",
            "meeting_name": meeting, "date_start": iso(start), "gmt_offset": "08:00:00"}


def ref_with(start_ms, laps=3):
    ref = RefEvents(source="archive")
    ref.add_start(start_ms, "SessionData.StatusSeries")
    for n in range(1, laps + 1):
        ref.add_crossing("1", start_ms + n * 90_000, n)
    return ref


class Vod:
    """A VOYO recording whose 0:00 is ``origin`` (unknown to the system)."""

    def __init__(self, store, sess, ref, origin=ORIGIN, asset="voyo-media:sgp-race"):
        self.s = SyncManager({"enabled": True, "mark_reaction_seconds": 0.0}, voyo_enabled=True, legacy_delay=0,
                             source_speed=1.0, store_path=store, vod=True)
        self.s.initialize(sess, ref)
        self.origin, self.asset, self.mono = origin, asset, 0.0

    def at_pb(self, pb):
        self.mono += 1.0
        self.s.update(parse_voyo_sample({"playback_time": pb, "paused": True, "asset": self.asset}, 0.0, self.mono),
                      0.0)
        self.s.target(self.mono, 0.0)

    def at(self, f1_ms):
        self.at_pb((f1_ms - self.origin) / 1000)

    def mark(self):
        return self.s.mark_stream_start(self.mono, 0.0)

    def lights_out(self):
        return self.s.add_event_anchor("start", self.mono, 0.0, None, "START")

    def state(self):
        return self.s.get_state(self.mono, 0.0, None)

    def shown(self):
        return self.s.target(self.mono, 0.0).ms


class LiveFromZero(LiveRace):
    """Live race whose VOYO stream begins (0:00) at ORIGIN, shown ``stream_delay`` behind live."""

    def pb(self):
        return max(0.0, (self.now - self.delay - ORIGIN) / 1000)


class StreamOriginTest(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp()) / "sync_calibration.json"

    # 1 ------------------------------------------------------------------------------------------
    def test_live_start_of_stream(self):
        r = LiveFromZero(self, stream_delay_s=40, start_ms=SCHED)
        r.to_video_showing(ORIGIN)                               # 0:00 airs (40 s behind live)
        self.assertEqual(r.pb(), 0.0)
        msg = r.sync.mark_stream_start(r.mono, r.now)
        self.assertIn("STREAM START 0:00 SET", msg)
        st = r.state()
        self.assertEqual(st["method"], "Stream start (0:00)")
        self.assertEqual(st["confidence"], "LOW")                 # the stream delay is only estimated (5 s)
        self.assertAlmostEqual(r.sync.mapping.offset * 1000, ORIGIN + 35_000, delta=1)
        # lights out makes it exact and fixes the origin
        r.to_video_showing(SCHED)
        self.assertIn("HIGH", r.sync.add_event_anchor("start", r.mono, r.now, None, "START"))
        st = r.state()
        self.assertEqual(st["streamStart"]["originUtc"], iso(ORIGIN)[11:23])
        self.assertEqual(st["streamStart"]["confidence"], "HIGH")
        self.assertEqual(st["streamStart"]["actualAtVideo"], 1800.0)

    def test_live_refuses_when_not_at_zero(self):
        r = LiveFromZero(self, stream_delay_s=10, start_ms=SCHED)
        r.to_video_showing(ORIGIN + 60_000)
        self.assertIn("move it to the absolute beginning", r.sync.mark_stream_start(r.mono, r.now))
        self.assertIsNone(r.state()["streamStart"])

    # 2 ------------------------------------------------------------------------------------------
    def test_old_gp_vod_start_of_stream(self):
        v = Vod(self.store, session(), ref_with(SCHED))
        v.at_pb(0.0)
        msg = v.mark()
        self.assertIn("stream anchor set but F1 reference unavailable", msg)
        st = v.state()
        self.assertTrue(st["streamStart"]["set"])
        self.assertIsNone(st["streamStart"]["originUtc"])
        self.assertEqual(st["confidence"], "UNSYNCED")            # nothing invented
        v.at(SCHED)                                              # lights out on the video
        self.assertIn("HIGH", v.lights_out())
        st = v.state()
        self.assertEqual(st["streamStart"]["originUtc"], iso(ORIGIN)[11:23])
        self.assertEqual(st["streamStart"]["scheduledAtVideo"], 1800.0)
        self.assertEqual(st["streamStart"]["actualAtVideo"], 1800.0)
        # the other order: lights out first, then 0:00
        w = Vod(Path(tempfile.mkdtemp()) / "c.json", session(), ref_with(SCHED))
        w.at(SCHED)
        w.lights_out()
        w.at_pb(0.0)
        self.assertIn(f"0:00 = {iso(ORIGIN)[11:19]} UTC", w.mark())

    # 3 ------------------------------------------------------------------------------------------
    def test_reopening_an_old_vod(self):
        v = Vod(self.store, session(), ref_with(SCHED))
        v.at_pb(0.0)
        v.mark()
        v.at(SCHED)
        v.lights_out()
        v.s.clear_anchor()                                       # only the stream start is left
        self.assertEqual(v.s.mapping.source, "stream-origin")
        # days later: dashboard restarted, the same VOD opened again
        again = Vod(self.store, session(), ref_with(SCHED))
        again.at(SCHED + 20 * MIN)
        st = again.state()
        self.assertTrue(st["streamStart"]["restored"])
        self.assertEqual(st["confidence"], "HIGH")
        self.assertEqual(st["method"], "Stream start (0:00)")
        self.assertAlmostEqual(again.shown(), SCHED + 20 * MIN, delta=50)

    # 4 + 5 --------------------------------------------------------------------------------------
    def test_anchors_per_gp_session_and_video(self):
        jp_origin = SCHED - 45 * MIN
        sgp = Vod(self.store, session(), ref_with(SCHED))
        sgp.at_pb(0.0)
        sgp.mark()
        sgp.at(SCHED)
        sgp.lights_out()
        jp = Vod(self.store, session(key=8001, meeting="Japanese Grand Prix"), ref_with(SCHED), origin=jp_origin,
                 asset="voyo-media:jpn-race")
        jp.at_pb(0.0)
        jp.mark()
        jp.at(SCHED)
        jp.lights_out()
        # Singapore qualifying (other session key) - even on the same video id: nothing reused
        q = Vod(self.store, session(key=9000, name="Qualifying"), ref_with(SCHED - 86_400_000))
        q.at_pb(0.0)
        self.assertIsNone(q.state()["streamStart"])
        self.assertIn("UNSYNCED", q.state()["confidence"])
        # each reopened VOD gets its own origin
        a = Vod(self.store, session(), ref_with(SCHED))
        a.at_pb(10.0)
        b = Vod(self.store, session(key=8001, meeting="Japanese Grand Prix"), ref_with(SCHED), origin=jp_origin,
                asset="voyo-media:jpn-race")
        b.at_pb(10.0)
        self.assertEqual(a.state()["streamStart"]["originUtc"], iso(ORIGIN)[11:23])
        self.assertEqual(b.state()["streamStart"]["originUtc"], iso(jp_origin)[11:23])
        # the Singapore race on another video (another broadcast) does not reuse it either
        other = Vod(self.store, session(), ref_with(SCHED), asset="voyo-media:sgp-race-replay")
        other.at_pb(0.0)
        self.assertIsNone(other.state()["streamStart"])

    # 6 + 9 --------------------------------------------------------------------------------------
    def test_delayed_gp_does_not_move_the_stream_start(self):
        actual = SCHED + 4 * MIN
        ref = ref_with(actual)
        ref.add_notice(SCHED - MIN, "RACE START DELAYED")
        v = Vod(self.store, session(), ref)
        v.at_pb(0.0)
        v.mark()
        v.at(actual)
        v.lights_out()
        ss = v.state()["streamStart"]
        self.assertEqual(ss["originUtc"], iso(ORIGIN)[11:23])     # not 13:00 / 13:04
        self.assertEqual(ss["scheduledAtVideo"], 1800.0)
        self.assertEqual(ss["actualAtVideo"], 2040.0)
        self.assertEqual(v.state()["startInfo"]["f1DelaySeconds"], 240.0)

    def test_live_f1_delayed_voyo_normal(self):
        r = LiveFromZero(self, stream_delay_s=5, start_ms=SCHED + 4 * MIN,
                         notices=[(SCHED - MIN, "START DELAYED")])
        r.to_video_showing(ORIGIN)
        r.sync.mark_stream_start(r.mono, r.now)
        r.to_video_showing(SCHED + 4 * MIN)
        msg = r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        self.assertIn("STREAM DELAY +0:05", msg)
        st = r.state()
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 240.0)
        self.assertEqual(st["streamStart"]["originUtc"], iso(ORIGIN)[11:23])

    # 7 ------------------------------------------------------------------------------------------
    def test_start_of_stream_and_lights_out_coexist(self):
        v = Vod(self.store, session(), ref_with(SCHED))
        v.at_pb(0.0)
        v.mark()
        v.at(SCHED)
        v.lights_out()
        st = v.state()
        self.assertEqual(st["method"], "Lights out")              # the F1 event drives the sync
        self.assertEqual([a["kind"] for a in st["anchors"]], ["start"])
        self.assertIsNotNone(st["streamStart"]["originUtc"])     # the origin is derived from it
        self.assertTrue(st["lightsOut"]["last"]["found"])
        # resetting the stream start keeps lights out
        self.assertIn("STREAM START RESET", v.s.reset_stream_start())
        self.assertEqual(v.state()["method"], "Lights out")
        self.assertEqual(v.state()["confidence"], "HIGH")

    # 8 ------------------------------------------------------------------------------------------
    def test_live_voyo_delayed_f1_on_time(self):
        r = LiveFromZero(self, stream_delay_s=4 * 60, start_ms=SCHED)
        r.to_video_showing(ORIGIN)
        r.sync.mark_stream_start(r.mono, r.now)
        r.to_video_showing(SCHED)
        self.assertIn("STREAM DELAY +4:00", r.sync.add_event_anchor("start", r.mono, r.now, None, "START"))
        st = r.state()
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 0.0)
        self.assertEqual(st["streamStart"]["originUtc"], iso(ORIGIN)[11:23])

    # 10 -----------------------------------------------------------------------------------------
    def test_live_both_delayed(self):
        lo = SCHED + 4 * MIN
        r = LiveFromZero(self, stream_delay_s=4 * 60, start_ms=lo, notices=[(SCHED - MIN, "RACE START DELAYED")])
        r.to_video_showing(ORIGIN)
        r.sync.mark_stream_start(r.mono, r.now)
        r.to_video_showing(lo)
        self.assertIn("STREAM DELAY +4:00", r.sync.add_event_anchor("start", r.mono, r.now, None, "START"))
        st = r.state()
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 240.0)
        self.assertEqual(st["streamStart"]["originUtc"], iso(ORIGIN)[11:23])      # not shifted by 4 or 8 min
        self.assertEqual(st["streamStart"]["actualAtVideo"], 2040.0)

    # 11 -----------------------------------------------------------------------------------------
    def test_vod_never_uses_todays_wall_clock(self):
        weeks_later = datetime(2026, 12, 24, 9, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch("server.sync.time.time", return_value=weeks_later):
            v = Vod(self.store, session(), ref_with(SCHED))
            v.at_pb(0.0)
            v.mark()
            self.assertIsNone(v.state()["streamStart"]["originUtc"])     # NOT "now"
            self.assertIsNone(v.s.mapping.offset)
            v.at(SCHED)
            v.lights_out()
            self.assertEqual(v.state()["streamStart"]["originUtc"], iso(ORIGIN)[11:23])
            self.assertAlmostEqual(v.shown(), SCHED, delta=50)

    # 12 -----------------------------------------------------------------------------------------
    def test_reset_stream_start(self):
        v = Vod(self.store, session(), ref_with(SCHED))
        v.at_pb(0.0)
        v.mark()
        v.at(SCHED)
        v.lights_out()
        v.s.clear_anchor()
        self.assertEqual(v.s.mapping.source, "stream-origin")
        self.assertIn("STREAM START RESET", v.s.reset_stream_start())
        st = v.state()
        self.assertIsNone(st["streamStart"])
        self.assertEqual(st["confidence"], "UNSYNCED")
        self.assertIn("no stream start", v.s.reset_stream_start())
        again = Vod(self.store, session(), ref_with(SCHED))       # also gone after reopening
        again.at_pb(5.0)
        self.assertIsNone(again.state()["streamStart"])

    def test_reasons_without_session_or_timing(self):
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, vod=True)
        s.update(parse_voyo_sample({"playback_time": 0.0, "paused": True, "asset": "x"}, 0.0, 1.0), 0.0)
        s.target(1.0, 0.0)
        self.assertIn("F1 session not identified", s.mark_stream_start(1.0, 0.0))
        v = Vod(self.store, session(), RefEvents(source="none"))
        v.at_pb(0.0)
        self.assertIn("historical timing unavailable", v.mark())


if __name__ == "__main__":
    unittest.main()
