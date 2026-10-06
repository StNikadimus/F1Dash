"""OpenF1 (https://openf1.org) - public historical F1 data, no authentication.

Used for
* session detection: which meeting/session a VOYO recording shows,
* reference events on F1's clock for synchronisation anchors:
    - lap-line crossings   (laps.date_start of lap n >= 2, not after a pit exit)
    - session start/finish (race_control category "SessionStatus",
      "SESSION STARTED" / "SESSION FINISHED", millisecond timestamps)

Verified against the F1 live-timing archive (2026 Japanese GP race):
* OpenF1 session_key 11253 / meeting_key 1281 are the live-timing keys;
* laps.date_start (#12, lap 6) = 05:22:02.092 = the TimingData message that
  raised NumberOfLaps to 5 -> same clock, millisecond resolution;
* SESSION STARTED 05:14:02.078 = lights out, 14 min after the scheduled
  sessions.date_start (05:00) -> date_start is only a schedule.

Requests are few (sessions+meetings per year, laps+race_control per session),
cached on disk, retried with back-off; failures never produce data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from .telemetry import parse_utc

log = logging.getLogger("openf1")

BASE_URL = "https://api.openf1.org/v1"
SESSION_NAMES = ["Practice 1", "Practice 2", "Practice 3", "Sprint Qualifying", "Sprint", "Qualifying", "Race"]


class OpenF1Error(Exception):
    pass


class OpenF1Client:
    def __init__(self, cache_dir: Path, base_url: str = BASE_URL, timeout: float = 15.0,
                 retries: int = 3, min_interval: float = 0.4) -> None:
        self.cache_dir = cache_dir
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.min_interval = min_interval
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None

    def _cache_path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.=-]", "_", key)[:120]
        return self.cache_dir / f"{safe}.json"

    async def get(self, endpoint: str, params: dict, ttl: Optional[float]) -> tuple[list, dict]:
        """Returns (records, meta). ``ttl`` None = cache forever. Raises OpenF1Error."""
        key = endpoint + "_" + "_".join(f"{k}={v}" for k, v in sorted(params.items()))
        path = self._cache_path(key)
        cached = None
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(cached.get("saved", 0))
                if ttl is None or age < ttl:
                    return cached["data"], {"cache": True, "stale": False}
            except (OSError, ValueError, KeyError):
                cached = None
        async with self._lock:
            err = "unknown error"
            for attempt in range(self.retries):
                wait = self.min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
                try:
                    async with httpx.AsyncClient(timeout=self.timeout,
                                                 headers={"User-Agent": "f1-tv-dashboard/1.0"}) as http:
                        r = await http.get(f"{self.base_url}/{endpoint}", params=params)
                    if r.status_code == 404:
                        data: Any = []                 # OpenF1 answers 404 for "no results"
                    elif r.status_code == 429:
                        err = "rate limited (HTTP 429)"
                        await asyncio.sleep(min(30.0, float(r.headers.get("retry-after") or 5 * (attempt + 1))))
                        continue
                    elif r.status_code != 200:
                        err = f"HTTP {r.status_code}"
                        await asyncio.sleep((1, 3, 8)[min(attempt, 2)])
                        continue
                    else:
                        data = r.json()
                    if isinstance(data, dict) and "detail" in data:
                        err = str(data.get("detail"))[:80]
                        break
                    if not isinstance(data, list):
                        err = "unexpected response"
                        break
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps({"saved": time.time(), "data": data}), encoding="utf-8")
                    except OSError:
                        pass
                    self.last_error = None
                    return data, {"cache": False, "stale": False}
                except (httpx.HTTPError, ValueError) as exc:
                    err = type(exc).__name__
                    await asyncio.sleep((1, 3, 8)[min(attempt, 2)])
            self.last_error = f"OpenF1 {endpoint}: {err}"
            log.warning("%s", self.last_error)
        if cached is not None:
            return cached["data"], {"cache": True, "stale": True}
        raise OpenF1Error(f"OpenF1 unavailable ({err})")

    # ------------------------------------------------------------------ endpoints
    async def sessions(self, year: int) -> list[dict]:
        data, _ = await self.get("sessions", {"year": year}, ttl=6 * 3600)
        return data

    async def meetings(self, year: int) -> list[dict]:
        data, _ = await self.get("meetings", {"year": year}, ttl=6 * 3600)
        return data

    async def session(self, session_key: int) -> Optional[dict]:
        data, _ = await self.get("sessions", {"session_key": session_key}, ttl=6 * 3600)
        return data[0] if data else None

    async def _session_data(self, endpoint: str, session: dict) -> list[dict]:
        end = parse_utc(session.get("date_end"))
        finished = end is not None and (datetime.now(timezone.utc) - end).total_seconds() > 3 * 3600
        data, _ = await self.get(endpoint, {"session_key": session["session_key"]},
                                 ttl=None if finished else 300)
        return data

    async def laps(self, session: dict) -> list[dict]:
        return await self._session_data("laps", session)

    async def race_control(self, session: dict) -> list[dict]:
        return await self._session_data("race_control", session)


# ---------------------------------------------------------------------------
# Reference events on F1's clock
# ---------------------------------------------------------------------------
@dataclass
class RefEvents:
    """Events that are visible on the video AND timestamped on F1's clock."""
    source: str = "none"                                    # openf1 | archive | feed
    crossings: dict[str, list[tuple[float, int]]] = field(default_factory=dict)   # num -> [(ms, completed lap)]
    starts: list[float] = field(default_factory=list)       # SESSION STARTED (lights out / clock start)
    # where each start came from (ms -> "SessionData.StatusSeries", "SessionStatus", "OpenF1
    # race_control", "ExtrapolatedClock ±1 s"); starts derived from the session clock are approx
    start_src: dict[float, str] = field(default_factory=dict)
    approx_starts: set = field(default_factory=set)
    # every report of a start: (ms, "<source family> · <topic>", approx). ``starts`` is derived from
    # it - one per start, the most authoritative report's time (server/lights_out.py)
    start_obs: list = field(default_factory=list)
    parse_errors: int = 0                                   # start / status messages that could not be read
    # other timestamped events for EVENT SYNC: (ms, label, precise, source) - track status changes
    # (SessionData.StatusSeries / TrackStatus, ms) and race control messages (whole seconds)
    events: list[tuple[float, str, bool, str]] = field(default_factory=list)
    # raw track status points (ms, code, from StatusSeries?, source); events derived in all_events()
    ts_points: list[tuple[float, str, bool, str]] = field(default_factory=list)
    finishes: list[float] = field(default_factory=list)     # SESSION FINISHED
    # race control messages about the START itself (delayed / postponed / suspended start
    # procedure / "FORMATION LAP WILL START AT 14:10"): (ms, message) - see start_notice()
    notices: list[tuple[float, str]] = field(default_factory=list)
    # session structure from the official timing (Q1/Q2/Q3, session clock, markers) - see
    # server/session_phases.py; None = not known (yet)
    timeline: Any = None

    def add_crossing(self, num: str, ms: float, lap: int) -> None:
        lst = self.crossings.setdefault(str(num), [])
        if not any(abs(t - ms) < 500 for t, _ in lst):
            lst.append((ms, lap))
            lst.sort()

    def empty(self) -> bool:
        return not self.crossings and not self.starts and not self.finishes

    def add_start(self, ms: float, src: str, approx: bool = False) -> None:
        """One report of a start. Reports of the same start (several topics / sources, a reconnect
        snapshot repeating it) are one start; its time comes from the most authoritative report."""
        from .lights_out import family
        fam = family(src)
        if any(abs(o[0] - ms) < 50 and family(o[1]) == fam and o[2] == approx for o in self.start_obs):
            return                                      # the same report again (e.g. after a reconnect)
        self.start_obs.append((float(ms), src, bool(approx)))
        self._derive_starts()

    def _derive_starts(self) -> None:
        from .lights_out import best, clusters
        self.starts, self.start_src, self.approx_starts = [], {}, set()
        for c in clusters(self.start_obs):
            t, src, approx = best(c)
            self.starts.append(t)
            self.start_src[t] = src
            if approx:
                self.approx_starts.add(t)

    def add_event(self, ms: float, label: str, precise: bool, source: str) -> None:
        """One event; the same event from two sources (within 2.5 s) is kept once, the precise one wins."""
        for i, (t, lab, pr, _src) in enumerate(self.events):
            if lab == label and abs(t - ms) < 2500:
                if precise and not pr:
                    self.events[i] = (ms, label, True, source)
                return
        self.events.append((ms, label, precise, source))
        self.events.sort(key=lambda e: e[0])

    def track_status(self, ms: float, status: Any, source: str, series: bool = False) -> None:
        """A track status (code "4" or name "SCDeployed") at ms (StatusSeries entries are change
        records with their own Utc; the TrackStatus topic repeats states)."""
        code = TRACK_STATUS_CODE.get(str(status), str(status))
        if code in TRACK_STATUS_EVENT and not any(abs(t - ms) < 50 and c == code and sr == series
                                                  for t, c, sr, _ in self.ts_points):
            self.ts_points.append((ms, code, series, source))

    def all_events(self) -> list[tuple[float, str, bool, str]]:
        """Race control events + track status changes (time order, one per moment)."""
        out = RefEvents()
        for t, lab, pr, src in self.events:
            out.add_event(t, lab, pr, src)
        for series in (True, False):
            pts = sorted(p for p in self.ts_points if p[2] == series)
            prev = None
            for t, code, _sr, src in pts:
                # StatusSeries: every entry is a change (a clear only after something to clear);
                # TrackStatus topic: a change of the repeated state (the first state is none)
                change = (code != "1" or prev not in (None, "1")) if series else (prev is not None and code != prev)
                prev = code
                if change:
                    out.add_event(t, TRACK_STATUS_EVENT[code], True, src)
        return out.events

    def race_control(self, ms: float, message: str, category: Any) -> None:
        label = rc_event_label(message, category)
        if label:
            self.add_event(ms, label, False, "RaceControlMessages")

    def add_notice(self, ms: float, text: str) -> None:
        if start_notice(text) and not any(abs(t - ms) < 1000 and m == text for t, m in self.notices):
            self.notices.append((ms, text[:160]))
            self.notices.sort()

    def first_crossing(self) -> Optional[float]:
        firsts = [lst[0][0] for lst in self.crossings.values() if lst]
        return min(firsts) if firsts else None

    def count(self) -> int:
        return sum(len(v) for v in self.crossings.values()) + len(self.starts) + len(self.finishes)


