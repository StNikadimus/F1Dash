"""F1 TV sign-in, authenticated / anonymous live connection, and the data an authenticated feed
may carry (Position.z incl. non-driver objects, CarData.z, tyres, track status, race control),
freshness, recording, diagnostics - and that no secret ever leaves the server.

No real F1 connection is made (not reachable from the test environment): the SignalR transport
is replaced by fakes; negotiate is answered by httpx.MockTransport.

Run:  python -m unittest tests.test_f1tv
"""
import asyncio
import base64
import gzip
import json
import logging
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.config import load_config  # noqa: E402
from server.f1tv_auth import AuthManager, AuthStore, parse_token, token_info  # noqa: E402
from server.sources import f1_live  # noqa: E402
from server.sources.f1_live import AuthRejected, F1LiveSource, FeedError, SubscribeFailed  # noqa: E402
from server.telemetry import encode_z  # noqa: E402

T0 = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)


def jwt(exp_in_s: float = 86400, product: str = "F1 TV Access", status: str = "active") -> str:
    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256', 'kid': 'k'})}.{b64({'exp': int(time.time() + exp_in_s), 'SubscribedProduct': product, 'SubscriptionStatus': status})}.c2ln"


def cookie(token: str) -> str:
    """The login-session cookie value as formula1.com stores it (URL-encoded JSON)."""
    return urllib.parse.quote(json.dumps({"data": {"subscriptionToken": token}}))


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage())


def capture_logs():
    h = LogCapture()
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.DEBUG)
    return h


def manager(d: str, subscription=True, opened=None, override=""):
    opened = opened if opened is not None else []
    m = AuthManager({"subscription": subscription}, AuthStore(Path(d) / "auth" / "f1tv_auth.json"), override,
                    opener=lambda url: opened.append(url) or True)
    m.login_url = "http://127.0.0.1:8080/f1tv/login"
    return m, opened


# ---------------------------------------------------------------------------
class ConfigTest(unittest.TestCase):
    def test_subscription_default_true(self):
        cfg = load_config("/nonexistent.toml")
        self.assertIs(cfg["f1_tv"]["subscription"], True)
        self.assertIs(load_config()["f1_tv"]["subscription"], True)      # the shipped config.toml

    def test_subscription_false_in_toml_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text("[f1_tv]\nsubscription = false\n")
            self.assertIs(load_config(p)["f1_tv"]["subscription"], False)
        os.environ["F1DASH_F1_TV_SUBSCRIPTION"] = "false"
        try:
            self.assertIs(load_config("/nonexistent.toml")["f1_tv"]["subscription"], False)
        finally:
            del os.environ["F1DASH_F1_TV_SUBSCRIPTION"]

    def test_f1_tv_section_comes_first_in_the_f1_config(self):
        text = (Path(__file__).resolve().parent.parent / "config" / "config.toml").read_text()
        self.assertLess(text.index("[f1_tv]"), text.index("[live]"))
        self.assertIn("subscription = true", text[text.index("[f1_tv]"):text.index("[live]")])

    def test_auth_files_are_git_ignored(self):
        gi = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
        for pat in ("data/", "data/auth/", "f1tv_auth.json", ".env"):
            self.assertIn(pat, gi)


class AuthStoreTest(unittest.TestCase):
    def test_store_only_the_token_private_file(self):
        with tempfile.TemporaryDirectory() as d:
            st = AuthStore(Path(d) / "auth" / "f1tv_auth.json")
            tok = jwt()
            st.save(tok)
            data = json.loads(st.path.read_text())
            self.assertEqual(set(data), {"subscription_token", "stored_utc"})
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(st.path.stat().st_mode), 0o600)
            self.assertEqual(st.load(), tok)
            k = st.key()
            self.assertEqual(st.key(), k)                       # stable per installation
            self.assertTrue(st.clear())
            self.assertIsNone(st.load())

    def test_parse_token_and_info(self):
        tok = jwt(product="F1 TV Pro")
        self.assertEqual(parse_token(cookie(tok)), tok)
        self.assertEqual(parse_token("Bearer " + tok), tok)
        self.assertIsNone(parse_token("nonsense"))
        info = token_info(tok)
        self.assertEqual((info["subscribed_product"], info["expired"]), ("F1 TV Pro", False))
        self.assertTrue(token_info(jwt(exp_in_s=-10))["expired"])


