"""Self-tests for the parser layer.   Run:  python -m unittest discover tests"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.feedstate import FeedState, deep_merge  # noqa: E402
from server.models import Availability  # noqa: E402
from server.normalizer import Normalizer  # noqa: E402
from server.race_control import process_messages  # noqa: E402
from server.remote import RemoteController  # noqa: E402
from server.telemetry import CarDataStore, PositionStore, decode_z, encode_z, parse_utc  # noqa: E402
from server.track import sector_ranges  # noqa: E402


class FeedStateTest(unittest.TestCase):
    def test_list_updates_by_index_and_delete(self):
        t = {"Stints": [{"Compound": "SOFT", "TotalLaps": 3}]}
        deep_merge(t, {"Stints": {"0": {"TotalLaps": 4}, "1": {"Compound": "HARD"}}})
        self.assertEqual(t["Stints"][0]["TotalLaps"], 4)
        self.assertEqual(t["Stints"][1]["Compound"], "HARD")
        deep_merge(t, {"Stints": {"_deleted": ["0"]}})
        self.assertEqual(len(t["Stints"]), 1)

    def test_keyframe_replaces(self):
        fs = FeedState()
        fs.apply("TrackStatus", {"Status": "2", "Message": "Yellow"})
        fs.apply("TrackStatus", {"Status": "1", "_kf": True})
        self.assertNotIn("Message", fs.get("TrackStatus"))

    def test_timing_alias_merges(self):
        fs = FeedState()
        fs.apply("TimingData", {"Lines": {"1": {"Position": "1"}}})
        fs.apply("TimingDataF1", {"Lines": {"1": {"GapToLeader": {"Value": "LAP 3"}}}, "_kf": True})
        self.assertEqual(fs.get("TimingData")["Lines"]["1"]["Position"], "1")


class RaceControlTest(unittest.TestCase):
    def msgs(self, *texts, **extra):
        return {"Messages": [dict({"Utc": "2026-03-29T05:00:00", "Category": "Other", "Message": t}, **extra)
                             for t in texts]}

    def test_investigation_then_penalty(self):
        r = process_messages(self.msgs(
            "TURN 1 INCIDENT INVOLVING CARS 44 (HAM) AND 16 (LEC) NOTED - CAUSING A COLLISION",
            "FIA STEWARDS: TURN 1 INCIDENT INVOLVING CARS 44 (HAM) AND 16 (LEC) UNDER INVESTIGATION - CAUSING A COLLISION",
        ))
        self.assertEqual(r.driver_flags["44"].investigation, "UNDER INVESTIGATION")
        r = process_messages(self.msgs(
            "FIA STEWARDS: TURN 1 INCIDENT INVOLVING CARS 44 (HAM) AND 16 (LEC) UNDER INVESTIGATION - CAUSING A COLLISION",
            "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 44 (HAM) - CAUSING A COLLISION",
            "FIA STEWARDS: PENALTY SERVED - 5 SECOND TIME PENALTY FOR CAR 44 (HAM) - CAUSING A COLLISION",
        ))
        f = r.driver_flags["44"]
        self.assertEqual(f.penalties[0]["label"], "+5s")
        self.assertTrue(f.penalties[0]["served"])
        self.assertIsNone(f.investigation)                      # decided -> incident closed
        self.assertNotIn("16", r.driver_flags)

    def test_no_further_investigation(self):
        r = process_messages(self.msgs(
            "TURN 13 INCIDENT INVOLVING CARS 43 (COL) AND 87 (BEA) NOTED",
            "FIA STEWARDS: TURN 13 INCIDENT INVOLVING CARS 43 (COL) AND 87 (BEA) REVIEWED NO FURTHER INVESTIGATION",
        ))
        self.assertNotIn("43", r.driver_flags)

    def test_sector_flags(self):
        m = {"Messages": [
            {"Category": "Flag", "Flag": "YELLOW", "Scope": "Sector", "Sector": 7, "Message": "YELLOW IN TRACK SECTOR 7"},
            {"Category": "Flag", "Flag": "DOUBLE YELLOW", "Scope": "Sector", "Sector": 8, "Message": "DOUBLE YELLOW IN TRACK SECTOR 8"},
            {"Category": "Flag", "Flag": "CLEAR", "Scope": "Sector", "Sector": 7, "Message": "CLEAR IN TRACK SECTOR 7"},
        ]}
        self.assertEqual(process_messages(m).sector_flags, {"8": "DOUBLE YELLOW"})

    def test_dsq_drive_through(self):
        r = process_messages(self.msgs("FIA STEWARDS: DRIVE THROUGH PENALTY FOR CAR 1 (VER) - SPEEDING IN THE PIT LANE",
                                       "CAR 23 (ALB) DISQUALIFIED - FUEL"))
        self.assertEqual(r.driver_flags["1"].penalties[0]["label"], "DT")
        self.assertTrue(r.driver_flags["23"].disqualified)


class TelemetryTest(unittest.TestCase):
    def test_z_roundtrip_and_position(self):
        obj = {"Position": [{"Timestamp": "2026-03-29T05:00:00.1234567Z",
                             "Entries": {"1": {"Status": "OnTrack", "X": 100, "Y": -200, "Z": 5},
                                         "4": {"Status": "OnTrack", "X": 0, "Y": 0, "Z": 0}}}]}
        dec = decode_z(encode_z(obj))
        samples = PositionStore().ingest(dec, lambda t: int(t.timestamp() * 1000))
        self.assertEqual(len(samples[0]["cars"]), 1)          # (0,0,0) = no fix, dropped
        self.assertEqual(samples[0]["cars"][0][:3], ["1", 100, -200])

    def test_cardata_2026_no_drs(self):
        cd = CarDataStore()
        cd.ingest({"Entries": [{"Utc": "2026-03-29T05:00:00Z", "Cars": {"1": {"Channels": {"0": 11000, "2": 300, "3": 8, "4": 104, "5": 100, "45": 12}}}}]})
        t = cd.telemetry("1", 2026)
        self.assertEqual((t.speed, t.gear, t.throttle, t.brake, t.drs), (300, 8, 100, True, None))
        self.assertEqual(cd.telemetry("1", 2024).drs, "OPEN")
        self.assertIsNone(t.ers_percent)

    def test_parse_utc_variants(self):
        self.assertEqual(parse_utc("2026-03-29T05:14:04.01Z").microsecond, 10000)
        self.assertIsNotNone(parse_utc("2026-03-29T04:18:28"))


class NormalizerTest(unittest.TestCase):
    def test_race_driver(self):
        fs = FeedState()
        fs.apply("SessionInfo", {"Type": "Race", "Name": "Sprint", "Path": "2026/x/", "Meeting": {"Circuit": {"Key": 46}}})
        fs.apply("DriverList", {"1": {"Tla": "NOR", "TeamColour": "F47600", "Line": 1}})
        fs.apply("TimingData", {"Lines": {"1": {"Position": "1", "GapToLeader": "LAP 5", "InPit": True,
                                                "LastLapTime": {"Value": "1:31.000", "PersonalFastest": True}}}})
        fs.apply("TimingAppData", {"Lines": {"1": {"Stints": [{"Compound": "SOFT", "TotalLaps": 7, "StartLaps": 2, "New": "true"}]}}})
        st = Normalizer().build(fs, datetime.now(timezone.utc), 1.0, Availability())
        d = st["drivers"]["1"]
        self.assertEqual(st["session"]["session_kind"], "race")
        self.assertEqual((d["tla"], d["team_color"], d["tyre"]["compound"], d["tyre"]["tyre_age"], d["tyre"]["laps"]),
                         ("NOR", "#F47600", "SOFT", 7, 5))
        self.assertTrue(d["in_pit"] and d["last_lap"]["personal_best"])


class TrackTest(unittest.TestCase):
    def test_sector_ranges_and_pitlane(self):
        pts = [[i * 10.0, 0.0] for i in range(100)]
        r = sector_ranges(pts, [{"n": 1, "x": 0, "y": 0}, {"n": 2, "x": 500, "y": 0}])
        self.assertEqual(r, [{"n": 1, "start": 0, "end": 50}, {"n": 2, "start": 50, "end": 0}])
        # pit lane reconstruction: tests/test_pitlane.py


class RemoteTest(unittest.TestCase):
    def test_whitelist(self):
        sent = []

        async def pub(m):
            sent.append(m)
        rc = RemoteController({"keymap": {"KEY_UP": "MOVE_UP", "KEY_X": "RM_RF"}}, lambda: ["1", "44"], pub)
        self.assertNotIn("KEY_X", rc.keymap)
        loop = asyncio.new_event_loop()
        self.assertFalse(loop.run_until_complete(rc.handle_command("SHELL", "ls")))
        self.assertTrue(loop.run_until_complete(rc.handle_key("KEY_UP", "test")))
        self.assertTrue(loop.run_until_complete(rc.handle_command("CHANGE_VIEW", "strategy")))
        self.assertEqual(rc.ui.view, "strategy")
        self.assertFalse(loop.run_until_complete(rc.handle_key("bad key!", "test")))
        loop.close()


class ArchiveFollowTest(unittest.TestCase):
    """Growing archive file served with HTTP Range, like a CDN would."""

    def test_follow_growing_stream(self):
        import httpx
        from server.sources.archive_follow import ArchiveFollower

        def line(i):
            obj = {"Position": [{"Timestamp": f"2026-03-29T05:00:{i:02d}.000Z",
                                 "Entries": {"1": {"Status": "OnTrack", "X": i, "Y": 2 * i, "Z": 1}}}]}
            return f'00:00:{i:02d}.000"{encode_z(obj)}"\r\n'.encode()

        content = {"Position.z": bytearray("\ufeff".encode() + b"".join(line(i) for i in range(30)))}

        def handler(req: httpx.Request) -> httpx.Response:
            topic = req.url.path.rsplit("/", 1)[-1].replace(".jsonStream", "")
            if topic not in content:
                return httpx.Response(404)
            body = bytes(content[topic])
            rng = req.headers.get("range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(body):
                    return httpx.Response(416)
                return httpx.Response(206, content=body[start:])
            return httpx.Response(200, content=body)

        got = []
        f = ArchiveFollower()
        f.reset("2026/x/y/")
        loop = asyncio.new_event_loop()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        loop.run_until_complete(f.poll(client, lambda t, d: got.append((t, d))))
        self.assertEqual(f.state, "following")
        self.assertEqual(len(got), 12)                     # first contact: only the newest lines
        content["Position.z"] += line(30) + line(31)[:20]    # one full + one partial line appended
        loop.run_until_complete(f.poll(client, lambda t, d: got.append((t, d))))
        self.assertEqual(len(got), 13)
        content["Position.z"] += line(31)[20:]
        loop.run_until_complete(f.poll(client, lambda t, d: got.append((t, d))))
        self.assertEqual(len(got), 14)
        last = decode_z(got[-1][1])
        self.assertEqual(last["Position"][0]["Entries"]["1"]["X"], 31)
        loop.run_until_complete(client.aclose())
        loop.close()


class TvModeTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.sent = []

        async def pub(m):
            self.sent.append(m)
        self.rc = RemoteController({
            "keymap": {"KEY_UP": "MOVE_UP", "KEY_OK": "OPEN_TELEMETRY", "KEY_SPACE": "CYCLE_TV_MODE",
                       "KEY_BACK": "CLOSE_PANEL", "KEY_PLAYPAUSE": "VIDEO_PLAY_PAUSE"},
            "keymap_video": {"KEY_OK": "VIDEO_FOCUS", "KEY_BACK": "VIDEO_UNFOCUS"},
            "keymap_video_focus": {"KEY_UP": "VIDEO_VOLUME:up", "KEY_LEFT": "VIDEO_SEEK:-10"},
        }, lambda: ["1", "44"], pub)
        self.rc.set_default_tv_mode("RACE_VIEW")

    def key(self, k):
        return self.loop.run_until_complete(self.rc.handle_key(k, "test"))

    def test_without_video_everything_is_dashboard(self):
        self.assertEqual(self.rc.ui.tv_mode_effective, "FULL_DASHBOARD")
        self.key("KEY_OK")
        self.assertEqual(self.rc.ui.view, "telemetry")             # OK keeps its dashboard meaning
        self.key("KEY_PLAYPAUSE")
        self.assertEqual(self.rc.ui.video_cmd["n"], 0)             # no video -> ignored

    def test_layers(self):
        self.loop.run_until_complete(self.rc.set_video_available(True, None))
        self.assertEqual(self.rc.ui.tv_mode_effective, "RACE_VIEW")
        self.key("KEY_UP")                                          # first press selects the leader
        self.key("KEY_UP")
        self.assertEqual(self.rc.ui.selected, "44")                # Up/Down still navigate
        self.key("KEY_OK")
        self.assertTrue(self.rc.ui.video_focus)
        self.key("KEY_UP")
        self.assertEqual(self.rc.ui.video_cmd["action"], "volume")
        self.assertEqual(self.rc.ui.selected, "44")                # not moved while video has focus
        self.key("KEY_BACK")
        self.assertFalse(self.rc.ui.video_focus)
        self.key("KEY_SPACE")
        self.assertEqual(self.rc.ui.tv_mode_effective, "VIDEO_FOCUS")
        self.key("KEY_BACK")
        self.assertEqual(self.rc.ui.tv_mode_effective, "RACE_VIEW")
        self.loop.run_until_complete(self.rc.set_video_available(False, "unreachable"))
        self.assertEqual(self.rc.ui.tv_mode_effective, "FULL_DASHBOARD")
        self.assertEqual(self.rc.ui.tv_mode, "RACE_VIEW")          # comes back when video returns

    def test_framing_headers(self):
        import httpx
        from server.video import framing_allowed
        self.assertFalse(framing_allowed(httpx.Headers({"X-Frame-Options": "SAMEORIGIN"}))[0])
        self.assertFalse(framing_allowed(httpx.Headers({"Content-Security-Policy": "frame-ancestors 'self'"}))[0])
        self.assertTrue(framing_allowed(httpx.Headers({"Content-Security-Policy": "frame-ancestors *"}))[0])
        self.assertTrue(framing_allowed(httpx.Headers({}))[0])

    def tearDown(self):
        self.loop.close()


class TvAgentTest(unittest.TestCase):
    def test_follow_modes_and_hotkeys(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import tv_launcher as tl

        class FakeOps(tl.NullOps):
            name, dpi_scale = "fake", 1.5
            def __init__(self): self.calls, self.fg, self.pending = [], False, []
            def screen_size(self): return 2880, 1620          # 1920x1080 @150 % in physical pixels
            def find_voyo(self, pid, hint): return 42
            def is_valid(self, w): return w == 42
            def place(self, w, rect, topmost): self.calls.append(("place", rect, topmost))
            def minimize(self, w): self.calls.append(("min",))
            def send_back(self, w): self.calls.append(("back",))
            def foreground_is(self, w): return self.fg
            def poll_hotkeys(self, active):
                self.calls.append(("hk", active))
                out, self.pending = (self.pending if active else []), []
                return out

        ops, sent = FakeOps(), []
        agent = tl.TvAgent(ops, "http://x", "", 32, "VOYO", None, http_get=lambda p: {}, http_key=sent.append)
        agent.step({"tv_mode_effective": "RACE_VIEW"})
        self.assertEqual(ops.calls[0], ("place", (0, -48, 1920, 1128), True))   # title bar pushed above
        agent.step({"tv_mode_effective": "VIDEO_FOCUS"})
        self.assertEqual(ops.calls[-2][:2], ("place", (0, -48, 2880, 1533)))
        ops.fg, ops.pending = True, ["T"]
        agent.step({"tv_mode_effective": "VIDEO_FOCUS"})
        self.assertEqual(sent, ["KEY_T"])
        agent.step({"tv_mode_effective": "FULL_DASHBOARD"})
        self.assertIn(("back",), ops.calls)                                     # behind the dashboard,
        self.assertNotIn(("min",), ops.calls)                                   # never minimized (pauses video)
        self.assertEqual(ops.calls[-1], ("hk", False))                          # no hotkeys in FULL_DASHBOARD


if __name__ == "__main__":
    unittest.main()
