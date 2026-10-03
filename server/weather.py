"""Weather report: current conditions (official F1 WeatherData) + forecast (external weather
models, compared with each other) + what it means for the race - a popup every N race laps.

Data, strongest first (nothing is invented; a value that is not in a source stays None = "N/A"):

1. **Current**: F1 live timing ``WeatherData`` (air / track temperature, humidity, wind m/s and
   direction, rainfall flag 0/1) - the normalized ``state["weather"]`` of the dashboard. The
   track-temperature trend comes from the same values seen during the session (in memory).
2. **Forecast**: the circuit's coordinates (bundled ``reference_tracks/f1-locations.json``, picked
   by the session's circuit name) are sent to
   * Open-Meteo (several numerical weather models in one request: ECMWF IFS, GFS, ICON - each
     model is a separate source), no key;
   * MET Norway Locationforecast (the Norwegian Meteorological Institute), no key.
   Every source is reduced to "first hour with rain, how long, how heavy". When they agree the
   result is given with its spread; when they disagree the window is widened and the confidence
   says LOW ("rain possible"). The forecast is hourly: a start time is never more precise than
   that, and a lap is only an estimate (``~lap 22`` / ``laps 18-25``) from the current race pace.
   Cached for ``forecast_cache_seconds`` per location; only fetched for a report.
   A recording (replay / VOD far from now) gets no forecast - a forecast for today says nothing
   about that race.
3. **Race impact**: fixed, documented rules on the data above (no LLM: this project has no LLM
   integration, and the text must never contain a value that is not in the data).

Rain intensity (mm/h -> category), ``[weather]`` config, defaults after the usual rain-rate classes
(light < 2.5 mm/h, moderate 2.5-7.6 mm/h, heavy > 7.6 mm/h), with "drizzle" below 0.5 mm/h:
    NONE < 0.1 <= DRIZZLE < 0.5 <= LIGHT < 2.5 <= MEDIUM < 7.6 <= HEAVY
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any, Awaitable, Callable, Optional

import httpx

log = logging.getLogger("weather")

CATEGORIES = ("NONE", "DRIZZLE", "LIGHT", "MEDIUM", "HEAVY")
DEFAULT_THRESHOLDS = {"drizzle_mm_h": 0.1, "light_mm_h": 0.5, "medium_mm_h": 2.5, "heavy_mm_h": 7.6}
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
MET_NO = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
UA = "f1-tv-dashboard/1.0 (self-hosted F1 timing dashboard)"
AGREE_S = 45 * 60            # rain starts within this of each other: the sources agree
LIVE_WINDOW_MS = 2 * 3600 * 1000   # shown time this far from now: a recording, no forecast
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def intensity(mm_h: Optional[float], th: dict = DEFAULT_THRESHOLDS) -> Optional[str]:
    if mm_h is None or not math.isfinite(mm_h):
        return None
    if mm_h >= th["heavy_mm_h"]:
        return "HEAVY"
    if mm_h >= th["medium_mm_h"]:
        return "MEDIUM"
    if mm_h >= th["light_mm_h"]:
        return "LIGHT"
    if mm_h >= th["drizzle_mm_h"]:
        return "DRIZZLE"
    return "NONE"


def compass(deg: Optional[float]) -> Optional[str]:
    if deg is None:
        return None
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


# ---------------------------------------------------------------------------------------- sources
@dataclass
class Series:
    """One forecast source: hourly intervals [start_ms, start_ms + 1 h) with mm/h, probability %, °C."""
    source: str
    points: list = field(default_factory=list)        # [(start_ms, mm_h|None, prob|None, temp|None)]


def parse_open_meteo(data: dict, models: tuple = OPEN_METEO_MODELS) -> list[Series]:
    """Open-Meteo hourly: ``precipitation`` is the sum of the PRECEDING hour (-> interval start = t - 1 h).
    With several models every variable is suffixed ``_<model>``."""
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    out = []
    for m in (models if any(k.endswith("_" + models[0]) for k in h) else (None,)):
        suf = f"_{m}" if m else ""
        pr, pp, tt = h.get("precipitation" + suf), h.get("precipitation_probability" + suf), h.get("temperature_2m" + suf)
        if not isinstance(pr, list):
            continue
        pts = []
        for i, t in enumerate(times):
            try:
                start = (int(t) - 3600) * 1000
            except (TypeError, ValueError):
                continue
            pts.append((start, _num(pr, i), _num(pp, i), _num(tt, i)))
        if any(p[1] is not None for p in pts):
            out.append(Series(f"Open-Meteo {m or 'best match'}", pts))
    return out


def parse_met_no(data: dict) -> list[Series]:
    """MET Norway compact: ``next_1_hours.details.precipitation_amount`` for [time, time + 1 h)."""
    pts = []
    for row in ((data or {}).get("properties") or {}).get("timeseries") or []:
        try:
            start = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00")).timestamp() * 1000
        except (KeyError, ValueError):
            continue
        d = row.get("data") or {}
        n1 = ((d.get("next_1_hours") or {}).get("details") or {})
        inst = ((d.get("instant") or {}).get("details") or {})
        pts.append((start, _f(n1.get("precipitation_amount")), _f(n1.get("probability_of_precipitation")),
                    _f(inst.get("air_temperature"))))
    return [Series("MET Norway", pts)] if any(p[1] is not None for p in pts) else []


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _num(arr: Any, i: int) -> Optional[float]:
    return _f(arr[i]) if isinstance(arr, list) and i < len(arr) else None


async def fetch_forecasts(lat: float, lon: float, http: Optional[httpx.AsyncClient] = None) -> tuple[list[Series], list[str]]:
    """All configured providers; a failing one is reported, never fatal. -> (series, errors)."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=12, headers={"User-Agent": UA}, follow_redirects=True)
    series, errors = [], []
    try:
        async def om():
            r = await http.get(OPEN_METEO, params={
                "latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}", "timeformat": "unixtime", "timezone": "GMT",
                "hourly": "precipitation,precipitation_probability,temperature_2m",
                "models": ",".join(OPEN_METEO_MODELS), "forecast_days": 2})
            r.raise_for_status()
            return parse_open_meteo(r.json())

        async def mn():
            r = await http.get(MET_NO, params={"lat": f"{lat:.3f}", "lon": f"{lon:.3f}"})
            r.raise_for_status()
            return parse_met_no(r.json())
        for name, res in zip(("Open-Meteo", "MET Norway"), await asyncio.gather(om(), mn(), return_exceptions=True)):
            if isinstance(res, Exception):
                errors.append(f"{name}: {type(res).__name__}")
            else:
                series.extend(res)
    finally:
        if own:
            await http.aclose()
    return series, errors


