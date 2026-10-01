"""Pit-lane reconstruction: geometry, validation, cache-first behaviour, rendering helpers.

Geometry passes are synthetic on real circuit outlines (see pitlane_synth.py - the project's
recordings contain no car positions); the timing-signal test uses the real 2026 Japanese GP
timing recording.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pitlane_synth as S  # noqa: E402
from server.pitlane import (ALGO_VERSION, M, PitLaneCache, PitLaneCollector, PitSignals, SegIndex,  # noqa: E402
                            collect_from_events, needs_reconstruction, reconstruct)
from server.sources.replay import Event, load_file  # noqa: E402
from server.telemetry import encode_z, parse_utc  # noqa: E402
from server.track import TrackGeometry, TrackProvider  # noqa: E402

SAMPLE = ROOT / "data" / "recordings" / "sample-2026-japan-race.json.gz"


def run_passes(pts, lane, i0, n=5, lateral=None, noise=0.5, gaps=None, track=True, signals="inpit",
               start=None, stop_s=2.5):
    """Feed n synthetic passes (different cars) into a collector; return (traversals, collector)."""
    col = PitLaneCollector(pts if track else None, keep_ms=None)
    t = start or S.T0
    for k in range(n):
        num = str(10 + k)
        lat = (lateral[k] if lateral else (k - (n - 1) / 2) * 0.5)
        entries, t_in, t_out = S.pass_samples(pts, lane, i0, t, seed=k + 1, lateral_m=lat, noise_m=noise,
                                              gap=(gaps or {}).get(k), stop_s=stop_s)
        for e in entries:
            e["Entries"] = {num: e["Entries"]["__NUM__"]}
            col.position_entry(e)
        if signals == "inpit":
            col.timing("TimingData", {"Lines": {num: {"InPit": True}}}, t_in)
            col.timing("TimingData", {"Lines": {num: {"InPit": False, "PitOut": True}}}, t_out)
        elif signals == "pitlanetime":        # 2026 feed often lacks the InPit edge: PitOut + official time
            col.timing("TimingDataF1", {"Lines": {num: {"PitOut": True}}}, t_out)
            col.timing("PitLaneTimeCollection", {"PitTimes": {num: {"RacingNumber": num,
                                                                  "Duration": f"{(t_out - t_in) / 1000:.1f}"}}}, t_out)
        t += timedelta(minutes=4)
    return col.poll(math.inf), col


def err_to(lane, centerline, track=None):
    """Mean / max distance (m) of a centerline from the true lane (or the track, for the few
    metres before the branch / after the merge that the drawing includes on purpose)."""
    ix = SegIndex(lane)
    tx = SegIndex(track, closed=True) if track else None
    ds = [min(ix.distance(p), tx.distance(p) if tx else math.inf) / M for p in centerline]
    return sum(ds) / len(ds), max(ds)


class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pts = S.track("az-2016")
        cls.lane, cls.i0 = S.pit_lane(cls.pts)


class ReconstructionTest(Setup):
    def test_single_traversal(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=1)
        rec = reconstruct(trs, True)
        self.assertEqual(rec.confidence, "MEDIUM")
        self.assertTrue(rec.drawable)
        mean, mx = err_to(self.lane, rec.centerline, self.pts)
        self.assertLess(mean, 2.0)
        self.assertLess(mx, 4.0)

    def test_multiple_traversals(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=6)
        rec = reconstruct(trs, True)
        self.assertEqual(rec.confidence, "HIGH")
        self.assertEqual(len(rec.used), 6)
        mean, mx = err_to(self.lane, rec.centerline, self.pts)
        self.assertLess(mean, 0.6)
        self.assertLess(mx, 2.0)

    def test_pit_out_plus_official_pit_lane_time(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=4, signals="pitlanetime")
        self.assertEqual([t.how for t in trs], ["PitLaneTime"] * 4)
        rec = reconstruct(trs, True)
        self.assertEqual(rec.confidence, "HIGH")

    def test_outlier_removal(self):
        # the 4th pass is 15 m to the side (e.g. another car's line through the fast lane / bad data)
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=6, lateral=[0, 0.4, -0.4, 15, 0.2, -0.2])
        rec = reconstruct(trs, True)
        rejected = [t for t, why in rec.rejected]
        self.assertEqual([t.num for t in rejected], ["13"])
        self.assertIn("outlier", rec.rejected[0][1])
        self.assertEqual(rec.confidence, "HIGH")
        self.assertLess(err_to(self.lane, rec.centerline, self.pts)[0], 0.6)

    def test_smoothing_no_gps_jitter(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=3, noise=1.5)
        rec = reconstruct(trs, True)
        c = rec.centerline
        self.assertLess(err_to(self.lane, c, self.pts)[0], 1.0)
        # heading changes between consecutive 2.5 m steps: a road, not a zig-zag
        worst = 0.0
        for i in range(1, len(c) - 1):
            h1 = math.atan2(c[i][1] - c[i - 1][1], c[i][0] - c[i - 1][0])
            h2 = math.atan2(c[i + 1][1] - c[i][1], c[i + 1][0] - c[i][0])
            worst = max(worst, abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi))
        self.assertLess(math.degrees(worst), 8.0)
        steps = [math.dist(c[i], c[i + 1]) for i in range(len(c) - 1)]
        self.assertLess(max(steps), 3.5 * M)

    def test_entry_and_exit(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=4)
        rec = reconstruct(trs, True)
        track = SegIndex(self.pts, closed=True)
        self.assertTrue(rec.entry_joined and rec.exit_joined)
        self.assertLess(track.distance(rec.centerline[0]), 6 * M)       # branches off the track
        self.assertLess(track.distance(rec.centerline[-1]), 6 * M)      # and rejoins it
        mid = rec.centerline[len(rec.centerline) // 2]
        self.assertGreater(track.distance(mid), 18 * M)                  # the lane itself is apart
        self.assertLess(rec.pit_in_frac, rec.pit_out_frac)
        self.assertLess(rec.pit_in_frac, 0.4)
        self.assertGreater(rec.pit_out_frac, 0.6)


class ValidationTest(Setup):
    def test_hole_in_position_data(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=1, gaps={0: (4.0, 11.0)})
        self.assertFalse(trs[0].accepted)
        self.assertIn("hole", trs[0].reason)
        self.assertIsNone(reconstruct(trs, True))

    def test_red_flag_or_garage_stay_rejected(self):
        trs, _ = run_passes(self.pts, self.lane, self.i0, n=1, stop_s=300)
        self.assertFalse(trs[0].accepted)
        self.assertIn("pit lane", trs[0].reason)

    def test_exit_without_entry_or_time_is_ignored(self):
        col = PitLaneCollector(self.pts, keep_ms=None)
        col.timing("TimingData", {"Lines": {"5": {"PitOut": True}}}, S.T0.timestamp() * 1000)
        self.assertEqual(col.poll(math.inf), [])

    def test_no_positions_at_all(self):
        col = PitLaneCollector(self.pts, keep_ms=None)
        t = S.T0.timestamp() * 1000
        col.timing("TimingData", {"Lines": {"5": {"InPit": True}}}, t)
        col.timing("TimingData", {"Lines": {"5": {"InPit": False}}}, t + 24_000)
        trs = col.poll(math.inf)
        self.assertEqual(len(trs), 1)
        self.assertFalse(trs[0].accepted)
        self.assertIn("samples", trs[0].reason)

    def test_garage_detour_rejected(self):
        # into the garage and back out the same way: the path turns back on itself
        base = S.T0.timestamp() * 1000
        col = PitLaneCollector(None, keep_ms=None)
        x0, y0 = 0.0, 0.0
        k = 0
        for i in range(60):                       # 300 m along the lane
            col.hist.add("7", base + k * 270, x0 + i * 50, y0); k += 1
        for i in range(12):                       # 60 m into the garage
            col.hist.add("7", base + k * 270, x0 + 3000, y0 + i * 50); k += 1
        for i in range(12, -1, -1):               # and back
            col.hist.add("7", base + k * 270, x0 + 3000, y0 + i * 50); k += 1
        for i in range(60, 120):
            col.hist.add("7", base + k * 270, x0 + i * 50, y0); k += 1
        col.timing("TimingData", {"Lines": {"7": {"InPit": True}}}, base)
        col.timing("TimingData", {"Lines": {"7": {"InPit": False}}}, base + (k - 1) * 270)
        trs = col.poll(math.inf)
        self.assertFalse(trs[0].accepted)
        self.assertIn("turns back", trs[0].reason)

    def test_low_confidence_is_neither_drawn_nor_stored(self):
        from server.pitlane import extract_traversal
        _, col = run_passes(self.pts, self.lane, self.i0, n=1)
        w = col.signals.windows(math.inf)[0]
        tr = extract_traversal(w, col.hist, None)       # entry / exit could not be matched to the track
        self.assertTrue(tr.accepted)
        self.assertFalse(tr.entry_joined or tr.exit_joined)
        rec = reconstruct([tr], True)                    # one pass, ends unconfirmed -> LOW
        self.assertEqual(rec.confidence, "LOW")
        self.assertFalse(rec.drawable)
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            v, what = cache.store(1, "X", 2026, 1, rec, "test")
            self.assertIsNone(v)
            self.assertIn("not stored", what)
            self.assertFalse(cache.path(1).exists())


class CacheTest(Setup):
    def rec(self, n=5, lane=None, i0=None):
        trs, _ = run_passes(self.pts, lane or self.lane, self.i0 if i0 is None else i0, n=n)
        return reconstruct(trs, True)

    def test_save_load_and_attach(self):
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d) / "tracks")
            v, what = cache.store(144, "Baku", 2026, 11377, self.rec(), "test")
            self.assertEqual((v["status"], v["confidence"]), ("verified", "HIGH"))
            raw = json.loads(cache.path(144).read_text())
            self.assertEqual((raw["algo_version"], raw["track_key"]), (ALGO_VERSION, 144))
            # a new process: the provider attaches it at once, before any pit stop
            tp = TrackProvider(Path(d), {})
            geo = TrackGeometry(144, "Baku", 2026, "multiviewer", [list(p) for p in self.pts])
            tp.attach_pitlane(geo, 2026)
            self.assertGreater(len(geo.pitlane), 50)
            self.assertEqual(geo.pitlane_info["state"], "cached")
            self.assertEqual(geo.pitlane_info["confidence"], "HIGH")

    def test_same_circuit_other_session_uses_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            cache.store(144, "Baku", 2026, 11377, self.rec(), "race")        # Baku race
            need, v = needs_reconstruction(cache, 144, 2026)                  # Baku qualifying next
            self.assertFalse(need)
            self.assertEqual(v["sessions"], [11377])

    def test_same_circuit_other_vod_same_geometry(self):
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            cache.store(144, "Baku", 2026, 11377, self.rec(), "vod A")
            v, what = cache.store(144, "Baku", 2026, 11373, self.rec(n=3), "vod B")   # other recording
            self.assertIn("confirmed", what)
            data = json.loads(cache.path(144).read_text())
            self.assertEqual(len(data["variants"]), 1)                          # no duplicate geometry
            self.assertEqual(sorted(data["variants"][0]["sessions"]), [11373, 11377])
            self.assertEqual(list(Path(d).glob("pitlane_geometry_*.json")), [cache.path(144)])  # keyed by circuit only

    def test_different_seasons_changed_layout(self):
        other_lane, oi0 = S.pit_lane(self.pts, side=-1)                         # other side of the straight
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            cache.store(144, "Baku", 2025, 1, self.rec(), "2025")
            trs, _ = run_passes(self.pts, other_lane, oi0, n=5)
            new_rec = reconstruct(trs, True)
            v, what = cache.store(144, "Baku", 2026, 2, new_rec, "2026")
            self.assertIn("differs", what)
            data = json.loads(cache.path(144).read_text())
            self.assertEqual(len(data["variants"]), 2)                          # old one not overwritten
            old = PitLaneCache.select(data, 2025)
            new = PitLaneCache.select(data, 2026)
            self.assertNotEqual(old["id"], new["id"])
            self.assertEqual(old["valid_until"], 2025)
            self.assertLess(err_to(self.lane, old["centerline"], self.pts)[0], 1.0)
            self.assertLess(err_to(other_lane, new["centerline"], self.pts)[0], 1.0)

    def test_same_layout_next_season_is_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            cache.store(144, "Baku", 2025, 1, self.rec(), "2025")
            need, v = needs_reconstruction(cache, 144, 2026)
            self.assertTrue(need)                                                # other season: verify once
            self.assertEqual(v["seasons"], [2025])                               # drawn meanwhile
            v, what = cache.store(144, "Baku", 2026, 2, self.rec(n=3), "2026")
            self.assertIn("confirmed", what)
            self.assertEqual(v["seasons"], [2025, 2026])
            self.assertFalse(needs_reconstruction(cache, 144, 2026)[0])

    def test_invalid_and_outdated_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            cache.store(144, "Baku", 2026, 1, self.rec(), "t")
            data = json.loads(cache.path(144).read_text())
            data["algo_version"] = ALGO_VERSION - 1                             # older algorithm
            cache.path(144).write_text(json.dumps(data))
            self.assertIsNone(cache.lookup(144, 2026))
            self.assertTrue(needs_reconstruction(cache, 144, 2026)[0])
            cache.path(144).write_text("{not json")                             # corrupt
            self.assertIsNone(cache.lookup(144, 2026))
            data["algo_version"] = ALGO_VERSION
            data["variants"][0]["status"] = "outdated"
            cache.path(144).write_text(json.dumps(data))
            self.assertIsNone(cache.lookup(144, 2026))

    def test_provisional_is_upgraded_by_more_passes(self):
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            v, _ = cache.store(144, "Baku", 2026, 1, self.rec(n=1), "t")
            self.assertEqual(v["status"], "provisional")
            self.assertTrue(needs_reconstruction(cache, 144, 2026)[0])
            v, what = cache.store(144, "Baku", 2026, 2, self.rec(n=5), "t")
            self.assertEqual(v["status"], "verified")
            self.assertIn("improved", what)


class SignalsAndVodTest(Setup):
    def test_real_2026_pit_signals(self):
        """Real Japanese GP 2026 timing: the InPit edge is often missing - PitOut + official pit-lane
        time still give every stop, and where both exist they agree."""
        sig = PitSignals()
        n_official = 0
        for e in load_file(SAMPLE):
            if e.topic in ("TimingData", "TimingDataF1", "PitLaneTimeCollection"):
                sig.feed(e.topic, e.data, e.t.timestamp() * 1000)
                if e.topic == "PitLaneTimeCollection":
                    n_official += sum(1 for p in (e.data.get("PitTimes") or {}).values() if isinstance(p, dict))
        wins = sig.windows()
        self.assertEqual(len(wins), n_official)
        self.assertTrue(all(8 <= (w.t_out - w.t_in) / 1000 <= 60 for w in wins))
        inpit = [w for w in wins if w.how == "InPit"]
        self.assertTrue(inpit and all(w.confirmed for w in inpit))
        self.assertTrue(any(w.how == "PitLaneTime" for w in wins))

    def test_vod_session_from_encoded_position_messages(self):
        events = []
        t = S.T0
        for k in range(4):
            num = str(20 + k)
            entries, t_in, t_out = S.pass_samples(self.pts, self.lane, self.i0, t, seed=k + 3)
            for i in range(0, len(entries), 3):                  # feed-style Position.z messages
                chunk = entries[i:i + 3]
                for e in chunk:
                    e["Entries"] = {num: e["Entries"]["__NUM__"]}
                events.append(Event(parse_utc(chunk[-1]["Timestamp"]), "Position.z", encode_z({"Position": chunk})))
            events.append(Event(parse_utc(S.iso(t_in)), "TimingData", {"Lines": {num: {"InPit": True}}}))
            events.append(Event(parse_utc(S.iso(t_out)), "TimingData", {"Lines": {num: {"InPit": False, "PitOut": True}}}))
            t += timedelta(minutes=5)
        events.sort(key=lambda e: e.t)
        trs = collect_from_events(events, self.pts)
        self.assertEqual(sum(t.accepted for t in trs), 4)
        self.assertEqual(reconstruct(trs, True).confidence, "HIGH")


class EngineLiveLearningTest(Setup):
    """Live / replay: passes arrive through the normal feed path; learning starts only when the
    cache needs it and stops once the pit lane is verified; the next session draws it at once."""

    def make_engine(self, d):
        from server.config import load_config
        from server.engine import Engine
        from server.hub import Hub
        from server.sources.base import Source

        class LiveStub(Source):
            mode = "live"

            async def run(self, sink):
                pass
        cfg = load_config(Path(d) / "none.toml")
        eng = Engine(cfg, LiveStub(), TrackProvider(Path(d), cfg["tracks"]), Hub())
        geo = TrackGeometry(144, "Baku", 2026, "multiviewer", [list(p) for p in self.pts])
        eng.tracks.attach_pitlane(geo, 2026)
        eng.geometry = geo
        return eng, geo

    def feed_passes(self, eng, n, start):
        t = start
        for k in range(n):
            num = str(30 + k)
            entries, t_in, t_out = S.pass_samples(self.pts, self.lane, self.i0, t, seed=k + 7)
            msgs = []
            for i in range(0, len(entries), 2):
                chunk = entries[i:i + 2]
                for e in chunk:
                    e["Entries"] = {num: e["Entries"]["__NUM__"]}
                msgs.append((parse_utc(chunk[-1]["Timestamp"]).timestamp() * 1000, "Position.z", encode_z({"Position": chunk})))
            msgs.append((t_in, "TimingData", {"Lines": {num: {"InPit": True}}}))
            msgs.append((t_out, "TimingData", {"Lines": {num: {"InPit": False, "PitOut": True}}}))
            for ms, topic, data in sorted(msgs, key=lambda m: m[0]):
                eng._ingest(topic, data, ms, False, "feed", ms)
            t += timedelta(minutes=3)
        # a minute of silence later: all passes complete
        eng._ingest("Heartbeat", {}, (t + timedelta(minutes=1)).timestamp() * 1000, False, "feed")
        eng.timeline.latest_event_ms = (t + timedelta(minutes=2)).timestamp() * 1000

    def test_learns_then_uses_cache(self):
        import asyncio
        asyncio.run(self._learns_then_uses_cache())

    async def _learns_then_uses_cache(self):
        async def poll(eng):
            task = eng._pit_poll()
            if task is not None:
                await task
        with tempfile.TemporaryDirectory() as d:
            eng, geo = self.make_engine(d)
            self.assertIsNone(geo.pitlane)
            eng._pit_start_learning(geo, 144, 2026, "Baku")
            self.assertIsNotNone(eng.pit_collector)
            self.assertEqual(geo.pitlane_info["state"], "learning")
            self.feed_passes(eng, 2, S.T0)
            await poll(eng)
            self.assertEqual(geo.pitlane_info["state"], "reconstructed")
            self.assertEqual(geo.pitlane_info["confidence"], "MEDIUM")       # 2 passes: provisional
            self.assertIsNotNone(eng.pit_collector)                           # keeps learning
            self.feed_passes(eng, 3, S.T0 + timedelta(minutes=20))
            await poll(eng)
            self.assertEqual(geo.pitlane_info["confidence"], "HIGH")
            self.assertEqual(geo.pitlane_info["status"], "verified")
            self.assertIsNone(eng.pit_collector)                              # verified: learning stops
            self.assertLess(err_to(self.lane, geo.pitlane, self.pts)[0], 0.8)
            dbg = eng.hub.pit_debug
            self.assertEqual(dbg["type"], "pitlane_debug")
            self.assertEqual(len([p for p in dbg["passes"] if p["accepted"]]), 5)
            # next session on the same circuit (e.g. qualifying, other recording): drawn at once
            eng2, geo2 = self.make_engine(d)
            self.assertEqual(geo2.pitlane_info["state"], "cached")
            self.assertIsNotNone(geo2.pitlane)
            eng2._pit_start_learning(geo2, 144, 2026, "Baku")
            self.assertIsNone(eng2.pit_collector)                             # no reconstruction


class EngineVodTest(Setup):
    """VOD: the loaded session archive is searched once (in a thread); a second recording or
    session of the same circuit uses the cache and runs no reconstruction at all."""

    def test_offline_then_cached(self):
        import asyncio
        from unittest import mock
        from server.config import load_config
        from server.engine import Engine
        from server.hub import Hub
        from server.openf1 import OpenF1Client
        from server.sources.vod import VodSource
        from server import engine as engmod

        events = [Event(S.T0 - timedelta(minutes=30), "SessionInfo",
                        {"Key": 11377, "Path": "2026/2026-09-26_Azerbaijan_Grand_Prix/2026-09-26_Race/",
                         "Meeting": {"Name": "Azerbaijan Grand Prix", "Circuit": {"Key": 144, "ShortName": "Baku"}}})]
        t = S.T0
        for k in range(4):
            num = str(40 + k)
            entries, t_in, t_out = S.pass_samples(self.pts, self.lane, self.i0, t, seed=k + 11)
            for i in range(0, len(entries), 3):
                chunk = entries[i:i + 3]
                for e in chunk:
                    e["Entries"] = {num: e["Entries"]["__NUM__"]}
                events.append(Event(parse_utc(chunk[-1]["Timestamp"]), "Position.z", encode_z({"Position": chunk})))
            events.append(Event(parse_utc(S.iso(t_in)), "TimingData", {"Lines": {num: {"InPit": True}}}))
            events.append(Event(parse_utc(S.iso(t_out)), "TimingData", {"Lines": {num: {"InPit": False, "PitOut": True}}}))
            t += timedelta(minutes=5)
        events.sort(key=lambda e: e.t)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tracks").mkdir()
            (Path(d) / "tracks" / "mv_144_2026.json").write_text(json.dumps(
                {"x": [p[0] for p in self.pts], "y": [p[1] for p in self.pts], "circuitName": "Baku"}))

            def make():
                cfg = load_config(Path(d) / "none.toml")
                cfg["source"]["mode"] = "vod"
                src = VodSource(cfg["vod"], OpenF1Client(Path(d) / "of1"), Path(d) / "arch")
                src.events = events
                return Engine(cfg, src, TrackProvider(Path(d), cfg["tracks"]), Hub())

            async def go(eng, session):
                await eng._pit_offline(session)

            eng = make()
            asyncio.run(go(eng, {"session_key": 11377, "date_start": "2026-09-26T11:00:00+00:00"}))
            v = eng.tracks.pitcache.lookup(144, 2026)
            self.assertEqual((v["status"], v["confidence"], v["traversals"]), ("verified", "HIGH", 4))
            self.assertLess(err_to(self.lane, v["centerline"], self.pts)[0], 0.8)
            # another recording / the qualifying of the same circuit
            eng2 = make()
            with mock.patch.object(engmod, "collect_from_events") as scan:
                asyncio.run(go(eng2, {"session_key": 11373, "date_start": "2026-09-25T12:00:00+00:00"}))
                scan.assert_not_called()
            self.assertEqual(eng2.hub.pit_debug["state"], "cached")


class RealBakuTest(unittest.TestCase):
    """REAL positions: four pit stops of the 2025 Azerbaijan GP race (OpenF1 / F1 Position.z,
    tests/fixtures/openf1_baku2025_pit.txt) with the official pit-lane times, and one normal
    racing lap of car 1 as the track outline (the MultiViewer outline is the same X/Y system)."""

    @classmethod
    def setUpClass(cls):
        import baku_real as B
        cls.B = B
        cls.track, cls.passes = B.load()
        cls.rec = reconstruct(cls.collect(cls.passes), True)

    @classmethod
    def collect(cls, passes, t_in_shift=0.0, drop=None, extra=None):
        col = PitLaneCollector(cls.track, keep_ms=None)
        for p in passes:
            for t, x, y in p["samples"]:
                if drop and drop(p, t):
                    continue
                col.hist.add(p["num"], t, x, y)
            for t, x, y in (extra or {}).get(p["num"], []):
                col.hist.add(p["num"], t, x, y)
            # like the 2026 feed: PitOut at the exit line + the official pit-lane time
            col.timing("TimingDataF1", {"Lines": {p["num"]: {"PitOut": True}}}, p["t_out"])
            col.timing("PitLaneTimeCollection", {"PitTimes": {p["num"]: {
                "Duration": str(p["lane_s"] + t_in_shift)}}}, p["t_out"])
        return col.poll(math.inf)

    def t_in(self, p):
        return p["t_out"] - p["lane_s"] * 1000

    def test_same_coordinate_system_and_high_confidence(self):
        rec = self.rec
        self.assertEqual(rec.confidence, "HIGH")
        self.assertEqual(len(rec.used), 4)
        self.assertLessEqual(rec.spread_m, 2.5)
        T = SegIndex(self.track, closed=True)
        c = rec.centerline
        n = len(c)
        mid = [T.distance(p) / M for p in c[int(n * .3):int(n * .6)]]
        # the pit lane runs beside the main straight, about 12 m from the racing line
        self.assertTrue(all(10.0 <= d <= 13.5 for d in mid), (min(mid), max(mid)))

    def test_raw_car_positions_follow_the_reconstructed_lane(self):
        C = SegIndex(self.rec.centerline)
        for p in self.passes:
            ds = [C.distance((x, y)) / M for t, x, y in p["samples"] if self.t_in(p) <= t <= p["t_out"]]
            self.assertLess(sum(ds) / len(ds), 0.3, p["num"])
            self.assertLess(max(ds), 4.0, p["num"])           # the box itself is a few m off the fast lane

    def test_entry_and_exit_branch_smoothly_from_the_track(self):
        T = SegIndex(self.track, closed=True)
        c = self.rec.centerline
        self.assertTrue(self.rec.entry_joined and self.rec.exit_joined)
        self.assertLess(T.distance(c[0]) / M, 6.0)
        self.assertLess(T.distance(c[-1]) / M, 6.0)
        # the real data jumps 24 m sideways within 0.22 s at the pit entry - drawn as a branch
        worst = 0.0
        for i in range(4, len(c) // 3 - 4):
            p0, p1, p2 = c[i - 4], c[i], c[i + 4]
            h1 = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            h2 = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
            worst = max(worst, abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi))
        self.assertLess(math.degrees(worst), 10.0)
        lateral = [T.distance(p) / M for p in c[:len(c) // 3]]
        rise = next(i for i, d in enumerate(lateral) if d > 10.0) * 2.5
        self.assertGreater(rise, 50.0)                        # reaches the lane over >50 m, not in one step
        notes = [n for t in self.rec.used for n in t.notes]
        self.assertTrue(any("bridged" in n for n in notes))
        self.assertTrue(any("behind the car" in n for n in notes))   # car 1: position replayed 23 m back

    def test_each_single_real_pass(self):
        C = SegIndex(self.rec.centerline)
        for p in self.passes:
            r = reconstruct(self.collect([p]), True)
            self.assertEqual(r.confidence, "MEDIUM", p["num"])
            ds = sorted(C.distance(q) / M for q in r.centerline)
            self.assertLess(ds[len(ds) // 2], 1.0, p["num"])          # median distance to the 4-pass line

    def test_real_anomalies_give_no_false_pass(self):
        ver = [p for p in self.passes if p["num"] == "1"]
        t_in = self.t_in(ver[0])
        # long hole in the position data inside the pit lane
        tr = self.collect(ver, drop=lambda p, t: t_in + 4000 < t < t_in + 11000)[0]
        self.assertFalse(tr.accepted)
        self.assertIn("hole", tr.reason)
        # positions end in the pit lane (car stopped / retired there)
        tr = self.collect(ver, drop=lambda p, t: t > p["t_out"] - 8000)[0]
        self.assertFalse(tr.accepted)
        # only the box stop: nothing before / after
        tr = self.collect(ver, drop=lambda p, t: not (t_in + 5000 < t < t_in + 10000))[0]
        self.assertFalse(tr.accepted)
        # red flag: the official time says 5 minutes in the pit lane
        tr = self.collect(ver, t_in_shift=280)[0]
        self.assertFalse(tr.accepted)
        # car reverses 40 m back up the pit lane after the stop
        box_t = next(t for t, x, y in ver[0]["samples"] if x == 1766)
        back = [(box_t + 2600 + k * 400, 1766 - k * 23.0, -171 - k * 9.6) for k in range(1, 18)]
        tr = self.collect(ver, drop=lambda p, t: box_t + 2500 < t < box_t + 2500 + 17 * 400 + 1, extra={"1": back})[0]
        self.assertFalse(tr.accepted)
        self.assertIn("turns back", tr.reason)
        # slow-downs / safety car / a stop on the track: no pit signals -> no pass at all
        col = PitLaneCollector(self.track, keep_ms=None)
        for t, x, y in ver[0]["samples"]:
            col.hist.add("1", t, x, y)
        self.assertEqual(col.poll(math.inf), [])

    def test_cache_restart_and_session_orders(self):
        with tempfile.TemporaryDirectory() as d:
            # Qualifying first: cars only go to the garage - no complete pass -> nothing stored
            tp = TrackProvider(Path(d), {})
            quali = reconstruct(self.collect(self.passes, t_in_shift=300), True)
            self.assertIsNone(quali)
            self.assertTrue(needs_reconstruction(tp.pitcache, 144, 2025)[0])
            # Race: reconstructed and stored
            v, what = tp.pitcache.store(144, "Baku", 2025, 9904, self.rec, "race")
            self.assertEqual(v["status"], "verified")
            # "restart": new objects, Qualifying again -> drawn at once, no reconstruction
            tp2 = TrackProvider(Path(d), {})
            geo = TrackGeometry(144, "Baku", 2025, "multiviewer", [list(p) for p in self.track])
            tp2.attach_pitlane(geo, 2025)
            self.assertEqual(geo.pitlane_info["state"], "cached")
            self.assertGreater(len(geo.pitlane), 100)
            self.assertFalse(needs_reconstruction(tp2.pitcache, 144, 2025)[0])
            # another recording of the race: same geometry confirmed, still one variant
            v, what = tp2.pitcache.store(144, "Baku", 2025, 9904, reconstruct(self.collect(self.passes[:2]), True), "vod 2")
            self.assertIn("confirmed", what)
            self.assertEqual(len(json.loads(tp2.pitcache.path(144).read_text())["variants"]), 1)

    def test_season_isolation_with_changed_layout(self):
        # a future season with the pit lane moved 25 m (every real position shifted sideways)
        moved = [{**p, "samples": [(t, x - 0.39 * 250, y + 0.92 * 250) for t, x, y in p["samples"]]}
                 for p in self.passes]
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            cache.store(144, "Baku", 2025, 9904, self.rec, "2025")
            rec27 = reconstruct(self.collect(moved), True)
            v, what = cache.store(144, "Baku", 2027, 1, rec27, "2027")
            self.assertIn("differs", what)
            data = json.loads(cache.path(144).read_text())
            v25, v27 = PitLaneCache.select(data, 2025), PitLaneCache.select(data, 2027)
            self.assertNotEqual(v25["id"], v27["id"])
            C25 = SegIndex([tuple(p) for p in v25["centerline"]])
            self.assertLess(C25.distance(self.rec.centerline[len(self.rec.centerline) // 2]) / M, 1.0)

    def test_legacy_cache_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            tracks = Path(d) / "tracks"
            tracks.mkdir()
            (tracks / "pitlane_144.json").write_text(json.dumps(
                {"points": [[0, 0], [100, 0], [200, 0]], "source": "learned from position data"}))
            tp = TrackProvider(Path(d), {})
            geo = TrackGeometry(144, "Baku", 2025, "multiviewer", [list(p) for p in self.track])
            tp.attach_pitlane(geo, 2025)
            self.assertIsNone(geo.pitlane)
            self.assertEqual(geo.pitlane_info["state"], "none")
            self.assertTrue(needs_reconstruction(tp.pitcache, 144, 2025)[0])


class GarageAndDnfTest(unittest.TestCase):
    """Real 2026 Japanese GP timing: DNF, garage (not on the map) vs a pit stop, and InPit flags
    that stay true for an hour because the "left the pit" message was lost."""

    @classmethod
    def setUpClass(cls):
        from server.timeline import Timeline
        cls.tl = Timeline(buffer_seconds=1e6)
        for e in load_file(SAMPLE):
            cls.tl.ingest(e.topic, e.data, e.t.timestamp() * 1000, e.t.timestamp() * 1000, e.snap)

    def at(self, hms):
        from datetime import datetime
        from server.models import Availability
        from server.normalizer import Normalizer
        t = datetime.fromisoformat(f"2026-03-29T{hms}+00:00")
        self.tl.advance(t.timestamp() * 1000)
        dr = Normalizer().build(self.tl.feed, t, 1.0, Availability())["drivers"]
        tla = lambda f: sorted(d["tla"] for d in dr.values() if f(d))
        return {"dnf": tla(lambda d: d["dnf"]), "garage": tla(lambda d: d["in_garage"]),
                "lane": tla(lambda d: d["in_pit"] and not d["in_garage"])}

    def test_real_race(self):
        self.assertEqual(self.at("05:43:20")["lane"], ["LIN"])                    # a pit stop
        self.assertEqual(self.at("05:46:00")["lane"], [])                         # LIN's InPit stuck, but racing
        s = self.at("05:50:30")
        self.assertEqual(s["lane"], ["ALB", "LAW", "SAI"])
        self.assertEqual(s["garage"], [])
        s = self.at("06:07:00")
        self.assertEqual((s["lane"], s["garage"], s["dnf"]), (["STR"], [], ["BEA"]))  # STR comes in
        s = self.at("06:08:30")
        self.assertEqual((s["lane"], s["garage"]), ([], ["STR"]))                # > 75 s: garage, off the map
        s = self.at("06:40:00")
        # STR: Retired (in the garage). BEA: stopped on track at 05:48:58 and never moved again -
        # the Retired flag never came, still a DNF (not in the garage: stays where it stopped)
        self.assertEqual((s["dnf"], s["garage"], s["lane"]), (["BEA", "STR"], ["STR"], []))
        self.assertEqual(self.at("05:49:30")["dnf"], [])                         # just stopped: not yet
        self.assertEqual(self.at("05:50:30")["dnf"], ["BEA"])
        # seeking back restores it (state kept with the checkpoints)
        s = self.at("06:07:00")
        self.assertEqual((s["lane"], s["garage"], s["dnf"]), (["STR"], [], ["BEA"]))

    def test_vod_checkpoint_restore(self):
        """VOD jumps restore a checkpoint (whole state as a snapshot): STR must still be in the garage."""
        from datetime import datetime
        from server.models import Availability
        from server.normalizer import Normalizer
        from server.openf1 import OpenF1Client
        from server.sources.vod import VodSource, _ms as ems
        from server.timeline import Timeline
        events = load_file(SAMPLE)
        src = VodSource({"session_key": 1}, OpenF1Client(Path("/nonexistent")), Path("/nonexistent"))
        src.events, src._times = events, [ems(e) for e in events]
        src.ckpts, src.ref = VodSource._prepare(events)
        src.state = "ready"
        tl = Timeline(buffer_seconds=120)
        for hms, garage in (("06:40:00", ["STR"]), ("06:08:30", ["STR"]), ("05:50:30", [])):
            t = datetime.fromisoformat(f"2026-03-29T{hms}+00:00")
            src.ensure(t.timestamp() * 1000, lambda tp, d, ms, snap: tl.ingest(tp, d, ms, ms, snap), tl)
            tl.advance(t.timestamp() * 1000)
            dr = Normalizer().build(tl.feed, t, 1.0, Availability())["drivers"]
            self.assertEqual(sorted(d["tla"] for d in dr.values() if d["in_garage"]), garage, hms)


class PerformanceTest(unittest.TestCase):
    """A full race worth of positions: the VOD search decodes only around the pit stops; a cache
    hit costs next to nothing."""

    def test_full_race_volume(self):
        import time
        pts = S.track("az-2016")
        lane, i0 = S.pit_lane(pts)
        events = []
        t0 = S.T0
        # 2 h of Position.z for 20 cars (one entry per message, ~3.7 Hz) - positions on the outline
        n_msgs = int(2 * 3600 * 3.7)
        for k in range(n_msgs):
            ts = t0 + timedelta(seconds=k / 3.7)
            p = pts[(k * 7) % len(pts)]
            entry = {"Timestamp": ts.isoformat().replace("+00:00", "Z"),
                     "Entries": {str(c): {"X": round(p[0]), "Y": round(p[1]), "Z": 0} for c in range(1, 21)}}
            events.append(Event(ts, "Position.z", encode_z({"Position": [entry]})))
        start = time.perf_counter()
        trs = collect_from_events(events, pts)                  # no pit stops: nothing decoded
        idle = time.perf_counter() - start
        self.assertEqual(trs, [])
        self.assertLess(idle, 2.0)
        # 20 pit windows: only their neighbourhood is decoded
        for k in range(20):
            t_out = t0 + timedelta(minutes=5 * k + 3)
            events.append(Event(t_out, "PitLaneTimeCollection", {"PitTimes": {str(k + 1): {"Duration": "22.0"}}}))
        events.sort(key=lambda e: e.t)
        start = time.perf_counter()
        collect_from_events(events, pts)
        busy = time.perf_counter() - start
        self.assertLess(busy, 10.0)
        with tempfile.TemporaryDirectory() as d:
            cache = PitLaneCache(Path(d))
            trs, _ = run_passes(pts, lane, i0, n=4)
            cache.store(1, "x", 2026, 1, reconstruct(trs, True), "t")
            start = time.perf_counter()
            for _ in range(100):
                needs_reconstruction(cache, 1, 2026)
            self.assertLess((time.perf_counter() - start) / 100, 0.02)
        print(f"\n  full race: {n_msgs} Position.z messages, search without stops {idle:.2f} s, "
              f"with 20 stops {busy:.2f} s")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RenderHelpersTest(unittest.TestCase):
    """dashboard/components/pitlane.js: separation from the main straight + cars in the pit lane."""

    def run_js(self, code: str):
        js = (ROOT / "dashboard" / "components" / "pitlane.js").read_text() + "\n" + code
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_separation_and_driver_detection(self):
        r = self.run_js("""
          const track = []; for (let i = 0; i <= 200; i++) track.push([i * 5, 0]);            // main straight
          for (let i = 200; i >= 0; i--) track.push([i * 5, 400]);                            // back straight
          const pit = []; for (let i = 0; i <= 100; i++) {                                     // pit lane 6 px beside
            const x = 250 + i * 5, k = Math.min(1, i / 15, (100 - i) / 15); pit.push([x, 6 * k]); }
          const off = PitLane.separation(pit, track, 20, 17, 40);
          const drawn = pit.map((p, i) => [p[0] + off[i][0], p[1] + off[i][1]]);
          const far = pit.map((p) => [p[0], p[1] + 100]);
          const off2 = PitLane.separation(far, track, 20, 17, 40);
          console.log(JSON.stringify({
            midY: drawn[50][1], endShift: Math.hypot(...off[0]) + Math.hypot(...off[100]),
            maxShift: Math.max(...off.map((o) => Math.hypot(o[0], o[1]))),
            farShift: Math.max(...off2.map((o) => Math.hypot(o[0], o[1]))),
            inPitFlag: PitLane.carOnPit([500, 1], pit, track, true, 150),
            nearerPit: PitLane.carOnPit([500, 5], pit, track, false, 150),
            onTrack: PitLane.carOnPit([500, 1], pit, track, false, 150),
            elsewhere: PitLane.carOnPit([100, 400], pit, track, true, 150),
          }));""")
        self.assertGreaterEqual(r["midY"], 19.5)          # pushed to a readable gap, on its own side
        self.assertLessEqual(r["maxShift"], 17.01)        # controlled: never more than the cap
        self.assertLess(r["endShift"], 0.01)              # still branches off / rejoins where it really does
        self.assertEqual(r["farShift"], 0)                # already apart: not moved at all
        self.assertEqual(r["inPitFlag"], 50)              # timing says in pit -> drawn on the pit lane
        self.assertEqual(r["nearerPit"], 50)              # closer to the pit lane than to the track
        self.assertEqual(r["onTrack"], -1)                # on the main straight -> stays there
        self.assertEqual(r["elsewhere"], -1)

    def test_real_baku_classification_and_offset(self):
        """Real 2025 Baku: car on the main straight vs in the pit lane (12 m apart), at the pit
        entry before the timing flag, at the exit; and the visual offset at map scale."""
        import baku_real as B
        track, passes = B.load()
        col = RealBakuTest.collect.__func__(type("X", (), {"track": track}), passes)
        rec = reconstruct(col, True)
        ver = next(p for p in passes if p["num"] == "1")
        t_in = ver["t_out"] - ver["lane_s"] * 1000
        at = lambda hms: next((x, y) for t, x, y in ver["samples"] if B.iso(t)[11:23] == hms)
        cases = {
            "racing line before the pit entry": (at("12:17:55.394"), False, False),
            "pit entry road, timing flag not yet set": (at("12:17:56.534"), False, True),
            "at the box": (at("12:18:06.495"), True, True),
            "pit exit, flag already cleared": (at("12:18:19.475"), False, True),
            "back on track after T2": (at("12:18:30.614"), False, False),
            "other car on the main straight (same time)": ((390, -875), False, False),
            "other car on the main straight, level with the box": ((1518, -403), False, False),
            "other car on the main straight near the exit": ((2516, 14), False, False),
        }
        in_pit_samples = [[x, y] for t, x, y in ver["samples"] if t_in <= t <= ver["t_out"]]
        js = f"""
          const pit = {json.dumps([list(p) for p in rec.centerline])}, track = {json.dumps([list(p) for p in track])};
          const cases = {json.dumps({k: [list(v[0]), v[1]] for k, v in cases.items()})};
          const out = {{}};
          for (const [k, [p, flag]] of Object.entries(cases)) out[k] = PitLane.carOnPit(p, pit, track, flag, 150) >= 0;
          out.allInPit = {json.dumps(in_pit_samples)}.every((p) => PitLane.carOnPit(p, pit, track, true, 150) >= 0);
          // the map: whole circuit in ~790 px -> about 0.35 px per metre
          const sc = 0.035, S = (p) => [p[0] * sc, -p[1] * sc];
          const off = PitLane.separation(pit.map(S), track.map(S), 20, 17, 48);
          const drawn = pit.map((p, i) => [S(p)[0] + off[i][0], S(p)[1] + off[i][1]]);
          const trS = track.map(S), n = pit.length;
          const d = (q) => PitLane.nearest(q, trS, true).d;
          out.midGap = Math.min(...drawn.slice(Math.floor(n * .35), Math.floor(n * .55)).map(d));
          out.rawMidGap = Math.min(...pit.slice(Math.floor(n * .35), Math.floor(n * .55)).map((p) => d(S(p))));
          out.endGap = [d(drawn[0]), d(drawn[n - 1])];
          out.maxShift = Math.max(...off.map((o) => Math.hypot(o[0], o[1])));
          // consecutive drawn points: no jump anywhere (a smooth road, no step at entry / exit)
          out.maxStep = Math.max(...drawn.slice(1).map((q, i) => Math.hypot(q[0] - drawn[i][0], q[1] - drawn[i][1])));
          console.log(JSON.stringify(out));"""
        r = self.run_js(js)
        for k, (_, _, want) in cases.items():
            self.assertEqual(r[k], want, k)
        self.assertTrue(r["allInPit"])
        self.assertLess(r["rawMidGap"], 5)             # 12 m = ~4 px: the roads would merge on the map
        self.assertGreaterEqual(r["midGap"], 16)       # drawn apart
        self.assertLess(max(r["endGap"]), 3)           # still starts / ends on the track
        self.assertLessEqual(r["maxShift"], 17.01)
        self.assertLess(r["maxStep"], 3.0)             # smooth: 2.5 m points are < 1 px apart + shift change


if __name__ == "__main__":
    unittest.main()
