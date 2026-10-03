"""Circuit geometry.

Sources, in order of preference:

1. MultiViewer circuit API (``https://api.multiviewer.app/api/v1/circuits/<key>/<year>``).
   This is the dataset FastF1 uses. Its x/y arrays are in exactly the same
   coordinate system as the live ``Position.z`` data, so car positions can be
   drawn on it without any fitting. It also contains the marshal sectors that
   race control refers to in "YELLOW IN TRACK SECTOR n" messages.
   Downloaded files are cached in ``data/tracks/mv_<key>_<year>.json``.

2. A KNOWN layout of the circuit (bacinger/f1-circuits, bundled) fitted onto the real
   car positions (server/track_match.py) -> ``data/tracks/ref_<key>.json``. Only used when
   it fits the positions within a few metres all around the lap.

3. Outline learned from one clean lap of real position data (last resort) ->
   ``data/tracks/learned_<key>.json``; only kept if it is a closed loop of a plausible
   length without gaps (a gap in the position stream would be drawn as a straight line).

"Track map wrong" (dashboard button / key W twice / POST /api/track/report) deletes the
cached outline of that circuit - never its pit lane - marks the source shown as rejected
for it (``data/tracks/track_reports.json``) and builds the outline again.

Pit lane: the MultiViewer data has no pit-lane polyline, so the pit lane is
reconstructed from real car positions of complete passes through the pit lane
(server/pitlane.py) and cached per circuit -> ``data/tracks/pitlane_geometry_<key>.json``.
It is never drawn from guesses.

Test mode uses real circuit outlines from the bacinger/f1-circuits GeoJSON
collection (MIT licence) in ``data/test_tracks``.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("track")

Point = list  # [x, y]


@dataclass
class TrackGeometry:
    circuit_key: Optional[int]
    name: str
    year: Optional[int]
    source: str                                   # multiviewer | learned | test
    points: list[Point]
    rotation: float = 0.0
    corners: list[dict[str, Any]] = field(default_factory=list)
    marshal_sectors: list[dict[str, Any]] = field(default_factory=list)   # {n, start, end}
    pitlane: Optional[list[Point]] = None
    pitlane_source: Optional[str] = None
    pitlane_info: dict = field(default_factory=dict)   # state / confidence / status / passes (pitlane.pit_info)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _nearest_index(points: list[Point], x: float, y: float) -> int:
    best, best_d = 0, float("inf")
    for i, (px, py) in enumerate(points):
        d = (px - x) ** 2 + (py - y) ** 2
        if d < best_d:
            best, best_d = i, d
    return best


def _polyline_length(points: list[Point]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def sector_ranges(points: list[Point], sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map marshal-sector start markers onto polyline index ranges."""
    marks = []
    for s in sectors:
        try:
            n = int(s["n"])
        except (KeyError, TypeError, ValueError):
            continue
        marks.append((n, _nearest_index(points, s["x"], s["y"])))
    marks.sort()
    out = []
    for i, (n, start) in enumerate(marks):
        end = marks[(i + 1) % len(marks)][1]
        out.append({"n": n, "start": start, "end": end})
    return out


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)


# --------------------------------------------------------------------------
# MultiViewer data
# --------------------------------------------------------------------------

def geometry_from_multiviewer(data: dict[str, Any], key: int, year: Optional[int]) -> Optional[TrackGeometry]:
    xs, ys = data.get("x") or [], data.get("y") or []
    if not xs or len(xs) != len(ys):
        return None
    points = [[float(x), float(y)] for x, y in zip(xs, ys)]
    corners = []
    for c in data.get("corners") or []:
        tp = c.get("trackPosition") or {}
        corners.append({"n": f"{c.get('number', '')}{c.get('letter', '') or ''}",
                        "x": float(tp.get("x", 0)), "y": float(tp.get("y", 0)),
                        "angle": float(c.get("angle", 0) or 0)})
    ms_marks = []
    for s in data.get("marshalSectors") or []:
        tp = s.get("trackPosition") or {}
        ms_marks.append({"n": s.get("number"), "x": float(tp.get("x", 0)), "y": float(tp.get("y", 0))})
    return TrackGeometry(
        circuit_key=key,
        name=data.get("circuitName") or data.get("location") or f"Circuit {key}",
        year=year,
        source="multiviewer",
        points=points,
        rotation=float(data.get("rotation", 0) or 0),
        corners=corners,
        marshal_sectors=sector_ranges(points, ms_marks) if ms_marks else [],
    )