class AuthManagerTest(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            self.assertEqual((m.state, m.token()), ("NO_LOGIN", None))
            m.store.save(jwt())
            m2, _ = manager(d)
            self.assertEqual(m2.state, "VALID")
            self.assertIsNotNone(m2.token())
            m.store.save(jwt(exp_in_s=-60))
            m3, _ = manager(d)
            self.assertEqual((m3.state, m3.token()), ("EXPIRED", None))
            off, _ = manager(d, subscription=False)
            m.store.save(jwt())
            self.assertEqual((off.state, off.token()), ("DISABLED", None))   # never used when off

    def test_browser_opens_once_then_only_when_forced(self):
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            m.request_login("first start")
            m.request_login("again")
            self.assertEqual(opened, ["http://127.0.0.1:8080/f1tv/login"])
            m.request_login("forced", force=True)
            self.assertEqual(len(opened), 2)
            off, opened_off = manager(d, subscription=False)
            off.request_login("x", force=True)
            self.assertEqual(opened_off, [])

    def test_complete_and_reject(self):
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            v = m.version
            ok, msg = m.complete("garbage")
            self.assertFalse(ok)
            ok, msg = m.complete(cookie(jwt(exp_in_s=-5)))
            self.assertFalse(ok)
            tok = jwt()
            ok, msg = m.complete(cookie(tok))
            self.assertTrue(ok, msg)
            self.assertEqual((m.state, m.token(), m.store.load(), m.version), ("VALID", tok, tok, v + 1))
            self.assertFalse(m.public_info()["confirmed_by_f1"])        # signed in, F1 has not accepted it yet
            m.confirm()
            self.assertTrue(m.public_info()["confirmed_by_f1"])
            m.reject("negotiate HTTP 401")
            self.assertFalse(m.public_info()["confirmed_by_f1"])
            self.assertEqual((m.state, m.token(), m.store.load()), ("REJECTED", None, None))
            self.assertEqual(len(opened), 1)                   # re-login started

    def test_expiry_while_running_triggers_login(self):
        with tempfile.TemporaryDirectory() as d:
            m, _ = manager(d)
            m.complete(cookie(jwt(exp_in_s=EXPIRY_OK)))
            m._token = jwt(exp_in_s=30)                          # now within the expiry margin
            self.assertIsNone(m.token())
            self.assertEqual(m.state, "EXPIRED")
            self.assertTrue(m.needs_login())

    def test_no_secret_in_logs_or_public_info(self):
        logs = capture_logs()
        with tempfile.TemporaryDirectory() as d:
            m, _ = manager(d)
            tok = jwt()
            m.complete(cookie(tok))
            info = json.dumps(m.public_info())
            m.reject("HTTP 403")
        sig = tok.split(".")[1]
        self.assertNotIn(sig, info)
        self.assertFalse(any(tok in t or sig in t for t in logs.text))


EXPIRY_OK = 86400


# ---------------------------------------------------------------------------
class FakeSink:
    def __init__(self):
        self.status = []
        self.fed = []

    async def feed(self, topic, data, ts, snapshot=False, origin="feed"):
        self.fed.append((topic, snapshot))

    async def begin_snapshot(self):
        pass

    def set_status(self, **st):
        self.status.append(st)

    def set_schedule(self, s):
        pass


def live_source(m: AuthManager, script):
    """F1LiveSource whose SignalR connection is ``script(src, attempt)`` (no network)."""
    src = F1LiveSource({"topics": list(f1_live.CORE_TOPICS) + ["TyreStintSeries", "AudioStreams"],
                        "reconnect_min": 0.01, "reconnect_max": 0.02}, None, auth=m)
    calls = []

    async def run_core(sink):
        calls.append({"token": src._conn_token, "topics": list(src._topics_now()), "mode": src.auth_mode})
        await script(src, len(calls) - 1, sink)

    async def nothing(*_a):
        await asyncio.sleep(3600)
    src._run_core = run_core
    src._run_legacy = run_core
    src._schedule_loop = nothing
    src._archive_loop = nothing
    return src, calls


async def run_for(src, sink, until, timeout=3.0):
    task = asyncio.create_task(src.run(sink))
    t0 = time.monotonic()
    while not until() and time.monotonic() - t0 < timeout:
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class LiveConnectionTest(unittest.TestCase):
    def test_subscription_false_is_anonymous_and_never_signs_in(self):
        async def script(src, n, sink):
            raise FeedError("closed")
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d, subscription=False)
            m.store.save(jwt())
            src, calls = live_source(m, script)
            sink = FakeSink()
            asyncio.run(run_for(src, sink, lambda: len(calls) >= 2))
            self.assertTrue(all(c["token"] is None and c["mode"] == "ANONYMOUS" for c in calls))
            self.assertEqual(opened, [])

    def test_authenticated_connection_uses_the_token(self):
        async def script(src, n, sink):
            sink.set_status(state="connected", auth=src.auth_mode)
            await asyncio.sleep(3600)
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            tok = jwt()
            m.complete(cookie(tok))
            src, calls = live_source(m, script)
            sink = FakeSink()
            asyncio.run(run_for(src, sink, lambda: bool(calls), 0.5))
            self.assertEqual((calls[0]["token"], calls[0]["mode"]), (tok, "AUTHENTICATED"))
            self.assertEqual(src._pick_transport(), "core")              # the legacy endpoint takes no token
            self.assertIn("AudioStreams", calls[0]["topics"])
            self.assertEqual(opened, [])
            self.assertFalse(any(tok in json.dumps(s) for s in sink.status))   # status never carries it

    def test_rejected_sign_in_falls_back_to_anonymous(self):
        async def script(src, n, sink):
            if src._conn_token:
                raise AuthRejected("negotiate rejected the F1 TV sign-in (HTTP 401)")
            await asyncio.sleep(3600)
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            m.complete(cookie(jwt()))
            src, calls = live_source(m, script)
            asyncio.run(run_for(src, FakeSink(), lambda: len(calls) >= 2))
            self.assertEqual([c["mode"] for c in calls[:2]], ["AUTHENTICATED", "ANONYMOUS"])
            self.assertEqual((m.state, m.store.load()), ("REJECTED", None))
            self.assertEqual(len(opened), 1)

    def test_first_start_opens_the_browser_and_reconnects_after_sign_in(self):
        tok = jwt()

        async def script(src, n, sink):
            if n == 0:
                await asyncio.sleep(0.05)
                src.auth.complete(cookie(tok))          # signed in while connected anonymously
            await asyncio.sleep(3600)
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            src, calls = live_source(m, script)
            asyncio.run(run_for(src, FakeSink(), lambda: len(calls) >= 2))
            self.assertEqual(len(opened), 1)
            self.assertEqual([(c["mode"], c["token"]) for c in calls[:2]], [("ANONYMOUS", None), ("AUTHENTICATED", tok)])

    def test_expired_sign_in_is_not_used(self):
        async def script(src, n, sink):
            await asyncio.sleep(3600)
        with tempfile.TemporaryDirectory() as d:
            m, opened = manager(d)
            m.store.save(jwt(exp_in_s=-100))
            m, opened = manager(d)
            src, calls = live_source(m, script)
            asyncio.run(run_for(src, FakeSink(), lambda: bool(calls), 0.5))
            self.assertEqual(calls[0]["mode"], "ANONYMOUS")
            self.assertEqual(len(opened), 1)

    def test_refused_subscription_retries_with_core_topics(self):
        async def script(src, n, sink):
            if n == 0:
                raise SubscribeFailed("Subscribe failed: unknown topic")
            await asyncio.sleep(3600)
        with tempfile.TemporaryDirectory() as d:
            m, _ = manager(d, subscription=False)
            src, calls = live_source(m, script)
            asyncio.run(run_for(src, FakeSink(), lambda: len(calls) >= 2))
            self.assertIn("AudioStreams", calls[0]["topics"])
            self.assertNotIn("AudioStreams", calls[1]["topics"])
            self.assertIn("Position.z", calls[1]["topics"])

    def test_negotiate_401_with_and_without_token(self):
        import httpx

        class Mock(httpx.AsyncClient):
            def __init__(self, *a, **k):
                k["transport"] = httpx.MockTransport(lambda req: httpx.Response(401))
                super().__init__(*a, **k)
        orig = f1_live.httpx.AsyncClient
        f1_live.httpx.AsyncClient = Mock
        try:
            with tempfile.TemporaryDirectory() as d:
                m, _ = manager(d)
                src = F1LiveSource({"topics": []}, None, auth=m)
                src._conn_token = jwt()
                with self.assertRaises(AuthRejected):
                    asyncio.run(src._run_core(FakeSink()))
                src._conn_token = None
                with self.assertRaises(FeedError) as cm:
                    asyncio.run(src._run_core(FakeSink()))
                self.assertNotIsInstance(cm.exception, AuthRejected)
        finally:
            f1_live.httpx.AsyncClient = orig


