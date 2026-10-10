"""The server VOYO player's session recorder (tools/voyo_session.py) - every decision without a browser:
selection from the event page, verification before recording, stalls, sign-outs, Chrome / recorder
failures, missing sound, the end of a recording (VOD and live), the session changing under it, never a
second recording, and the report the package is closed with.

Run (from main/):  python -m unittest tests.test_voyo_session
"""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import voyo_episodes as ve  # noqa: E402
from tools.voyo_session import EPISODES_JS, KEY_RE, Limits, Obs, SessionRecorder, Target  # noqa: E402

FP1, SQ, SPRINT = "63660752", "63660945", "63661233"
ORIGIN = "https://voyo.si"
EP = "/play/category/2102/episodes/"
T0 = 1_791_634_000.0


def event_page(*cards):
    return {"origin": ORIGIN, "url_path": "/f1/vn-kitajske",
            "items": [{"href": ORIGIN + EP + i, "text": t, "card": t} for i, t in cards]}


WEEKEND = event_page((FP1, "Prvi prosti trening"), (SQ, "Sprint kvalifikacije"), (SPRINT, "Sprint"),
                     ("63661300", "Povzetek sprinta"))


def player(t, media_id=SPRINT, paused=False, duration=None, live=None, **kw):
    p = {"video": True, "t": t, "paused": paused, "ended": False, "ready": 4, "duration": duration,
         "live": duration is None if live is None else live, "muted": False, "media_id": media_id,
         "url_path": EP + str(media_id), "manifests": [], "login_form": False}
    p.update(kw)
    return p


class Driver:
    """Runs a SessionRecorder like the player does: discover when asked, then one step per tick."""

    def __init__(self, kind="sprint", until=None, limits=None, page=WEEKEND, start=T0):
        self.s = SessionRecorder(Target(kind, "Chinese Grand Prix", ve_label(kind), start, until), ORIGIN + "/f1/vn-kitajske",
                                 limits or Limits())
        self.page = page
        self.now = T0
        self.log = []

    def tick(self, dt=5.0, page=None, capture=None, audio=None, chrome=True):
        self.now += dt
        o = Obs(now=self.now, page=page, capture=capture, audio_streams=audio, chrome_alive=chrome)
        acts = self.s.step(o)
        if ("discover",) in acts:
            o.episodes = self.page
            acts = self.s.step(o)
        self.log.append(acts)
        return acts

    def to_recording(self, media_id=SPRINT, duration=None, start_t=0.0):
        self.tick()                                             # discover + select
        assert self.s.state == "OPENING", self.s.state
        acts = self.tick()                                      # open
        assert ("navigate", ORIGIN + EP + self.s.episode["id"]) in acts, acts
        t = start_t
        for _ in range(4):
            t += 5
            self.tick(page=player(t, media_id, duration=duration))
        assert self.s.state == "RECORDING", (self.s.state, self.s.problem)
        return t


def ve_label(kind):
    from server.voyo_episodes import KIND_LABELS
    return KIND_LABELS[kind]


GOOD_CAP = {"alive": True, "grew_age_s": 1.0}