# --------------------------------------------------------------------------
# GeoJSON (test mode)
# --------------------------------------------------------------------------

def _resample(points: list[Point], step: float) -> list[Point]:
    out = [points[0]]
    carry = 0.0
    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        seg = math.dist((x0, y0), (x1, y1))
        if seg == 0:
            continue
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t])
            d += step
        carry = seg - (d - step)
    return out


def geometry_from_geojson(path: Path) -> TrackGeometry:
    gj = json.loads(path.read_text())
    feat = gj["features"][0] if gj.get("type") == "FeatureCollection" else gj
    geom = feat["geometry"]
    coords = geom["coordinates"]
    if geom["type"] == "MultiLineString":
        coords = max(coords, key=len)
    lat0 = sum(c[1] for c in coords) / len(coords)
    lon0 = sum(c[0] for c in coords) / len(coords)
    k = math.cos(math.radians(lat0))
    # decimetres, like the F1 position feed
    pts = [[(lon - lon0) * 111320.0 * k * 10.0, (lat - lat0) * 110540.0 * 10.0] for lon, lat, *_ in coords]
    if math.dist(pts[0], pts[-1]) > 1:
        pts.append(list(pts[0]))
    pts = _resample(pts, 50.0)            # one point every 5 m
    props = feat.get("properties") or {}
    return TrackGeometry(circuit_key=None, name=props.get("Name") or path.stem, year=None,
                         source="test", points=pts)


# --------------------------------------------------------------------------
# learning from real positions
# --------------------------------------------------------------------------

class OutlineLearner:
    """Records one complete, pit-free lap of one car as the track outline."""

    def __init__(self) -> None:
        self.recording: dict[str, dict[str, Any]] = {}
        self.result: Optional[list[Point]] = None

    def observe(self, num: str, x: float, y: float, laps: Optional[int], in_pit: bool) -> None:
        if self.result is not None or laps is None:
            return
        rec = self.recording.get(num)
        if rec is None or rec["lap"] != laps:
            if rec is not None and not rec["dirty"] and rec["lap"] == laps - 1 and len(rec["pts"]) > 150:
                self.result = rec["pts"]
                return
            self.recording[num] = {"lap": laps, "pts": [[x, y]], "dirty": in_pit or rec is None}
            return
        if in_pit:
            rec["dirty"] = True
        if math.dist(rec["pts"][-1], (x, y)) > 20:
            rec["pts"].append([x, y])


OUTLINE_MIN_DM, OUTLINE_MAX_DM = 25_000, 80_000    # 2.5 - 8 km
OUTLINE_MAX_GAP_DM = 600                          # 60 m between two recorded points = data missing
OUTLINE_CLOSE_DM = 800                            # end of the lap within 80 m of its start


def outline_problem(points: list) -> Optional[str]:
    """Why a learned outline must not be drawn (None = it is a plausible full lap)."""
    if len(points) < 150:
        return f"only {len(points)} points"
    length = _polyline_length(points)
    if not OUTLINE_MIN_DM <= length <= OUTLINE_MAX_DM:
        return f"length {length / 10:.0f} m is not a full lap"
    gap = max(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))
    if gap > OUTLINE_MAX_GAP_DM:
        return f"a {gap / 10:.0f} m gap in the positions (part of the lap missing)"
    if math.dist(points[0], points[-1]) > OUTLINE_CLOSE_DM:
        return f"not a closed loop (start and end {math.dist(points[0], points[-1]) / 10:.0f} m apart)"
    return None