# ---------------------------------------------------------------------------
def live_engine(cfg_over=None):
    from test_live import LiveClock, make_engine
    src = LiveClock()
    src.t = T0
    eng = make_engine(src, **(cfg_over or {}))
    return src, eng


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def pos_msg(t, entries):
    return encode_z({"Position": [{"Timestamp": iso(t), "Entries": entries}]})


def car_msg(t, cars):
    return encode_z({"Entries": [{"Utc": iso(t), "Cars": {n: {"Channels": ch} for n, ch in cars.items()}}]})


class PositionAndSafetyCarTest(unittest.TestCase):
    def run_feed(self, sc_keys=None):
        async def go():
            src, eng = live_engine({"f1_tv__safety_car_position_keys": sc_keys} if sc_keys is not None else None)
            await eng.feed("DriverList", {"1": {"Tla": "VER"}, "44": {"Tla": "HAM"}}, T0)
            await eng.feed("Position.z", pos_msg(T0, {
                "1": {"Status": "OnTrack", "X": 100, "Y": 200, "Z": 30},
                "44": {"Status": "OnTrack", "X": -50, "Y": 70, "Z": 31},
                "241": {"Status": "OnTrack", "X": 7, "Y": 8, "Z": 9},          # not on the driver list
                "63": {"Status": "OnTrack", "X": 0, "Y": 0, "Z": 0}}), T0)     # no GPS fix
            eng.tick()
            out = {"t0": (dict(eng.positions.latest), eng.map_info(), eng.diagnostics())}
            src.t = T0 + timedelta(seconds=10)
            eng.tick()
            out["t10"] = (eng.map_info(), eng.diagnostics())
            return out
        return asyncio.run(go())

    def test_parsing_z_and_non_driver_objects(self):
        r = self.run_feed()
        latest, mi, diag = r["t0"]
        self.assertEqual(latest["1"][1:], (100, 200, "OnTrack", 30))
        self.assertNotIn("63", latest)                                  # (0,0,0) = no fix, not a position
        self.assertEqual(mi["non_driver_objects"], ["241"])
        self.assertFalse(mi["safety_car"]["available"])                 # never guessed
        self.assertIn("241", mi["safety_car"]["reason"])
        self.assertEqual((diag["positions"]["drivers"], diag["positions"]["fresh"]), (2, 2))
        self.assertEqual(r["t10"][1]["positions"]["fresh"], 0)          # 10 s old: stale

    def test_safety_car_only_when_its_key_is_configured_and_present(self):
        r = self.run_feed(["241"])
        sc = r["t0"][1]["safety_car"]
        self.assertEqual((sc["available"], sc["x"], sc["y"], sc["z"], sc["fresh"]), (True, 7, 8, 9, True))
        self.assertEqual(r["t0"][1]["non_driver_objects"], [])
        self.assertFalse(r["t10"][0]["safety_car"]["fresh"])
        r = self.run_feed(["999"])                                      # configured, but not in the feed
        self.assertFalse(r["t0"][1]["safety_car"]["available"])

    def test_no_position_data_reported(self):
        async def go():
            src, eng = live_engine()
            return eng.map_info()["safety_car"]
        self.assertEqual(asyncio.run(go())["reason"], "no Position.z data received")


