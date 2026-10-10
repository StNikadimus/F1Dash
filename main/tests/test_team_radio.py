"""TEAM RADIO: parsing the TeamRadio topic / OpenF1 rows, the audio endpoint (fetched from the F1 archive
only for a clip of the session shown now), the optional AI transcripts and their machine authentication.

No network: the archive is an httpx.MockTransport / a patched fetch.

Run (from main/):  python -m unittest tests.test_team_radio
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import team_radio  # noqa: E402
from server.config import load_config  # noqa: E402
from server.feedstate import FeedState  # noqa: E402
from server.models import Availability  # noqa: E402
from server.normalizer import Normalizer  # noqa: E402

SP = "2026/2026-03-29_Japanese_Grand_Prix/2026-03-29_Race/"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64


def cap(n, utc, file=None, path=None):
    return {"Utc": utc, "RacingNumber": n, "Path": path if path is not None else f"TeamRadio/{file}.mp3"}


class ParseTest(unittest.TestCase):
    def setUp(self):
        self._t = team_radio.transcripts
        team_radio.transcripts = None

    def tearDown(self):
        team_radio.transcripts = self._t

    def test_list_and_keyed_dict_forms(self):
        caps = [cap("1", "2026-03-29T05:10:00.1234567Z", "LANNOR01_4_20260329_141000"),
                cap("16", "2026-03-29T05:05:00Z", "CHALEC16_16_20260329_140500")]
        a = team_radio.parse_captures({"Captures": caps}, SP)
        b = team_radio.parse_captures({"Captures": {"1": caps[1], "0": caps[0], "x": caps[0]}}, SP)
        self.assertEqual([c["driver"] for c in a], ["16", "1"])                 # oldest first
        self.assertEqual(a, b)
        c = a[1]
        self.assertEqual(c["utc"], "2026-03-29T05:10:00.123Z")                  # F1's 7-digit fraction
        self.assertEqual((c["file"], c["src"], c["playable"]), ("LANNOR01_4_20260329_141000.mp3", "feed", True))
        self.assertRegex(c["id"], r"^[0-9a-f]{16}$")
        self.assertEqual(c["id"], team_radio.clip_id(SP, "TeamRadio/LANNOR01_4_20260329_141000.mp3"))
        self.assertNotIn("transcript", c)

    def test_duplicates_once(self):
        x = cap("44", "2026-03-29T05:00:00Z", "LEWHAM01_44_20260329_140000")
        clips = team_radio.parse_captures({"Captures": [x, dict(x), dict(x, Utc="2026-03-29T05:00:01Z")]}, SP)
        self.assertEqual(len(clips), 1)

    def test_malformed_entries_are_skipped(self):
        bad = [None, "x", 5, [], {},
               cap("63", "2026-03-29T05:00:00Z", path="../../etc/passwd"),
               cap("63", "2026-03-29T05:00:00Z", path="https://evil.example/a.mp3"),
               cap("63", "2026-03-29T05:00:00Z", path="TeamRadio/a b.mp3"),
               cap("63", "2026-03-29T05:00:00Z", path="TeamRadio/x.wav"),
               cap("63", "2026-03-29T05:00:00Z", path="TeamRadio/../x.mp3"),
               {"Utc": "garbage", "RacingNumber": "x", "Path": None},
               {"RacingNumber": "4"},                                         # no path, no time: nothing to show
               cap("81", "2026-03-29T05:00:00Z", "OSCPIA01_81_20260329_140000")]
        clips = team_radio.parse_captures({"Captures": bad}, SP)
        self.assertEqual([c["driver"] for c in clips], ["81"])

    def test_untrusted_driver_and_time(self):
        clips = team_radio.parse_captures({"Captures": [
            cap("<img>", "2026-03-29T05:00:00Z", "A"), cap("1234", "2026-03-29T05:00:00Z", "B"),
            cap("1", "1900-01-01T00:00:00Z", "C"), cap(None, None, "D")]}, SP)
        self.assertEqual([c["driver"] for c in clips], [None, None, "1", None])   # kept (file valid), fields cleared
        self.assertEqual(sum(c["utc"] is None for c in clips), 2)

    def test_no_path_entry_kept_only_with_driver_and_time(self):
        clips = team_radio.parse_captures({"Captures": [{"Utc": "2026-03-29T05:00:00Z", "RacingNumber": "4"}]}, SP)
        self.assertEqual(len(clips), 1)
        self.assertFalse(clips[0]["playable"])
        self.assertIsNone(clips[0]["file"])

    def test_bad_session_path_never_playable(self):
        for sp in (None, "", "../../", "2026/a/../b/", "http://x/2026/a/b/", "2026/a/b"):
            clips = team_radio.parse_captures({"Captures": [cap("1", "2026-03-29T05:00:00Z", "A")]}, sp)
            self.assertEqual(len(clips), 1, sp)
            self.assertFalse(clips[0]["playable"], sp)

    def test_not_a_dict(self):
        for v in (None, [], "x", {"Captures": "x"}, {"Captures": None}):
            self.assertEqual(team_radio.parse_captures(v, SP), [])

    def test_bounded(self):
        caps = [cap("1", "2026-03-29T05:00:00Z", f"F{i}") for i in range(team_radio.MAX_CLIPS + 50)]
        self.assertEqual(len(team_radio.parse_captures({"Captures": caps}, SP)), team_radio.MAX_CLIPS)


class OpenF1Test(unittest.TestCase):
    BASE = "https://livetiming.formula1.com/static/"

    def row(self, url, n=1, date="2026-03-29T05:00:00.123000+00:00"):
        return {"date": date, "driver_number": n, "meeting_key": 1, "session_key": 2, "recording_url": url}

    def test_only_archive_recordings_of_this_session(self):
        good = self.BASE + SP + "TeamRadio/LANNOR01_4_20260329_140000.mp3"
        rows = [self.row(good, 4, "2026-03-29T05:01:00+00:00"),
                self.row(good.replace("https://", "http://")),
                self.row(good.replace("livetiming.formula1.com", "livetiming.formula1.com.evil.example")),
                self.row(good + "?x=1"), self.row(good + "#x"),
                self.row(self.BASE + "2025/other/session/TeamRadio/A.mp3"),
                self.row(self.BASE + SP + "TeamRadio/../../x.mp3"),
                self.row(self.BASE + SP + "Other/A.mp3"),
                self.row(good, "abc"), self.row(good, 4, "not a date"), self.row(None), "x", None,
                self.row(self.BASE + SP + "TeamRadio/CHALEC16_16_20260329_135900.mp3", 16, "2026-03-29T04:59:00+00:00")]
        caps = team_radio.openf1_captures(rows, SP, self.BASE)
        self.assertEqual([c["RacingNumber"] for _, c in caps], ["16", "4"])        # sorted by time
        utc, c = caps[1]
        self.assertEqual(c, {"Utc": "2026-03-29T05:01:00.000Z", "RacingNumber": "4",
                             "Path": "TeamRadio/LANNOR01_4_20260329_140000.mp3", "_src": "openf1"})
        clips = team_radio.parse_captures({"Captures": [x for _, x in caps]}, SP)
        self.assertEqual({x["src"] for x in clips}, {"openf1"})

    def test_bad_inputs(self):
        self.assertEqual(team_radio.openf1_captures(None, SP), [])
        self.assertEqual(team_radio.openf1_captures({"x": 1}, SP), [])
        self.assertEqual(team_radio.openf1_captures([self.row(self.BASE + SP + "TeamRadio/A.mp3")], "../x/"), [])


class VodFallbackTest(unittest.TestCase):
    """The VOD source adds OpenF1's clips only when the F1 archive has no TeamRadio stream."""

    def test_fallback_adds_timed_events(self):
        from server.sources.vod import VodSource
        from server.sources.replay import Event
        src = VodSource.__new__(VodSource)
        src.radio_wait_s = 3.0
        url = "https://livetiming.formula1.com/static/" + SP + "TeamRadio/LANNOR01_4_20260329_140000.mp3"

        class OF1:
            async def team_radio(self, session):
                return [{"date": "2026-03-29T05:00:10+00:00", "driver_number": 4, "recording_url": url},
                        {"date": "2026-03-29T05:00:20+00:00", "driver_number": 4,
                         "recording_url": "https://evil.example/x.mp3"}]
        src.openf1 = OF1()
        t0 = datetime(2026, 3, 29, 5, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 3, 29, 5, 0, 30, tzinfo=timezone.utc)
        events = [Event(t=t0, topic="SessionInfo", data={}), Event(t=t1, topic="TrackStatus", data={})]
        out = asyncio.run(src._openf1_radio(events, {}, SP))
        self.assertEqual([e.topic for e in out], ["SessionInfo", "TeamRadio", "TrackStatus"])
        cap0 = out[1].data["Captures"]["0"]
        self.assertEqual((cap0["RacingNumber"], cap0["_src"]), ("4", "openf1"))

    def test_slow_openf1_does_not_hold_the_load(self):
        from server.sources.vod import VodSource
        src = VodSource.__new__(VodSource)
        src.radio_wait_s = 0.2

        class OF1:
            async def team_radio(self, session):
                await asyncio.sleep(30)
        src.openf1 = OF1()

        async def run():
            t0 = asyncio.get_running_loop().time()
            out = await src._openf1_radio(["e"], {}, SP)
            took = asyncio.get_running_loop().time() - t0
            src._radio_bg.cancel()
            return out, took
        out, took = asyncio.run(run())
        self.assertEqual(out, ["e"])
        self.assertLess(took, 2)

    def test_fallback_failure_keeps_events(self):
        from server.openf1 import OpenF1Error
        from server.sources.vod import VodSource
        src = VodSource.__new__(VodSource)
        src.radio_wait_s = 3.0

        class OF1:
            async def team_radio(self, session):
                raise OpenF1Error("429")
        src.openf1 = OF1()
        self.assertEqual(asyncio.run(src._openf1_radio(["e"], {}, SP)), ["e"])