# ---------------------------------------------------------------------------------------- analysis
def rain_window(s: Series, now_ms: float, horizon_ms: float, th: dict) -> dict:
    """First rain of this source within the horizon (an interval that already began counts)."""
    rows = [p for p in sorted(s.points) if p[0] + 3600_000 > now_ms and p[0] < now_ms + horizon_ms]
    if not rows or all(p[1] is None for p in rows):
        return {"source": s.source, "known": False}
    start = end = None
    peak = 0.0
    for t0, mm, _prob, _temp in rows:
        wet = mm is not None and mm >= th["drizzle_mm_h"]
        if wet and start is None:
            start = max(t0, now_ms)
        if start is not None:
            if wet:
                peak = max(peak, mm)
                end = t0 + 3600_000
            else:
                break
    temps = [p[3] for p in rows if p[3] is not None]
    return {"source": s.source, "known": True, "rain": start is not None, "start": start, "end": end,
            "peak": peak if start is not None else 0.0, "temp_now": temps[0] if temps else None,
            "temp_later": temps[min(len(temps) - 1, 2)] if temps else None,
            "prob": max((p[2] for p in rows if p[2] is not None), default=None)}


def combine(wins: list[dict], now_ms: float, th: dict) -> dict:
    known = [w for w in wins if w.get("known")]
    if not known:
        return {"available": False}
    wet = [w for w in known if w["rain"]]
    out: dict = {"available": True, "sources_used": [w["source"] for w in known],
                 "sources_rain": [w["source"] for w in wet]}
    if not wet:
        out.update(rain_expected=False, confidence="HIGH" if len(known) >= 2 else "MEDIUM")
    else:
        starts = sorted(w["start"] for w in wet)
        ends = sorted(w["end"] for w in wet if w["end"] is not None)
        peaks = sorted(w["peak"] for w in wet)
        spread = starts[-1] - starts[0]
        all_agree = len(wet) == len(known)
        out.update(rain_expected=True if all_agree else "POSSIBLE",
                   start_ms=(starts[0], starts[-1]), end_ms=(ends[0], ends[-1]) if ends else None,
                   intensity=intensity(median(peaks), th),
                   intensity_range=sorted({intensity(p, th) for p in peaks}, key=CATEGORIES.index),
                   duration_min=round(median([(w["end"] - w["start"]) / 60000 for w in wet if w["end"]])) if ends else None,
                   confidence=("HIGH" if len(wet) >= 2 and spread <= AGREE_S else "MEDIUM" if spread <= AGREE_S
                               else "LOW") if all_agree else "LOW")
    tn = [w["temp_now"] for w in known if w.get("temp_now") is not None]
    tl = [w["temp_later"] for w in known if w.get("temp_later") is not None]
    out["air_trend_c"] = round(median(tl) - median(tn), 1) if tn and tl else None
    return out