class SelectionTest(unittest.TestCase):
    def test_selected_verified_then_recorded(self):
        d = Driver("sprint")
        d.to_recording()
        s = d.s
        self.assertEqual(s.episode["id"], SPRINT)
        self.assertEqual(s.key, f"sprint-{SPRINT}-20261010")
        self.assertRegex(s.key, KEY_RE)
        self.assertIn("mediaId", s.verification["reason"])
        self.assertTrue(s.recording_wanted())
        st = s.status()
        self.assertEqual((st["state"], st["episode_id"], st["target"]["kind"]), ("RECORDING", SPRINT, "sprint"))

    def test_each_session_of_the_weekend_finds_its_own_recording(self):
        for kind, want in (("practice1", FP1), ("sprint_qualifying", SQ), ("sprint", SPRINT)):
            d = Driver(kind)
            d.tick()
            self.assertEqual((d.s.state, d.s.episode["id"]), ("OPENING", want), kind)

    def test_not_found_is_retried_and_never_replaced_by_another_session(self):
        d = Driver("race", limits=Limits(discover_retry_s=60))
        d.tick()
        self.assertEqual(d.s.state, "NOT_FOUND")
        self.assertFalse(d.s.recording_wanted())
        self.assertIn("no Race recording", d.s.problem)
        self.assertEqual(d.tick(dt=10), [])                     # waits ...
        d.page = event_page((FP1, "Prvi prosti trening"), ("63669999", "Dirka"))      # ... the race appears
        d.tick(dt=60)
        self.assertEqual((d.s.state, d.s.episode["id"]), ("OPENING", "63669999"))

    def test_ambiguous_is_shown_with_its_candidates(self):
        d = Driver("race", page=event_page(("11111111", "Dirka"), ("22222222", "Dirka")))
        d.tick()
        self.assertEqual(d.s.state, "AMBIGUOUS")
        self.assertEqual({c["id"] for c in d.s.status()["candidates"]}, {"11111111", "22222222"})
        self.assertIsNone(d.s.key)

    def test_wrong_video_is_not_recorded(self):
        d = Driver("sprint", limits=Limits(retry_after_fail_s=120))
        d.tick()
        d.tick()
        d.tick(page=player(5, media_id=SQ))                     # the page plays the sprint qualifying
        self.assertEqual(d.s.state, "FAILED")
        self.assertFalse(d.s.recording_wanted())
        self.assertIn("wrong recording", d.s.problem)
        d.tick(dt=130)                                          # tried again after the pause
        self.assertEqual(d.s.state, "OPENING")

    def test_unverifiable_video_times_out(self):
        d = Driver("sprint", limits=Limits(verify_timeout_s=60))
        d.tick()
        d.tick()
        t = 0
        for _ in range(14):
            t += 5
            d.tick(page=player(t, media_id=None))                # plays, but nothing says which recording
        self.assertEqual(d.s.state, "FAILED")
        self.assertIn("could not confirm", d.s.problem)

    def test_verified_by_the_manifest(self):
        d = Driver("sprint_qualifying")
        d.tick()
        d.tick()
        t = 0
        for _ in range(4):
            t += 5
            d.tick(page=player(t, media_id=None, manifests=[f"https://cdn.example.net/{SQ}/manifest.mpd"]))
        self.assertEqual(d.s.state, "RECORDING")
        self.assertEqual(d.s.verification["format"], "dash")

    def test_url_verification_when_configured(self):
        d = Driver("sprint", limits=Limits(verification="url"))
        d.tick()
        d.tick()
        t = 0
        for _ in range(4):
            t += 5
            d.tick(page=player(t, media_id=None, url_path=EP + SPRINT))
        self.assertEqual(d.s.state, "RECORDING")

    def test_finished_recording_from_the_start(self):
        d = Driver("practice1")
        d.tick()
        d.tick()
        t = 1800                                               # VOYO resumes where it was left
        for _ in range(4):
            t += 5
            d.tick(page=player(t, FP1, duration=3700), capture={"alive": False})
        self.assertEqual((d.s.state, d.s.live, d.s.duration), ("RECORDING", False, 3700))
        acts = d.tick(page=player(t + 5, FP1, duration=3700), capture={"alive": False})
        self.assertNotIn(("seek0",), acts)                      # not before the recorder runs ...
        acts = d.tick(page=player(t + 10, FP1, duration=3700), capture=GOOD_CAP, audio=1)
        self.assertEqual(acts, [("seek0",)])                    # ... then back to 0:00, once
        self.assertEqual(d.s.report()["played_from"], 0.0)
        for k in range(1, 4):
            self.assertNotIn(("seek0",), d.tick(page=player(5 * k, FP1, duration=3700), capture=GOOD_CAP, audio=1))
        self.assertEqual(d.s.state, "RECORDING")

    def test_paused_and_muted_player_is_started(self):
        d = Driver("sprint")
        d.tick()
        d.tick()
        acts = d.tick(page=player(0, paused=True, muted=True))
        self.assertIn(("play",), acts)
        self.assertIn(("unmute",), acts)

    def test_a_link_to_another_site_is_opened_on_voyo_only(self):
        page = {"origin": ORIGIN, "items": [{"href": "https://evil.example" + EP + SPRINT, "text": "Sprint"}]}
        d = Driver("sprint", page=page)
        d.tick()
        self.assertEqual(d.s.episode_url, ORIGIN + EP + SPRINT)          # only its path is used, on the VOYO page's site


QUALI = "63661274"
SINGAPORE = ((FP1, "F1 prosti trening", "9. 10. 2026"), (SQ, "F1 sprint kvalifikacije", "9. 10. 2026"),
             (SPRINT, "F1 sprint dirka", "10. 10. 2026"), (QUALI, "F1 kvalifikacije", "10. 10. 2026"))