class NormalizerTest(unittest.TestCase):
    def test_state_radio_newest_first(self):
        fs = FeedState()
        fs.apply("SessionInfo", {"Type": "Race", "Name": "Race", "Path": SP})
        fs.apply("TeamRadio", {"Captures": [cap("1", "2026-03-29T05:00:00Z", "A")]})
        fs.apply("TeamRadio", {"Captures": {"1": cap("16", "2026-03-29T05:02:00Z", "B")}})   # live delta
        st = Normalizer().build(fs, datetime.now(timezone.utc), 1.0, Availability())
        self.assertEqual([c["driver"] for c in st["radio"]], ["16", "1"])
        self.assertTrue(all(c["playable"] for c in st["radio"]))
        self.assertNotIn("url", json.dumps(st["radio"]))                        # never an URL for the browser

    def test_no_radio(self):
        fs = FeedState()
        fs.apply("SessionInfo", {"Type": "Race", "Name": "Race", "Path": SP})
        st = Normalizer().build(fs, datetime.now(timezone.utc), 1.0, Availability())
        self.assertEqual(st["radio"], [])


def archive(handler):
    """A RadioAudio whose HTTP client talks to a MockTransport."""
    calls = []

    def h(request):
        calls.append(str(request.url))
        return handler(request)
    ra = team_radio.RadioAudio(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(h)))
    return ra, calls


