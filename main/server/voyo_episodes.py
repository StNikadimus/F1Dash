"""Which VOYO recording (episode) is the F1 session to record - discovery, classification, selection,
verification. Pure logic (no browser, no network): tools/voyo_server_player.py feeds it what the
signed-in VOYO page shows and what the player loaded; tests feed it fixtures.

Why not the page address: one VOYO event page lists several recordings (practice, qualifying, sprint,
race, studio shows ...), each its own episode ``/play/category/<c>/episodes/<id>``; the page URL does
not say which one plays. What identifies a recording here:

* the EPISODE ID - in the episode link, in the player's ``mediaId`` and in the manifest path the player
  loads (MPEG-DASH ``.mpd`` or HLS ``.m3u8``; checked by hand: FP1 63660752 / Sprint Qualifying 63660945
  (DASH), Sprint 63661233 (HLS) - the same id in all three places). IDs are found, never configured;
* the TITLE of the link / card / player (Slovenian or English) -> the session kind, with the words that
  mark a show ABOUT a session (preview, studio, highlights ...) excluded.

Selection never guesses: exactly one recording of the wanted kind (after the meeting / date tie-breaks)
is selected; none -> NOT_FOUND, several -> AMBIGUOUS, an unsure title -> not taken. A different session
is never taken instead. After opening, the recording counts as verified only when the player shows the
selected id (``mediaId`` or a manifest path) - the page address alone is not proof.

Privacy: manifest / page addresses are reduced to scheme + host + path with token-like path parts
replaced (``redact_url``) before they are logged or stored; query strings never leave the page.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import urlsplit

KINDS = ("practice1", "practice2", "practice3", "sprint_qualifying", "sprint", "qualifying", "race")
KIND_LABELS = {"practice1": "Practice 1", "practice2": "Practice 2", "practice3": "Practice 3",
               "sprint_qualifying": "Sprint Qualifying", "sprint": "Sprint", "qualifying": "Qualifying",
               "race": "Race"}
EPISODE_PATH_RE = re.compile(r"/episodes?/(\d{5,12})(?=$|[/?#])")
ID_RUN_RE = re.compile(r"(?<![0-9])([0-9]{6,12})(?![0-9])")
SAFE_ID_RE = re.compile(r"^[0-9]{5,12}$")

# a show about a session, not the session itself (checked before the session words)
NOT_SESSION = re.compile(
    r"\b(povzetek|povzetki|highlights?|najboljs\w*|intervju\w*|interview\w*|studio|napoved\w*|preview|analiz\w*|"
    r"analysis|pogovor\w*|oddaj\w*|magazin\w*|reportaz\w*|trailer|napovednik|pred\s+(dirko|sprintom|kvalifikacijami|"
    r"treningom)|po\s+(dirki|sprintu|kvalifikacijah|treningu)|pre\s?race|post\s?race|pre\s?show|post\s?show|grid\s*walk|"
    r"press\s+conference|novinarsk\w*|konferenc\w*|onboard|zgodb\w*|dokumentar\w*|documentary|kviz|quiz)\b")
SPRINT = re.compile(r"\bsprint\w*|\bsprintersk\w*")
QUALI = re.compile(r"\bkvalifik\w*|\bqualifying\b|\bquali\b|\bshootout\b|\bsq\b")
PRACTICE = re.compile(r"\bprost\w*\s+trening\w*|\btrening\w*|\bfree\s+practice\b|\bpractice\b|\bfp\s*[123]\b")
RACE = re.compile(r"\bdirk[aeiou]?\b|\brace\b|\bglavna\s+dirka\b")
NUMBER_WORDS = {"1": ("1", "prvi", "prva", "first", "i"), "2": ("2", "drugi", "druga", "second", "ii"),
                "3": ("3", "tretji", "tretja", "third", "iii")}


def normalize(text: Optional[str]) -> str:
    """lower case, no diacritics (š -> s), one space; ordinal dots kept apart ("1." -> "1 ")."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"(\d)\.", r"\1 ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


@dataclass
class Classification:
    kind: Optional[str]                 # one of KINDS, or None
    confidence: str                     # "high" | "low" | "none"
    excluded: bool = False              # a show about a session (preview, highlights ...)
    why: str = ""


def _practice_number(t: str) -> Optional[str]:
    m = re.search(r"\bfp\s*([123])\b", t)
    if m:
        return m.group(1)
    for num, words in NUMBER_WORDS.items():
        for w in words:
            if re.search(rf"\b{w}\s+(prost\w*\s+)?(trening|free\s+practice|practice)", t) or \
                    re.search(rf"\b(trening\w*|practice)\s+{w}\b", t):
                return num
    return None