class TrackProvider:
    def __init__(self, data_dir: Path, cfg: dict[str, Any]) -> None:
        self.dir = data_dir / "tracks"
        self.test_dir = data_dir / "test_tracks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.api_url: str = cfg.get("api_url", "")
        self.learn_pitlane = bool(cfg.get("learn_pitlane", True))
        self.learn_outline = bool(cfg.get("learn_outline", True))
        self._failed: dict[tuple, float] = {}
        from .pitlane import PitLaneCache
        self.pitcache = PitLaneCache(self.dir)

    # ---- loading ---------------------------------------------------------
    async def load(self, circuit_key: Optional[int], year: Optional[int], name: Optional[str]) -> Optional[TrackGeometry]:
        if circuit_key is None:
            return None
        geo = None
        rejected = self.rejected(circuit_key)
        cache = self.dir / f"mv_{circuit_key}_{year}.json"
        if "multiviewer" not in rejected:
            if cache.exists():
                geo = self._from_mv_file(cache, circuit_key, year)
            if geo is None:
                geo = await self._download(circuit_key, year, cache)
            if geo is None:
                # any other cached year of the same circuit
                others = sorted(self.dir.glob(f"mv_{circuit_key}_*.json"), reverse=True)
                for p in others:
                    geo = self._from_mv_file(p, circuit_key, year)
                    if geo:
                        log.warning("Using cached geometry %s for circuit %s/%s", p.name, circuit_key, year)
                        break
        ref = self.dir / f"ref_{circuit_key}.json"
        if geo is None and "reference" not in rejected and ref.exists():
            try:
                d = json.loads(ref.read_text())
                geo = TrackGeometry(circuit_key=circuit_key, name=d.get("name") or name or "", year=year,
                                    source="reference", points=d["points"])
                geo.pitlane_info = {}
                log.info("Loaded the known layout %s aligned to car positions for circuit %s (%s)",
                         d.get("ref_id"), circuit_key, d.get("fit"))
            except (OSError, ValueError, KeyError):
                log.warning("Unreadable %s - ignored", ref.name)
        learned = self.dir / f"learned_{circuit_key}.json"
        if geo is None and "learned" not in rejected and learned.exists():
            d = json.loads(learned.read_text())
            why = outline_problem(d.get("points") or [])
            if why:
                log.warning("Learned outline for circuit %s not used: %s", circuit_key, why)
            else:
                geo = TrackGeometry(circuit_key=circuit_key, name=d.get("name") or name or "", year=year,
                                    source="learned", points=d["points"])
                log.info("Loaded learned outline for circuit %s", circuit_key)
        if geo is None:
            log.warning("No geometry available for circuit %s (%s)", circuit_key, name)
            return None
        if name and geo.source != "multiviewer":
            geo.name = name
        self.attach_pitlane(geo, year)
        return geo

    def _from_mv_file(self, path: Path, key: int, year: Optional[int]) -> Optional[TrackGeometry]:
        try:
            return geometry_from_multiviewer(json.loads(path.read_text()), key, year)
        except Exception:  # noqa: BLE001
            log.exception("Corrupt geometry cache %s", path)
            return None

    async def _download(self, key: int, year: Optional[int], cache: Path) -> Optional[TrackGeometry]:
        if not self.api_url or year is None:
            return None
        k = (key, year)
        if time.monotonic() - self._failed.get(k, -1e9) < 600:
            return None
        url = self.api_url.format(circuit_key=key, year=year)
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "f1-tv-dashboard/1.0"},
                                         follow_redirects=True) as client:
                r = await client.get(url)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
            geo = geometry_from_multiviewer(data, key, year)
            if geo is None:
                raise RuntimeError("no x/y data in response")
            _write_json(cache, data)
            log.info("Downloaded circuit geometry %s/%s (%s, %d points)", key, year, geo.name, len(geo.points))
            return geo
        except Exception as exc:  # noqa: BLE001
            self._failed[k] = time.monotonic()
            log.warning("Circuit geometry download failed for %s/%s from %s: %s", key, year, url, exc)
            return None

    def load_test(self, name: str) -> TrackGeometry:
        path = self.test_dir / f"{name}.geojson"
        if not path.exists():
            files = sorted(self.test_dir.glob("*.geojson"))
            if not files:
                raise FileNotFoundError(f"No test tracks in {self.test_dir}")
            log.warning("Test track %s not found, using %s", name, files[0].stem)
            path = files[0]
        return geometry_from_geojson(path)

    # ---- pit lane (cache-first, see server/pitlane.py) -------------------------
    def attach_pitlane(self, geo: TrackGeometry, year: Optional[int], variant: Optional[dict] = None,
                       state: Optional[str] = None, extra: Optional[dict] = None) -> Optional[dict]:
        """Cached pit lane of this circuit for this season (or the given variant) onto ``geo``.
        Only HIGH/MEDIUM geometry is ever attached; nothing is drawn otherwise."""
        from .pitlane import pit_info
        if variant is None and state is None:
            variant = self.pitcache.lookup(geo.circuit_key, year)
            state = "cached" if variant else "none"
        if variant:
            geo.pitlane = [list(p) for p in variant["centerline"]]
            geo.pitlane_source = (f"reconstructed from {variant.get('traversals')} pit-lane pass(es) - "
                                  f"{variant.get('confidence')}, {variant.get('status')}")
        else:
            geo.pitlane, geo.pitlane_source = None, None
        geo.pitlane_info = pit_info(variant, state or ("cached" if variant else "none"), extra)
        return variant

    # ---- "track map wrong" -------------------------------------------------------
    def _reports_path(self) -> Path:
        return self.dir / "track_reports.json"

    def _reports(self) -> dict:
        try:
            return json.loads(self._reports_path().read_text())
        except (OSError, ValueError):
            return {}

    def rejected(self, key: Optional[int]) -> set:
        return set((self._reports().get(str(key)) or {}).get("rejected") or [])

    def reset_circuit(self, key: int, source: Optional[str]) -> list[str]:
        """Forget the outline of one circuit (all cached outline files) and never use the
        source that was shown again for it - the pit lane cache is NOT touched. When every
        source has been rejected, the list starts over (no dead end)."""
        removed = []
        for pat in (f"mv_{key}_*.json", f"ref_{key}.json", f"learned_{key}.json"):
            for p in self.dir.glob(pat):
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    log.warning("Could not delete %s", p)
        reports = self._reports()
        ent = reports.setdefault(str(key), {"rejected": []})
        if source and source not in ent["rejected"]:
            ent["rejected"].append(source)
        if {"multiviewer", "reference", "learned"} <= set(ent["rejected"]):
            ent["rejected"] = [source] if source else []
        ent["reported_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_json(self._reports_path(), reports)
        self._failed.clear()
        log.warning("Track map of circuit %s reported wrong (%s): deleted %s; rejected sources now: %s",
                    key, source, ", ".join(removed) or "nothing cached", ", ".join(ent["rejected"]) or "none")
        return removed

    def save_reference(self, key: int, name: str, ref_id: str, fit) -> TrackGeometry:
        _write_json(self.dir / f"ref_{key}.json", {"name": name, "ref_id": ref_id, "points": fit.points,
                                                   "fit": fit.reason, "median_m": fit.median_m, "p90_m": fit.p90_m,
                                                   "scale": fit.scale, "mirror": fit.mirror,
                                                   "rotation_deg": fit.rotation_deg})
        log.info("Saved the known layout %s aligned to car positions for circuit %s (%s)", ref_id, key, fit.reason)
        return TrackGeometry(circuit_key=key, name=name, year=None, source="reference", points=fit.points)

    def save_outline(self, key: int, name: str, points: list[Point]) -> TrackGeometry:
        _write_json(self.dir / f"learned_{key}.json", {"name": name, "points": points})
        log.info("Saved learned track outline for circuit %s (%d points)", key, len(points))
        return TrackGeometry(circuit_key=key, name=name, year=None, source="learned", points=points)