# A race control message that concerns the start of the session (not a mid-session event):
# "START DELAYED", "RACE START POSTPONED", "START PROCEDURE SUSPENDED", "START ABORTED",
# "FORMATION LAP WILL START AT 14:10", "EXTRA FORMATION LAP", "SESSION START DELAYED" ...
_START_NOTICE = re.compile(r"(START|FORMATION LAP).*(DELAY|POSTPON|SUSPEND|ABORT|WILL START AT|\bAT \d{1,2}[:.]\d{2})"
                           r"|(DELAY|POSTPON|SUSPEND|ABORT).*(START|FORMATION LAP)|EXTRA FORMATION LAP")
_START_AT = re.compile(r"\bAT (\d{1,2})[:.](\d{2})\b")


def start_notice(text: Any) -> bool:
    return isinstance(text, str) and bool(_START_NOTICE.search(text.upper()))


def announced_start_ms(text: str, notice_ms: float, gmt_offset: Any) -> Optional[float]:
    """'FORMATION LAP WILL START AT 14:10' (track time) -> UTC ms (the next 14:10 after the notice).
    None without a time or without the track's GMT offset."""
    m = _START_AT.search((text or "").upper())
    if not m or not isinstance(gmt_offset, str):
        return None
    sign = -1 if gmt_offset.strip().startswith("-") else 1
    try:
        parts = [int(x) for x in gmt_offset.strip().lstrip("+-").split(":")[:2]]
    except ValueError:
        return None
    off_ms = sign * (parts[0] * 3600 + (parts[1] if len(parts) > 1 else 0) * 60) * 1000
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    local = notice_ms + off_ms
    day = local - local % 86_400_000
    t = day + (hh * 3600 + mm * 60) * 1000
    if t < local - 3600_000:
        t += 86_400_000
    return t - off_ms