def classify_title(title: Optional[str]) -> Classification:
    """The F1 session a VOYO title names (Slovenian / English): "1. prosti trening", "Prvi prosti trening",
    "FP1", "Sprint kvalifikacije", "Sprint", "Kvalifikacije", "Dirka" ... A preview / studio / highlights
    show is excluded; a title without a session word is not a session (confidence "none")."""
    t = normalize(title)
    if not t:
        return Classification(None, "none", why="no title")
    if NOT_SESSION.search(t):
        return Classification(None, "none", excluded=True, why=f"not a session recording ({NOT_SESSION.search(t).group(0)})")
    sprint, quali = bool(SPRINT.search(t)), bool(QUALI.search(t))
    if sprint and quali:
        return Classification("sprint_qualifying", "high", why="sprint + qualifying words")
    if sprint:
        return Classification("sprint", "high", why="sprint word")
    if quali:
        return Classification("qualifying", "high", why="qualifying word")
    if PRACTICE.search(t):
        n = _practice_number(t)
        if n:
            return Classification(f"practice{n}", "high", why=f"practice {n}")
        return Classification(None, "low", why="a practice session without its number")
    if RACE.search(t):
        return Classification("race", "high", why="race word")
    return Classification(None, "none", why="no session word")


# ---------------------------------------------------------------------------------------------
# episodes found on the page
# ---------------------------------------------------------------------------------------------
@dataclass
class Episode:
    id: str
    title: str
    path: str                            # /play/category/<c>/episodes/<id> (path only)
    kind: Optional[str]
    confidence: str
    excluded: bool = False
    why: str = ""
    date: Optional[str] = None           # what the card says (ISO date if it could be read)
    live: Optional[bool] = None          # the card says LIVE / V ŽIVO
    texts: list = field(default_factory=list)
    card: str = ""                       # the text around the link (meeting, date ...)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("texts", None)
        d["card"] = d["card"][:160]
        return d


def episode_id(href: Optional[str]) -> Optional[str]:
    m = EPISODE_PATH_RE.search(urlsplit(str(href or "")).path or "")
    return m.group(1) if m else None


DATE_RE = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})?")
LIVE_RE = re.compile(r"\b(v\s*zivo|live|neposredn\w*)\b")


def _card_date(text: str, year: int) -> Optional[str]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(3) or year), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc).date().isoformat()
    except ValueError:
        return None


def episodes_from_page(items: Iterable[dict], year: Optional[int] = None) -> list[Episode]:
    """The recordings the page links to - EPISODES_JS's items [{href, text, label, alt, card, ld_name}] ->
    one Episode per id (texts of all its links merged), classified by its best title."""
    year = year or datetime.now(timezone.utc).year
    by_id: dict[str, dict] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        eid = episode_id(it.get("href"))
        if not eid:
            continue
        d = by_id.setdefault(eid, {"path": urlsplit(str(it.get("href"))).path[:200], "texts": [], "card": ""})
        for k in ("ld_name", "label", "text", "alt", "title"):
            v = str(it.get(k) or "").strip()
            if v and v not in d["texts"]:
                d["texts"].append(v[:200])
        card = str(it.get("card") or "")
        if len(card) > len(d["card"]):
            d["card"] = card[:400]
    out = []
    for eid, d in by_id.items():
        # the recording's own words first (link text, label, image alt, JSON-LD name); the card around it
        # (meeting, date, description) only when they name no session
        best = None
        for text in d["texts"]:
            c = classify_title(text)
            if c.excluded:                       # one of its own texts says "preview / highlights": not the session
                best = (text, c)
                break
            if best is None or (c.confidence == "high", c.kind is not None) > (best[1].confidence == "high",
                                                                                 best[1].kind is not None):
                best = (text, c)
        if (best is None or (best[1].kind is None and not best[1].excluded)) and d["card"]:
            c = classify_title(d["card"])
            if c.kind or c.excluded or best is None:
                best = (d["card"], c)
        title, c = best if best else ("", Classification(None, "none", why="no text"))
        card_n = normalize(d["card"])
        out.append(Episode(id=eid, title=title[:160], path=d["path"], kind=c.kind, confidence=c.confidence,
                           excluded=c.excluded, why=c.why, date=_card_date(d["card"], year),
                           live=True if LIVE_RE.search(card_n) else None, texts=d["texts"], card=d["card"]))
    return out


# ---------------------------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------------------------
@dataclass
class Selection:
    state: str                           # SELECTED | NOT_FOUND | AMBIGUOUS
    episode: Optional[Episode]
    reason: str
    candidates: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "episode": self.episode.to_json() if self.episode else None,
                "candidates": [e.to_json() for e in self.candidates][:10]}


def _tokens(s: Optional[str]) -> set:
    stop = {"grand", "prix", "velika", "nagrada", "vn", "gp", "formula", "f1", "the", "of", "2026", "2025", "2027"}
    return {w for w in normalize(s).split() if len(w) > 2 and w not in stop}