def singapore_page(twins=True):
    """VOYO's real event page (as EPISODES_JS reads it): <a class="episode"> cards whose href is the event page,
    the id only in data-uniq / onclick; the playEpisode twin carries the address that opens the recording."""
    ev = ORIGIN + "/vsebina/vn-singapurja"
    items = [{"href": ev, "data_uniq": i, "onclick": f'return onPlayClick("{i}"),!1', "text": f"{t} {d}", "card": f"{t} {d}"}
             for i, t, d in SINGAPORE]
    if twins:
        items += [{"href": ev, "data_uniq": f"media/{i}", "onclick": f'return playEpisode("{i}","{EP}{i}"),!1',
                   "text": t, "card": f"{t} {d}"} for i, t, d in SINGAPORE]
    return {"origin": ORIGIN, "url_path": "/vsebina/vn-singapurja", "items": items}


class ProductionEventPageTest(unittest.TestCase):
    def test_every_session_of_the_singapore_page_is_recorded(self):
        for kind, want in (("practice1", FP1), ("sprint_qualifying", SQ), ("sprint", SPRINT), ("qualifying", QUALI)):
            d = Driver(kind, page=singapore_page())
            d.to_recording(media_id=want)                          # opens EP + id, verified by mediaId, records
            self.assertEqual((d.s.episode["id"], d.s.episode_url), (want, ORIGIN + EP + want), kind)
            self.assertEqual(d.s.key, f"{kind}-{want}-20261010")

    def test_a_card_without_an_address_is_not_opened(self):
        d = Driver("qualifying", page=singapore_page(twins=False), limits=Limits(retry_after_fail_s=120))
        acts = d.tick()
        self.assertEqual(d.s.state, "FAILED")
        self.assertIn(f"recording {QUALI} but not the address", d.s.problem)
        self.assertFalse(any(a[0] == "navigate" for a in acts))
        self.assertIsNone(d.s.key)
        d.page = singapore_page()                                   # the page shows the address later: retried
        d.tick(dt=121)
        self.assertEqual((d.s.state, d.s.episode_url), ("OPENING", ORIGIN + EP + QUALI))


    def test_last_weekends_page_is_not_recorded_for_the_next_weekend(self):
        nxt = T0 + 13 * 86400                                        # the next weekend's qualifying, page not changed
        d = Driver("qualifying", page=singapore_page(), start=nxt, limits=Limits(discover_retry_s=60))
        d.now = nxt - 900                                            # the window opens before the session
        acts = d.tick()
        self.assertEqual(d.s.state, "NOT_FOUND")
        self.assertIn("probably stale", d.s.problem)
        self.assertFalse(any(a[0] == "navigate" for a in acts))
        self.assertIsNone(d.s.key)
        self.assertEqual(d.tick(dt=30), [])                          # looked at again after discover_retry_s
        self.assertEqual(d.tick(dt=31), [])
        self.assertEqual(d.s.state, "NOT_FOUND")