def lap_at(t_ms: float, now_ms: float, lap: Optional[int], lap_s: Optional[float]) -> Optional[int]:
    if lap is None or not lap_s:
        return None
    return int(lap + max(0.0, t_ms - now_ms) / 1000 / lap_s)


def leader_lap_s(drivers: dict) -> Optional[float]:
    """Current race pace: median of the leader's last completed laps (F1 line-crossing times)."""
    lead = next((d for d in drivers.values() if d.get("position") == 1), None)
    marks = [m[0] for m in (lead or {}).get("lap_marks") or [] if m and m[0] is not None]
    gaps = [(b - a) / 1000 for a, b in zip(marks, marks[1:]) if 30 <= (b - a) / 1000 <= 300][-6:]
    return median(gaps) if len(gaps) >= 2 else None


# ---------------------------------------------------------------------------------------- report
@dataclass
class WeatherReport:
    generated_at: str
    session_lap: Optional[int]
    total_laps: Optional[int]
    trigger: str
    current: dict
    forecast: dict
    race_impact: dict
    sources: list
    display_seconds: int
    simulated: bool = False

    def to_dict(self) -> dict:
        return {"type": "weather_report", **asdict(self)}


def _fmt_window(f: dict, now_ms: float, lap: Optional[int], lap_s: Optional[float], total: Optional[int],
                key: str) -> tuple[Optional[str], Optional[str], Optional[list]]:
    """-> (lap text, time text, [lap lo, lap hi]) for start_ms / end_ms - always marked as an estimate."""
    w = f.get(key)
    if not w:
        return None, None, None
    m0, m1 = (max(0, round((x - now_ms) / 60000)) for x in w)
    tt = "now" if m1 <= 0 else f"~{m0} min" if m1 - m0 < 10 else f"~{m0}–{m1} min"
    l0, l1 = lap_at(w[0], now_ms, lap, lap_s), lap_at(w[1], now_ms, lap, lap_s)
    if l0 is None:
        return None, tt, None
    if total and l0 > total:
        return "after the race", tt, None
    if total:
        l1 = min(l1, total)
    return (f"~lap {l0}" if l1 - l0 <= 2 else f"laps {l0}–{l1}"), tt, [l0, l1]