class ClockStart:
    """Lights out / session start from ``ExtrapolatedClock`` when no status message has it: the
    clock waits at its full value (e.g. 02:00:00, Extrapolating false) and starts running at the
    start; its posts are whole seconds -> the start follows to about ±1 s (approx)."""

    def __init__(self) -> None:
        self.full: Optional[int] = None
        self.done = False

    def feed(self, utc_ms: Optional[float], remaining: Any, extrapolating: Any) -> Optional[float]:
        from .session_phases import hms_ms
        rem = hms_ms(remaining) if isinstance(remaining, str) else None
        if self.done or utc_ms is None or rem is None:
            return None
        if extrapolating is not True:
            if self.full is None or rem > self.full:
                self.full = rem                     # waiting at the full value (or a later reset)
            if self.full - rem < 1000:
                return None
        if self.full is None or not 0 < self.full - rem <= 10_000:
            return None
        self.done = True
        return utc_ms - (self.full - rem)


def status_series_starts(series: Any) -> list[tuple[Optional[float], str]]:
    """SessionData.StatusSeries entries (list or {"2": {...}}) -> [(utc ms or None if unreadable,
    status)] for SessionStatus entries."""
    out = []
    items = series.values() if isinstance(series, dict) else series if isinstance(series, list) else []
    for e in items:
        if isinstance(e, dict) and isinstance(e.get("SessionStatus"), str):
            u = parse_utc(e.get("Utc")) if e.get("Utc") else None
            out.append((u.timestamp() * 1000 if u else None, e["SessionStatus"]))
    return out