class CarDataTest(unittest.TestCase):
    def test_all_channels_freshness_no_fake_values(self):
        async def go():
            src, eng = live_engine()
            await eng.feed("SessionInfo", {"Path": "2025/x/", "Name": "Race", "Type": "Race"}, T0)
            await eng.feed("CarData.z", car_msg(T0, {"1": {"0": 11200, "2": 312, "3": 8, "4": 104, "5": 0,
                                                           "45": 12, "7": 3}}), T0)
            eng.tick()
            out = []
            for dt in (0, 6, 31):
                src.t = T0 + timedelta(seconds=dt)
                eng.tick()
                out.append(eng.cardata.car_object("1", 2025, eng._now_pres_ms()))
            return out
        now, stale, gone = asyncio.run(go())
        self.assertEqual((now["speed"], now["rpm"], now["gear"], now["throttle"], now["brake"], now["drs"]),
                         (312, 11200, 8, 100, False, "OPEN"))
        self.assertEqual(now["channels"], {"7": 3})                     # unknown channel passed on, not interpreted
        self.assertIsNone(now["ers"])                                   # not in the feed: None, never 0
        self.assertEqual((now["fresh"], now["age_ms"]), (True, 0))
        self.assertEqual((stale["fresh"], stale["speed"]), (False, 312))  # flagged stale
        self.assertEqual((gone["fresh"], gone["speed"], gone["channels"]), (False, None, {}))  # not shown as current

    def test_2026_has_no_drs_meaning(self):
        from server.telemetry import CarDataStore
        c = CarDataStore()
        c.ingest({"Entries": [{"Utc": iso(T0), "Cars": {"1": {"Channels": {"45": 12}}}}]})
        o = c.car_object("1", 2026, T0.timestamp() * 1000)
        self.assertEqual((o["drs"], o["drs_raw"]), (None, 12))