CHROME = next((c for c in (os.environ.get("CHROME_BIN"), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
               if c and os.access(c, os.X_OK)), None)

# a local copy of the event page's shape (no network: <base> only resolves hrefs); the handlers record any call
FIXTURE_HTML = """<!doctype html><html><head><meta charset="utf-8"><base href="https://voyo.si/vsebina/vn-singapurja">
<script>window.__calls = []; function onPlayClick() { __calls.push('onPlayClick'); }
function playEpisode() { __calls.push('playEpisode'); }</script></head><body>
<nav><a href="/vsebina/f1">Formula 1</a> <a href="/vsebina/vn-singapurja">VN Singapurja</a></nav>
<div class="slider">%(slider)s</div><div class="list">%(list)s</div>
<div class="row"><a class="episode" href="/vsebina/vn-singapurja" data-uniq="media/abc">F1 napovednik</a></div>
<pre id="out"></pre>
<script>(%(js)s).then((r) => { document.getElementById('out').textContent = JSON.stringify({r: r, calls: __calls}); });
</script></body></html>"""


def fixture_html():
    one = ('<div class="item"><a class="episode" href="/vsebina/vn-singapurja" data-uniq="%s" onclick="%s">'
           '<img alt="%s"><h3>%s</h3><p>%s</p></a></div>')
    q = lambda x: html.escape(x, quote=True)  # noqa: E731
    slider = "".join(one % (i, q(f'return onPlayClick("{i}"),!1'), t, t, d) for i, t, d in SINGAPORE)
    lst = "".join(one % ("media/" + i, q(f'return playEpisode("{i}","{EP}{i}"),!1'), t, t, d) for i, t, d in SINGAPORE)
    return FIXTURE_HTML % {"slider": slider, "list": lst, "js": EPISODES_JS}


@unittest.skipUnless(CHROME, "headless Chromium not installed")
class EpisodesJsInChromiumTest(unittest.TestCase):
    """EPISODES_JS itself, in a real (headless) Chromium, on a local fixture of the production DOM."""

    def test_reads_the_real_cards_without_clicking(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "event.html"
            f.write_text(fixture_html())
            dom = subprocess.run([CHROME, "--headless", "--no-sandbox", "--disable-gpu", f"--user-data-dir={d}/profile",
                                  "--virtual-time-budget=10000", "--dump-dom", f.as_uri()],
                                 capture_output=True, text=True, timeout=60).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
        self.assertTrue(m and m.group(1), dom[-500:])
        got = json.loads(html.unescape(m.group(1)))
        self.assertEqual(got["calls"], [])                          # nothing clicked, no handler called
        items = got["r"]["items"]
        self.assertEqual(len(items), 9)                              # 4 cards twice + the malformed one; no nav links
        for it in items:                                             # each card's own text, never its neighbours'
            self.assertLessEqual(sum(t in it["card"] for _, t, _ in SINGAPORE), 1, it)
        eps = ve.episodes_from_page(items)
        self.assertEqual({e.id: (e.kind, e.path, e.date) for e in eps},
                         {FP1: ("practice1", EP + FP1, "2026-10-09"), SQ: ("sprint_qualifying", EP + SQ, "2026-10-09"),
                          SPRINT: ("sprint", EP + SPRINT, "2026-10-10"), QUALI: ("qualifying", EP + QUALI, "2026-10-10")})


class HealthTest(unittest.TestCase):
    def rec(self, **kw):
        d = Driver("sprint", **kw)
        self.t = d.to_recording()
        return d

    def test_stall_play_then_reload_without_a_new_recording(self):
        d = self.rec(limits=Limits(stall_play_s=20, stall_reload_s=60))
        key = d.s.key
        acts = []
        for _ in range(5):
            acts += d.tick(page=player(self.t), capture=GOOD_CAP, audio=1)
        self.assertIn(("play",), acts)
        for _ in range(8):
            acts += d.tick(page=player(self.t), capture=GOOD_CAP, audio=1)
        self.assertIn(("navigate", ORIGIN + EP + SPRINT), acts)
        self.assertEqual((d.s.state, d.s.key), ("RECORDING", key))     # the same recording goes on
        self.assertGreaterEqual(d.s.recoveries, 1)
        d.tick(page=player(self.t + 5), capture=GOOD_CAP, audio=1)
        d.tick(page=player(self.t + 10), capture=GOOD_CAP, audio=1)
        self.assertFalse(d.s.problem)                                   # moving again: the problem is gone

    def test_sign_out_signs_in_again(self):
        d = self.rec()
        acts = d.tick(page={"video": False, "login_form": True})
        self.assertEqual(acts, [("login",), ("navigate", ORIGIN + EP + SPRINT)])
        self.assertEqual(d.s.state, "RECORDING")
        self.assertEqual(d.tick(page={"video": False, "login_form": True}), [])     # not again at once
        self.assertIn("signed the player out", d.s.issues[-1][1])

    def test_chrome_crash_is_counted(self):
        d = self.rec()
        d.tick(chrome=False)
        self.assertEqual(d.s.recoveries, 1)
        self.assertEqual(d.s.state, "RECORDING")

    def test_recorder_that_stops_growing_is_restarted_once(self):
        d = self.rec(limits=Limits(grow_restart_s=45))
        t = self.t
        acts = []
        for _ in range(3):
            t += 5
            acts += d.tick(page=player(t), capture={"alive": True, "grew_age_s": 60}, audio=1)
        self.assertEqual(acts.count(("restart_capture",)), 1)
        self.assertIn("has not grown", d.s.problem)

    def test_recorder_not_running_is_shown(self):
        d = self.rec()
        t = self.t
        for _ in range(8):
            t += 5
            d.tick(page=player(t), capture={"alive": False, "last_error": "ffmpeg stopped (1)"}, audio=1)
        self.assertIn("not running", d.s.problem)

    def test_recorder_restart_is_in_the_report(self):
        d = self.rec()
        t = self.t + 5
        cap = {"alive": True, "grew_age_s": 1, "last_error": None, "failures": 0}
        d.tick(page=player(t), capture=cap, audio=1)
        cap = {"alive": True, "grew_age_s": 1, "last_error": "ffmpeg stopped (-9)", "failures": 1}   # restarted already
        d.tick(page=player(t + 5), capture=cap, audio=1)
        d.tick(page=player(t + 10), capture=cap, audio=1)
        self.assertEqual(d.s.recoveries, 1)                              # once per failure
        self.assertIn("stopped and was restarted", d.s.report()["issues"][-1]["text"])
        d.tick(page=player(t + 15), capture={**cap, "failures": 2}, audio=1)     # the same error again: counted again
        self.assertEqual(d.s.recoveries, 2)

    def test_missing_sound_is_shown(self):
        d = self.rec(limits=Limits(no_audio_warn_s=60))
        t = self.t
        for _ in range(15):
            t += 5
            d.tick(page=player(t), capture=GOOD_CAP, audio=0)
        self.assertIn("no sound", d.s.problem)
        t += 5
        d.tick(page=player(t), capture=GOOD_CAP, audio=1)
        self.assertFalse(d.s.problem)

    def test_switched_to_another_recording_goes_back(self):
        d = self.rec()
        acts = d.tick(page=player(self.t + 5, media_id=SQ), capture=GOOD_CAP, audio=1)
        self.assertEqual(acts, [("navigate", ORIGIN + EP + SPRINT)])
        self.assertEqual(d.s.state, "RECORDING")


class EndTest(unittest.TestCase):
    def test_vod_ends_at_its_end_and_reports(self):
        d = Driver("practice1")
        t = d.to_recording(FP1, duration=120)
        acts = []
        while d.s.state == "RECORDING" and t < 200:
            t = min(t + 5, 120)
            acts = d.tick(page=player(t, FP1, duration=120), capture=GOOD_CAP, audio=1)
        self.assertEqual(acts[-1], ("finish", "the recording ended"))
        self.assertEqual(d.s.state, "DONE")
        rep = d.s.report()
        self.assertEqual((rep["key"], rep["episode_id"], rep["played_to"]), (d.s.key, FP1, 120))
        self.assertGreater(rep["expected_s"], 60)
        self.assertEqual(d.tick(page=player(120, FP1, duration=120)), [])      # finished: never again

    def test_next_episode_autoplay_at_the_end_is_the_end(self):
        d = Driver("practice1")
        t = d.to_recording(FP1, duration=3600)
        t = 3590
        d.tick(page=player(t, FP1, duration=3600), capture=GOOD_CAP, audio=1)
        acts = d.tick(page=player(3, SQ, duration=2400), capture=GOOD_CAP, audio=1)
        self.assertEqual(acts, [("finish", "the recording ended (the page went on to the next one)")])

    def test_live_ends_with_the_session_window(self):
        d = Driver("sprint", until=T0 + 600)
        t = d.to_recording()
        acts = []
        while d.s.state == "RECORDING":
            t += 5
            acts = d.tick(dt=30, page=player(t), capture=GOOD_CAP, audio=1)
        self.assertEqual(acts[-1], ("finish", "the session window is over"))

    def test_live_stream_ended(self):
        d = Driver("sprint")
        t = d.to_recording()
        acts = d.tick(page=player(t + 5, ended=True), capture=GOOD_CAP, audio=1)
        self.assertEqual(acts, [("finish", "the recording ended")])

    def test_window_closes_before_anything_was_found(self):
        d = Driver("race", until=T0 + 120)
        d.tick()
        self.assertEqual(d.s.state, "NOT_FOUND")
        self.assertEqual(d.tick(dt=200), [])
        self.assertEqual(d.s.state, "DONE")
        self.assertIn("before the recording could start", d.s.end_reason)


if __name__ == "__main__":
    unittest.main()


class SecretsTest(unittest.TestCase):
    def test_no_token_in_status_report_or_actions(self):
        import json
        d = Driver("sprint")
        d.tick()
        d.tick()
        man = [f"https://cdn.example.net/hls/{SPRINT}/master.m3u8?hdnts=exp=1~hmac=SECRETSIG&token=SECRET123"]
        t = 0
        acts = []
        for _ in range(4):
            t += 5
            acts += d.tick(page=player(t, media_id=None, manifests=man))
        self.assertEqual(d.s.state, "RECORDING")
        blob = json.dumps([d.s.status(), d.s.report(), d.s.verification, acts])
        for secret in ("SECRET123", "SECRETSIG", "hdnts", "token="):
            self.assertNotIn(secret, blob)
        self.assertIn(f"/hls/{SPRINT}/master.m3u8", blob)

    def test_sign_in_page_while_verifying(self):
        d = Driver("sprint")
        d.tick()
        d.tick()
        acts = d.tick(page={"video": False, "login_form": True})
        self.assertEqual(acts, [("login",), ("navigate", ORIGIN + EP + SPRINT)])
        self.assertEqual(d.s.state, "VERIFYING")
