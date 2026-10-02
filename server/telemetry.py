"""Decoding of the compressed high-rate topics: Position.z and CarData.z.

Both topics are Base64 encoded raw-DEFLATE compressed JSON.

CarData.z channels (reverse engineered by the FastF1 project):
    0  = RPM
    2  = speed (km/h)
    3  = gear
    4  = throttle (0..100, occasionally 104 = sensor artefact)
    5  = brake (only 0 / 100 -> on/off, NOT a pressure percentage)
    45 = DRS state (pre-2026 only; DRS is abolished from 2026)
No channel carries ERS state of charge, deployment/harvest or overtake mode.
"""
from __future__ import annotations

import base64
import json
import logging
import zlib
from datetime import datetime, timezone
from typing import Any, Optional

from .models import TelemetryState

log = logging.getLogger("telemetry")


def decode_z(payload: Any) -> Any:
    """Decode a ``.z`` payload (Base64 + raw deflate). Returns parsed JSON."""
    if isinstance(payload, (dict, list)):
        return payload            # already decoded (e.g. simulator / some recordings)
    raw = base64.b64decode(payload)
    return json.loads(zlib.decompress(raw, -zlib.MAX_WBITS))


def encode_z(obj: Any) -> str:
    """Inverse of decode_z (used by the simulator so it exercises the real decoder)."""
    comp = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    data = comp.compress(json.dumps(obj, separators=(",", ":")).encode()) + comp.flush()
    return base64.b64encode(data).decode()