class TrackStatusAndRaceControlTest(unittest.TestCase):
    def build(self, topics):
        from server.feedstate import FeedState
        from server.models import Availability
        from server.normalizer import Normalizer
        fs = FeedState()
        t = T0
        for topic, data in topics:
            t += timedelta(seconds=1)
            fs.apply(topic, data, False, t.timestamp() * 1000)
        return Normalizer().build(fs, t + timedelta(seconds=1), 1.0, Availability(), rc_until=t + timedelta(seconds=1))

    def rc(self, *msgs):
        return {"Messages": [dict(m, Utc=iso(T0 - timedelta(seconds=30 - i))) for i, m in enumerate(msgs)]}

    def test_track_status_states(self):
        for code, state in (("1", "GREEN"), ("2", "YELLOW"), ("4", "SAFETY_CAR"), ("5", "RED_FLAG"),
                            ("6", "VSC"), ("7", "VSC_ENDING"), ("9", "TRACK_STATUS_9")):
            st = self.build([("SessionStatus", {"Status": "Started"}), ("TrackStatus", {"Status": code, "Message": "x"})])
            self.assertEqual(st["track_status"]["state"], state, code)
            self.assertEqual(st["track_status"]["timestamp"], int((T0 + timedelta(seconds=2)).timestamp() * 1000), code)
        st = self.build([("SessionStatus", {"Status": "Started"}), ("TrackStatus", {"Status": "4"})])
        self.assertEqual(st["track_status"]["source"], "TrackStatus")

    def test_double_yellow_pit_lane_and_red_flag_restart(self):
        rc = self.rc({"Category": "Flag", "Flag": "DOUBLE YELLOW", "Scope": "Sector", "Sector": 4,
                      "Message": "DOUBLE YELLOW IN TRACK SECTOR 4"},
                     {"Category": "Other", "Message": "PIT EXIT CLOSED"},
                     {"Category": "Flag", "Flag": "RED", "Scope": "Track", "Message": "RED FLAG"},
                     {"Category": "Other", "Message": "PIT EXIT OPEN"},
                     {"Category": "Flag", "Flag": "YELLOW", "Scope": "Track", "Message": "YELLOW"})
        st = self.build([("SessionStatus", {"Status": "Started"}), ("RaceControlMessages", rc)])
        ts = st["track_status"]
        self.assertEqual((ts["pit_exit"], ts["red_flag_restart"], ts["source"]), ("OPEN", True, "RaceControl"))
        st = self.build([("SessionStatus", {"Status": "Started"}), ("TrackStatus", {"Status": "2"}),
                         ("RaceControlMessages", self.rc({"Category": "Flag", "Flag": "DOUBLE YELLOW", "Scope": "Sector",
                                                          "Sector": 4, "Message": "DOUBLE YELLOW IN TRACK SECTOR 4"}))])
        self.assertEqual(st["track_status"]["state"], "DOUBLE_YELLOW")

    def test_safety_car_state(self):
        rc = self.rc({"Category": "SafetyCar", "Mode": "SAFETY CAR", "Status": "DEPLOYED", "Message": "SAFETY CAR DEPLOYED"})
        st = self.build([("SessionStatus", {"Status": "Started"}), ("RaceControlMessages", rc)])
        self.assertEqual((st["track_status"]["state"], st["track_status"]["sc_phase"]), ("SAFETY_CAR", "SAFETY CAR DEPLOYED"))
        m = st["race_control"][0]
        self.assertEqual((m["mode"], m["status"], m["importance"], m["tags"]), ("SAFETY CAR", "DEPLOYED", "high", ["safety_car"]))

    def test_message_tags_only_from_the_text(self):
        rc = self.rc({"Category": "Other", "Message": "CAR 1 (VER) TIME 1:31.000 DELETED - TRACK LIMITS AT TURN 4 LAP 7"},
                     {"Category": "Other", "Message": "FIA STEWARDS: UNSAFE RELEASE OF CAR 4 (NOR) UNDER INVESTIGATION"},
                     {"Category": "Other", "Message": "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 4 (NOR) - UNSAFE RELEASE"},
                     {"Category": "Other", "Message": "FIA STEWARDS: TURN 1 INCIDENT NO FURTHER INVESTIGATION"},
                     {"Category": "Other", "Message": "RISK OF RAIN FOR F1 RACE IS 10%"})
        st = self.build([("RaceControlMessages", rc)])
        tags = {m["text"][:20]: m["tags"] for m in st["race_control"]}
        self.assertEqual(tags["CAR 1 (VER) TIME 1:3"], ["deleted_lap", "track_limits"])
        self.assertEqual(tags["FIA STEWARDS: UNSAFE"], ["investigation", "unsafe_release"])
        self.assertIn("penalty", tags["FIA STEWARDS: 5 SECO"])
        self.assertNotIn("penalty", tags["FIA STEWARDS: TURN 1"])          # no penalty invented
        self.assertEqual(tags["RISK OF RAIN FOR F1 "], [])
        nor = st["drivers"] if st["drivers"] else {}
        self.assertEqual(nor, {})                                          # no timing: no invented cars

    def test_tyre_stint_series(self):
        st = self.build([("DriverList", {"1": {"Tla": "VER"}}),
                         ("TimingData", {"Lines": {"1": {"Position": "1"}}}),
                         ("TyreStintSeries", {"Stints": {"1": [{"Compound": "SOFT", "New": "true", "TotalLaps": 12, "StartLaps": 0},
                                                               {"Compound": "HARD", "New": "false", "TotalLaps": 25, "StartLaps": 4}]}})])
        d = st["drivers"]["1"]
        self.assertEqual([(s["compound"], s["stint"], s["laps"], s["new"], s["source"]) for s in d["stints"]],
                         [("SOFT", 1, 12, True, "TyreStintSeries"), ("HARD", 2, 21, False, "TyreStintSeries")])
        self.assertEqual((d["tyre"]["compound"], d["tyre"]["tyre_age"]), ("HARD", 25))