# track status -> event label (TrackStatus "Status" codes and SessionData.StatusSeries names)
TRACK_STATUS_CODE = {"AllClear": "1", "Yellow": "2", "SCDeployed": "4", "Red": "5", "VSCDeployed": "6",
                     "VSCEnding": "7"}
TRACK_STATUS_EVENT = {"1": "TRACK CLEAR", "2": "TRACK YELLOW", "4": "SAFETY CAR DEPLOYED", "5": "RED FLAG",
                      "6": "VSC DEPLOYED", "7": "VSC ENDING"}


def rc_event_label(message: Any, category: Any) -> Optional[str]:
    """Race control messages that mark a moment visible on the TV (whole-second Utc)."""
    m, c = str(message or "").upper(), str(category or "").upper()
    if "VIRTUAL SAFETY CAR DEPLOYED" in m:
        return "VSC DEPLOYED"
    if "VIRTUAL SAFETY CAR ENDING" in m:
        return "VSC ENDING"
    if "SAFETY CAR DEPLOYED" in m:
        return "SAFETY CAR DEPLOYED"
    if "SAFETY CAR IN THIS LAP" in m:
        return "SAFETY CAR ENDING"
    if m.startswith("GREEN LIGHT - PIT EXIT OPEN"):
        return "PIT EXIT OPEN"
    if m.startswith("PIT EXIT CLOSED"):
        return "PIT EXIT CLOSED"
    if m == "RED FLAG" or (c == "FLAG" and m.startswith("RED FLAG")):
        return "RED FLAG"
    if m.startswith("CHEQUERED FLAG"):
        return "CHEQUERED FLAG"
    if m == "TRACK CLEAR":
        return "TRACK CLEAR"
    if "SESSION SUSPENDED" in m:
        return "SESSION SUSPENDED"
    if "SESSION RESUMED" in m:
        return "SESSION RESUMED"
    return None


