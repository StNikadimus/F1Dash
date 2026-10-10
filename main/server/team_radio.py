"""Team radio: the radio clips F1 publishes for a session, for the dashboard's TEAM RADIO panel.

Where the clips come from (docs: main/README.md "Team radio"):

* the official live-timing ``TeamRadio`` topic - live on the existing SignalR connection (whatever F1 sends
  to it; F1 TV Access does not officially include live team radio), and in replays / VOD from the session's
  archive ``TeamRadio.jsonStream`` (fed in step with the video, so nothing arrives before it was published);
  each capture is ``{"Utc", "RacingNumber", "Path": "TeamRadio/<file>.mp3"}``, the file lives at
  ``https://livetiming.formula1.com/static/<SessionInfo.Path><Path>``;
* OpenF1 ``/v1/team_radio`` - only as a fallback for archived sessions whose F1 archive has no TeamRadio
  stream; its ``recording_url`` must point to the same F1 archive (anything else is ignored).

Everything that comes from outside is untrusted: session paths, clip paths, driver numbers and times are
validated, the browser never receives a URL to follow - it plays ``/api/radio/audio/<id>``, which this
server fetches only from the configured archive host, for a clip of the current session, size-limited and
checked to be an audio file. Nothing is written to disk (a small in-memory cache for seeking / replays).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .telemetry import parse_utc

log = logging.getLogger("team_radio")

DEFAULT_ARCHIVE = "https://livetiming.formula1.com/static/"
# SessionInfo.Path, e.g. "2024/2024-03-02_Bahrain_Grand_Prix/2024-03-02_Race/"
SESSION_PATH_RE = re.compile(r"^\d{4}/[A-Za-z0-9_.\-]{1,120}/[A-Za-z0-9_.\-]{1,120}/$")
# a capture's Path, e.g. "TeamRadio/MAXVER01_1_20240302_152342.mp3"
CLIP_PATH_RE = re.compile(r"^TeamRadio/[A-Za-z0-9_\-]{1,120}\.mp3$")
DRIVER_RE = re.compile(r"^\d{1,3}$")
CLIP_ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_CLIPS = 2000                                     # a race has ~50-200; anything far beyond is not real


def _items(v: Any) -> list:
    """F1 lists sometimes arrive as dicts keyed "0", "1", ... - an ordered list."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        pairs = []
        for k, x in v.items():
            try:
                pairs.append((int(k), x))
            except (TypeError, ValueError):
                continue
        return [x for _, x in sorted(pairs, key=lambda p: p[0])]
    return []


def _utc(v: Any) -> Optional[str]:
    """F1's Utc ("2024-03-02T15:23:42.1240000Z" / OpenF1's "+00:00") -> ISO UTC with Z, or None."""
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        t = parse_utc(v)
    except (ValueError, TypeError):
        return None
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(timezone.utc)
    if not 2010 <= t.year <= 2100:
        return None
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def valid_session_path(p: Any) -> Optional[str]:
    return p if isinstance(p, str) and SESSION_PATH_RE.match(p) and ".." not in p else None


def clip_id(session_path: str, clip_path: str) -> str:
    return hashlib.sha256(f"{session_path}|{clip_path}".encode()).hexdigest()[:16]