# ---------------------------------------------------------------------------
class RecordingTest(unittest.TestCase):
    def test_recording_has_the_data_and_no_secrets_and_replays(self):
        from server.recorder import Recorder
        from server.sources.replay import load_file
        tok = jwt()
        with tempfile.TemporaryDirectory() as d:
            m, _ = manager(d)
            m.complete(cookie(tok))
            rec = Recorder(Path(d) / "rec")
            src = F1LiveSource({"topics": []}, rec, auth=m)
            src._conn_token = tok
            sink = FakeSink()

            async def go():
                await src._handle_snapshot(sink, {"SessionInfo": {"Path": "2026/test/", "Name": "Race"},
                                                  "Heartbeat": {"Utc": iso(T0)}, "TyreStintSeries": {"Stints": {}}})
                await src._handle_feed(sink, ["Position.z", pos_msg(T0, {"1": {"Status": "OnTrack", "X": 1, "Y": 2, "Z": 3}}), iso(T0)])
                await src._handle_feed(sink, ["CarData.z", car_msg(T0, {"1": {"2": 300}}), iso(T0)])
                await src._handle_feed(sink, ["TrackStatus", {"Status": "4", "Message": "SCDeployed"}, iso(T0)])
            asyncio.run(go())
            rec.set_session("2026/test/", "test")
            rec.close()
            files = list((Path(d) / "rec").glob("*.jsonl.gz"))
            text = gzip.open(files[0], "rt").read()
            self.assertNotIn(tok, text)
            self.assertNotIn(tok.split(".")[1], text)
            evs = load_file(files[0])
            self.assertEqual([e.topic for e in evs if not e.snap], ["Position.z", "CarData.z", "TrackStatus"])
            self.assertIn("TyreStintSeries", [e.topic for e in evs if e.snap])
            # replay reproduces the decoded data
            from server.telemetry import CarDataStore, PositionStore, decode_z
            ps, cs = PositionStore(), CarDataStore()
            for e in evs:
                if e.topic == "Position.z":
                    ps.ingest(decode_z(e.data), lambda dt: int(dt.timestamp() * 1000))
                if e.topic == "CarData.z":
                    cs.ingest(decode_z(e.data))
            self.assertEqual(ps.latest["1"][1:], (1, 2, "OnTrack", 3))
            self.assertEqual(cs.latest["1"]["2"], 300)


