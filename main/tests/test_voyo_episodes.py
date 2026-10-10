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

FP1, SQ, SPRINT, QUALI = "63660752", "63660945", "63661233", "63661274"
EP = "/play/category/2102/episodes/"
EVENT = "https://voyo.si/vsebina/vn-singapurja"


def card(eid, title, date, uniq="plain", handler="onPlayClick"):
    """An item as EPISODES_JS reads VOYO's real event-page card: <a class="episode"> whose href is the EVENT page,
    the id in data-uniq ("<id>" / "media/<id>") and in the inline play handler."""
    onclick = (f'return onPlayClick("{eid}"),!1' if handler == "onPlayClick" else
               f'return playEpisode("{eid}","{EP}{eid}"),!1' if handler == "playEpisode" else "")
    return {"href": EVENT, "data_uniq": eid if uniq == "plain" else f"media/{eid}" if uniq == "media" else "",
            "onclick": onclick, "text": f"{title} {date}", "label": "", "title": "", "alt": title,
            "card": f"{title} {date}"}


def singapore():
    """The VN Singapurja page as seen in production (2026-10): every card twice - a plain onPlayClick card and a
    media/<id> playEpisode one - next to links that are no recordings."""
    shown = ((FP1, "F1 prosti trening", "9. 10. 2026"), (SQ, "F1 sprint kvalifikacije", "9. 10. 2026"),
             (SPRINT, "F1 sprint dirka", "10. 10. 2026"), (QUALI, "F1 kvalifikacije", "10. 10. 2026"))
    return ([card(i, t, d) for i, t, d in shown] + [card(i, t, d, uniq="media", handler="playEpisode") for i, t, d in shown] +
            [{"href": EVENT, "text": "VN Singapurja", "card": "VN Singapurja"},                      # the page itself
             {"href": "https://voyo.si/vsebina/f1", "data_uniq": "", "onclick": "", "text": "Formula 1"},
             card("1234", "F1 napovednik", "8. 10. 2026"),                                       # too short an id
             {**card(QUALI, "F1 kvalifikacije", ""), "data_uniq": "media/abc"},               # not digits
             {**card("63669999", "F1 dirka", ""), "data_uniq": "63669998"}])                  # sources disagree


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


class ProductionEventPageTest(unittest.TestCase):
    """The real VOYO DOM: cards link to the event page, the id is in data-uniq / onclick only. The old reading
    (/episodes/<id> in the href) found NOTHING there - every session NOT_FOUND, zero recordings."""

    def test_old_reading_found_nothing(self):
        self.assertEqual({ve.episode_id(it.get("href")) for it in singapore()}, {None})

    def test_ids_from_data_uniq_and_the_play_handler(self):
        self.assertEqual(ve.item_episode(card(QUALI, "F1 kvalifikacije", "", handler="")), (QUALI, None))
        self.assertEqual(ve.item_episode(card(QUALI, "F1 kvalifikacije", "", uniq="media", handler="")), (QUALI, None))
        self.assertEqual(ve.item_episode(card(QUALI, "F1 kvalifikacije", "", uniq="", handler="onPlayClick")),
                         (QUALI, None))
        self.assertEqual(ve.item_episode(card(QUALI, "F1 kvalifikacije", "", uniq="", handler="playEpisode")),
                         (QUALI, EP + QUALI))
        self.assertEqual(ve.item_episode({"href": "https://voyo.si" + EP + QUALI}), (QUALI, EP + QUALI))  # old pages
        for bad in ({"href": EVENT}, {"data_uniq": "media/"}, {"data_uniq": "1234"}, {"data_uniq": "1234567890123"},
                    {"data_uniq": "media/6366127x"}, {"data_uniq": "63661274; x"}, {"onclick": "onPlayClick(63661274)"},
                    {"onclick": 'evil("63661274")'}, {"data_uniq": "63661274", "onclick": 'onPlayClick("63661233")'},
                    {"href": "https://voyo.si" + EP + SQ, "data_uniq": QUALI}, {"href": "http://[::1"}, {}):
            self.assertEqual(ve.item_episode(bad), (None, None), bad)
        # a playEpisode path that is not that recording's own episode address is not used
        self.assertEqual(ve.item_episode({"onclick": f'playEpisode("{QUALI}","/play/category/2102/episodes/{SQ}")'}),
                         (QUALI, None))
        self.assertEqual(ve.item_episode({"onclick": f'playEpisode("{QUALI}","javascript:alert(1)")'}), (QUALI, None))

    def test_the_singapore_page_yields_its_four_sessions(self):
        eps = ve.episodes_from_page(singapore())
        self.assertEqual(len(eps), 4)                                  # deduplicated by id, junk ignored
        by = {e.id: e for e in eps}
        self.assertEqual({i: (e.kind, e.confidence) for i, e in by.items()},
                         {FP1: ("practice1", "high"), SQ: ("sprint_qualifying", "high"),
                          SPRINT: ("sprint", "high"), QUALI: ("qualifying", "high")})
        self.assertEqual({i: e.path for i, e in by.items()}, {i: EP + i for i in by})   # the playEpisode twin's path
        self.assertEqual({i: e.date for i, e in by.items()},
                         {FP1: "2026-10-09", SQ: "2026-10-09", SPRINT: "2026-10-10", QUALI: "2026-10-10"})
        self.assertEqual(by[QUALI].title, "F1 kvalifikacije 10. 10. 2026")
        for kind, want in (("practice1", FP1), ("sprint_qualifying", SQ), ("sprint", SPRINT), ("qualifying", QUALI)):
            s = ve.select_episode(eps, kind, "Singapore Grand Prix VN Singapurja")
            self.assertEqual((s.state, s.episode.id), ("SELECTED", want), kind)
        self.assertEqual(ve.select_episode(eps, "race").state, "NOT_FOUND")   # not on the page yet

    def test_the_twin_order_does_not_matter(self):
        items = singapore()
        for order in (items, list(reversed(items))):
            eps = ve.episodes_from_page(order)
            self.assertEqual(sorted((e.id, e.path) for e in eps), sorted((i, EP + i) for i in (FP1, SQ, SPRINT, QUALI)))

    def test_cards_without_an_address_are_found_but_have_no_path(self):
        eps = ve.episodes_from_page([card(QUALI, "F1 kvalifikacije", "10. 10. 2026")])
        self.assertEqual([(e.id, e.kind, e.path) for e in eps], [(QUALI, "qualifying", None)])

    def test_an_unnumbered_practice_is_practice_1_only_on_a_sprint_weekend(self):
        normal = ve.episodes_from_page([card(FP1, "F1 prosti trening", ""), card(QUALI, "F1 kvalifikacije", "")])
        self.assertEqual({e.id: e.kind for e in normal}, {FP1: None, QUALI: "qualifying"})
        self.assertEqual(ve.select_episode(normal, "practice1").state, "NOT_FOUND")
        two = ve.episodes_from_page([card(FP1, "F1 prosti trening", ""), card("63660753", "F1 prosti trening", ""),
                                     card(SPRINT, "F1 sprint dirka", "")])
        self.assertEqual(ve.select_episode(two, "practice1").state, "NOT_FOUND")          # which one? never guessed
        numbered = ve.episodes_from_page([card(FP1, "F1 1. prosti trening", ""), card("63660753", "F1 prosti trening", ""),
                                          card(SPRINT, "F1 sprint dirka", "")])
        self.assertEqual({e.id: e.kind for e in numbered}["63660753"], None)


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
