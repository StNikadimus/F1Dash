"""Pit-lane geometry for a circuit as soon as the circuit is known - from the official F1
live-timing archive, not from the pit stops of the session being watched.

Why: the pit lane is static circuit geometry, but no source this project can use publishes it
as a polyline (the MultiViewer circuit data has the track outline, corners and marshal
sectors only; the live feed has no geometry at all). What F1 does publish, for every finished
session, is the complete ``Position.z`` stream in its static archive
(``livetiming.formula1.com/static/<path>/Position.z.jsonStream``) - the same coordinate system
as the live positions and the MultiViewer outline. A race at the circuit holds ~20-60 normal
pit stops, i.e. complete passes pit entry -> pit lane -> pit exit, which server/pitlane.py turns
into a consensus centerline (trimmed median, outlier rejection, confidence). The result is
cached per circuit (``data/tracks/pitlane_geometry_<circuit_key>.json``) and used for every later
session there - the season rules of the cache (a variant from an earlier season is valid until
a different layout is verified) are those of server/pitlane.py.

So the order is:  circuit identified -> cached pit lane? -> else the most recent finished race
(then sprint, then other sessions) at this circuit from the archive -> reconstruct -> cache ->
draw. Live pit passes of the current session only *validate* it afterwards (Engine). If no
archived session gives a reliable centerline, the pit lane is reported as unavailable.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("pitlane_seed")

ARCHIVE = "https://livetiming.formula1.com/static/"
SEED_TOPICS = ["Heartbeat", "TimingData", "TimingDataF1", "PitLaneTimeCollection", "Position.z"]
TYPE_RANK = {"Race": 0, "Sprint": 1, "Qualifying": 2, "Sprint Qualifying": 3, "Sprint Shootout": 3, "Practice": 4}
MAX_SESSIONS = 3                      # archived sessions tried before giving up


async def _index(http: httpx.AsyncClient, year: int) -> dict:
    r = await http.get(f"{ARCHIVE}{year}/Index.json")
    if r.status_code != 200:
        raise RuntimeError(f"archive index {year}: HTTP {r.status_code}")
    return json.loads(r.content.decode("utf-8-sig"))


def candidate_sessions(indexes: dict[int, dict], circuit_key: int, exclude_path: Optional[str],
                       now: Optional[datetime] = None) -> list[dict]:
    """Finished sessions held at this circuit (F1's own Meeting.Circuit.Key), best first:
    races before sprints before qualifying before practice, newer before older."""
    from .sources.f1_live import _local_to_utc
    now = now or datetime.now(timezone.utc)
    out = []
    for year, idx in indexes.items():
        for m in idx.get("Meetings") or []:
            if (m.get("Circuit") or {}).get("Key") != circuit_key:
                continue
            for s in m.get("Sessions") or []:
                path = s.get("Path")
                end = _local_to_utc(s.get("EndDate"), s.get("GmtOffset"))
                if not path or path == exclude_path or end is None or end > now - timedelta(minutes=30):
                    continue
                kind = s.get("Name") if s.get("Name") in TYPE_RANK else s.get("Type")
                out.append({"path": path, "year": year, "end": end, "name": f"{m.get('Name')} {s.get('Name')}",
                            "rank": TYPE_RANK.get(kind or "", 5)})
    out.sort(key=lambda c: (c["rank"], -c["end"].timestamp()))
    return out


async def seed_pitlane(circuit_key: int, year: Optional[int], track_points: Optional[list],
                       cache_dir: Path, exclude_path: Optional[str] = None,
                       loader: Optional[Callable] = None, progress: Optional[Callable[[str], None]] = None
                       ) -> tuple[Optional[list], Optional[dict], str]:
    """-> (passes, session used, note). ``passes`` = traversals of the first archived session
    that yields a drawable reconstruction (the caller reconstructs + caches them with the
    normal pit-lane code path); (None, None, why) when nothing reliable was found."""
    from .pitlane import collect_from_events, reconstruct
    from .sources.replay import load_archive
    loader = loader or load_archive
    years = [y for y in ((year or datetime.now(timezone.utc).year), (year or datetime.now(timezone.utc).year) - 1,
                         (year or datetime.now(timezone.utc).year) - 2)]
    indexes: dict = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "f1-tv-dashboard/1.0"},
                                 follow_redirects=True) as http:
        for y in years:
            try:
                indexes[y] = await _index(http, y)
            except Exception as exc:  # noqa: BLE001
                log.info("Pit lane: F1 archive index %s not readable (%s)", y, exc)
    if not indexes:
        return None, None, "the F1 archive is not reachable"
    cands = candidate_sessions(indexes, circuit_key, exclude_path)
    if not cands:
        return None, None, f"no finished session at circuit {circuit_key} in the F1 archive ({min(years)}-{max(years)})"
    notes = []
    for c in cands[:MAX_SESSIONS]:
        if progress:
            progress(f"loading pit-lane passes from the F1 archive: {c['name']} {c['year']}")
        log.info("Pit lane circuit %s: reading the archived positions of %s %s (%s)", circuit_key, c["name"],
                 c["year"], c["path"])
        try:
            events = await loader(c["path"], cache_dir, SEED_TOPICS)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{c['name']} {c['year']}: {exc}")
            continue
        passes = await asyncio.to_thread(collect_from_events, events, track_points)
        rec = await asyncio.to_thread(reconstruct, passes, track_points is not None)
        acc = sum(1 for t in passes if t.accepted)
        if rec is not None and rec.drawable:
            log.info("Pit lane circuit %s: %d of %d archived passes usable (%s %s) -> %s",
                     circuit_key, acc, len(passes), c["name"], c["year"], rec.confidence)
            return passes, c, f"{c['name']} {c['year']}: {acc} passes, {rec.confidence}"
        notes.append(f"{c['name']} {c['year']}: {acc} usable passes - "
                     f"{rec.note if rec else 'no complete pass through the pit lane'}")
    return None, None, "; ".join(notes) or "no usable archived session"


def describe(variant: Optional[dict]) -> dict[str, Any]:
    """Short facts about a cached variant for the diagnostics (no geometry)."""
    if not variant:
        return {}
    return {k: variant.get(k) for k in ("status", "confidence", "traversals", "seasons", "source", "spread_m")}
