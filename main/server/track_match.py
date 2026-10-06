"""Track outline from a KNOWN circuit layout, aligned to the real car positions.

Instead of drawing the circuit from one lap of car positions (gaps in the position stream
leave parts of the track missing or drawn as straight lines), the outline of the circuit is
taken from a reference layout (bacinger/f1-circuits, MIT - real circuit geometry in WGS84,
bundled in ``server/reference_tracks``) and fitted onto the positions F1 actually sends:
rotation, mirroring, scale and offset are found by a coarse search plus an ICP refinement
(2-D similarity transform, Umeyama). The result is only used when it really fits:

* the median distance of the car positions to the fitted outline is small (a few metres),
* 90 % of the positions are within ~15 m,
* the positions cover most of the outline (cars have been all around it),
* the scale is plausible (F1 units are decimetres; the reference is in metres x 10).

Otherwise nothing is drawn from it (a different layout, too little data). Pure Python, runs in
a worker thread (``asyncio.to_thread``).
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REF_DIR = Path(__file__).resolve().parent / "reference_tracks"
CELL = 300.0                       # dm (30 m) grid cell for nearest-point lookups
MEDIAN_OK = 70.0                   # dm: median distance of the positions to the outline
P90_OK = 160.0                     # dm
COVER_OK = 0.85                    # share of the outline with positions within COVER_DIST
COVER_DIST = 300.0                 # dm
SCALE_OK = (0.85, 1.18)
MIN_SAMPLES = 400


Point = tuple


# ----------------------------------------------------------------------------- reference data
def _norm(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# F1 SessionInfo names (Meeting.Circuit.ShortName / Meeting.Location) that differ from the reference index
ALIASES = {
    "sakhir": "bh-2002", "bahrain": "bh-2002", "suzuka": "jp-1962", "melbourne": "au-1953",
    "albert park": "au-1953", "shanghai": "cn-2004", "jeddah": "sa-2021", "miami": "us-2022",
    "imola": "it-1953", "monte carlo": "mc-1929", "monaco": "mc-1929", "catalunya": "es-1991",
    "barcelona": "es-1991", "montreal": "ca-1978", "spielberg": "at-1969", "silverstone": "gb-1948",
    "hungaroring": "hu-1986", "budapest": "hu-1986", "spa francorchamps": "be-1925", "spa": "be-1925",
    "zandvoort": "nl-1948", "monza": "it-1922", "baku": "az-2016", "singapore": "sg-2008",
    "marina bay": "sg-2008", "austin": "us-2012", "mexico city": "mx-1962", "interlagos": "br-1940",
    "sao paulo": "br-1940", "las vegas": "us-2023", "lusail": "qa-2004", "yas marina circuit": "ae-2009",
    "yas island": "ae-2009", "yas marina": "ae-2009", "abu dhabi": "ae-2009", "madrid": "es-2026",
    "madring": "es-2026",
}


def reference_id(*names: Optional[str]) -> Optional[str]:
    """Reference layout for a circuit, from the names F1 gives it - None if not known
    (never guessed from a partial match)."""
    index = _index()
    for name in names:
        n = _norm(name)
        if not n:
            continue
        if n in ALIASES:
            return ALIASES[n]
        for e in index:
            if n in (_norm(e.get("location")), _norm(e.get("name"))):
                return e["id"]
    return None


def circuit_location(ref_id: Optional[str]) -> Optional[tuple[float, float]]:
    """(lat, lon) of a known layout (f1-locations.json) - for the weather forecast."""
    for e in _index():
        if e.get("id") == ref_id and e.get("lat") is not None and e.get("lon") is not None:
            return float(e["lat"]), float(e["lon"])
    return None


def circuit_outline_latlon(ref_id: Optional[str], max_points: int = 120) -> Optional[list]:
    """The known layout's outline as [[lat, lon], ...] (bundled GeoJSON, real coordinates) - the
    circuit marker on the weather radar."""
    if not ref_id:
        return None
    try:
        gj = json.loads((REF_DIR / f"{ref_id}.geojson").read_text(encoding="utf-8"))
        coords = gj["features"][0]["geometry"]["coordinates"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    if coords and isinstance(coords[0][0], list):          # MultiLineString / Polygon
        coords = coords[0]
    step = max(1, len(coords) // max_points)
    return [[round(c[1], 5), round(c[0], 5)] for c in coords[::step]]


def _index() -> list:
    try:
        return json.loads((REF_DIR / "f1-locations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def reference_points(ref_id: str) -> Optional[list]:
    """The reference outline in decimetres (local east / north), one point every 5 m."""
    from .track import geometry_from_geojson
    path = REF_DIR / f"{ref_id}.geojson"
    if not path.exists():
        return None
    return [tuple(p) for p in geometry_from_geojson(path).points]


# ----------------------------------------------------------------------------- geometry
class _Grid:
    def __init__(self, pts: list) -> None:
        self.pts = pts
        self.g: dict = {}
        for i, (x, y) in enumerate(pts):
            self.g.setdefault((int(x // CELL), int(y // CELL)), []).append(i)

    def nearest(self, x: float, y: float, max_ring: int = 6) -> tuple[float, int]:
        cx, cy = int(x // CELL), int(y // CELL)
        best, bi = float("inf"), -1
        for r in range(max_ring + 1):
            for gx in range(cx - r, cx + r + 1):
                for gy in range(cy - r, cy + r + 1):
                    if max(abs(gx - cx), abs(gy - cy)) != r:
                        continue
                    for i in self.g.get((gx, gy), ()):
                        px, py = self.pts[i]
                        d = (px - x) ** 2 + (py - y) ** 2
                        if d < best:
                            best, bi = d, i
            if bi >= 0 and math.sqrt(best) <= r * CELL:
                break
        return math.sqrt(best), bi


def _apply(p: tuple, s: float, c: float, sn: float, tx: float, ty: float, mirror: bool) -> tuple:
    x, y = p
    if mirror:
        x = -x
    return (s * (c * x - sn * y) + tx, s * (sn * x + c * y) + ty)


def _umeyama(src: list, dst: list) -> tuple[float, float, float, float, float]:
    """Similarity transform (scale, cos, sin, tx, ty) mapping src onto dst (least squares)."""
    n = len(src)
    mx = sum(p[0] for p in src) / n
    my = sum(p[1] for p in src) / n
    nx = sum(p[0] for p in dst) / n
    ny = sum(p[1] for p in dst) / n
    a = b = var = 0.0
    for (x, y), (u, v) in zip(src, dst):
        x, y, u, v = x - mx, y - my, u - nx, v - ny
        a += x * u + y * v
        b += x * v - y * u
        var += x * x + y * y
    ang = math.atan2(b, a)
    s = math.hypot(a, b) / var if var else 1.0
    c, sn = math.cos(ang), math.sin(ang)
    return s, c, sn, nx - s * (c * mx - sn * my), ny - s * (sn * mx + c * my)


@dataclass
class Fit:
    ok: bool
    reason: str
    points: Optional[list] = None          # the reference outline in F1 coordinates
    median_m: Optional[float] = None
    p90_m: Optional[float] = None
    coverage: Optional[float] = None
    scale: Optional[float] = None
    mirror: Optional[bool] = None
    rotation_deg: Optional[float] = None


def _prepare(ref: list, samples: list) -> dict:
    step = max(1, len(samples) // 600)
    obs = [tuple(p) for p in samples[::step]]
    ox = sum(p[0] for p in obs) / len(obs)
    oy = sum(p[1] for p in obs) / len(obs)
    rx = sum(p[0] for p in ref) / len(ref)
    ry = sum(p[1] for p in ref) / len(ref)
    ref_c = [(x - rx, y - ry) for x, y in ref]
    r_ref = math.sqrt(sum(x * x + y * y for x, y in ref_c) / len(ref_c))
    r_obs = math.sqrt(sum((x - ox) ** 2 + (y - oy) ** 2 for x, y in obs) / len(obs))
    return {"obs": obs, "ogrid": _Grid(obs), "ox": ox, "oy": oy, "ref_c": ref_c,
            "s0": r_obs / r_ref if r_ref else 1.0, "sub": ref_c[:: max(1, len(ref_c) // 300)]}


def _coarse(P: dict) -> list:
    """(score, degrees, mirror) for every 4 degrees, mirrored and not - best first."""
    ogrid, sub, s0, ox, oy = P["ogrid"], P["sub"], P["s0"], P["ox"], P["oy"]
    cands = []
    for mirror in (False, True):
        for deg in range(0, 360, 4):
            a = math.radians(deg)
            c, sn = math.cos(a), math.sin(a)
            ds = sorted(ogrid.nearest(*_apply(p, s0, c, sn, ox, oy, mirror), max_ring=3)[0] for p in sub)
            cands.append((ds[len(ds) // 2], deg, mirror))
    cands.sort()
    return cands


def _refine(P: dict, deg: float, mirror: bool) -> tuple:
    """ICP from one start orientation until the transform settles."""
    obs, ref_c = P["obs"], P["ref_c"]
    a = math.radians(deg)
    s, c, sn, tx, ty = P["s0"], math.cos(a), math.sin(a), P["ox"], P["oy"]
    for _ in range(30):
        moved = [_apply(p, s, c, sn, tx, ty, mirror) for p in ref_c]
        mgrid = _Grid(moved)
        src, dst = [], []
        for q in obs:
            d, i = mgrid.nearest(*q)
            if i >= 0 and d < 600:
                src.append((-ref_c[i][0], ref_c[i][1]) if mirror else ref_c[i])
                dst.append(q)
        if len(src) < MIN_SAMPLES // 4:
            break
        prev = (s, c, sn, tx, ty)
        s, c, sn, tx, ty = _umeyama(src, dst)
        if abs(s - prev[0]) < 1e-4 and abs(c - prev[1]) < 1e-5 and abs(sn - prev[2]) < 1e-5 and \
                abs(tx - prev[3]) < 1 and abs(ty - prev[4]) < 1:
            break                                     # converged
    moved = [_apply(p, s, c, sn, tx, ty, mirror) for p in ref_c]
    mgrid = _Grid(moved)
    d_obs = sorted(mgrid.nearest(*q)[0] for q in obs)
    med, p90 = d_obs[len(d_obs) // 2], d_obs[int(len(d_obs) * 0.9)]
    sample = moved[::5]
    cover = sum(1 for p in sample if P["ogrid"].nearest(*p, max_ring=2)[0] <= COVER_DIST) / len(sample)
    return med, p90, cover, s, c, sn, mirror, moved


def fit_reference(ref: list, samples: list, min_cover: float = None, starts: int = 4) -> Fit:
    """Fit the reference outline ``ref`` (dm, local) onto car positions ``samples`` (F1 X/Y):
    coarse search over rotation / mirroring, then ICP (similarity transform) from the best
    ``starts`` orientations; the closest result is checked against the acceptance limits."""
    min_cover = COVER_OK if min_cover is None else min_cover
    if len(samples) < MIN_SAMPLES:
        return Fit(False, f"too few positions ({len(samples)} < {MIN_SAMPLES})")
    P = _prepare(ref, samples)
    best = None
    seen: list = []
    for _, deg, mirror in _coarse(P):
        if len(seen) >= starts:
            break
        if any(m == mirror and abs((deg - d + 180) % 360 - 180) < 20 for d, m in seen):
            continue                                   # (a neighbour of an orientation already tried)
        seen.append((deg, mirror))
        r = _refine(P, deg, mirror)
        if best is None or r[0] < best[0]:
            best = r
    med, p90, cover, s, c, sn, mirror, moved = best
    fit = Fit(False, "", [[round(p[0], 1), round(p[1], 1)] for p in moved], round(med / 10, 1),
              round(p90 / 10, 1), round(cover, 2), round(s, 3), mirror, round(math.degrees(math.atan2(sn, c)), 1))
    if not SCALE_OK[0] <= s <= SCALE_OK[1]:
        fit.reason = f"scale {s:.2f} implausible - another layout?"
    elif med > MEDIAN_OK or p90 > P90_OK:
        fit.reason = f"does not fit the positions (median {med / 10:.1f} m, 90% {p90 / 10:.1f} m) - another layout?"
    elif cover < min_cover:
        fit.reason = f"positions cover only {cover:.0%} of the outline yet"
    else:
        fit.ok = True
        fit.reason = f"fits the car positions (median {med / 10:.1f} m, 90% within {p90 / 10:.1f} m)"
    return fit


# ----------------------------------------------------------------------------- checks / search
def known_layouts() -> list[dict]:
    """[{id, name, location}] of the bundled layouts (for the "choose the circuit" menu)."""
    return [{"id": e["id"], "name": e.get("name"), "location": e.get("location")}
            for e in sorted(_index(), key=lambda e: (e.get("location") or ""))
            if (REF_DIR / f"{e['id']}.geojson").exists()]


@dataclass
class Check:
    ok: Optional[bool]          # None = not enough positions to tell
    reason: str
    median_m: Optional[float] = None
    far_share: Optional[float] = None   # positions farther than FAR_DM from the outline
    coverage: Optional[float] = None    # share of the outline with positions near it


FAR_DM = 400.0                  # 40 m off the drawn outline: that car is not on it
FAR_SHARE_BAD = 0.05            # more than 5 % of the positions off the outline -> wrong / partial outline
CHECK_MIN = 1500


def check_outline(points: list, samples: list) -> Check:
    """Does the drawn outline explain where the cars actually drive? A missing part of the
    track shows as positions far from the outline; a wrong layout as both."""
    if len(samples) < CHECK_MIN or len(points) < 10:
        return Check(None, f"not enough positions yet ({len(samples)})")
    step = max(1, len(samples) // 1500)
    obs = [tuple(p) for p in samples[::step]]
    grid = _Grid([tuple(p) for p in _densify(points)])
    ds = sorted(grid.nearest(*q)[0] for q in obs)
    far = sum(1 for d in ds if d > FAR_DM) / len(ds)
    ogrid = _Grid(obs)
    cover = sum(1 for p in points[:: max(1, len(points) // 300)]
                if ogrid.nearest(*p, max_ring=2)[0] <= COVER_DIST) / len(points[:: max(1, len(points) // 300)])
    med = ds[len(ds) // 2] / 10
    if far > FAR_SHARE_BAD:
        return Check(False, f"{far:.0%} of the car positions are more than {FAR_DM / 10:.0f} m off the drawn "
                            "track (part of it missing, or another layout)", round(med, 1), round(far, 3), round(cover, 2))
    return Check(True, f"car positions match the drawn track (median {med:.1f} m)", round(med, 1), round(far, 3),
                 round(cover, 2))


def _densify(points: list, step: float = 100.0) -> list:
    out = []
    for a, b in zip(points, points[1:]):
        n = max(1, int(math.dist(a, b) // step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    out.append(tuple(points[-1]))
    return out


def identify(samples: list, hint: Optional[str] = None) -> tuple[Optional[str], Fit]:
    """Which known layout are the cars driving on? The hinted one (from the circuit name)
    first; otherwise every bundled layout is fitted and the best one that really fits wins."""
    tried: list = []
    if hint:
        ref = reference_points(hint)
        if ref:
            f = fit_reference(ref, samples)
            if f.ok:
                return hint, f
            tried.append((hint, f))
    if len(samples) < MIN_SAMPLES:
        return None, Fit(False, "too few positions")
    step = max(1, len(samples) // 600)
    obs = samples[::step]
    ox = sum(p[0] for p in obs) / len(obs)
    oy = sum(p[1] for p in obs) / len(obs)
    r_obs = math.sqrt(sum((p[0] - ox) ** 2 + (p[1] - oy) ** 2 for p in obs) / len(obs))
    # quick ranking of every layout of a similar size by its coarse score, full fit for the best 3
    ranked = []
    for e in known_layouts():
        if e["id"] == hint:
            continue
        ref = reference_points(e["id"])
        if not ref:
            continue
        rx = sum(p[0] for p in ref) / len(ref)
        ry = sum(p[1] for p in ref) / len(ref)
        r_ref = math.sqrt(sum((p[0] - rx) ** 2 + (p[1] - ry) ** 2 for p in ref) / len(ref))
        if not r_ref or not 0.7 <= r_obs / r_ref <= 1.4:
            continue                           # a circuit of another size: not even tried
        ranked.append((_coarse(_prepare(ref, samples))[0][0], e["id"], ref))
    ranked.sort(key=lambda r: r[0])
    best: tuple = (None, Fit(False, "no known layout fits the car positions"))
    for _, rid, ref in ranked[:3]:
        f = fit_reference(ref, samples)
        if f.ok and (not best[1].ok or f.median_m < best[1].median_m):
            best = (rid, f)
    return best
