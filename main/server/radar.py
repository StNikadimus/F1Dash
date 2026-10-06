"""Weather radar for the weather report: precipitation around the circuit, its intensity and
movement, and an estimated rain ETA.

Two layers, each from a documented API without a key, each labelled with its source:

* **Radar imagery: RainViewer** (``api.rainviewer.com/public/weather-maps.json``). This is the
  global composite of national weather radars: past frames every 10 min, plus nowcast frames when
  the API lists them. The dashboard draws its map tiles centred on the circuit, so this is the
  observed radar picture. RainViewer publishes colour images, not numbers, so they are only drawn,
  never measured.
* **Numbers: Open-Meteo 15-minute precipitation** on an N x N grid of points around the
  circuit, with ``past_minutely_15`` and ``forecast_minutely_15`` (mm per 15 min -> mm/h). The
  intensity, movement and ETA come from this grid only (it is also drawn when there is no
  RainViewer imagery: TEST mode, RainViewer unreachable). Open-Meteo's 15-min values come from
  its high-resolution nowcast models, not from radar, and the source line says so.

Analysis (``analyse``):
* intensity at the circuit = the strongest cell within 0.75 grid steps of the centre, mapped with
  the weather report's thresholds (NONE / DRIZZLE / LIGHT / MEDIUM / HEAVY);
* movement = the intensity-weighted centre of the rain cells over the past frames:
  APPROACHING / MOVING AWAY (its distance to the circuit changes >= MOVE_KM), OVER CIRCUIT,
  FORMING / DISSIPATING (rain area grows / shrinks >= 50 % without moving), STATIONARY;
* rain ETA from two independent estimates: (a) the first forecast frame with >= LIGHT at the
  circuit (15-min steps), (b) the nearest rain cell's distance / its approach speed. Both present
  and within 20 min -> ETA = their mean, confidence 0.8; one -> 0.5; they disagree -> UNCERTAIN.
  Nothing supports an ETA -> None (never invented).

Cache: the grid ``refresh_seconds`` (180 s), the RainViewer frame list 300 s. A failed refresh
keeps the last data while it is younger than ``max_age_seconds`` (900 s), labelled with its age.
Older data is not shown. A recording (replay / VOD) gets no radar: the radar shows now, not the
race being watched.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from statistics import mean
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("radar")

RAINVIEWER = "https://api.rainviewer.com/public/weather-maps.json"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
UA = "f1-tv-dashboard/1.0 (self-hosted F1 timing dashboard)"
STEP_MIN = 15
MOVE_KM = 8.0             # the rain's distance to the circuit changes this much: it moves
DEFAULTS = {"enabled": True, "radius_km": 100, "grid_points": 9, "animation": True, "history_minutes": 30,
            "forecast_minutes": 30, "refresh_seconds": 180, "max_age_seconds": 900, "max_zoom": 7}


def _km_per_deg_lon(lat: float) -> float:
    return 111.32 * math.cos(math.radians(lat))


def grid_points(lat: float, lon: float, radius_km: float, n: int) -> list[tuple[float, float, float, float]]:
    """Row-major north -> south, west -> east: (lat, lon, east km, north km)."""
    step = 2 * radius_km / (n - 1)
    pts = []
    for i in range(n):
        north = radius_km - i * step
        for j in range(n):
            east = -radius_km + j * step
            pts.append((lat + north / 111.32, lon + east / _km_per_deg_lon(lat), east, north))
    return pts


def parse_grid(data: Any, n: int) -> list[dict]:
    """Open-Meteo multi-location minutely_15 -> frames [{"t": interval start ms, "cells": [mm/h]}]."""
    locs = data if isinstance(data, list) else [data]
    if len(locs) != n * n:
        raise ValueError(f"{len(locs)} grid points instead of {n * n}")
    times = ((locs[0] or {}).get("minutely_15") or {}).get("time") or []
    frames = []
    for k, t in enumerate(times):
        cells = []
        for loc in locs:
            v = (((loc or {}).get("minutely_15") or {}).get("precipitation") or [None] * len(times))
            x = v[k] if k < len(v) else None
            cells.append(None if x is None else round(float(x) * 60 / STEP_MIN, 2))
        frames.append({"t": (int(t) - STEP_MIN * 60) * 1000, "cells": cells})
    return frames


def analyse(frames: list[dict], now_ms: float, n: int, radius_km: float, th: dict) -> dict:
    """Intensity at the circuit, movement and rain ETA from the grid (see the module doc)."""
    from .weather import intensity
    step = 2 * radius_km / (n - 1)
    pos = [(-radius_km + (k % n) * step, radius_km - (k // n) * step) for k in range(n * n)]   # (east, north)
    near = [k for k, (e, no) in enumerate(pos) if math.hypot(e, no) <= 0.75 * step + 1e-6]

    def at_circuit(f):
        vals = [f["cells"][k] for k in near if f["cells"][k] is not None]
        return max(vals) if vals else None

    def blob(f):
        wet = [(k, v) for k, v in enumerate(f["cells"]) if v is not None and v >= th["drizzle_mm_h"]]
        if not wet:
            return None
        w = sum(v for _, v in wet)
        return (sum(pos[k][0] * v for k, v in wet) / w, sum(pos[k][1] * v for k, v in wet) / w, len(wet),
                min(math.hypot(*pos[k]) for k, _ in wet))
    frames = sorted(frames, key=lambda f: f["t"])
    past = [f for f in frames if f["t"] <= now_ms]
    fut = [f for f in frames if f["t"] > now_ms]
    cur = past[-1] if past else (frames[0] if frames else None)
    out: dict = {"current_intensity": None, "area_max_intensity": None, "movement": None,
                 "rain_eta_minutes": None, "eta_label": None, "confidence": None, "direction_deg": None,
                 "speed_kmh": None, "nearest_rain_km": None}
    if cur is None:
        return out
    c_now = at_circuit(cur)
    out["current_intensity"] = intensity(c_now, th) if c_now is not None else None
    vals = [v for v in cur["cells"] if v is not None]
    out["area_max_intensity"] = intensity(max(vals), th) if vals else None
    b_now = blob(cur)
    out["nearest_rain_km"] = round(b_now[3]) if b_now else None
    # movement over the past frames
    hist = [(f["t"], blob(f)) for f in past if blob(f)]
    approach_kmh = None
    if len(hist) >= 2:
        (t0, b0), (t1, b1) = hist[0], hist[-1]
        dt_h = max((t1 - t0) / 3600_000, 1e-6)
        vx, vy = (b1[0] - b0[0]) / dt_h, (b1[1] - b0[1]) / dt_h
        d0, d1 = math.hypot(b0[0], b0[1]), math.hypot(b1[0], b1[1])
        out["speed_kmh"] = round(math.hypot(vx, vy))
        out["direction_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360) if out["speed_kmh"] else None
        if d1 - d0 <= -MOVE_KM:
            out["movement"], approach_kmh = "APPROACHING", (d0 - d1) / dt_h
        elif d1 - d0 >= MOVE_KM:
            out["movement"] = "MOVING AWAY"
        elif b1[2] >= b0[2] * 1.5 and b1[2] - b0[2] >= 2:
            out["movement"] = "FORMING"
        elif b1[2] <= b0[2] * 0.5:
            out["movement"] = "DISSIPATING"
        else:
            out["movement"] = "STATIONARY"
    elif b_now:
        out["movement"] = "FORMING" if len(hist) == 1 and len(past) > 1 else None
    if c_now is not None and c_now >= th["drizzle_mm_h"]:
        out["movement"] = "OVER CIRCUIT"
        out["rain_eta_minutes"], out["eta_label"], out["confidence"] = 0, "NOW", 0.9
        return out
    if b_now is None and not any(blob(f) for f in fut):
        out["eta_label"] = "NO RAIN"
        return out
    # (a) nowcast frames: first >= LIGHT at the circuit (rain within that 15-min interval)
    eta_a = None
    for f in fut:
        v = at_circuit(f)
        if v is not None and v >= th["light_mm_h"]:
            eta_a = max(0.0, (f["t"] + STEP_MIN * 30_000 - now_ms) / 60_000)
            break
    # (b) the nearest rain's approach speed
    eta_b = None
    if approach_kmh and approach_kmh > 1 and b_now:
        eta_b = b_now[3] / approach_kmh * 60
        if eta_b > 180:
            eta_b = None
    if eta_a is not None and eta_b is not None:
        if abs(eta_a - eta_b) <= 20:
            out["rain_eta_minutes"], out["confidence"] = round((eta_a + eta_b) / 2), 0.8
        else:
            out["eta_label"], out["confidence"] = "UNCERTAIN", 0.3
    elif eta_a is not None or eta_b is not None:
        out["rain_eta_minutes"], out["confidence"] = round(eta_a if eta_a is not None else eta_b), 0.5
    else:
        out["eta_label"] = "RAIN POSSIBLE" if out["movement"] in ("APPROACHING", "FORMING") else "NOT EXPECTED"
    return out


def tile_zoom(lat: float, radius_km: float, max_zoom: int) -> int:
    """Largest zoom where the radar area spans <= 2 tiles (RainViewer: tiles up to max_zoom)."""
    for z in range(max_zoom, 0, -1):
        if 2 * radius_km <= 2 * 40075 * math.cos(math.radians(lat)) / 2 ** z:
            return z
    return 1


class Radar:
    def __init__(self, cfg: Optional[dict], thresholds: dict, http_factory: Optional[Callable] = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.th = thresholds
        self.n = max(3, int(self.cfg["grid_points"]) | 1)            # odd: a cell on the circuit
        self._http = http_factory or (lambda: httpx.AsyncClient(timeout=15, headers={"User-Agent": UA},
                                                               follow_redirects=True))
        self._grid: dict = {}          # (lat, lon) -> (monotonic, wall ms, frames)
        self._maps: Optional[tuple] = None

    async def _fetch_grid(self, http, lat: float, lon: float) -> list[dict]:
        pts = grid_points(lat, lon, float(self.cfg["radius_km"]), self.n)
        r = await http.get(OPEN_METEO, params={
            "latitude": ",".join(f"{p[0]:.3f}" for p in pts), "longitude": ",".join(f"{p[1]:.3f}" for p in pts),
            "minutely_15": "precipitation", "timeformat": "unixtime",
            "past_minutely_15": max(1, int(self.cfg["history_minutes"]) // STEP_MIN + 1),
            "forecast_minutely_15": max(1, int(self.cfg["forecast_minutes"]) // STEP_MIN + 1)})
        r.raise_for_status()
        return parse_grid(r.json(), self.n)

    async def _fetch_maps(self, http) -> dict:
        r = await http.get(RAINVIEWER)
        r.raise_for_status()
        return r.json()

    async def get(self, lat: float, lon: float, now_ms: float) -> tuple[Optional[list], Optional[float], Optional[dict], list]:
        """-> (grid frames, their age in s or None, RainViewer maps, errors). Uses the cache."""
        key = (round(lat, 2), round(lon, 2))
        errors: list = []
        hit = self._grid.get(key)
        need_grid = not hit or time.monotonic() - hit[0] >= float(self.cfg["refresh_seconds"])
        need_maps = not self._maps or time.monotonic() - self._maps[0] >= 300
        if need_grid or need_maps:
            async with self._http() as http:
                tasks = [self._fetch_grid(http, lat, lon) if need_grid else None,
                         self._fetch_maps(http) if need_maps else None]
                res = await asyncio.gather(*[t for t in tasks if t], return_exceptions=True)
            it = iter(res)
            if need_grid:
                g = next(it)
                if isinstance(g, Exception):
                    errors.append(f"Open-Meteo: {type(g).__name__}")
                else:
                    self._grid[key] = hit = (time.monotonic(), time.time() * 1000, g)
            if need_maps:
                m = next(it)
                if isinstance(m, Exception):
                    errors.append(f"RainViewer: {type(m).__name__}")
                else:
                    self._maps = (time.monotonic(), m)
        frames = age = None
        if hit:
            age = time.monotonic() - hit[0]
            if age <= float(self.cfg["max_age_seconds"]):
                frames = hit[2]
        maps = self._maps[1] if self._maps and time.monotonic() - self._maps[0] <= float(self.cfg["max_age_seconds"]) else None
        return frames, (round(age) if frames is not None else None), maps, errors

    def build(self, lat: float, lon: float, now_ms: float, frames: Optional[list], age_s: Optional[float],
              maps: Optional[dict], errors: list, outline: Optional[list], name: Optional[str],
              simulated: bool = False) -> dict:
        radius = float(self.cfg["radius_km"])
        base = {"available": False, "radius_km": radius, "center": {"lat": lat, "lon": lon}, "circuit": name,
                "outline": outline, "grid": {"n": self.n}, "thresholds": self.th, "simulated": simulated,
                "animation": bool(self.cfg["animation"])}
        if not frames:
            return {**base, "reason": "radar data unavailable" + (f" ({'; '.join(errors)})" if errors else "")}
        lo, hi = now_ms - float(self.cfg["history_minutes"]) * 60_000, now_ms + float(self.cfg["forecast_minutes"]) * 60_000
        shown = []
        for f in sorted(frames, key=lambda f: f["t"]):
            mid = f["t"] + STEP_MIN * 30_000
            if lo - STEP_MIN * 60_000 < mid <= hi + STEP_MIN * 60_000:
                rel = round((f["t"] - now_ms) / 60_000)
                shown.append({"t": f["t"], "rel_min": rel, "kind": "past" if f["t"] + STEP_MIN * 60_000 <= now_ms
                              else "now" if f["t"] <= now_ms else "forecast", "cells": f["cells"]})
        ana = analyse(frames, now_ms, self.n, radius, self.th)
        rv = None
        if maps and not simulated and isinstance(maps.get("radar"), dict) and maps.get("host"):
            rr = maps["radar"]
            fr = [{"t": int(x["time"]) * 1000, "path": x["path"], "kind": "past"} for x in rr.get("past") or []
                  if isinstance(x, dict) and x.get("path") and int(x.get("time", 0)) * 1000 >= lo]
            fr += [{"t": int(x["time"]) * 1000, "path": x["path"], "kind": "nowcast"} for x in rr.get("nowcast") or []
                   if isinstance(x, dict) and x.get("path") and int(x.get("time", 0)) * 1000 <= hi]
            if fr:
                rv = {"host": maps["host"], "frames": fr, "zoom": tile_zoom(lat, radius, int(self.cfg["max_zoom"])),
                      "color": 2, "options": "1_1"}
        latest = max((f["t"] for f in shown if f["t"] <= now_ms), default=None)
        return {**base, "available": True, "frames": shown, "rainviewer": rv,
                "provider": ("RainViewer radar + " if rv else "") + ("SIMULATED" if simulated else "Open-Meteo 15-min"),
                "analysis_source": "SIMULATED grid" if simulated else "Open-Meteo 15-min precipitation (nowcast models)",
                "timestamp_ms": rv["frames"][-1]["t"] if rv and [x for x in rv["frames"] if x["kind"] == "past"] else latest,
                "age_s": age_s, "stale": bool(age_s and age_s > float(self.cfg["refresh_seconds"]) * 1.5),
                "errors": errors, **ana}


# ---------------------------------------------------------------------------------------- TEST scenarios
RADAR_SCENARIOS = {"norain": None, "approaching": ("approach", 1.5), "over": ("over", 1.5),
                   "away": ("away", 1.5), "heavyrain": ("approach", 10.0), "radaroff": "off",
                   # the forecast scenarios of the weather report get a matching radar picture
                   "dry": None, "drizzle": ("approach", 0.3), "light": ("approach", 1.2), "medium": ("approach", 4.0),
                   "heavy": ("approach", 9.0), "stopping": ("away", 1.0), "noforecast": None,
                   "disagree": ("form", 0.8)}


def scenario_frames(name: str, now_ms: float, n: int, radius_km: float) -> Optional[list]:
    """A rain cell moving over the grid (TEST mode only - simulated, labelled as such)."""
    spec = RADAR_SCENARIOS.get(name)
    if spec == "off":
        return None
    step = 2 * radius_km / (n - 1)
    t0 = now_ms - now_ms % (STEP_MIN * 60_000)
    frames = []
    for k in range(-3, 3):
        t = t0 + k * STEP_MIN * 60_000
        cells = [0.0] * (n * n)
        if spec:
            kind, mm = spec
            minutes = (t - now_ms) / 60_000
            if kind == "approach":                       # from the west-north-west at ~60 km/h
                cx, cy = -56 + minutes, 20 - minutes * 0.4
            elif kind == "away":                         # over the circuit ~40 min ago, leaving east at 150 km/h
                cx, cy = (minutes + 40) * 2.5, 0.0
            elif kind == "over":
                cx, cy = minutes * 0.2, 0.0
            else:                                        # forming south of the circuit
                cx, cy, mm = 0.0, -45.0, mm * max(0.0, 1 + minutes / 45)
            for i in range(n):
                for j in range(n):
                    e, no = -radius_km + j * step, radius_km - i * step
                    d = math.hypot(e - cx, no - cy)
                    cells[i * n + j] = round(mm * max(0.0, min(1.0, (55 - d) / 30)), 2)   # flat core, 55 km edge
        frames.append({"t": t, "cells": cells})
    return frames