class AudioFetchTest(unittest.TestCase):
    URL = team_radio.DEFAULT_ARCHIVE + SP + "TeamRadio/A.mp3"

    def get(self, ra, url=None):
        return asyncio.run(ra.get(url or self.URL))

    def err(self, ra, url=None):
        with self.assertRaises(team_radio.RadioAudioError) as cm:
            self.get(ra, url)
        return cm.exception.status, cm.exception.message

    def test_ok_and_cached(self):
        ra, calls = archive(lambda r: httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"}))
        self.assertEqual(self.get(ra), MP3)
        self.assertEqual(self.get(ra), MP3)
        self.assertEqual(len(calls), 1)

    def test_errors(self):
        cases = [(httpx.Response(404), 404), (httpx.Response(410), 404), (httpx.Response(403), 403),
                 (httpx.Response(429), 503), (httpx.Response(500), 502),
                 (httpx.Response(301, headers={"location": "https://evil.example/"}), 502),     # never followed
                 (httpx.Response(200, content=b"<html>nope</html>", headers={"content-type": "text/html"}), 502),
                 (httpx.Response(200, content=b"not an mp3 at all", headers={"content-type": "audio/mpeg"}), 502),
                 (httpx.Response(200, content=b"", headers={"content-type": "audio/mpeg"}), 502)]
        for resp, status in cases:
            ra, _ = archive(lambda r, resp=resp: resp)
            self.assertEqual(self.err(ra)[0], status, resp)

    def test_too_large(self):
        ra, _ = archive(lambda r: httpx.Response(200, content=MP3 * 4000, headers={"content-type": "audio/mpeg"}))
        ra.max_clip = 4096
        self.assertEqual(self.err(ra), (502, "recording too large"))

    def test_timeout_and_unreachable(self):
        def boom(exc):
            def h(r):
                raise exc
            return h
        ra, _ = archive(boom(httpx.ReadTimeout("slow")))
        self.assertEqual(self.err(ra)[0], 504)
        ra, _ = archive(boom(httpx.ConnectError("down")))
        self.assertEqual(self.err(ra)[0], 502)

    def test_failures_are_not_hammered(self):
        ra, calls = archive(lambda r: httpx.Response(404))
        self.err(ra)
        self.err(ra)
        self.assertEqual(len(calls), 1)                                         # negative cache
        ra, calls = archive(lambda r: httpx.Response(429))
        self.err(ra)
        self.err(ra)
        self.assertEqual(len(calls), 2)                                         # rate limit: retried later

    def test_only_the_archive(self):
        ra, calls = archive(lambda r: httpx.Response(200, content=MP3))
        for u in ("https://evil.example/static/" + SP + "TeamRadio/A.mp3",
                  "http://livetiming.formula1.com/static/" + SP + "TeamRadio/A.mp3",
                  team_radio.DEFAULT_ARCHIVE + SP + "TeamRadio/A.txt"):
            self.assertEqual(self.err(ra, u)[0], 400, u)
        self.assertEqual(calls, [])

    def test_cache_bounded(self):
        ra, _ = archive(lambda r: httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"}))
        ra.cache_bytes = len(MP3) * 2
        for i in range(5):
            self.get(ra, team_radio.DEFAULT_ARCHIVE + SP + f"TeamRadio/F{i}.mp3")
        self.assertEqual(len(ra.cache), 2)


class RangeTest(unittest.TestCase):
    def test_ranges(self):
        br = team_radio.byte_range
        self.assertIsNone(br(None, 100))
        self.assertIsNone(br("bytes=0-1,5-6", 100))
        self.assertEqual(br("bytes=0-0", 100), (0, 0))
        self.assertEqual(br("bytes=10-", 100), (10, 99))
        self.assertEqual(br("bytes=90-500", 100), (90, 99))
        self.assertEqual(br("bytes=-10", 100), (90, 99))
        for bad in ("bytes=100-", "bytes=50-10", "bytes=x-1", "bytes=-0"):
            with self.assertRaises(ValueError, msg=bad):
                br(bad, 100)


class TranscriptStoreTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._t = team_radio.transcripts

    def tearDown(self):
        team_radio.transcripts = self._t
        shutil.rmtree(self.d, ignore_errors=True)

    def test_put_validates_and_marks_ai(self):
        st = team_radio.TranscriptStore(self.d / "t.json")
        cid = "0123456789abcdef"
        rec = st.put(cid, " Box\x00 box\x1b, \nfor softs ", "faster-whisper small.en<script>", "en", 1.7)
        self.assertEqual((rec["text"], rec["kind"], rec["model"], rec["lang"], rec["confidence"]),
                         ("Box box, \nfor softs", "ai", "faster-whisper small.enscript", "en", 1.0))
        for bad in (("x", "t"), (cid.upper(), "t"), (cid, ""), (cid, "x" * 3000)):
            with self.assertRaises(ValueError):
                st.put(bad[0], bad[1], "m", None, None)
        with self.assertRaises((ValueError, TypeError)):
            st.put(cid, "t", "m", None, "high")
        again = team_radio.TranscriptStore(self.d / "t.json")                   # persisted
        self.assertEqual(again.get(cid)["text"], "Box box, \nfor softs")

    def test_shown_with_the_clip(self):
        team_radio.transcripts = team_radio.TranscriptStore(self.d / "t.json")
        c = cap("1", "2026-03-29T05:00:00Z", "A")
        cid = team_radio.clip_id(SP, c["Path"])
        team_radio.transcripts.put(cid, "Copy", "m", "en", 0.5)
        clip = team_radio.parse_captures({"Captures": [c]}, SP)[0]
        self.assertEqual(clip["transcript"]["kind"], "ai")
        self.assertEqual(clip["transcript"]["text"], "Copy")

    def test_corrupt_file(self):
        (self.d / "t.json").write_text("{nope")
        self.assertIsNone(team_radio.TranscriptStore(self.d / "t.json").get("0123456789abcdef"))


class EndpointTest(unittest.TestCase):
    """/api/radio/* on a real app (create_app); the hub state is set directly (no engine running)."""

    def make(self, token="", protect=False, **radio):
        import server.app as appmod
        from starlette.testclient import TestClient
        self.d = Path(tempfile.mkdtemp())
        self._patch = mock.patch.object(appmod, "DATA_DIR", self.d)
        self._patch.start()
        cfg = load_config(self.d / "none.toml")
        cfg["source"]["mode"] = "test"
        cfg["voyo"]["check_reachability"] = False
        cfg["f1_tv"]["auth_file"] = str(self.d / "auth.json")
        cfg["f1_tv"]["open_browser"] = False
        cfg["remote"]["token"] = token
        cfg["security"]["protect_dashboard"] = protect
        cfg["team_radio"].update(radio)
        self.app = appmod.create_app(cfg)
        self.hub = self.app.state.runtime.hub
        caps = [cap("1", "2026-03-29T05:00:00Z", "LANNOR01_4_20260329_140000"),
                {"Utc": "2026-03-29T05:01:00Z", "RacingNumber": "16"}]
        self.clips = list(reversed(team_radio.parse_captures({"Captures": caps}, SP)))
        self.hub.state = {"session": {"path": SP}, "radio": self.clips}
        self.cid = next(c["id"] for c in self.clips if c["playable"])
        self.dead = next(c["id"] for c in self.clips if not c["playable"])
        self.TC = TestClient
        return TestClient(self.app, client=("127.0.0.1", 40000))

    def tearDown(self):
        team_radio.transcripts = None
        if hasattr(self, "_patch"):
            self._patch.stop()
            shutil.rmtree(self.d, ignore_errors=True)

    def fetch(self, data=MP3, exc=None):
        async def f(_self, url):
            self.fetched.append(url)
            if exc:
                raise exc
            return data
        self.fetched = []
        return mock.patch.object(team_radio.RadioAudio, "_fetch", f)

    def test_audio(self):
        c = self.make()
        with self.fetch():
            r = c.get(f"/api/radio/audio/{self.cid}")
            self.assertEqual((r.status_code, r.headers["content-type"], r.content), (200, "audio/mpeg", MP3))
            self.assertEqual(r.headers["accept-ranges"], "bytes")
            self.assertIn("private", r.headers["cache-control"])
            self.assertEqual(self.fetched, [team_radio.DEFAULT_ARCHIVE + SP + "TeamRadio/LANNOR01_4_20260329_140000.mp3"])
            r = c.get(f"/api/radio/audio/{self.cid}", headers={"Range": "bytes=0-2"})
            self.assertEqual((r.status_code, r.content, r.headers["content-range"]), (206, b"ID3", f"bytes 0-2/{len(MP3)}"))
            self.assertEqual(c.get(f"/api/radio/audio/{self.cid}", headers={"Range": "bytes=99999-"}).status_code, 416)
            for cid in (self.dead, "f" * 16, "XYZ", "0123456789ABCDEF"):
                r = c.get(f"/api/radio/audio/{cid}")
                self.assertEqual(r.status_code, 404, cid)
            self.assertEqual(len(self.fetched), 1)                               # cached; nothing else fetched

    def test_audio_follows_the_session_shown_now(self):
        c = self.make()
        with self.fetch():
            self.hub.state = {"session": {"path": "2026/other/session/"}, "radio": []}
            self.assertEqual(c.get(f"/api/radio/audio/{self.cid}").status_code, 404)
            # a forged state entry with a path outside the archive layout is still refused
            self.hub.state = {"session": {"path": SP}, "radio": [dict(self.clips[0], file="../../x.mp3")]}
            self.assertEqual(c.get(f"/api/radio/audio/{self.clips[0]['id']}").status_code, 404)
            self.hub.state = {"session": {"path": "../"}, "radio": self.clips}
            self.assertEqual(c.get(f"/api/radio/audio/{self.cid}").status_code, 404)
        self.assertEqual(self.fetched, [])

    def test_audio_archive_failure_is_explained(self):
        c = self.make()
        with self.fetch(exc=team_radio.RadioAudioError(404, "recording not available (F1 archive answered 404)")):
            r = c.get(f"/api/radio/audio/{self.cid}")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"ok": False, "error": "recording not available (F1 archive answered 404)"})
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_disabled(self):
        c = self.make(enabled=False)
        with self.fetch():
            self.assertEqual(c.get(f"/api/radio/audio/{self.cid}").status_code, 404)
        self.assertEqual(c.get("/api/radio/clips").status_code, 404)
        self.assertEqual(c.post("/api/radio/transcript", json={}).status_code, 404)

    def test_transcripts_loopback_without_token(self):
        c = self.make()
        r = c.get("/api/radio/clips")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["id"] for x in r.json()["clips"]], [self.cid])          # only playable clips
        lan = self.TC(self.app, client=("192.168.1.50", 40000))
        self.assertEqual(lan.get("/api/radio/clips").status_code, 401)
        self.assertEqual(lan.post("/api/radio/transcript", json={"id": self.cid, "text": "x"}).status_code, 401)
        r = c.post("/api/radio/transcript", json={"id": self.cid, "text": "Box this lap", "model": "small.en",
                                                  "lang": "en", "confidence": 0.8})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["transcript"]["kind"], "ai")
        self.assertEqual(c.post("/api/radio/transcript", json={"id": "nope", "text": "x"}).status_code, 400)
        self.assertEqual(c.post("/api/radio/transcript", json={"id": self.cid, "text": "x" * 6000}).status_code, 400)
        self.assertNotEqual(c.post("/api/radio/transcript", content=b"{" + b"x" * 20000).status_code, 200)
        clip = team_radio.parse_captures({"Captures": [cap("1", "2026-03-29T05:00:00Z", "LANNOR01_4_20260329_140000")]}, SP)[0]
        self.assertEqual(clip["transcript"]["text"], "Box this lap")
        self.assertTrue((self.d / "radio_transcripts.json").exists())

    def test_transcripts_need_the_remote_token(self):
        c = self.make(token="s3cret-token")
        self.assertEqual(c.get("/api/radio/clips").status_code, 401)               # even this machine
        lan = self.TC(self.app, client=("192.168.1.50", 40000))
        self.assertEqual(lan.get("/api/radio/clips", headers={"X-Remote-Token": "wrong"}).status_code, 401)
        r = lan.get("/api/radio/clips", headers={"X-Remote-Token": "s3cret-token"})
        self.assertEqual(r.status_code, 200, r.text)
        r = lan.post("/api/radio/transcript", headers={"X-Remote-Token": "s3cret-token"},
                     json={"id": self.cid, "text": "Copy"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_transcripts_off(self):
        c = self.make(transcripts=False)
        self.assertEqual(c.get("/api/radio/clips").status_code, 404)
        self.assertIsNone(team_radio.transcripts)

    def test_transcribe_tool_with_a_fake_model(self):
        """tools/transcribe_radio.py against the real endpoints (faster-whisper replaced by a stub)."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import transcribe_radio
        c = self.make(token="tok")
        c.headers["X-Remote-Token"] = "tok"
        heard = []

        def fake(audio):
            heard.append(audio)
            return "Box box", "en", 0.9
        with self.fetch():
            self.assertEqual(transcribe_radio.transcribe_pending(c, fake, "stub", log=lambda m: None), 1)
            self.assertEqual(heard, [MP3])
            self.hub.state["radio"] = team_radio.parse_captures(
                {"Captures": [cap("1", "2026-03-29T05:00:00Z", "LANNOR01_4_20260329_140000")]}, SP)
            self.assertEqual(transcribe_radio.transcribe_pending(c, fake, "stub", log=lambda m: None), 0)  # done
        self.assertEqual(self.hub.state["radio"][0]["transcript"]["model"], "stub")
        c.headers["X-Remote-Token"] = "wrong"
        with self.assertRaises(SystemExit):
            transcribe_radio.transcribe_pending(c, fake, "stub", log=lambda m: None)

    def test_protected_dashboard_gates_audio(self):
        self.make(protect=True)
        lan = self.TC(self.app, client=("192.168.1.50", 40000))
        with self.fetch():
            r = lan.get(f"/api/radio/audio/{self.cid}", follow_redirects=False)
        self.assertIn(r.status_code, (401, 403, 303))
        self.assertEqual(self.fetched, [])

    def test_hello_and_no_credentials_in_state(self):
        c = self.make()
        with c.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
        self.assertEqual(hello["type"], "hello")
        self.assertIs(hello["config"]["team_radio"], True)
        blob = json.dumps(self.hub.state).lower()
        for s in ("token", "cookie", "authorization", "http"):
            self.assertNotIn(s, blob)


if __name__ == "__main__":
    unittest.main()