def parse_captures(team_radio: Any, session_path: Any) -> list[dict]:
    """The TeamRadio topic state -> clips, oldest first, each once. Invalid entries are skipped; an entry
    with no file path at all is kept only if it has a driver and a time (shown, but not playable)."""
    caps = _items(team_radio.get("Captures")) if isinstance(team_radio, dict) else []
    sp = valid_session_path(session_path)
    out: dict[str, dict] = {}
    for c in caps[-MAX_CLIPS:]:
        if not isinstance(c, dict):
            continue
        utc = _utc(c.get("Utc"))
        drv = str(c.get("RacingNumber") or "").strip()
        drv = drv if DRIVER_RE.match(drv) else None
        raw_path = c.get("Path")
        path = raw_path if isinstance(raw_path, str) and CLIP_PATH_RE.match(raw_path) else None
        if raw_path not in (None, "") and path is None:
            continue                                   # a path that is not an archive clip: bad data, skipped
        if path is None and (utc is None or drv is None):
            continue
        src = "openf1" if c.get("_src") == "openf1" else "feed"
        cid = clip_id(sp or "-", path or f"{drv}|{utc}")
        if cid in out:
            continue                                   # the same clip again (reconnect snapshot, duplicates)
        out[cid] = {"id": cid, "utc": utc, "driver": drv, "src": src, "playable": bool(path and sp),
                    "file": path.split("/", 1)[1] if path else None}
    clips = sorted(out.values(), key=lambda x: (x["utc"] or "9999", x["id"]))
    known = transcripts.all() if transcripts is not None else {}
    for c in clips:
        t = known.get(c["id"])
        if t:
            c["transcript"] = t
    return clips


def openf1_captures(rows: Any, session_path: str, archive_base: str = DEFAULT_ARCHIVE) -> list[tuple[str, dict]]:
    """OpenF1 /team_radio rows of one session -> [(utc, capture)] in the F1 feed's capture form (marked
    "_src": "openf1"). Only recording URLs on the F1 archive under this session's path are accepted."""
    sp = valid_session_path(session_path)
    base = urlparse(archive_base)
    if sp is None or not isinstance(rows, list):
        return []
    prefix = base.path.rstrip("/") + "/" + sp
    out = []
    for r in rows[:MAX_CLIPS]:
        if not isinstance(r, dict):
            continue
        u = r.get("recording_url")
        if not isinstance(u, str) or len(u) > 400:
            continue
        p = urlparse(u)
        if p.scheme != "https" or p.netloc != base.netloc or p.query or p.fragment or not p.path.startswith(prefix):
            continue
        rel = p.path[len(prefix):]
        utc = _utc(r.get("date"))
        drv = str(r.get("driver_number") or "").strip()
        if not CLIP_PATH_RE.match(rel) or utc is None or not DRIVER_RE.match(drv):
            continue
        out.append((utc, {"Utc": utc, "RacingNumber": drv, "Path": rel, "_src": "openf1"}))
    out.sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------------------------------------
# audio: fetched from the archive only on request, for a clip of the current session
# ---------------------------------------------------------------------------------------------
class RadioAudioError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _looks_like_mp3(b: bytes) -> bool:
    return b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)