def parse_utc(value: Any) -> Optional[datetime]:
    """Parse the various ISO timestamp flavours used in the feed."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python < 3.11 compatible handling of 7-digit fractions
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


POS_FRESH_MS = 5000      # a position / telemetry sample older than this (vs the shown F1 time) is stale
CAR_FRESH_MS = 5000
CAR_HIDE_MS = 30000      # telemetry older than this is not shown at all (never as if it were current)


class PositionStore:
    """Keeps the latest position per car / object and yields samples for streaming.

    ``latest[key] = (t, x, y, status, z)`` - t on the presentation time base (F1 ms when live),
    x / y / z in F1's local track coordinates (decimetres, not GPS). Keys are whatever F1 sends
    (car numbers; anything else is kept as a non-driver object, never drawn as a car)."""

    def __init__(self) -> None:
        self.latest: dict[str, tuple] = {}
        self.received = False

    def reset(self) -> None:
        self.latest.clear()
        self.received = False

    def ingest(self, obj: Any, map_time) -> list[dict[str, Any]]:
        """Returns a list of samples ``{"t": ms, "cars": [[num, x, y, onTrack], ...]}``."""
        out: list[dict[str, Any]] = []
        if not isinstance(obj, dict):
            return out
        for entry in obj.get("Position") or []:
            if not isinstance(entry, dict):
                continue
            ts = parse_utc(entry.get("Timestamp"))
            if ts is None:
                continue
            t = map_time(ts)
            cars = []
            for num, p in (entry.get("Entries") or {}).items():
                if not isinstance(p, dict):
                    continue
                try:
                    x, y = int(p.get("X", 0)), int(p.get("Y", 0))
                except (TypeError, ValueError):
                    continue
                status = str(p.get("Status", ""))
                try:
                    z = int(p.get("Z", 0) or 0)
                except (TypeError, ValueError):
                    z = None
                # (0,0,0) is sent for cars without a GPS fix -> not a real position
                if x == 0 and y == 0 and not z:
                    continue
                on_track = 1 if status == "OnTrack" else 0
                self.latest[str(num)] = (t, x, y, status, z)
                cars.append([str(num), x, y, on_track])
            if cars:
                self.received = True
                out.append({"t": t, "cars": cars})
        return out


    def freshness(self, now_ms: float, keys=None) -> dict[str, dict]:
        """Per key: age of the newest sample relative to the shown F1 time ``now_ms``."""
        out = {}
        for k, v in self.latest.items():
            if keys is not None and k not in keys:
                continue
            age = max(0, int(now_ms - v[0]))
            out[k] = {"age_ms": age, "fresh": age <= POS_FRESH_MS}
        return out


_DRS_OPEN = {10, 12, 14}
KNOWN_CHANNELS = {"0": "rpm", "2": "speed", "3": "gear", "4": "throttle", "5": "brake", "45": "drs_raw"}


def decode_drs(value: Optional[int], year: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    if year is not None and year >= 2026:
        return None        # no DRS in the 2026 regulations; meaning of channel 45 is unpublished
    if value in _DRS_OPEN:
        return "OPEN"
    if value == 8:
        return "ELIGIBLE"
    if value in (0, 1):
        return "OFF"
    return None


class CarDataStore:
    def __init__(self) -> None:
        self.latest: dict[str, dict[str, int]] = {}
        self.received = False
        self.last_utc: Optional[datetime] = None

    def reset(self) -> None:
        self.latest.clear()
        self.received = False

    def ingest(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        for entry in obj.get("Entries") or []:
            if not isinstance(entry, dict):
                continue
            ets = parse_utc(entry.get("Utc"))
            for num, car in (entry.get("Cars") or {}).items():
                ch = (car or {}).get("Channels") if isinstance(car, dict) else None
                if not isinstance(ch, dict):
                    continue
                clean = {}
                for k, v in ch.items():
                    try:
                        clean[str(k)] = int(v)
                    except (TypeError, ValueError):
                        pass
                if clean:
                    if ets is not None:
                        clean["_t"] = int(ets.timestamp() * 1000)     # F1 time of the sample
                    self.latest[str(num)] = clean
                    self.received = True
            if ets:
                self.last_utc = ets

    def telemetry(self, num: str, year: Optional[int]) -> TelemetryState:
        ch = self.latest.get(num)
        if not ch:
            return TelemetryState()
        thr = ch.get("4")
        brk = ch.get("5")
        return TelemetryState(
            speed=ch.get("2"),
            rpm=ch.get("0"),
            gear=ch.get("3"),
            throttle=None if thr is None else max(0, min(100, thr)),
            brake=None if brk is None else brk > 0,
            drs=decode_drs(ch.get("45"), year),
            ch45_raw=ch.get("45"),
        )

    def car_object(self, num: str, year: Optional[int], now_ms: Optional[float]) -> dict:
        """Telemetry of one car as sent to the dashboards: only what CarData.z carries.
        Unknown channels are passed on as ``channels`` (new fields need no redesign); ERS is
        not in the feed -> None. Old data is flagged (``fresh`` false) and, beyond
        CAR_HIDE_MS, no value is shown at all."""
        ch = self.latest.get(num) or {}
        t = ch.get("_t")
        age = None if t is None or now_ms is None else max(0, int(now_ms - t))
        fresh = age is not None and age <= CAR_FRESH_MS
        obj = {"driver": num, "t": t, "age_ms": age, "fresh": fresh, "ers": None}
        tel = self.telemetry(num, year)
        hide = age is None or age > CAR_HIDE_MS
        for k in ("speed", "rpm", "gear", "throttle", "brake", "drs"):
            obj[k] = None if hide else getattr(tel, k)
        obj["drs_raw"] = None if hide else tel.ch45_raw
        obj["channels"] = {} if hide else {k: v for k, v in ch.items() if k != "_t" and k not in KNOWN_CHANNELS}
        return obj

    def objects(self, year: Optional[int], now_ms: Optional[float]) -> dict[str, dict]:
        return {num: self.car_object(num, year, now_ms) for num in self.latest}

    def freshness(self, now_ms: float) -> dict[str, dict]:
        out = {}
        for k, v in self.latest.items():
            t = v.get("_t")
            if t is not None:
                age = max(0, int(now_ms - t))
                out[k] = {"age_ms": age, "fresh": age <= CAR_FRESH_MS}
        return out

    def compact(self, year: Optional[int]) -> dict[str, list]:
        """Compact form for the websocket: num -> [speed, rpm, gear, throttle, brake, drs, ch45]."""
        out = {}
        for num in self.latest:
            t = self.telemetry(num, year)
            out[num] = [t.speed, t.rpm, t.gear, t.throttle,
                        None if t.brake is None else (1 if t.brake else 0), t.drs, t.ch45_raw]
        return out