class DiagnosticsAndHttpTest(unittest.TestCase):
    def test_report_lists_received_and_missing_topics(self):
        from server.diagnostics import format_report

        async def go():
            src, eng = live_engine()
            await eng.feed("DriverList", {"1": {"Tla": "VER"}}, T0)
            await eng.feed("TimingData", {"Lines": {"1": {"Position": "1"}}}, T0)
            await eng.feed("CarData.z", car_msg(T0, {"1": {"2": 300}}), T0)
            eng.tick()
            eng.source.topics = ["TimingData", "Position.z", "CarData.z"]
            eng.source.connection_info = lambda: {"topics_subscribed": eng.source.topics,
                                                  "f1_tv": {"subscription": True, "state": "VALID", "reason": "x",
                                                            "product": "F1 TV Access"}}
            eng._status = {"state": "connected", "auth": "AUTHENTICATED", "transport": "core"}
            return eng.diagnostics()
        rep = asyncio.run(go())
        av = {t["topic"]: t["available"] for t in rep["topics"]}
        self.assertEqual((av["TimingData"], av["CarData.z"], av["Position.z"], av["DriverList"]), (True, True, False, True))
        self.assertEqual((rep["car_data"]["fresh"], rep["positions"]["with_data"]), (1, 0))
        text = format_report(rep)
        self.assertIn("✓ TimingData", text)
        self.assertIn("✗ Position.z", text)
        self.assertIn("Connection: AUTHENTICATED", text)
        self.assertIn("[not subscribed - sent anyway]", text)          # DriverList arrived although not asked for

    def test_sign_in_pages_and_no_secret_over_http(self):
        from starlette.testclient import TestClient
        from server.app import create_app
        tok = jwt()
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config("/nonexistent.toml")
            cfg["live"]["record"] = False
            cfg["f1_tv"]["auth_file"] = str(Path(d) / "auth" / "f1tv_auth.json")
            cfg["f1_tv"]["open_browser"] = False
            app = create_app(cfg)
            local = TestClient(app, client=("127.0.0.1", 50000))
            lan = TestClient(app, client=("192.168.1.20", 50000))
            self.assertEqual(lan.get("/f1tv/login").status_code, 403)
            page = local.get("/f1tv/login")
            self.assertEqual(page.status_code, 200)
            self.assertIn("account.formula1.com", page.text)
            key = AuthStore(Path(cfg["f1_tv"]["auth_file"])).key()
            self.assertEqual(local.post("/f1tv/callback", data={"key": "wrong", "session": cookie(tok)}).status_code, 403)
            self.assertEqual(lan.post("/f1tv/callback", data={"key": key, "session": cookie(tok)}).status_code, 403)
            r = local.post("/f1tv/callback", data={"key": key, "session": cookie(tok)})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(AuthStore(Path(cfg["f1_tv"]["auth_file"])).load(), tok)
            for path in ("/f1tv/status", "/api/diagnostics", "/api/diagnostics?format=text", "/api/state", "/api/health"):
                body = local.get(path).text
                self.assertNotIn(tok.split(".")[1], body, path)
            self.assertEqual(local.get("/f1tv/status").json()["state"], "VALID")
            eng = app.state.engine
            eng.set_status(state="connected", auth="AUTHENTICATED")
            self.assertNotIn(tok.split(".")[1], json.dumps(eng.hub.status))
            self.assertEqual(eng.hub.status["f1tv"]["product"], "F1 TV Access")