def select_episode(episodes: list[Episode], kind: str, meeting: Optional[str] = None,
                   session_start: Optional[float] = None) -> Selection:
    """The one recording of ``kind`` - or why there is none. Never another kind, never a guess."""
    if kind not in KINDS:
        return Selection("NOT_FOUND", None, f"unknown session kind {kind!r}")
    same = [e for e in episodes if e.kind == kind and not e.excluded]
    unsure = [e for e in episodes if e.kind is None and e.confidence == "low" and not e.excluded]
    if not same:
        seen = sorted({KIND_LABELS.get(e.kind, e.kind) for e in episodes if e.kind and not e.excluded})
        note = f"; {len(unsure)} title(s) too unclear to tell" if unsure else ""
        return Selection("NOT_FOUND", None, f"no {KIND_LABELS[kind]} recording on the page (found: "
                                            f"{', '.join(seen) or 'no F1 session recordings'}){note}", unsure)
    pool = [e for e in same if e.confidence == "high"]
    if not pool:
        return Selection("AMBIGUOUS", None, f"{KIND_LABELS[kind]}: only unsure titles", same)
    if len(pool) > 1 and meeting:
        want = _tokens(meeting)
        scored = [(len(want & _tokens(" ".join(e.texts + [e.title, e.card]))), e) for e in pool]
        top = max(s for s, _e in scored)
        if top > 0:
            pool = [e for s, e in scored if s == top]
    if len(pool) > 1 and session_start:
        day = datetime.fromtimestamp(session_start, timezone.utc).date()
        dated = [e for e in pool if e.date]
        near = [e for e in dated if abs((datetime.fromisoformat(e.date).date() - day).days) <= 1]
        if near:
            pool = near
    if len(pool) > 1:
        live = [e for e in pool if e.live]
        if len(live) == 1:
            pool = live
    if len(pool) == 1:
        e = pool[0]
        return Selection("SELECTED", e, f"{KIND_LABELS[kind]}: episode {e.id} \"{e.title}\" ({e.why})")
    return Selection("AMBIGUOUS", None, f"{len(pool)} recordings look like {KIND_LABELS[kind]} - not choosing "
                                        "between them", pool)


# ---------------------------------------------------------------------------------------------
# what the player loaded: manifests (HLS / DASH) and the player's own id
# ---------------------------------------------------------------------------------------------
TOKENISH = re.compile(r"(token|sig|signature|hmac|exp|expires|policy|key|auth|acl|hdnts|hdntl)[=_~-]", re.I)


def redact_url(url: Optional[str]) -> str:
    """scheme://host/path without the query / fragment, token-like path parts -> "…" (for logs and files)."""
    try:
        p = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    try:
        host = (p.hostname or "") + (f":{p.port}" if p.port else "")
    except ValueError:                                   # a port that is not a number
        return ""
    parts = []
    for seg in p.path.split("/"):
        if TOKENISH.search(seg) or (len(seg) > 40 and re.fullmatch(r"[A-Za-z0-9_\-=~.%]+", seg)):
            parts.append("…")
        else:
            parts.append(seg[:80])
    return f"{p.scheme}://{host}{'/'.join(parts)[:300]}"


def manifest_info(url: Optional[str]) -> Optional[dict]:
    """A manifest the player loaded -> {"format": "dash" | "hls", "path": redacted, "ids": [...]} - the media
    ids are the digit runs of its path. None when it is not a manifest."""
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return None
    low = path.lower()
    fmt = "dash" if low.endswith(".mpd") else "hls" if low.endswith(".m3u8") else None
    if fmt is None:
        return None
    return {"format": fmt, "path": redact_url(url), "ids": ID_RUN_RE.findall(path)[:6]}


@dataclass
class Verification:
    state: str                           # VERIFIED | WRONG | UNVERIFIED
    reason: str
    evidence: list = field(default_factory=list)
    format: Optional[str] = None


def verify_episode(expected_id: str, kind: Optional[str], player: Optional[dict]) -> Verification:
    """Does the player play the selected recording? ``player`` = PLAYER_JS's answer {media_id, manifests:
    [url path], title, og_title, path}. VERIFIED needs the id in the player (mediaId) or in a manifest path;
    WRONG when the player shows another id (and no manifest with ours) or a title of another session."""
    p = player or {}
    ev, fmt = [], None
    mid = str(p.get("media_id") or "").strip()
    manifests = [m for m in (manifest_info(u) for u in (p.get("manifests") or [])) if m]
    hit = [m for m in manifests if expected_id in m["ids"]]
    if hit:
        fmt = hit[-1]["format"]
        ev.append(f"{fmt.upper()} manifest {hit[-1]['path']}")
    if mid == expected_id:
        ev.append(f"player mediaId {mid}")
    titles = [p.get("media_title"), p.get("og_title"), p.get("title")]
    for t in titles:
        c = classify_title(t)
        if kind and c.kind and c.confidence == "high" and c.kind != kind:
            return Verification("WRONG", f"the player shows {KIND_LABELS[c.kind]} (\"{str(t)[:80]}\"), not "
                                         f"{KIND_LABELS[kind]}", ev, fmt)
    if ev:
        return Verification("VERIFIED", "episode " + expected_id + " playing (" + "; ".join(ev) + ")", ev, fmt)
    if mid and SAFE_ID_RE.match(mid) and mid != expected_id:
        return Verification("WRONG", f"the player plays media {mid}, not episode {expected_id}", ev, fmt)
    if manifests:
        return Verification("UNVERIFIED", f"the player's manifest ({manifests[-1]['path']}) does not carry episode "
                                          f"{expected_id}", ev, manifests[-1]["format"])
    return Verification("UNVERIFIED", "the player does not show which recording it plays yet", ev, fmt)