def build_report(state: dict, now_ms: float, series: Optional[list], errors: list, cfg: dict,
                 trend_c: Optional[float], trigger: str, simulated: bool = False) -> WeatherReport:
    th = {**DEFAULT_THRESHOLDS, **{k: float(v) for k, v in (cfg.get("thresholds") or {}).items() if k in DEFAULT_THRESHOLDS}}
    s = state.get("session") or {}
    w = state.get("weather") or {}
    lap, total = s.get("lap"), s.get("total_laps")
    lap_s = leader_lap_s(state.get("drivers") or {})
    ws = w.get("wind_speed")
    current = {
        "air_temperature": w.get("air_temp"), "track_temperature": w.get("track_temp"),
        "humidity": w.get("humidity"),
        "wind_speed_kmh": round(ws * 3.6, 1) if ws is not None else None,
        "wind_direction_deg": w.get("wind_direction"), "wind_direction": compass(w.get("wind_direction")),
        "rainfall": w.get("rainfall"),
        "condition": None if w.get("rainfall") is None else ("RAIN" if w.get("rainfall") else "DRY"),
        "track_temp_trend_c": trend_c,
        "available": any(w.get(k) is not None for k in ("air_temp", "track_temp", "humidity", "rainfall")),
    }
    sources = (["F1 live timing (WeatherData)"] if current["available"] else [])
    horizon = float(cfg.get("horizon_hours", 3)) * 3600_000
    if series is None:
        fc = {"available": False, "reason": "no forecast for a recording" if trigger != "test" else "no forecast"}
    elif not series:
        fc = {"available": False, "reason": errors[0] if errors == ["circuit location unknown"] else
              "forecast services unavailable" + (f" ({'; '.join(errors)})" if errors else "")}
    else:
        fc = combine([rain_window(x, now_ms, horizon, th) for x in series], now_ms, th)
        sources += fc.get("sources_used", [])
    forecast = {"available": fc.get("available", False), "reason": fc.get("reason"),
                "rain_expected": fc.get("rain_expected"), "intensity": fc.get("intensity"),
                "intensity_range": fc.get("intensity_range"), "duration_min": fc.get("duration_min"),
                "confidence": fc.get("confidence"), "air_trend_c": fc.get("air_trend_c"),
                "sources_rain": fc.get("sources_rain"), "lap_estimate": lap_s is not None,
                "lap_time_s": round(lap_s, 1) if lap_s else None}
    if fc.get("start_ms"):
        forecast["expected_lap"], forecast["expected_time"], forecast["expected_laps"] = \
            _fmt_window(fc, now_ms, lap, lap_s, total, "start_ms")
    if fc.get("end_ms"):
        forecast["end_lap"], forecast["end_time"], _ = _fmt_window(fc, now_ms, lap, lap_s, total, "end_ms")
    forecast["text"] = forecast_text(current, forecast)
    impact = race_impact(current, forecast)
    return WeatherReport(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), session_lap=lap,
                         total_laps=total, trigger=trigger, current=current, forecast=forecast,
                         race_impact=impact, sources=sources,
                         display_seconds=int(cfg.get("display_seconds", 15)), simulated=simulated)


def forecast_text(cur: dict, f: dict) -> str:
    if not f["available"]:
        return f"Weather forecast unavailable ({f.get('reason') or 'no source'})."
    if f.get("expected_lap"):
        when = "around " + f["expected_lap"].lstrip("~") if f["expected_lap"] != "after the race" else "after the race"
    elif f.get("expected_time"):
        when = "now" if f["expected_time"] == "now" else "in " + f["expected_time"]
    else:
        when = None
    raining = cur.get("rainfall") is True
    if f["rain_expected"] is False:
        return "No rain expected for the rest of the session." if not raining else \
            "It is raining now; the forecast sources expect it to stop."
    inten = (f.get("intensity") or "").lower()
    rng = f.get("intensity_range") or []
    if len(rng) > 1:
        inten = f"{rng[0].lower()} to {rng[-1].lower()}"
    if f["rain_expected"] == "POSSIBLE":
        txt = f"Rain possible {when or 'later in the session'} - the forecast sources disagree."
    elif raining and f.get("expected_time") == "now":
        txt = f"{inten.capitalize()} rain continues"
    else:
        txt = f"{inten.capitalize()} rain expected {when or 'later in the session'}"
    if f["rain_expected"] != "POSSIBLE":
        if f.get("end_lap") or f.get("end_time"):
            if f.get("end_lap") and f["end_lap"] != "after the race":
                txt += f", easing around {f['end_lap'].lstrip('~')}"
            elif f.get("end_time"):
                txt += f", easing in {f['end_time']}"
        elif f.get("duration_min"):
            txt += f" for about {f['duration_min']} min"
        txt += "."
    if f.get("air_trend_c") is not None and abs(f["air_trend_c"]) >= 1.5:
        txt += f" Air temperature {'falling' if f['air_trend_c'] < 0 else 'rising'} by about {abs(f['air_trend_c']):.0f}°C."
    return txt