def status_series_track(series: Any) -> list[tuple[Optional[float], str]]:
    """SessionData.StatusSeries TrackStatus entries -> [(utc ms or None, status name)]."""
    out = []
    items = series.values() if isinstance(series, dict) else series if isinstance(series, list) else []
    for e in items:
        if isinstance(e, dict) and isinstance(e.get("TrackStatus"), str):
            u = parse_utc(e.get("Utc")) if e.get("Utc") else None
            out.append((u.timestamp() * 1000 if u else None, e["TrackStatus"]))
    return out


def merge_refs(primary: Optional[RefEvents], other: Optional[RefEvents]) -> RefEvents:
    """OpenF1 + F1 archive: lap crossings from the primary (OpenF1) when it has them, start /
    finish / start-delay notices from both - one missing source never hides the other's events."""
    if primary is None and other is None:
        return RefEvents(source="none")
    if primary is None or other is None:
        return primary or other
    out = RefEvents(source=f"{primary.source}+{other.source}")
    out.crossings = primary.crossings if primary.crossings else other.crossings
    for ref in (primary, other):
        for t, src, approx in ref.start_obs:
            out.add_start(t, src, approx)
        for t in ref.finishes:
            if not any(abs(x - t) < 2500 for x in out.finishes):
                out.finishes.append(t)
        for t, m in ref.notices:
            out.add_notice(t, m)
        for t, lab, pr, src in ref.events:
            out.add_event(t, lab, pr, src)
        out.ts_points += [p for p in ref.ts_points if p not in out.ts_points]
        out.parse_errors += ref.parse_errors
    out.finishes.sort()
    out.timeline = primary.timeline or other.timeline
    return out


def ref_events_from_openf1(laps: list[dict], race_control: list[dict]) -> RefEvents:
    ev = RefEvents(source="openf1")
    by_driver: dict[str, list[dict]] = {}
    for lap in laps:
        if isinstance(lap, dict) and lap.get("driver_number") is not None:
            by_driver.setdefault(str(lap["driver_number"]), []).append(lap)
    for num, lst in by_driver.items():
        lst.sort(key=lambda x: x.get("lap_number") or 0)
        for i, lap in enumerate(lst):
            n = lap.get("lap_number")
            t = parse_utc(lap.get("date_start"))
            # the start of lap n (n >= 2) is the line crossing that completed lap n-1;
            # a pit-out lap starts in the pit lane (not a crossing on track)
            if t and isinstance(n, int) and n >= 2 and not lap.get("is_pit_out_lap"):
                ev.add_crossing(num, t.timestamp() * 1000, n - 1)
            # the last lap's completion (chequered flag / end of run)
            if i == len(lst) - 1 and t and isinstance(n, int) and lap.get("lap_duration"):
                ev.add_crossing(num, t.timestamp() * 1000 + float(lap["lap_duration"]) * 1000, n)
    for rc in race_control:
        if not isinstance(rc, dict):
            continue
        t = parse_utc(rc.get("date"))
        msg = str(rc.get("message") or "").upper()
        if not t:
            continue
        if rc.get("category") != "SessionStatus":
            ev.add_notice(t.timestamp() * 1000, msg)
            ev.race_control(t.timestamp() * 1000, msg, rc.get("category"))
            continue
        if "SESSION SUSPENDED" in msg or "SESSION ABORTED" in msg:
            ev.add_event(t.timestamp() * 1000, "SESSION SUSPENDED", True, "OpenF1 race_control")
        if "SESSION STARTED" in msg or "SESSION RESUMED" in msg:
            ev.add_start(t.timestamp() * 1000, "OpenF1 · race_control " + msg)
        elif "SESSION FINISHED" in msg:
            ev.finishes.append(t.timestamp() * 1000)
    ev.starts.sort()
    ev.finishes.sort()
    return ev


