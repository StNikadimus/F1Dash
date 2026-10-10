"""Which VOYO recording is the F1 session to record (server/voyo_episodes.py): titles -> session kinds,
several recordings on one event page, missing / ambiguous metadata, HLS / DASH manifests, verification
of what the player plays, and redaction of addresses.

The episode ids below are the ones checked by hand on VOYO (FP1 63660752 and Sprint Qualifying 63660945
with DASH manifests, Sprint 63661233 with HLS) - fixtures only, never configured anywhere.

Run (from main/):  python -m unittest tests.test_voyo_episodes
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import voyo_episodes as ve  # noqa: E402

FP1, SQ, SPRINT = "63660752", "63660945", "63661233"
EP = "/play/category/2102/episodes/"


def page(*cards):
    """EPISODES_JS-like items: (id, link text, card text)."""
    return [{"href": "https://voyo.si" + EP + i, "text": t, "card": c} for i, t, c in cards]


class TitleTest(unittest.TestCase):
    def test_slovenian_and_english_titles(self):
        cases = {
            "1. prosti trening": "practice1", "Prvi prosti trening": "practice1", "Prosti trening 1": "practice1",
            "FP1": "practice1", "Free Practice 1": "practice1", "VN Singapurja: 2. prosti trening": "practice2",
            "Drugi prosti trening": "practice2", "Practice 3": "practice3", "Tretji prosti trening": "practice3",
            "Sprint kvalifikacije": "sprint_qualifying", "Šprint kvalifikacije": "sprint_qualifying",
            "Sprint Qualifying": "sprint_qualifying", "Sprint Shootout": "sprint_qualifying",
            "Sprint": "sprint", "Šprint": "sprint", "Sprinterska dirka": "sprint",
            "Kvalifikacije": "qualifying", "Kvalifikacije za VN Italije": "qualifying", "Qualifying": "qualifying",
            "Dirka": "race", "VN Monaka – dirka": "race", "Race": "race",
        }
        for title, kind in cases.items():
            c = ve.classify_title(title)
            self.assertEqual((c.kind, c.confidence), (kind, "high"), title)

    def test_shows_about_a_session_are_not_the_session(self):
        for title in ("Povzetek dirke", "Pred dirko", "Po dirki: intervjuji", "Highlights: Sprint", "F1 studio",
                      "Napoved kvalifikacij", "Analiza sprinta", "Race highlights", "Pre-race show",
                      "Novinarska konferenca po kvalifikacijah"):
            c = ve.classify_title(title)
            self.assertTrue(c.excluded, title)
            self.assertIsNone(c.kind, title)

    def test_unclear_and_missing_titles(self):
        self.assertEqual(ve.classify_title("Prosti trening").confidence, "low")      # which one?
        self.assertIsNone(ve.classify_title("Prosti trening").kind)
        for t in (None, "", "Velika nagrada Singapurja", "Epizoda 4", "Formula 1"):
            c = ve.classify_title(t)
            self.assertEqual((c.kind, c.confidence), (None, "none"), t)


class EventPageTest(unittest.TestCase):
    def test_several_recordings_on_one_event_page(self):
        eps = ve.episodes_from_page(page(
            (FP1, "Prvi prosti trening", "VN Kitajske · Prvi prosti trening · 21. 3. 2026"),
            (SQ, "Sprint kvalifikacije", "VN Kitajske · Sprint kvalifikacije · 21. 3. 2026"),
            (SPRINT, "Sprint", "VN Kitajske · Sprint · V ŽIVO"),
            ("63661300", "Povzetek sprinta", "Povzetek sprinta"),
        ))
        by = {e.id: e for e in eps}
        self.assertEqual(by[FP1].kind, "practice1")
        self.assertEqual(by[FP1].date, "2026-03-21")
        self.assertEqual(by[SQ].kind, "sprint_qualifying")
        self.assertEqual((by[SPRINT].kind, by[SPRINT].live), ("sprint", True))
        self.assertTrue(by["63661300"].excluded)
        for kind, want in (("practice1", FP1), ("sprint_qualifying", SQ), ("sprint", SPRINT)):
            s = ve.select_episode(eps, kind)
            self.assertEqual((s.state, s.episode.id), ("SELECTED", want), kind)

    def test_links_without_text_use_the_card_and_duplicates_merge(self):
        eps = ve.episodes_from_page([
            {"href": EP + SQ, "text": "", "alt": "", "card": "Sprint kvalifikacije"},
            {"href": EP + SQ + "?autoplay=1", "text": "Predvajaj", "card": ""},
            {"href": "/play/category/2102/", "text": "Vse epizode"},                       # not an episode
            {"href": "javascript:void(0)", "text": "Dirka"},
        ])
        self.assertEqual([(e.id, e.kind) for e in eps], [(SQ, "sprint_qualifying")])
        self.assertEqual(eps[0].path, EP + SQ)

    def test_a_recording_is_named_by_its_own_link_not_by_a_shared_row(self):
        # a page whose cards share one container: each "card" text holds every title, highlights included
        row = "VN Kitajske 2026 Prvi prosti trening 21. 3. 2026 Sprint kvalifikacije Sprint Povzetek sprinta"
        eps = ve.episodes_from_page(page((FP1, "Prvi prosti trening", row), (SQ, "Sprint kvalifikacije", row),
                                         (SPRINT, "Sprint", row), ("63661300", "Povzetek sprinta", row)))
        by = {e.id: (e.kind, e.excluded) for e in eps}
        self.assertEqual(by, {FP1: ("practice1", False), SQ: ("sprint_qualifying", False), SPRINT: ("sprint", False),
                              "63661300": (None, True)})
        # a link without words (an image / a play button) falls back to its card
        eps = ve.episodes_from_page([{"href": EP + SQ, "text": "", "card": "Sprint kvalifikacije · 21. 3."}])
        self.assertEqual(eps[0].kind, "sprint_qualifying")

    def test_never_another_session_instead(self):
        eps = ve.episodes_from_page(page((FP1, "Prvi prosti trening", ""), (SQ, "Sprint kvalifikacije", "")))
        s = ve.select_episode(eps, "race")
        self.assertEqual((s.state, s.episode), ("NOT_FOUND", None))
        self.assertIn("Practice 1", s.reason)
        self.assertIn("Sprint Qualifying", s.reason)
        self.assertEqual(ve.select_episode(eps, "qualifying").state, "NOT_FOUND")    # not the sprint one

    def test_ambiguous_is_said_not_guessed(self):
        eps = ve.episodes_from_page(page(("11111111", "Dirka", ""), ("22222222", "Dirka", "")))
        s = ve.select_episode(eps, "race")
        self.assertEqual(s.state, "AMBIGUOUS")
        self.assertEqual({e.id for e in s.candidates}, {"11111111", "22222222"})
        unsure = ve.episodes_from_page(page(("33333333", "Prosti trening", "")))
        s = ve.select_episode(unsure, "practice1")
        self.assertEqual(s.state, "NOT_FOUND")
        self.assertIn("unclear", s.reason)

    def test_tie_breaks_meeting_date_live(self):
        eps = ve.episodes_from_page(page(("11111111", "Dirka", "VN Japonske · Dirka · 29. 3. 2026"),
                                         ("22222222", "Dirka", "VN Kitajske · Dirka · 22. 3. 2026")))
        s = ve.select_episode(eps, "race", meeting="Chinese Grand Prix VN Kitajske")
        self.assertEqual(s.episode.id, "22222222")
        start = datetime(2026, 3, 29, 6, 0, tzinfo=timezone.utc).timestamp()
        s = ve.select_episode(eps, "race", meeting="Formula 1 Grand Prix", session_start=start)
        self.assertEqual(s.episode.id, "11111111")
        eps = ve.episodes_from_page(page(("11111111", "Dirka", "Dirka"), ("22222222", "Dirka", "Dirka · V živo")))
        self.assertEqual(ve.select_episode(eps, "race").episode.id, "22222222")

    def test_stale_page_without_the_new_session(self):
        # last weekend's page: only finished sessions - the new one is not there yet (not found, retried)
        eps = ve.episodes_from_page(page((FP1, "Prvi prosti trening", "21. 3. 2026")))
        self.assertEqual(ve.select_episode(eps, "practice2").state, "NOT_FOUND")
        self.assertEqual(ve.select_episode([], "race").state, "NOT_FOUND")
        self.assertEqual(ve.select_episode(eps, "warmup").state, "NOT_FOUND")


class ManifestAndVerificationTest(unittest.TestCase):
    DASH = f"https://vod.cdn.example.net/vod/{FP1}/dash/manifest.mpd?token=SECRET123&exp=999"
    HLS = f"https://live.cdn.example.net/hls/{SPRINT}/master.m3u8?hdnts=exp=1~acl=/*~hmac=abcdef"

    def test_dash_and_hls_manifests(self):
        d = ve.manifest_info(self.DASH)
        self.assertEqual((d["format"], d["ids"]), ("dash", [FP1]))
        h = ve.manifest_info(self.HLS)
        self.assertEqual((h["format"], h["ids"]), ("hls", [SPRINT]))
        self.assertIsNone(ve.manifest_info("https://cdn.example.net/seg_0001.m4s"))
        self.assertIsNone(ve.manifest_info(None))

    def test_secrets_never_leave(self):
        for url in (self.DASH, self.HLS):
            red = ve.redact_url(url)
            for secret in ("SECRET123", "token", "hdnts", "hmac", "exp=", "?"):
                self.assertNotIn(secret, red, url)
        red = ve.redact_url("https://cdn.example.net/hdnts=exp=1~hmac=ffff/" + "a" * 64 + f"/{SQ}/x.mpd")
        self.assertEqual(red, f"https://cdn.example.net/…/…/{SQ}/x.mpd")
        self.assertEqual(ve.redact_url("https://user:pw@voyo.si/play?x=1"), "https://voyo.si/play")
        self.assertEqual(ve.redact_url("javascript:alert(1)"), "")
        self.assertEqual(ve.redact_url("http://127.0.0.1:8778/f1/x?a=1"), "http://127.0.0.1:8778/f1/x")
        self.assertEqual(ve.redact_url("http://host:notaport/x"), "")

    def test_verified_by_media_id_or_manifest(self):
        v = ve.verify_episode(FP1, "practice1", {"media_id": FP1, "og_title": "Prvi prosti trening"})
        self.assertEqual(v.state, "VERIFIED")
        v = ve.verify_episode(SQ, "sprint_qualifying", {"manifests": [f"https://cdn.example.net/{SQ}/manifest.mpd"]})
        self.assertEqual((v.state, v.format), ("VERIFIED", "dash"))
        v = ve.verify_episode(SPRINT, "sprint", {"manifests": [self.HLS]})
        self.assertEqual((v.state, v.format), ("VERIFIED", "hls"))
        self.assertNotIn("SECRET", " ".join(v.evidence))

    def test_wrong_or_unknown_video_is_not_verified(self):
        v = ve.verify_episode(FP1, "practice1", {"media_id": SQ})
        self.assertEqual(v.state, "WRONG")
        v = ve.verify_episode(FP1, "practice1", {"media_id": FP1, "og_title": "Sprint kvalifikacije"})
        self.assertEqual(v.state, "WRONG")                          # the right id but another session's title
        v = ve.verify_episode(FP1, "practice1", {"path": EP + FP1})  # the address alone is not proof
        self.assertEqual(v.state, "UNVERIFIED")
        v = ve.verify_episode(FP1, "practice1", {"manifests": [self.HLS]})
        self.assertEqual(v.state, "UNVERIFIED")
        self.assertEqual(ve.verify_episode(FP1, "practice1", None).state, "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