def race_impact(cur: dict, f: dict) -> dict:
    """Fixed rules on the data above - never a value that is not in it, never a winner."""
    details = []
    raining = cur.get("rainfall") is True
    inten = f.get("intensity")
    if f.get("available") and f.get("rain_expected"):
        heavy = inten in ("MEDIUM", "HEAVY")
        ends = f.get("end_lap") or f.get("end_time")
        if raining and ends and f.get("expected_time") == "now":
            when = f.get("end_lap") if f.get("end_lap") and f["end_lap"] != "after the race" else f"in {f.get('end_time')}"
            details.append(f"Rain is expected to ease {when.lstrip('~') if when.startswith('~lap') else when}: a drying "
                           "racing line may develop, improving grip and making tyre strategy increasingly important.")
        elif raining:
            details.append("Rain is falling now" + ("; if it intensifies, intermediate conditions may become "
                                                     "relevant." if not heavy else " and more is expected - grip will stay low."))
        elif inten == "DRIZZLE":
            details.append("Drizzle may make the track slippery without forcing a tyre change.")
        else:
            details.append("The track may become increasingly slippery" +
                           ("; intermediate tyres could become relevant if the rain sets in as forecast." if heavy else
                            "; intermediate conditions are possible if the rainfall intensifies."))
        if ends and not (raining and f.get("expected_time") == "now"):
            details.append("Once the rain stops a drying line may develop, making the timing of tyre changes important.")
    elif raining and f.get("available") and f.get("rain_expected") is False:
        details.append("Rain is expected to stop: a drying racing line may develop, improving grip and making "
                       "tyre strategy increasingly important.")
    trend = cur.get("track_temp_trend_c")
    if trend is not None and trend >= 2:
        details.append(f"Track temperature has risen {trend:.0f}°C recently, which may increase tyre degradation "
                       "and thermal stress.")
    elif trend is not None and trend <= -2:
        details.append(f"Track temperature has fallen {abs(trend):.0f}°C recently, which may reduce grip and make "
                       "tyre warm-up more difficult.")
    if not details:
        details.append("No significant weather changes are expected for the remainder of the race."
                       if f.get("available") else
                       "Forecast unavailable - only the current conditions are known.")
    return {"summary": details[0], "details": details[1:]}


# ---------------------------------------------------------------------------------------- scenarios
def scenario_series(name: str, now_ms: float) -> Optional[list]:
    """TEST mode: synthetic forecast sources (clearly marked as simulated)."""
    hr = 3600_000

    def mk(src, rain_from_h, rain_to_h, mm, temp=21.0, dt=-0.5):
        pts = []
        for i in range(-1, 6):
            t0 = now_ms - now_ms % hr + i * hr
            wet = rain_from_h is not None and rain_from_h <= i < rain_to_h
            pts.append((t0, mm if wet else 0.0, 80.0 if wet else 10.0, temp + dt * i))
        return Series(f"SIMULATED {src}", pts)
    table = {"dry": (None, 0, 0.0), "drizzle": (1, 3, 0.2), "light": (1, 3, 1.2), "medium": (1, 3, 4.0),
             "heavy": (1, 3, 9.0), "stopping": (-1, 1, 1.0)}
    if name == "noforecast":
        return []
    if name == "disagree":
        return [mk("model A", 1, 2, 1.0), mk("model B", None, 0, 0.0), mk("model C", 3, 4, 3.0)]
    if name not in table:
        return None
    a, b, mm = table[name]
    return [mk("model A", a, b, mm), mk("model B", a, b, mm * 1.2)]


SCENARIO_CURRENT = {"dry": False, "drizzle": False, "light": False, "medium": False, "heavy": False,
                    "stopping": True, "noforecast": False, "disagree": False}
SCENARIOS = tuple(SCENARIO_CURRENT)