# ---------------------------------------------------------------------------
# Session detection from a VOYO page title
# ---------------------------------------------------------------------------
def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t)


# Slovenian (and English) name stems -> words found in OpenF1 meeting_name / location / circuit
MEETING_ALIASES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("avstralij", "australi", "melbourn"), ("australian", "melbourne")),
    (("kitajsk", "kitajs", "chines", "china", "sangaj", "shanghai"), ("chinese", "shanghai")),
    (("japonsk", "japan", "suzuk"), ("japanese", "suzuka")),
    (("bahrajn", "bahrain", "sakhir"), ("bahrain",)),
    (("savdsk", "savdov", "saudi", "dzed", "jeddah"), ("saudi", "jeddah")),
    (("miami",), ("miami",)),
    (("kanad", "canad", "montreal"), ("canadian", "montreal")),
    (("monak", "monaco", "monte carl"), ("monaco",)),
    (("barcelon", "katalon", "katalunij", "catalun"), ("barcelona", "catalunya")),
    (("spanij", "spansk", "spanish", "madrid"), ("spanish", "madrid")),
    (("avstrij", "austri", "spielberg"), ("austrian", "spielberg")),
    (("velike britanije", "britansk", "british", "silverston", "anglij"), ("british", "silverstone")),
    (("belgij", "belgi", "spa-franc", "spa franc"), ("belgian", "spa-francorchamps")),
    (("madzarsk", "hungar", "hungaror"), ("hungarian", "hungaroring")),
    (("nizozemsk", "dutch", "zandvoort"), ("dutch", "zandvoort")),
    (("italij", "italian", "monz"), ("italian", "monza")),
    (("emilij", "imol"), ("emilia", "imola")),
    (("azerbajdz", "azerbaij", "baku"), ("azerbaijan", "baku")),
    (("singapur", "singapor", "marina bay"), ("singapore",)),
    (("malezij", "malaysi", "kuala lumpur"), ("kuala lumpur",)),
    (("zdruzenih drzav", r"zda\b", "united states grand prix", "austin", "americas"), ("united states grand prix", "austin")),
    (("mehik", "mexic"), ("mexico",)),
    (("brazil", "sao paul", "interlagos"), ("sao paulo", "brazil", "interlagos")),
    (("las vegas", "vegas"), ("las vegas",)),
    (("katar", "qatar", "lusail"), ("qatar", "lusail")),
    (("abu dab", "abu dhab", "yas marin", "emiratov"), ("abu dhabi", "yas marina")),
]

SESSION_PATTERNS: list[tuple[str, str]] = [
    (r"sprint[ -]?(kval|quali|shootout)|kvalifikacij\w* za (sprint|sprint)|kval\w* (za )?sprint|\bsq\b", "Sprint Qualifying"),
    (r"\b(1\.?|prv\w*) ?(prost\w* )?trening|prost\w* trening ?1\b|\btrening ?1\b|\bfp ?1\b|\bpractice ?1\b|\bfree practice 1\b", "Practice 1"),
    (r"\b(2\.?|drug\w*) ?(prost\w* )?trening|prost\w* trening ?2\b|\btrening ?2\b|\bfp ?2\b|\bpractice ?2\b|\bfree practice 2\b", "Practice 2"),
    (r"\b(3\.?|tretj\w*) ?(prost\w* )?trening|prost\w* trening ?3\b|\btrening ?3\b|\bfp ?3\b|\bpractice ?3\b|\bfree practice 3\b", "Practice 3"),
    (r"\bsprint\w*\b|\bsprinterska\b", "Sprint"),
    (r"\bkvalifikacij\w*|\bqualifying\b|\bkvali\b|\bquali\b", "Qualifying"),
    (r"\bdirk[aio]\b|\bdirko\b|\brace\b|\bglavna dirka\b|\bgrand prix race\b", "Race"),
]
# Site / programme boiler plate that must not be read as a session word
# ("Glej dirke online" = "watch races online" is VOYO's generic page suffix).
TAGLINES = [r"glej\b[^|–—-]*\bonline", r"\bvoyo\b(\.si)?", r"\bposnetek\b", r"\bv zivo\b", r"\bprenos\w*\b",
            r"\bonline\b", r"\bformula ?(1|ena)\b", r"\bf1\b", r"\bfia\b"]


