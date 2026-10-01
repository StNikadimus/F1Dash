#!/usr/bin/env python3
"""Pre-download circuit geometry (and optionally pit lanes) for a whole season.

    python tools/fetch_tracks.py                 # current season, geometry only
    python tools/fetch_tracks.py --year 2026 --pitlane

Geometry  : MultiViewer circuit API -> data/tracks/mv_<circuitKey>_<year>.json
Pit lanes : reconstructed from REAL archived car positions of complete passes
            through the pit lane in the most recent finished race (or other
            session) at each circuit (server/pitlane.py)
            -> data/tracks/pitlane_geometry_<circuitKey>.json

The server downloads missing geometry automatically as well; this tool only
makes the dashboard independent of the API during a race weekend.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import DATA_DIR, load_config  # noqa: E402
from server.sources.f1_live import _local_to_utc  # noqa: E402
from server.sources.replay import load_archive  # noqa: E402
from server.pitlane import collect_from_events, reconstruct  # noqa: E402
from server.track import TrackProvider  # noqa: E402

log = logging.getLogger("fetch_tracks")
ARCHIVE = "https://livetiming.formula1.com/static/"


async def season_index(year: int) -> dict:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "f1-tv-dashboard/1.0"}) as http:
        r = await http.get(f"{ARCHIVE}{year}/Index.json")
        r.raise_for_status()
        return json.loads(r.content.decode("utf-8-sig"))


async def learn_pitlane(path: str, key: int, tracks: TrackProvider, geo, year: int, name: str) -> bool:
    log.info("  reconstructing the pit lane from %s", path)
    events = await load_archive(path, DATA_DIR / "archive_cache",
                                ["Heartbeat", "TimingData", "TimingDataF1", "PitLaneTimeCollection", "Position.z"])
    passes = await asyncio.to_thread(collect_from_events, events, geo.points if geo else None)
    rec = reconstruct(passes, geo is not None)
    if rec is None or not rec.drawable:
        log.warning("  no reliable pit lane (%s)", rec.note if rec else "no complete pass through the pit lane")
        return False
    variant, what = tracks.pitcache.store(key, name, year, None, rec, f"F1 archive {path}")
    log.info("  %s: %s, %s", what, rec.confidence, rec.note)
    return variant is not None


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=datetime.now(timezone.utc).year)
    ap.add_argument("--pitlane", action="store_true", help="also learn pit lanes from archived position data")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = load_config()
    tracks = TrackProvider(DATA_DIR, cfg["tracks"])
    idx = await season_index(args.year)
    now = datetime.now(timezone.utc)
    seen: set[int] = set()
    for m in idx.get("Meetings") or []:
        circuit = (m.get("Circuit") or {})
        key = circuit.get("Key")
        if key is None or key in seen:
            continue
        seen.add(key)
        log.info("%s (%s, circuit %s)", m.get("Name"), circuit.get("ShortName"), key)
        cache = DATA_DIR / "tracks" / f"mv_{key}_{args.year}.json"
        if args.force and cache.exists():
            cache.unlink()
        geo = await tracks.load(key, args.year, circuit.get("ShortName"))
        log.info("  geometry: %s", f"{len(geo.points)} points ({geo.source})" if geo else "NOT AVAILABLE")

        if args.pitlane:
            cached = tracks.pitcache.lookup(key, args.year)
            if cached and cached.get("status") == "verified" and args.year in (cached.get("seasons") or []) \
                    and not args.force:
                log.info("  pit lane already cached (%s)", cached.get("confidence"))
                continue
            done = [s for s in m.get("Sessions") or []
                    if s.get("Path") and (_local_to_utc(s.get("EndDate"), s.get("GmtOffset")) or now) < now]
            done.sort(key=lambda s: (s.get("Type") == "Race", s.get("EndDate") or ""), reverse=True)
            for s in done[:2]:
                try:
                    if await learn_pitlane(s["Path"], key, tracks, geo, args.year, circuit.get("ShortName") or ""):
                        break
                except Exception as exc:  # noqa: BLE001
                    log.warning("  failed: %s", exc)
            else:
                if not done:
                    log.info("  no finished session yet - pit lane will be learned live")


if __name__ == "__main__":
    asyncio.run(main())
