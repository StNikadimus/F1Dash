"""LIGHTS OUT resolver (server/lights_out.py) + the public F1 SignalR fallback (server/lights_out_probe.py).

The public SignalR connection is replaced by a fake source that sends what the real feed sends
(SessionInfo, SessionData.StatusSeries with "Started", SessionStatus) - no network in tests.

Run:  python -m unittest tests.test_lights_out_resolver
"""
import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import DATA_DIR  # noqa: E402

from server.engine import Engine  # noqa: E402
from server.lights_out import resolve_lights_out  # noqa: E402
from server.lights_out_probe import PublicStartProbe  # noqa: E402
from server.openf1 import RefEvents, merge_refs, ref_events_from_openf1  # noqa: E402
from server.sources.replay import META_TOPICS, load_file  # noqa: E402
from server.sources.vod import ref_events_from_archive  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from server.telemetry import parse_utc  # noqa: E402
from tests.test_sync_start import MIN, SCHED, LiveRace, iso  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((ROOT / "tests" / "fixtures" / "openf1_2026_japan.json").read_text())
JP_SCHED = parse_utc("2026-03-29T05:00:00Z").timestamp() * 1000
JP_LO = parse_utc("2026-03-29T05:14:02.078Z").timestamp() * 1000
_EV = None


def archive_events():
    global _EV
    if _EV is None:
        _EV = load_file(DATA_DIR / "recordings" / "sample-2026-japan-race.json.gz")
    return _EV


def jp_session(key=11253, name="Race"):
    base = next(x for x in FIX["sessions"] if x["session_key"] == 11253)
    return {**base, "session_key": key, "session_name": name, "gmt_offset": "09:00:00"}


class FakePublicFeed:
    """Stands in for F1LiveSource(anonymous): sends the given messages, like a (re)connect would."""

    def __init__(self, cfg, messages):
        self.cfg, self.messages = cfg, messages

    async def run(self, sink):
        for topic, data, ts, snap in self.messages:
            await sink.feed(topic, data, datetime.fromtimestamp(ts / 1000, timezone.utc), snapshot=snap)


def public_feed_msgs(key, start_ms, snapshot_twice=False):
    info = ("SessionInfo", {"Key": key, "Name": "Race", "Type": "Race"}, start_ms, True)
    series = ("SessionData", {"StatusSeries": {"2": {"Utc": iso(start_ms), "SessionStatus": "Started"}}},
              start_ms + 5000, True)
    msgs = [info, series]
    if snapshot_twice:                              # a reconnect: the snapshot again + the live message
        msgs += [info, series, ("SessionStatus", {"Status": "Started"}, start_ms, False)]
    return msgs