if __name__ == "__main__":
    unittest.main()


class HubRobustnessTest(unittest.TestCase):
    """A value JSON cannot hold must never refuse a dashboard connection or stop publishing."""

    def test_unsendable_values_do_not_break_dashboards(self):
        from server.hub import Hub

        class FakeWS:
            async def send_text(self, text):
                pass

        async def go():
            hub = Hub()
            hub.publish_state({"session": {"a": 1}, "order": [], "drivers": {"1": {"x": 1}}})
            circ = {}
            circ["self"] = circ
            # a set (not JSON) is sent as a list; a circular section keeps the previous one
            hub.publish_state({"session": {"a": 2, "odd": {3, 4}}, "track_status": circ, "order": [],
                               "drivers": {"1": {"x": 2}}})
            hub.status = {"type": "status", "when": datetime(2026, 1, 1)}     # not JSON: sent as text
            c = await hub.add(FakeWS(), "test")
            msgs = []
            while not c.queue.empty():
                msgs.append(json.loads(c.queue.get_nowait()))
            c.task.cancel()
            return hub, msgs
        hub, msgs = asyncio.run(go())
        types = [m.get("type") for m in msgs]
        state = next(m for m in msgs if m.get("type") == "state")
        self.assertEqual(sorted(state["session"]["odd"]), [3, 4])
        self.assertEqual(state["drivers"]["1"], {"x": 2})
        self.assertIn("status", types)