class RadioAudio:
    def __init__(self, archive_base: str = DEFAULT_ARCHIVE, cache_mb: float = 12, max_clip_mb: float = 8,
                 timeout_s: float = 10, client_factory=None) -> None:
        self.base = archive_base if archive_base.endswith("/") else archive_base + "/"
        self.host = urlparse(self.base).netloc
        self.cache: "OrderedDict[str, bytes]" = OrderedDict()
        self.cache_bytes = int(cache_mb * 1024 * 1024)
        self.max_clip = int(max_clip_mb * 1024 * 1024)
        self.timeout = timeout_s
        self.failed: dict[str, tuple[float, RadioAudioError]] = {}    # url -> (until, error): no hammering
        self._sem = asyncio.Semaphore(2)
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False, headers={"User-Agent": "f1-tv-dashboard/1.0"}))

    def url(self, session_path: str, file: str) -> str:
        return f"{self.base}{session_path}TeamRadio/{file}"

    async def get(self, url: str) -> bytes:
        p = urlparse(url)
        if p.netloc != self.host or not url.startswith(self.base) or not url.endswith(".mp3"):
            raise RadioAudioError(400, "not an archive recording")
        hit = self.cache.get(url)
        if hit is not None:
            self.cache.move_to_end(url)
            return hit
        until = self.failed.get(url)
        if until and until[0] > time.monotonic():
            raise until[1]
        async with self._sem:
            try:
                data = await self._fetch(url)
            except RadioAudioError as exc:
                if exc.status in (404, 410, 403, 502):
                    self.failed[url] = (time.monotonic() + 300, exc)
                    if len(self.failed) > 500:
                        self.failed.pop(next(iter(self.failed)))
                raise
        self.cache[url] = data
        while sum(len(v) for v in self.cache.values()) > self.cache_bytes and len(self.cache) > 1:
            self.cache.popitem(last=False)
        return data

    async def _fetch(self, url: str) -> bytes:
        try:
            async with self._client_factory() as http:
                async with http.stream("GET", url) as r:
                    if r.status_code in (404, 410):
                        raise RadioAudioError(404, f"recording not available (F1 archive answered {r.status_code})")
                    if r.status_code in (401, 403):
                        raise RadioAudioError(403, f"recording not accessible (F1 archive answered {r.status_code})")
                    if r.status_code == 429:
                        raise RadioAudioError(503, "F1 archive is rate limiting - try again later")
                    if r.status_code != 200:
                        raise RadioAudioError(502, f"F1 archive answered {r.status_code}")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not (ctype.startswith("audio/") or ctype in ("application/octet-stream",
                                                                              "binary/octet-stream")):
                        raise RadioAudioError(502, "the archive did not return audio")
                    if int(r.headers.get("content-length") or 0) > self.max_clip:
                        raise RadioAudioError(502, "recording too large")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self.max_clip:
                            raise RadioAudioError(502, "recording too large")
        except httpx.TimeoutException:
            raise RadioAudioError(504, "F1 archive did not answer in time") from None
        except httpx.HTTPError as exc:
            raise RadioAudioError(502, f"F1 archive not reachable ({type(exc).__name__})") from None
        data = bytes(buf)
        if not data or not _looks_like_mp3(data):
            raise RadioAudioError(502, "the archive file is not an MP3 recording")
        return data


def byte_range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """One "bytes=a-b" range -> (start, end) inclusive, or None (whole file). Raises ValueError if unsatisfiable."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                raise ValueError
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        raise ValueError("bad range") from None
    end = min(end, size - 1)
    if start > end or start >= size:
        raise ValueError("unsatisfiable")
    return start, end


# ---------------------------------------------------------------------------------------------
# optional transcripts (main/tools/transcribe_radio.py on a PC with a GPU): AI text, never official
# ---------------------------------------------------------------------------------------------
class TranscriptStore:
    MAX_TEXT = 2000

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._mtime = None

    def _load(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            self._data, self._mtime = {}, None
            return
        if m == self._mtime:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = {k: v for k, v in (d.get("clips") or {}).items()
                          if CLIP_ID_RE.match(str(k)) and isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            self._data = {}
        self._mtime = m

    def get(self, cid: str) -> Optional[dict]:
        return self.all().get(cid)

    def all(self) -> dict[str, dict]:
        """All transcripts (read-only; reloaded when the file changed)."""
        with self._lock:
            self._load()
            return self._data

    def put(self, cid: str, text: str, model: str, lang: Optional[str], confidence: Optional[float]) -> dict:
        if not CLIP_ID_RE.match(cid or ""):
            raise ValueError("bad clip id")
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or "")).strip()
        if not text or len(text) > self.MAX_TEXT:
            raise ValueError("empty or too long text")
        model = re.sub(r"[^A-Za-z0-9_.\-/ ]", "", str(model or ""))[:60] or "unknown"
        rec = {"text": text, "kind": "ai", "model": model,
               "lang": re.sub(r"[^a-z]", "", str(lang or ""))[:5] or None,
               "confidence": round(max(0.0, min(1.0, float(confidence))), 2) if confidence is not None else None,
               "created": int(time.time())}
        with self._lock:
            self._load()
            self._data[cid] = rec
            if len(self._data) > 20000:                   # bounded: the oldest go
                for k in sorted(self._data, key=lambda k: self._data[k].get("created", 0))[:2000]:
                    del self._data[k]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "clips": self._data}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime
        return rec


transcripts: Optional[TranscriptStore] = None       # set by the app when [team_radio] transcripts = true