class ResolverTest(unittest.TestCase):
    # A: the primary (F1 TV timing) has it
    def test_a_live_primary_used(self):
        r = LiveRace(self, stream_delay_s=10, start_ms=SCHED + 90_000)
        r.sync.feed_ref.family = lambda: "F1 TV timing"
        r.step(30 * 60 + 2 * 60)
        res = r.sync.lights_out_result()
        self.assertEqual(res.timestamp_ms, SCHED + 90_000)
        self.assertEqual(res.source, "F1 TV timing · SessionStatus")
        self.assertEqual(res.confidence, "VERY HIGH")
        self.assertEqual((res.session_key, res.historical), (9999, False))

    # B + J + K: primary without the start -> public F1 SignalR fallback; L works; reconnect no duplicates
    def test_b_live_fallback_public_signalr(self):
        r = LiveRace(self, stream_delay_s=30, start_ms=None)        # the primary never delivers the start
        r.sync.feed_ref.family = lambda: "F1 TV timing"
        r.step(30 * 60 + 3 * 60)                                    # 15:03 - the start was at 15:01
        self.assertFalse(r.sync.lights_out_result().ok)
        real_start = SCHED + MIN
        host = SimpleNamespace(sync=r.sync, source=SimpleNamespace(mode="live", auth_mode="AUTHENTICATED"),
                               _status={"state": "connected"}, _lo_probe=None, _lo_enabled=True, _lo_cfg={},
                               _src_now_ms=lambda: r.now)

        async def go():
            await Engine._lights_out_fallback(
                host, probe_factory=lambda cfg: FakePublicFeed(cfg, public_feed_msgs(9999, real_start, True)))
            self.assertIsNotNone(host._lo_probe)                    # opened: start due, primary has none
            self.assertEqual(host._lo_probe.source.cfg["topics"][:3], ["Heartbeat", "SessionInfo", "SessionStatus"])
            await host._lo_probe.task
            await Engine._lights_out_fallback(host)                # start known now -> closed
            self.assertIsNone(host._lo_probe)
        asyncio.run(go())
        res = r.sync.lights_out_result()
        self.assertEqual(res.timestamp_ms, real_start)
        self.assertEqual(res.family, "F1 SignalR")
        self.assertEqual(res.confidence, "HIGH")
        fams = [o[1] for o in r.sync.feed_ref.ref.start_obs]
        # K: the snapshot twice (reconnect) + the live SessionStatus message = one report of one source
        self.assertEqual(fams, ["F1 SignalR · SessionData.StatusSeries"])
        self.assertEqual(len(r.sync.feed_ref.ref.starts), 1)
        # J: the existing L sync uses it
        r.to_video_showing(real_start)
        self.assertIn("LIGHTS OUT ✓", r.sync.add_event_anchor("start", r.mono, r.now, None, "START"))
        self.assertAlmostEqual(r.shown(), real_start, delta=50)

    def test_b2_no_fallback_when_the_primary_is_the_public_feed_or_not_due(self):
        r = LiveRace(self, stream_delay_s=30, start_ms=None)
        r.step(10 * 60)                                             # 14:40: start not due yet
        host = SimpleNamespace(sync=r.sync, source=SimpleNamespace(mode="live", auth_mode="AUTHENTICATED"),
                               _status={"state": "connected"}, _lo_probe=None, _lo_enabled=True, _lo_cfg={},
                               _src_now_ms=lambda: r.now)
        asyncio.run(Engine._lights_out_fallback(host, probe_factory=lambda c: FakePublicFeed(c, [])))
        self.assertIsNone(host._lo_probe)
        r.step(25 * 60)                                             # due, but the primary IS the public feed
        host.source.auth_mode = "ANONYMOUS"
        asyncio.run(Engine._lights_out_fallback(host, probe_factory=lambda c: FakePublicFeed(c, [])))
        self.assertIsNone(host._lo_probe)

    # C + D: historical, delayed race; today's clock irrelevant
    def test_c_d_historical_delayed_race(self):
        weeks_later = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch("server.lights_out.time.time", return_value=weeks_later), \
                mock.patch("server.sync.time.time", return_value=weeks_later):
            s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, vod=True)
            s.initialize(jp_session(), ref_events_from_archive([e for e in archive_events() if e.topic in META_TOPICS]))
            res = s.lights_out_result()
        self.assertEqual(res.timestamp_ms, JP_LO)                   # actual, not the 05:00 schedule
        self.assertEqual(res.scheduled_ms, JP_SCHED)                # kept as metadata only
        self.assertEqual(res.source, "F1 archive · SessionData.StatusSeries")
        self.assertTrue(res.historical)
        self.assertEqual(res.confidence, "HIGH")

    def test_openf1_is_the_historical_fallback(self):
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, vod=True)
        s.initialize(jp_session(), ref_events_from_openf1(FIX["laps"], FIX["race_control"]))   # no archive
        res = s.lights_out_result()
        self.assertEqual((res.timestamp_ms, res.family), (JP_LO, "OpenF1"))

    # E: Singapore race vs qualifying (the server runs from one into the other)
    def test_e_session_identity(self):
        r = LiveRace(self, stream_delay_s=10, start_ms=None)       # session 9999 = race
        quali_start = SCHED - 24 * 3600_000
        col = r.sync.feed_ref
        col.observe("SessionInfo", {"Key": 9998, "Name": "Qualifying"}, quali_start - 60_000, False)
        col.observe("SessionData", {"StatusSeries": {"1": {"Utc": iso(quali_start), "SessionStatus": "Started"}}},
                    quali_start, False)
        col.observe("TimingData", {"Lines": {"1": {"NumberOfLaps": 1}}}, quali_start + 90_000, False)
        self.assertEqual(col.ref.starts, [quali_start])
        col.observe("SessionInfo", {"Key": 9999, "Name": "Race"}, SCHED - 3600_000, False)
        self.assertEqual(col.ref.starts, [])                        # the qualifying start is gone
        self.assertIsNone(col.ref.first_crossing())
        self.assertFalse(r.sync.lights_out_result().ok)
        # a saved result of another session is never used
        r.sync.store.data["lights_out"] = {"9998": {"timestamp_ms": quali_start, "source": "F1 TV timing · x",
                                                    "session_key": 9998}}
        self.assertFalse(r.sync.lights_out_result().ok)

    # F + G: several sources
    def test_f_sources_agree_raise_confidence(self):
        both = merge_refs(ref_events_from_openf1(FIX["laps"], FIX["race_control"]),
                          ref_events_from_archive([e for e in archive_events() if e.topic in META_TOPICS]))
        res = resolve_lights_out(both.start_obs, both.first_crossing(), jp_session(), historical=True)
        self.assertEqual(res.timestamp_ms, JP_LO)
        self.assertEqual(res.family, "F1 archive")                  # VOD priority: archive before OpenF1
        self.assertEqual(res.agreeing, 2)
        self.assertEqual(res.confidence, "VERY HIGH")
        self.assertFalse(res.conflict)

    def test_g_sources_disagree_conflict_by_priority(self):
        t = SCHED + 19 * MIN + 4120
        obs = [(t, "F1 TV timing · SessionStatus", False), (t + 2880, "F1 SignalR · SessionData.StatusSeries", False)]
        res = resolve_lights_out(obs, None, {"session_key": 1, "date_start": iso(SCHED)}, historical=False)
        self.assertEqual(res.timestamp_ms, t)                       # the more authoritative source
        self.assertTrue(res.conflict)
        self.assertIn("F1 SignalR", res.conflict_detail)
        self.assertEqual(res.confidence, "HIGH")                    # VERY HIGH lowered by the conflict
        ref = RefEvents()
        for o in obs:
            ref.add_start(*o)
        self.assertEqual(ref.starts, [t])                           # one start, not two
        old = [(t + 2880, "OpenF1 · race_control SESSION STARTED", False),
               (t, "F1 archive · SessionData.StatusSeries", False)]
        self.assertEqual(resolve_lights_out(old, None, {"session_key": 1}, historical=True).timestamp_ms, t)

    # H: reopening the same VOD - the saved result; never another session's
    def test_h_cache_per_session(self):
        store = Path(tempfile.mkdtemp()) / "sync_calibration.json"
        a = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, store_path=store,
                        vod=True)
        a.initialize(jp_session(), ref_events_from_archive([e for e in archive_events() if e.topic in META_TOPICS]))
        self.assertEqual(a.lights_out_result().timestamp_ms, JP_LO)
        saved = json.loads(store.read_text())["lights_out"]["11253"]
        self.assertEqual((saved["timestamp_ms"], saved["session_key"]), (JP_LO, 11253))
        for k in ("source", "confidence", "resolved_at", "meeting_key"):
            self.assertIn(k, saved)
        b = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, store_path=store,
                        vod=True)
        b.initialize(jp_session(), RefEvents(source="none"))       # reopened, data not there (yet)
        res = b.lights_out_result()
        self.assertTrue(res.cached)
        self.assertEqual(res.timestamp_ms, JP_LO)
        c = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, store_path=store,
                        vod=True)
        c.initialize(jp_session(11249, "Qualifying"), RefEvents(source="none"))
        self.assertFalse(c.lights_out_result().ok)                  # qualifying: not the race's start
        # I with the saved result: Event Sync lists it and SET works
        lo = next(e for e in b.event_catalog() if e["label"] == "LIGHTS OUT")
        self.assertIn("(saved)", lo["source"])
        k = (JP_SCHED - 1800_000) / 1000
        b.update(parse_voyo_sample({"playback_time": JP_LO / 1000 - k, "paused": True, "asset": "x"}, 0.0, 1.0), 0.0)
        b.target(1.0, 0.0)
        self.assertIn("LIGHTS OUT ✓", b.event_set(1.0, 0.0, lo["id"]))
        self.assertAlmostEqual(b.mapping.offset, k, places=2)

    # I: Event Sync shows the resolved lights out with source and confidence
    def test_i_event_sync_lists_the_resolved_lights_out(self):
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, vod=True)
        s.initialize(jp_session(), ref_events_from_archive(archive_events()))
        lo = next(e for e in s.event_catalog() if e["label"] == "LIGHTS OUT")
        self.assertEqual((lo["ms"], lo["confidence"]), (JP_LO, "HIGH"))
        self.assertEqual(lo["source"], "F1 archive · SessionData.StatusSeries")
        st = s.get_state(0.0, 0.0, None)
        self.assertEqual(st["eventSync"]["lightsOut"]["utc"], "05:14:02.078")
        self.assertEqual(st["lightsOut"]["resolved"]["source"], "F1 archive · SessionData.StatusSeries")

    # L: nothing verified -> unavailable, never the schedule
    def test_l_no_source_no_guess(self):
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, vod=True)
        s.initialize(jp_session(), RefEvents(source="none"))
        res = s.lights_out_result()
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "No verified actual race-start timestamp found")
        self.assertEqual(res.scheduled_ms, JP_SCHED)
        self.assertNotIn("LIGHTS OUT", [e["label"] for e in s.event_catalog()])
        self.assertIsNone(s.get_state(0.0, 0.0, None)["eventSync"]["lightsOut"]["timestamp_ms"])
        only_restart = resolve_lights_out([(SCHED + 40 * MIN, "F1 SignalR · SessionStatus", False)], SCHED + 5 * MIN,
                                          {"session_key": 1}, historical=False)
        self.assertFalse(only_restart.ok)                           # a restart after lap 1 is not lights out


if __name__ == "__main__":
    unittest.main()