# ---------------------------------------------------------------------------------------- reporter
class WeatherReporter:
    """When to show a report (every ``every_laps`` completed race laps) and building it (forecast
    cached ``forecast_cache_seconds`` per location)."""

    def __init__(self, cfg: Optional[dict], mode: str,
                 fetch: Callable[[float, float], Awaitable[tuple[list, list]]] = fetch_forecasts) -> None:
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        self.every = max(1, int(self.cfg.get("every_laps", 15)))
        self.mode = mode
        self.auto = self.enabled and (mode != "test" or bool(self.cfg.get("test_auto", False)))
        self.ttl = float(self.cfg.get("forecast_cache_seconds", 600))
        self._fetch = fetch
        self._cache: dict = {}
        self._base: Optional[int] = None           # last lap multiple already reported (or skipped)
        self._hist: list = []                      # [(shown ms, track temp)] for the trend
        self.last: Optional[dict] = None

    def observe(self, state: dict, shown_ms: float) -> Optional[int]:
        """Every published state. -> the lap to report now, or None."""
        w = state.get("weather") or {}
        if w.get("track_temp") is not None:
            if self._hist and shown_ms < self._hist[-1][0]:
                self._hist = [h for h in self._hist if h[0] <= shown_ms]       # seek back
            if not self._hist or shown_ms - self._hist[-1][0] >= 30_000:
                self._hist.append((shown_ms, w["track_temp"]))
                self._hist = self._hist[-240:]
        s = state.get("session") or {}
        lap = s.get("lap")
        if not self.auto or s.get("session_kind") != "race" or not s.get("live") or lap is None:
            return None
        done = int(lap) - 1                            # completed race laps
        mult = done - done % self.every
        if self._base is None or mult < self._base:   # first sight / seek back: no backlog popup
            self._base = mult
            return None
        if mult > self._base and mult > 0:
            self._base = mult
            if s.get("total_laps") and done >= int(s["total_laps"]):
                return None
            return mult
        return None

    def trend(self, shown_ms: float, window_ms: float = 15 * 60_000) -> Optional[float]:
        old = [h for h in self._hist if h[0] <= shown_ms - window_ms * 0.6]
        if not old or not self._hist:
            return None
        ref = min(old, key=lambda h: abs(h[0] - (shown_ms - window_ms)))
        return round(self._hist[-1][1] - ref[1], 1)

    async def forecast(self, location: Optional[tuple], shown_ms: float) -> tuple[Optional[list], list]:
        if abs(time.time() * 1000 - shown_ms) > LIVE_WINDOW_MS:
            return None, []                         # a recording: today's forecast is not that race's
        if location is None:
            return [], ["circuit location unknown"]
        key = (round(location[0], 2), round(location[1], 2))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1], hit[2]
        series, errors = await self._fetch(*location)
        self._cache[key] = (time.monotonic(), series, errors)
        return series, errors

    async def report(self, state: dict, shown_ms: float, location: Optional[tuple], trigger: str,
                     scenario: Optional[str] = None) -> Optional[dict]:
        if scenario:
            series, errors = scenario_series(scenario, shown_ms), []
            st = json.loads(json.dumps(state, default=str))
            st.setdefault("weather", {})["rainfall"] = SCENARIO_CURRENT.get(scenario, False)
            rep = build_report(st, shown_ms, series, errors, self.cfg, self.trend(shown_ms), "test", simulated=True)
        else:
            series, errors = await self.forecast(location, shown_ms)
            rep = build_report(state, shown_ms, series, errors, self.cfg, self.trend(shown_ms), trigger)
            if not rep.current["available"] and not rep.forecast["available"] and trigger == "auto":
                log.info("[WEATHER] No weather data for the lap %s report - not shown", rep.session_lap)
                return None
        c, f = rep.current, rep.forecast
        log.info("[WEATHER] Current: %s air / %s track / %s humidity / %s",
                 _u(c["air_temperature"], "°C"), _u(c["track_temperature"], "°C"), _u(c["humidity"], "%"),
                 c["condition"] or "rain N/A")
        log.info("[WEATHER] Forecast: %s", f["text"] if f["available"] else f"unavailable ({f.get('reason')})")
        log.info("[WEATHER] Report generated for lap %s (%s)", rep.session_lap, trigger)
        self.last = rep.to_dict()
        return self.last


def _u(v: Any, unit: str) -> str:
    return "N/A" if v is None else f"{v:.1f}{unit}" if isinstance(v, float) else f"{v}{unit}"