@dataclass
class TitleInfo:
    text: str
    meeting_words: tuple[str, ...] = ()
    meeting_hits: int = 0               # how many different Grands Prix the text names
    session_name: Optional[str] = None
    session_names: tuple[str, ...] = ()  # all session types named (more than one = ambiguous)
    year: Optional[int] = None
    date: Optional[str] = None          # YYYY-MM-DD if the metadata contains a date
    cleaned: str = ""


def clean_text(text: str) -> str:
    t = norm(text)
    t = re.sub(r"[-_/]+", " ", t)                       # url slugs: vn-azerbajdzana-dirka
    for pat in TAGLINES:
        t = re.sub(pat, " ", t)
    t = re.sub(r"\bvn\b", " velika nagrada ", t)         # VN = Velika nagrada
    t = re.sub(r"\bgp\b", " grand prix ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_title(*texts: Optional[str], published: Optional[str] = None) -> TitleInfo:
    """Normalise VOYO title / media title / og:title / URL slug (Slovenian or English)."""
    texts = tuple(dict.fromkeys(x.strip() for x in texts if x and x.strip()))      # same text twice = once
    joined = " | ".join(texts)
    t = " | ".join(clean_text(x) for x in texts)
    info = TitleInfo(text=joined[:200], cleaned=t[:200])
    hits = []
    for stems, words in MEETING_ALIASES:
        if any(re.search(r"\b" + s, t) for s in stems):             # stems are regex prefixes
            hits.append(words)
    if hits:
        info.meeting_words = hits[0]
    info.meeting_hits = len(hits)
    names = []
    for pat, name in SESSION_PATTERNS:
        if re.search(pat, t) and name not in names:
            names.append(name)
    if "Sprint Qualifying" in names:                                 # "sprint kvalifikacije" is one session
        names = [n for n in names if n not in ("Sprint", "Qualifying")]
    info.session_names = tuple(names)
    info.session_name = names[0] if len(names) == 1 else None
    m = re.search(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d\d)\b", t)
    if m:
        info.date = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        info.year = int(m.group(3))
    else:
        y = re.search(r"\b(20[1-3]\d)\b", t)
        if y:
            info.year = int(y.group(1))
    if published:
        d = parse_utc(published)
        if d is not None:
            info.published = d                                        # type: ignore[attr-defined]
    return info


@dataclass
class Detection:
    session: Optional[dict]
    reason: str
    candidates: list[dict] = field(default_factory=list)
    status: str = "failed"              # detected | ambiguous | failed
    how: str = ""                       # what the decision is based on (shown to the user)


def match_session(info: TitleInfo, sessions: list[dict], meetings: list[dict],
                  now: Optional[datetime] = None, video_seconds: Optional[float] = None,
                  bare_title_is_race: bool = True, race_min_video_seconds: float = 8100,
                  assume_recent_days: float = 21) -> Detection:
    """Which OpenF1 session a VOYO recording shows - or honestly: not sure.

    Never guesses: several Grands Prix / several session types / several seasons
    without a year -> "ambiguous" with candidates for the selector.
    A title that names only the Grand Prix is a Race only by VOYO's naming
    convention AND if the video is long enough for a race broadcast.
    """
    now = now or datetime.now(timezone.utc)
    if not info.meeting_words:
        return Detection(None, f"Grand Prix not recognised in '{info.text[:80]}'", status="failed")
    if info.meeting_hits > 1:
        return Detection(None, "the title names more than one Grand Prix", status="ambiguous")
    mnames = {m.get("meeting_key"): m.get("meeting_name") or "" for m in meetings}

    def meeting_hit(s: dict) -> bool:
        hay = " ".join([norm(mnames.get(s.get("meeting_key"), "")), norm(s.get("location") or ""),
                        norm(s.get("circuit_short_name") or "")])
        return any(w in hay for w in info.meeting_words)

    cands = []
    for s in sessions:
        start = parse_utc(s.get("date_start"))
        if not start or s.get("is_cancelled") or start > now or not meeting_hit(s):
            continue
        cands.append(s)
    cands.sort(key=lambda s: s.get("date_start") or "")
    if info.year:
        in_year = [s for s in cands if str(s.get("date_start", ""))[:4] == str(info.year)]
        if not in_year:
            return Detection(None, f"no finished {info.year} session of this Grand Prix in OpenF1 "
                             "(year in the title does not match)", cands[-8:], "failed")
        cands = in_year
    if info.date:
        cands = [s for s in cands if abs((parse_utc(s["date_start"]) -
                                          parse_utc(info.date + "T12:00:00Z")).total_seconds()) < 2 * 86400]
        if not cands:
            return Detection(None, f"no session of this Grand Prix around {info.date}", status="failed")
    pub = getattr(info, "published", None)
    if pub is not None:
        # a recording is published after its session - and not months later
        near = [s for s in cands if -3600 < (pub - parse_utc(s["date_start"])).total_seconds() < 21 * 86400]
        if near:
            cands = near
    if not cands:
        return Detection(None, "no finished OpenF1 session matches this Grand Prix", status="failed")
    how = []
    if len(info.session_names) > 1:
        return Detection(None, f"the title names several sessions ({', '.join(info.session_names)})",
                         cands[-8:], "ambiguous")
    session_name = info.session_name
    if session_name is None:
        vid = video_seconds or 0
        if bare_title_is_race and vid >= race_min_video_seconds:
            session_name = "Race"
            how.append(f"title names only the Grand Prix and the video is {vid / 3600:.1f} h long "
                       "(VOYO names race recordings after the Grand Prix)")
        else:
            why = "video length unknown" if not vid else f"video only {vid / 3600:.1f} h long"
            return Detection(None, f"session type not in the title ({why}) - choose it", cands[-8:], "ambiguous")
    else:
        how.append(f"title says {session_name}")
    typed = [s for s in cands if s.get("session_name") == session_name or
             (session_name == "Sprint Qualifying" and s.get("session_name") == "Sprint Shootout")]
    if not typed:
        return Detection(None, f"this Grand Prix has no {session_name} in OpenF1", cands[-8:], "failed")
    seasons = sorted({str(s.get("date_start", ""))[:4] for s in typed})
    chosen = typed[-1]
    season = str(chosen.get("date_start"))[:4]
    if len(seasons) > 1 and not info.year and pub is None:
        age_days = (now - parse_utc(chosen["date_start"])).total_seconds() / 86400
        if assume_recent_days and age_days <= assume_recent_days:
            how.append(f"season {season} ASSUMED: no year in the title, the {season} {session_name} was "
                       f"{age_days:.0f} day(s) ago (other seasons: {', '.join(x for x in seasons if x != season)}) "
                       "- change it with SELECT SESSION")
        else:
            return Detection(None, f"{session_name} exists in several seasons ({', '.join(seasons)}) and the "
                             "title has no year - choose the season", typed[-4:], "ambiguous")
    else:
        how.append(f"season {season}" + (" from the title" if info.year else
                                          " from the publish date" if pub else " (only season in OpenF1)"))
    return Detection(chosen, f"{mnames.get(chosen.get('meeting_key')) or chosen.get('location')} — {session_name}",
                     typed[-3:], "detected", "; ".join(how))
