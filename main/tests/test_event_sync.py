"""EVENT SYNC: real, timestamped F1 events <-> the VOYO position, several points combined.

The VOD scenarios use the recorded 2026 Japanese GP race (F1 archive, data/recordings): a really
delayed start (scheduled 05:00 UTC, "FORMATION LAP WILL START AT 14:10", lights out 05:14:02.078).
Its timestamped events: PIT EXIT OPEN / CLOSED (race control, whole seconds), TRACK YELLOW,
LIGHTS OUT, every lap start (line crossings, ms), SAFETY CAR DEPLOYED (StatusSeries, ms),
SAFETY CAR ENDING (race control), TRACK CLEAR, CHEQUERED FLAG. There is no 10 / 5 / 3 / 1 minute
or formation lap event in the F1 data - none is offered.

Run:  python -m unittest tests.test_event_sync
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import DATA_DIR  # noqa: E402

from server.openf1 import RefEvents  # noqa: E402
from server.sources.replay import load_file  # noqa: E402
from server.sources.vod import ref_events_from_archive  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from server.telemetry import parse_utc  # noqa: E402
from tests.test_sync_start import MIN, SCHED, LiveRace  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((ROOT / "tests" / "fixtures" / "openf1_2026_japan.json").read_text())
JP_SCHED = parse_utc("2026-03-29T05:00:00Z").timestamp() * 1000
JP_LO = parse_utc("2026-03-29T05:14:02.078Z").timestamp() * 1000
JP_ORIGIN = JP_SCHED - (28 * 60 + 50) * 1000          # the recording's 0:00 (unknown to the system)
EVENTS = None


def jp_ref():
    global EVENTS
    if EVENTS is None:
        EVENTS = load_file(DATA_DIR / "recordings" / "sample-2026-japan-race.json.gz")
    return ref_events_from_archive(EVENTS)


def jp_session(key=11253, name="Race"):
    base = next(x for x in FIX["sessions"] if x["session_key"] == 11253)
    return {**base, "session_key": key, "session_name": name, "gmt_offset": "09:00:00",
            "meeting_name": "Japanese Grand Prix"}


class Vod:
    def __init__(self, store=None, sess=None, ref=None, origin=JP_ORIGIN, asset="voyo-media:jp-race"):
        self.s = SyncManager({"enabled": True, "mark_reaction_seconds": 0.0}, voyo_enabled=True, legacy_delay=0,
                             source_speed=1.0, store_path=store, vod=True)
        self.s.initialize(sess or jp_session(), ref if ref is not None else jp_ref())
        self.origin, self.asset, self.mono = origin, asset, 0.0

    def at_pb(self, pb):
        self.mono += 1.0
        self.s.update(parse_voyo_sample({"playback_time": pb, "paused": True, "asset": self.asset}, 0.0, self.mono),
                      0.0)
        self.s.target(self.mono, 0.0)

    def at(self, f1_ms, late_s=0.0):
        self.at_pb((f1_ms - self.origin) / 1000 + late_s)

    def ev(self, label, n=0):
        lst = [e for e in self.s.event_catalog() if e["label"] == label]
        return lst[n]

    def set(self, label, late_s=0.0, n=0):
        e = self.ev(label, n)
        self.at(e["ms"], late_s)
        return self.s.event_set(self.mono, 0.0, e["id"])

    def state(self):
        return self.s.get_state(self.mono, 0.0, None)

    def shown(self):
        return self.s.target(self.mono, 0.0).ms


class CatalogTest(unittest.TestCase):
    def test_only_real_events(self):
        v = Vod()
        labels = [e["label"] for e in v.s.event_catalog()]
        for want in ("PIT EXIT OPEN", "PIT EXIT CLOSED", "LIGHTS OUT", "LAP 2", "LAP 53", "SAFETY CAR DEPLOYED",
                     "SAFETY CAR ENDING", "TRACK CLEAR", "CHEQUERED FLAG"):
            self.assertIn(want, labels)
        for never in ("FORMATION LAP", "5 MINUTES", "1 MINUTE", "10 MINUTES"):
            self.assertNotIn(never, labels)                         # not in the F1 data -> not offered
        lo = v.ev("LIGHTS OUT")
        self.assertEqual((lo["ms"], lo["precise"]), (JP_LO, True))
        self.assertEqual(v.ev("SAFETY CAR DEPLOYED")["ms"], parse_utc("2026-03-29T05:48:11.661Z").timestamp() * 1000)
        self.assertFalse(v.ev("PIT EXIT CLOSED")["precise"])        # race control: whole seconds
        self.assertEqual(len([x for x in labels if x == "SAFETY CAR DEPLOYED"]), 1)   # TrackStatus + RCM = one

    def test_delayed_race_has_no_scheduled_event(self):
        v = Vod()
        cat = v.s.event_catalog()
        self.assertFalse(any(abs(e["ms"] - JP_SCHED) < 60_000 for e in cat))   # nothing at the 05:00 schedule
        self.assertEqual(v.ev("LIGHTS OUT")["ms"], JP_LO)          # the actual, delayed lights out

    def test_no_spoilers_before_the_video_reaches_them(self):
        v = Vod()
        v.at_pb(10.0)
        menu = [e["label"] for e in v.s.event_menu()]
        self.assertIn("LIGHTS OUT", menu)
        self.assertNotIn("SAFETY CAR DEPLOYED", menu)              # unsynced: race incidents hidden
        self.assertNotIn("CHEQUERED FLAG", menu)
        v.set("LIGHTS OUT")
        v.at(parse_utc("2026-03-29T05:50:00Z").timestamp() * 1000)
        menu = [e["label"] for e in v.s.event_menu()]
        self.assertIn("SAFETY CAR DEPLOYED", menu)                 # reached on the synchronised video
        self.assertNotIn("CHEQUERED FLAG", menu)
        self.assertGreater(v.state()["eventSync"]["hiddenIncidents"], 0)


class EventSyncTest(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp()) / "sync_calibration.json"

    # 1 + 2: before lights out, two events without lights out
    def test_events_before_lights_out_without_lights_out(self):
        origin = JP_SCHED - 45 * MIN                                 # this broadcast starts 04:15
        v = Vod(self.store, origin=origin)
        msg = v.set("PIT EXIT OPEN")
        self.assertIn("EVENT SYNC PIT EXIT OPEN ✓", msg)
        self.assertAlmostEqual(v.s.mapping.offset * 1000, origin, delta=1)
        v.set("PIT EXIT CLOSED")
        st = v.state()
        self.assertEqual(st["confidence"], "HIGH")                  # two independent points agree
        self.assertEqual(st["anchorsIndependent"], 2)
        self.assertNotIn("start", [a["kind"] for a in st["anchors"]])   # no lights out needed
        self.assertAlmostEqual(v.shown(), parse_utc("2026-03-29T04:40:01Z").timestamp() * 1000, delta=50)

    # 3 + 4: several points, lights out + laps
    def test_multiple_points_lights_out_and_laps(self):
        v = Vod(self.store)
        for label, late in (("LIGHTS OUT", 0.05), ("LAP 2", -0.04), ("LAP 3", 0.02), ("LAP 5", 0.0)):
            v.set(label, late)
        st = v.state()
        self.assertEqual(st["confidence"], "HIGH")
        self.assertEqual(len(st["eventSync"]["points"]), 4)
        self.assertTrue(st["errorMeasured"])
        self.assertLess(abs(v.s.mapping.offset * 1000 - JP_ORIGIN), 60)    # combined, not the last point
        self.assertEqual({p["label"] for p in st["eventSync"]["points"]}, {"LIGHTS OUT", "LAP 2", "LAP 3", "LAP 5"})

    # 5: a deliberately bad point
    def test_outlier_is_flagged_not_used_not_erased(self):
        v = Vod(self.store)
        for label in ("LIGHTS OUT", "LAP 2", "LAP 3", "LAP 4"):
            v.set(label)
        msg = v.set("LAP 6", late_s=4.6)                           # pressed 4.6 s late
        self.assertIn("OUTLIER", msg)
        st = v.state()
        rows = {p["label"]: p for p in st["eventSync"]["points"]}
        self.assertEqual(rows["LAP 6"]["state"], "outlier")
        self.assertAlmostEqual(rows["LAP 6"]["residual"], -4.6, delta=0.05)
        self.assertEqual(len(rows), 5)                              # kept and shown
        self.assertAlmostEqual(v.s.mapping.offset * 1000, JP_ORIGIN, delta=5)
        self.assertEqual(st["confidence"], "HIGH")
        self.assertIn("1 outlier", st["reason"])

    # 7 + 11 (VOD, never today's clock)
    def test_old_gp_vod_never_uses_todays_clock(self):
        weeks_later = datetime(2026, 12, 24, 9, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch("server.sync.time.time", return_value=weeks_later):
            v = Vod(self.store)
            v.set("LIGHTS OUT")
            v.set("SAFETY CAR DEPLOYED")
            self.assertAlmostEqual(v.s.mapping.offset * 1000, JP_ORIGIN, delta=5)
            self.assertAlmostEqual(v.shown(), parse_utc("2026-03-29T05:48:11.661Z").timestamp() * 1000, delta=50)

    # 8 + 9: isolation and reopening
    def test_session_and_video_isolation_and_reopen(self):
        race = Vod(self.store, jp_session(9636, "Race"), asset="voyo-media:sgp-race")
        race.set("LIGHTS OUT")
        race.set("LAP 3")
        quali = Vod(self.store, jp_session(9627, "Qualifying"), asset="voyo-media:sgp-race")
        quali.at_pb(100.0)
        self.assertEqual(quali.state()["eventSync"]["points"], [])   # Singapore qualifying: nothing shared
        self.assertEqual(quali.state()["confidence"], "UNSYNCED")
        other_video = Vod(self.store, jp_session(9636, "Race"), asset="voyo-media:other-broadcast")
        other_video.at_pb(100.0)
        self.assertEqual(other_video.state()["eventSync"]["points"], [])
        again = Vod(self.store, jp_session(9636, "Race"), asset="voyo-media:sgp-race")
        again.at_pb(3000.0)
        st = again.state()
        self.assertEqual({p["label"] for p in st["eventSync"]["points"]}, {"LIGHTS OUT", "LAP 3"})
        self.assertTrue(all(p["eventId"] for p in st["eventSync"]["points"]))
        self.assertEqual(st["confidence"], "HIGH")

    # 10 + 11: with MARK STREAM START; clearing keeps it
    def test_with_stream_start_and_clear(self):
        v = Vod(self.store)
        v.at_pb(0.0)
        self.assertIn("STREAM START 0:00 SET", v.s.mark_stream_start(v.mono, 0.0))
        v.set("LIGHTS OUT")
        v.set("LAP 2")
        st = v.state()
        self.assertEqual(st["streamStart"]["originUtc"][:12], "04:31:10.000")   # derived from the event points
        self.assertIn("EVENT SYNC POINTS CLEARED (2)", v.s.clear_event_points())
        st = v.state()
        self.assertEqual(st["eventSync"]["points"], [])
        self.assertTrue(st["streamStart"]["set"])                  # the stream start stays ...
        self.assertEqual(st["method"], "Stream start (0:00)")      # ... and keeps the sync
        self.assertAlmostEqual(v.shown(), JP_LO + 0, delta=600_000)
        # and the reverse: resetting the stream start keeps event points
        v.set("LAP 3")
        v.s.reset_stream_start()
        self.assertEqual([p["label"] for p in v.state()["eventSync"]["points"]], ["LAP 3"])

    def test_remove_one_point_and_reset_the_same_event(self):
        v = Vod(self.store)
        v.set("LIGHTS OUT", 0.5)
        v.set("LIGHTS OUT", 0.0)                                   # the same event again: replaced
        pts = v.state()["eventSync"]["points"]
        self.assertEqual(len(pts), 1)
        v.set("LAP 2")
        pid = next(p["id"] for p in v.state()["eventSync"]["points"] if p["label"] == "LAP 2")
        self.assertIn("REMOVED LAP 2", v.s.remove_point(pid))
        self.assertEqual([p["label"] for p in v.state()["eventSync"]["points"]], ["LIGHTS OUT"])

    def test_live_event_sync_and_delayed_start(self):
        r = LiveRace(self, stream_delay_s=37.35, start_ms=SCHED + 18 * MIN,
                     notices=[(SCHED - MIN, "RACE START DELAYED")])
        r.to_video_showing(SCHED + 18 * MIN)
        cat = r.sync.event_catalog()
        lo = next(e for e in cat if e["label"] == "LIGHTS OUT")
        self.assertEqual(lo["ms"], SCHED + 18 * MIN)               # the actual start, not 15:00
        r.sync.event_set(r.mono, r.now, lo["id"])
        r.to_video_showing(SCHED + 18 * MIN + 2 * 90_000)
        lap = next(e for e in r.sync.event_catalog() if e["label"] == "LAP 3")
        r.sync.event_set(r.mono, r.now, lap["id"])
        st = r.state()
        self.assertEqual(st["confidence"], "HIGH")
        self.assertAlmostEqual(st["streamDelaySeconds"], 37.35, delta=0.1)


class RemoteNavigationTest(unittest.TestCase):
    def test_keys_drive_the_event_menu_and_back(self):
        from starlette.testclient import TestClient
        from server.app import create_app
        from server.config import load_config
        tmp = tempfile.mkdtemp()
        cfg = load_config(Path(tmp) / "none.toml")
        cfg["source"]["mode"] = "test"
        cfg["test"]["time_scale"] = 5
        cfg["voyo"]["check_reachability"] = False
        cfg["f1_tv"]["auth_file"] = str(Path(tmp) / "auth.json")
        cfg["f1_tv"]["open_browser"] = False
        cfg["remote"]["keymap"] = {"KEY_UP": "MOVE_UP", "KEY_DOWN": "MOVE_DOWN", "KEY_OK": "OPEN_TELEMETRY",
                                   "KEY_BACK": "CLOSE_PANEL"}

        def until(ws, pred, n=400):
            for _ in range(n):
                m = ws.receive_json()
                if pred(m):
                    return m
            raise AssertionError("not received")
        with TestClient(create_app(cfg)) as c, c.websocket_connect("/ws?client=remote") as phone:
            # the simulator's session start must be in the data first
            until(phone, lambda m: m["type"] == "sync" and (m.get("eventSync") or {}).get("events"), n=3000)
            phone.send_json({"type": "command", "command": "SYNC_MENU", "arg": "open"})
            phone.send_json({"type": "command", "command": "SYNC_EVENT_MENU", "arg": "open"})
            until(phone, lambda m: m["type"] == "ui" and m["sync_menu"] and m["sync_events"])
            s1 = until(phone, lambda m: m["type"] == "sync" and (m.get("eventSync") or {}).get("sel"))["eventSync"]
            phone.send_json({"type": "key", "key": "KEY_DOWN"})       # ▼: next event (not the driver list)
            es = lambda m: m.get("eventSync") or {}                    # noqa: E731
            s2 = es(until(phone, lambda m: m["type"] == "sync" and (es(m).get("sel") not in (None, s1["sel"])
                                                                    or len(s1["events"]) == 1)))
            phone.send_json({"type": "key", "key": "KEY_UP"})
            until(phone, lambda m: m["type"] == "sync" and es(m).get("sel") == s1["sel"])
            phone.send_json({"type": "key", "key": "KEY_OK"})         # OK: SET
            st = until(phone, lambda m: m["type"] == "sync" and es(m).get("points"))
            self.assertEqual(st["eventSync"]["points"][0]["eventId"], s1["sel"])
            phone.send_json({"type": "key", "key": "KEY_BACK"})       # BACK: out of the sub-menu
            ui = until(phone, lambda m: m["type"] == "ui" and not m["sync_events"])
            self.assertTrue(ui["sync_menu"])                          # still in SYNC
            self.assertTrue(s2["events"])
            # closing SYNC closes the sub-menu too; the keys are normal dashboard keys again
            phone.send_json({"type": "command", "command": "SYNC_EVENT_MENU", "arg": "open"})
            until(phone, lambda m: m["type"] == "ui" and m["sync_events"])
            phone.send_json({"type": "command", "command": "SYNC_MENU", "arg": "close"})
            ui = until(phone, lambda m: m["type"] == "ui" and not m["sync_menu"])
            self.assertFalse(ui["sync_events"])
            phone.send_json({"type": "key", "key": "KEY_DOWN"})
            ui = until(phone, lambda m: m["type"] == "ui" and m.get("selected"))
            self.assertFalse(ui["sync_events"])


if __name__ == "__main__":
    unittest.main()
