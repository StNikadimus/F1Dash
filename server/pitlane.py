"""Automatic pit-lane reconstruction for the minimap (cache-first, per circuit).

Coordinates: the F1 position feed (``Position.z``) is not GPS latitude/longitude - it
already is a local, metric, circuit-fixed X/Y system in decimetres, identical to the
circuit outline (MultiViewer data, see server/track.py). All geometry here therefore
works directly in those decimetres (1 m = 10 units); no projection is needed, and the
result is stored and drawn in the same system as the track.

Pipeline (one complete pit-lane traversal = pit entry -> pit lane -> pit exit):

  timing signals          InPit rising edge = pit entry line; InPit falling / PitOut
                          rising = pit exit line; PitLaneTimeCollection = the official
                          pit-lane time of that stop (entry = exit - duration when the
                          InPit edge is missing, and a cross-check when it is not)
  -> raw positions        of that car from before the entry to after the exit
  -> filter               position spikes; stationary samples (box stop) -> one point;
                          a glitch that jumps back behind the car and replays a stretch
                          (real Baku 2025: 23 m back) is dropped; map-matched positions
                          that jump 20+ m sideways at the pit entry within one sample
                          (real Baku 2025: 24 m in 0.22 s) are bridged by a smooth curve
  -> traversal extraction extended back/forward to where the car left / rejoined the
                          racing line (so the drawing shows where the lane branches off)
  -> validation           enough samples, no data holes, plausible length/duration,
                          no garage detour / U-turn, not on the main track
  -> resampling           by arc length (5 m)
  -> alignment            every traversal projected onto a reference line (normals)
  -> centerline           trimmed median of the lateral offsets (robust, not a mean)
  -> outlier removal      traversals far from the consensus are dropped
  -> smoothing            Gaussian-weighted smoothing along the arc (sigma 10 m, one-sided at the ends)
  -> resampling           Catmull-Rom at 2.5 m -> renderable polyline P1..Pn

Nothing is drawn from a guess: a LOW-confidence result is neither drawn nor stored.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

from .telemetry import decode_z, parse_utc

log = logging.getLogger("pitlane")

ALGO_VERSION = 1              # bump when the reconstruction changes -> old caches are rebuilt
CACHE_FORMAT = 1

M = 10.0                      # decimetres per metre
STEP = 5 * M                  # resampling step for alignment
OUT_STEP = 2.5 * M            # output spacing
JOIN_DIST = 5 * M             # a car this close to the track centerline is "on the track"
PAD_MAX_MS = 20_000           # look this far before the entry / after the exit for the join
PAD_NOJOIN_MS = 3_000         # without a track outline: fixed extension
MIN_PIT_S, MAX_PIT_S = 8.0, 120.0      # entry->exit; longer = garage visit / red flag
DUR_TOLERANCE_S = 3.0         # timing entry/exit vs official pit-lane time
MAX_GAP_MS = 4_000            # a hole in the position data larger than this rejects the pass
MIN_CORE_SAMPLES = 10
MIN_LEN, MAX_LEN = 150 * M, 2500 * M
MAX_SPEED = 120 * M / 1000    # dm per ms (430 km/h) - faster jumps are position spikes
LATERAL_WINDOW = 15 * M       # a traversal contributes to a station only within this distance
TRIM = 4 * M                  # offsets further than this from the median are ignored
SMOOTH_SIGMA = 10 * M
MAX_TRAVERSALS = 12           # kept per variant (for re-estimation / confidence upgrades)
LAYOUT_CHANGE = 5 * M         # RMS deviation from the cache that means "different pit lane"

Pt = tuple  # (x, y)


# --------------------------------------------------------------------------- geometry helpers
def _dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def path_length(pts: list) -> float:
    return sum(_dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def resample(pts: list, step: float) -> list:
    """Equidistant points along the polyline (first and last point kept)."""
    if len(pts) < 2:
        return [tuple(p) for p in pts]
    out = [tuple(pts[0])]
    carry = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = _dist(a, b)
        if seg <= 1e-9:
            continue
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            d += step
        carry = seg - (d - step)
    if _dist(out[-1], pts[-1]) > step * 0.25:
        out.append(tuple(pts[-1]))
    else:
        out[-1] = tuple(pts[-1])
    return out


def _normals(pts: list) -> list:
    out = []
    n = len(pts)
    for i in range(n):
        a, b = pts[max(0, i - 1)], pts[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1.0
        out.append((-dy / L, dx / L))
    return out


class SegIndex:
    """Nearest-point queries on a polyline (grid bucketed; fine for a few thousand points)."""

    def __init__(self, pts: list, closed: bool = False, cell: float = 40 * M) -> None:
        self.pts = [tuple(p) for p in pts]
        self.cell = cell
        self.grid: dict[tuple, list[int]] = {}
        n = len(self.pts)
        segs = n if closed and n > 2 else n - 1
        self.segs = []
        for i in range(max(0, segs)):
            a, b = self.pts[i], self.pts[(i + 1) % n]
            self.segs.append((a, b))
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for gx in range(int(x0 // cell), int(x1 // cell) + 1):
                for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                    self.grid.setdefault((gx, gy), []).append(i)

    def nearest(self, p: Pt, max_r: float = 400 * M) -> tuple[float, int, float]:
        """(distance, segment index, t in [0,1]) - distance inf if nothing within max_r."""
        best = (math.inf, -1, 0.0)
        if not self.segs:
            return best
        gx, gy = int(p[0] // self.cell), int(p[1] // self.cell)
        r = 0
        while r * self.cell <= max_r + self.cell:
            cands = set()
            for x in range(gx - r, gx + r + 1):
                for y in range(gy - r, gy + r + 1):
                    if max(abs(x - gx), abs(y - gy)) == r:
                        cands.update(self.grid.get((x, y), ()))
            for i in cands:
                a, b = self.segs[i]
                dx, dy = b[0] - a[0], b[1] - a[1]
                L2 = dx * dx + dy * dy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
                d = math.hypot(p[0] - (a[0] + dx * t), p[1] - (a[1] + dy * t))
                if d < best[0]:
                    best = (d, i, t)
            if best[0] <= r * self.cell:        # nothing closer can be in a further ring
                break
            r += 1
        return best

    def distance(self, p: Pt) -> float:
        return self.nearest(p)[0]


def smooth(pts: list, sigma: float, step: float) -> list:
    """Gaussian-weighted smoothing along an equidistant polyline. Near the ends the window is
    one-sided (clipped) instead of shrinking to nothing, so a noisy end point does not leave a
    kink; the ends lie on straight track (branch / merge), where this adds no bias."""
    n = len(pts)
    if n < 5:
        return list(pts)
    k = max(1, int(round(3 * sigma / step)))
    w = [math.exp(-0.5 * (i * step / sigma) ** 2) for i in range(-k, k + 1)]
    out = []
    for i in range(n):
        sx = sy = sw = 0.0
        for j in range(max(0, i - k), min(n, i + k + 1)):
            ww = w[j - i + k]
            sx += pts[j][0] * ww
            sy += pts[j][1] * ww
            sw += ww
        out.append((sx / sw, sy / sw))
    return out


def catmull_rom(pts: list, step: float) -> list:
    """Centripetal-free (uniform) Catmull-Rom through the points, re-sampled at ``step``."""
    if len(pts) < 3:
        return list(pts)
    dense = []
    ext = [pts[0]] + list(pts) + [pts[-1]]
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        seg = _dist(p1, p2)
        nsub = max(2, int(seg / (step / 2)))
        for s in range(nsub):
            t = s / nsub
            t2, t3 = t * t, t * t * t
            dense.append(tuple(0.5 * ((2 * p1[c]) + (-p0[c] + p2[c]) * t + (2 * p0[c] - 5 * p1[c] + 4 * p2[c] - p3[c]) * t2
                                      + (-p0[c] + 3 * p1[c] - 3 * p2[c] + p3[c]) * t3) for c in (0, 1)))
    dense.append(tuple(pts[-1]))
    return resample(dense, step)


# --------------------------------------------------------------------------- timing signals
@dataclass
class PitWindow:
    num: str
    t_in: float               # pit entry line (ms, F1 clock)
    t_out: float              # pit exit line
    how: str                  # "InPit" | "PitLaneTime"
    confirmed: bool           # official pit-lane time agrees with the timing edges


class PitSignals:
    """Pit entry/exit moments of every car from the timing feed (clock A)."""

    def __init__(self) -> None:
        self.in_pit: dict[str, bool] = {}
        self.pit_out: dict[str, bool] = {}
        self.rise: dict[str, float] = {}              # num -> last InPit rising edge
        self.exits: list[tuple[str, float, Optional[float]]] = []   # (num, t_out, t_in from InPit)
        self.durations: list[tuple[str, float, float]] = []          # (num, published at, seconds)
        self.latest_ms = -math.inf

    def feed(self, topic: str, data: Any, t_ms: float) -> None:
        self.latest_ms = max(self.latest_ms, t_ms)
        if topic in ("TimingData", "TimingDataF1") and isinstance(data, dict):
            for num, line in (data.get("Lines") or {}).items():
                if not isinstance(line, dict):
                    continue
                num = str(num)
                if "InPit" in line:
                    v = bool(line["InPit"])
                    prev = self.in_pit.get(num)
                    if v and prev is not True:
                        self.rise[num] = t_ms
                    elif not v and prev is True:
                        self._exit(num, t_ms)
                    self.in_pit[num] = v
                if "PitOut" in line:
                    v = bool(line["PitOut"])
                    if v and self.pit_out.get(num) is not True:
                        self._exit(num, t_ms)
                    self.pit_out[num] = v
        elif topic == "PitLaneTimeCollection" and isinstance(data, dict):
            for num, p in (data.get("PitTimes") or {}).items():
                if isinstance(p, dict) and p.get("Duration") not in (None, ""):
                    try:
                        sec = float(p["Duration"])
                    except (TypeError, ValueError):
                        continue
                    # the same stop re-sent (key frame / reconnect snapshot) is one stop
                    if not any(n == str(num) and abs(d - sec) < 0.05 and abs(t - t_ms) < 120_000
                               for n, t, d in self.durations):
                        self.durations.append((str(num), t_ms, sec))

    def _exit(self, num: str, t_ms: float) -> None:
        # InPit falling and PitOut rising come together (or within a moment): one exit
        for i in range(len(self.exits) - 1, -1, -1):
            n, t, _ = self.exits[i]
            if n == num and abs(t - t_ms) < 3_000:
                return
        rise = self.rise.pop(num, None)
        t_in = rise if rise is not None and 0 < t_ms - rise <= MAX_PIT_S * 1000 * 5 else None
        self.exits.append((num, t_ms, t_in))

    def windows(self, until_ms: Optional[float] = None) -> list[PitWindow]:
        """Resolved pit windows. Exits younger than 10 s (official time may still come) wait."""
        out = []
        limit = (self.latest_ms if until_ms is None else until_ms) - 10_000
        used: set = set()
        for num, t_out, t_in in self.exits:
            if t_out > limit:
                continue
            dur = None
            for k, (n, t_pub, sec) in enumerate(self.durations):
                if n == num and -5_000 <= t_pub - t_out <= 15_000:
                    dur = sec
                    used.add(k)
                    break
            if t_in is not None:
                took = (t_out - t_in) / 1000
                confirmed = dur is not None and abs(took - dur) <= DUR_TOLERANCE_S
                if dur is not None and not confirmed:
                    continue                          # edges and official time disagree: not trusted
                out.append(PitWindow(num, t_in, t_out, "InPit", confirmed))
            elif dur is not None:
                out.append(PitWindow(num, t_out - dur * 1000, t_out, "PitLaneTime", True))
        # official pit-lane time without any timing edge (dropped messages): F1 publishes it at the
        # moment the car crosses the pit exit line, so exit = publish time, entry = exit - duration
        for k, (num, t_pub, sec) in enumerate(self.durations):
            if k in used or t_pub > limit:
                continue
            if any(n == num and abs(t_pub - t) < 15_000 for n, t, _ in self.exits):
                continue
            out.append(PitWindow(num, t_pub - sec * 1000, t_pub, "PitLaneTime", True))
        out.sort(key=lambda w: w.t_out)
        return out


# --------------------------------------------------------------------------- positions
class PositionHistory:
    """Per-car position samples (t_ms, x, y) on the F1 clock."""

    def __init__(self, keep_ms: Optional[float] = None) -> None:
        self.keep_ms = keep_ms
        self.cars: dict[str, list] = {}

    def add(self, num: str, t: float, x: float, y: float) -> None:
        lst = self.cars.setdefault(str(num), [])
        if lst and t <= lst[-1][0]:
            if t == lst[-1][0]:
                return
            bisect.insort(lst, (t, float(x), float(y)))
        else:
            lst.append((t, float(x), float(y)))
        if self.keep_ms is not None and len(lst) > 64 and lst[-1][0] - lst[0][0] > self.keep_ms * 1.25:
            cut = bisect.bisect_left(lst, (lst[-1][0] - self.keep_ms,))
            del lst[:cut]

    def add_entry(self, entry: dict) -> None:
        """One ``Position`` entry {"Timestamp", "Entries": {num: {X, Y, ...}}}."""
        ts = parse_utc((entry or {}).get("Timestamp"))
        if ts is None:
            return
        t = ts.timestamp() * 1000
        for num, e in ((entry or {}).get("Entries") or {}).items():
            if isinstance(e, dict) and e.get("X") is not None and e.get("Y") is not None:
                x, y = float(e["X"]), float(e["Y"])
                if x == 0 and y == 0:
                    continue                          # "no position" sentinel
                self.add(num, t, x, y)

    def window(self, num: str, t0: float, t1: float) -> list:
        lst = self.cars.get(str(num)) or []
        i = bisect.bisect_left(lst, (t0,))
        j = bisect.bisect_right(lst, (t1, math.inf, math.inf))
        return lst[i:j]


# --------------------------------------------------------------------------- traversals
@dataclass
class Traversal:
    num: str
    t_in: float
    t_out: float
    pts: list                     # resampled path (STEP)
    raw: list                     # filtered raw samples (x, y) - debug only
    entry_joined: bool
    exit_joined: bool
    s_in: float                   # arc length of the pit entry line on pts
    s_out: float
    how: str
    accepted: bool = True
    reason: str = ""
    notes: list = field(default_factory=list)     # e.g. "position jump at the entry bridged"


def _travel_dir(seg: list, j: int, back: float = 15 * M) -> Optional[tuple]:
    """Unit direction of travel arriving at seg[j] (over the last ``back`` of path)."""
    k = j
    while k > 0 and _dist(seg[k][1:], seg[j][1:]) < back:
        k -= 1
    dx, dy = seg[j][1] - seg[k][1], seg[j][2] - seg[k][2]
    L = math.hypot(dx, dy)
    return (dx / L, dy / L) if L > 2 * M else None


def _monotonic(seg: list) -> tuple[list, int]:
    """Drop a position glitch that jumps back behind the car and replays a stretch (seen in
    real F1 data: 23 m back within one sample, then the same metres again) and sub-metre
    backward noise. A car that really moves backwards (garage, reversing) moves back
    gradually - that is kept, so the U-turn check rejects the pass."""
    out = [seg[0]]
    replay = False
    for smp in seg[1:]:
        d = _travel_dir(out, len(out) - 1)
        if d is None:
            out.append(smp)
            continue
        last = out[-1]
        prog = (smp[1] - last[1]) * d[0] + (smp[2] - last[2]) * d[1]
        if prog >= -0.5 * M:
            replay = False
            out.append(smp)
            continue
        dt = max(1.0, smp[0] - last[0]) / 1000
        if replay or prog > -1.5 * M:
            continue                                  # still behind after a glitch / noise
        if -prog / dt > 40 * M and -prog > 5 * M:
            replay = True                             # impossible backward jump: a glitch
            continue
        out.append(smp)                               # gradual backward motion: real
    return out, len(seg) - len(out)


def _collapse_stops(seg: list, radius: float = 3 * M, min_ms: float = 800) -> list:
    """A (nearly) stopped car - the box stop, a queue at the pit exit - is one point, whatever
    the position noise around it (otherwise noise makes a zig-zag where the car stood)."""
    out: list = []
    i = 0
    n = len(seg)
    while i < n:
        j = i + 1
        sx, sy, c = seg[i][1], seg[i][2], 1
        while j < n and math.hypot(seg[j][1] - sx / c, seg[j][2] - sy / c) <= radius:
            sx, sy, c = sx + seg[j][1], sy + seg[j][2], c + 1
            j += 1
        if c >= 3 and seg[j - 1][0] - seg[i][0] >= min_ms:
            out.append(((seg[i][0] + seg[j - 1][0]) / 2, sx / c, sy / c))
            i = j
        else:
            out.append(seg[i])
            i += 1
    return out


def _hermite(a: tuple, ta: tuple, b: tuple, tb: tuple, n: int) -> list:
    out = []
    for i in range(1, n):
        u = i / n
        h00, h10 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u
        h01, h11 = -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
        out.append((h00 * a[0] + h10 * ta[0] + h01 * b[0] + h11 * tb[0],
                    h00 * a[1] + h10 * ta[1] + h01 * b[1] + h11 * tb[1]))
    return out


def _bridge_jumps(seg: list) -> tuple[list, int]:
    """F1 positions are map-matched: entering the pit lane they can jump 20+ m sideways within
    one sample (the car cannot; real 2025 Baku: 24 m in 0.22 s). Such a physically impossible step is replaced by a smooth
    tangent-continuous curve from ~25 m before it to ~35 m after it, so the drawing shows a
    branch instead of a teleport. Only impossible steps are touched."""
    bridged = 0
    i = 1
    while i < len(seg):
        a, b = seg[i - 1], seg[i]
        dt = (b[0] - a[0]) / 1000
        step = _dist(a[1:], b[1:])
        d = _travel_dir(seg, i - 1)
        if dt <= 0 or step < 8 * M or d is None:
            i += 1
            continue
        lateral = abs((b[1] - a[1]) * d[1] - (b[2] - a[2]) * d[0]) / dt
        # impossible for a car: > 100 m/s, or > 40 m/s sideways (a real corner / pit entry
        # swerve stays far below that - verified on real 2025 Baku data)
        if step / dt <= 100 * M and lateral <= 40 * M:
            i += 1
            continue
        ia, run = i - 1, 0.0
        while ia > 0 and run < 25 * M:
            run += _dist(seg[ia][1:], seg[ia - 1][1:])
            ia -= 1
        ib, run = i, 0.0
        while ib < len(seg) - 1 and run < 35 * M:
            run += _dist(seg[ib][1:], seg[ib + 1][1:])
            ib += 1
        pa, pb = seg[ia][1:], seg[ib][1:]
        chord = _dist(pa, pb)
        da = _travel_dir(seg, ia) or d
        db_pt = min(len(seg) - 1, ib + 3)
        dbx, dby = seg[db_pt][1] - pb[0], seg[db_pt][2] - pb[1]
        L = math.hypot(dbx, dby)
        db = (dbx / L, dby / L) if L > 1 * M else ((pb[0] - pa[0]) / chord, (pb[1] - pa[1]) / chord)
        n = max(2, int(chord / (2 * M)))
        pts = _hermite(pa, (da[0] * chord, da[1] * chord), pb, (db[0] * chord, db[1] * chord), n)
        ta, tb = seg[ia][0], seg[ib][0]
        mid = [(ta + (tb - ta) * k / n, x, y) for k, (x, y) in enumerate(pts, 1)]
        seg = seg[:ia + 1] + mid + seg[ib:]
        bridged += 1
        i = ia + 1 + len(mid) + 1
    return seg, bridged


def _despike(samples: list) -> list:
    """Drop single position spikes (implied speed impossible both into and out of the sample)."""
    if len(samples) < 3:
        return samples
    keep = [samples[0]]
    for i in range(1, len(samples) - 1):
        a, b, c = keep[-1], samples[i], samples[i + 1]
        v_in = _dist(a[1:], b[1:]) / max(1.0, b[0] - a[0])
        v_out = _dist(b[1:], c[1:]) / max(1.0, c[0] - b[0])
        v_skip = _dist(a[1:], c[1:]) / max(1.0, c[0] - a[0])
        if v_in > MAX_SPEED and v_out > MAX_SPEED and v_skip <= MAX_SPEED:
            continue
        keep.append(b)
    keep.append(samples[-1])
    return keep


def extract_traversal(win: PitWindow, hist: PositionHistory, track: Optional[SegIndex]) -> Traversal:
    """Positions of one pit window -> validated traversal (accepted=False with a reason)."""
    def reject(why: str, raw: Optional[list] = None) -> Traversal:
        return Traversal(win.num, win.t_in, win.t_out, [], raw or [], False, False, 0, 0, win.how, False, why)

    dur_s = (win.t_out - win.t_in) / 1000
    if not MIN_PIT_S <= dur_s <= MAX_PIT_S:
        return reject(f"{dur_s:.0f} s in the pit lane (garage visit / red flag / bad timing)")
    samples = _despike(hist.window(win.num, win.t_in - PAD_MAX_MS, win.t_out + PAD_MAX_MS))
    core = [s for s in samples if win.t_in <= s[0] <= win.t_out]
    raw_xy = [(s[1], s[2]) for s in samples]
    if len(core) < MIN_CORE_SAMPLES:
        return reject(f"only {len(core)} position samples in the pit lane", raw_xy)
    times = [s[0] for s in samples if win.t_in - 2000 <= s[0] <= win.t_out + 2000]
    if max((b - a for a, b in zip(times, times[1:])), default=0) > MAX_GAP_MS:
        return reject("hole in the position data", raw_xy)
    if core[0][0] > win.t_in + MAX_GAP_MS or core[-1][0] < win.t_out - MAX_GAP_MS:
        # e.g. a car that stops in the pit lane and leaves the session: no complete pass
        return reject("positions do not cover the whole pit lane (start after the entry / end before the exit)", raw_xy)
    # extend to the points where the car left / rejoined the track
    entry_joined = exit_joined = False
    if track is not None:
        i0 = next((i for i, s in enumerate(samples) if s[0] >= win.t_in), 0)
        start = None
        for i in range(i0, -1, -1):
            if track.distance((samples[i][1], samples[i][2])) <= JOIN_DIST:
                start, entry_joined = i, True
                break
        i1 = max(i for i, s in enumerate(samples) if s[0] <= win.t_out)
        end = None
        for i in range(i1, len(samples)):
            if track.distance((samples[i][1], samples[i][2])) <= JOIN_DIST:
                end, exit_joined = i, True
                break
        if start is None:
            start = next(i for i, s in enumerate(samples) if s[0] >= win.t_in - PAD_NOJOIN_MS)
        if end is None:
            end = max(i for i, s in enumerate(samples) if s[0] <= win.t_out + PAD_NOJOIN_MS)
        # not on the main track while "in the pit lane"
        core_d = sorted(track.distance((s[1], s[2])) for s in core)
        if core_d[len(core_d) // 2] < 2.5 * M:
            return reject("positions stay on the main track - timing flags do not match the positions", raw_xy)
    else:
        start = next(i for i, s in enumerate(samples) if s[0] >= win.t_in - PAD_NOJOIN_MS)
        end = max(i for i, s in enumerate(samples) if s[0] <= win.t_out + PAD_NOJOIN_MS)
    notes: list = []
    # a little of the track before the branch / after the merge, so the lane visibly leaves
    # and rejoins it (drawn under the track outline, invisible on the map)
    if entry_joined:
        run = 0.0
        while start > 0 and run < 30 * M:
            run += _dist(samples[start][1:], samples[start - 1][1:])
            start -= 1
    if exit_joined:
        run = 0.0
        while end < len(samples) - 1 and run < 30 * M:
            run += _dist(samples[end][1:], samples[end + 1][1:])
            end += 1
    seg, n_back = _monotonic(_collapse_stops(samples[start:end + 1]))
    if n_back:
        notes.append(f"{n_back} position sample(s) behind the car dropped")
    seg, n_jump = _bridge_jumps(seg)
    if n_jump:
        notes.append(f"{n_jump} impossible position jump(s) bridged")
    # stationary samples (box stop) -> one point
    path: list = []
    t_of: list = []
    for t, x, y in seg:
        if not path or _dist(path[-1], (x, y)) >= 1 * M:
            path.append((x, y))
            t_of.append(t)
    length = path_length(path)
    if not MIN_LEN <= length <= MAX_LEN:
        return reject(f"path {length / M:.0f} m long - not a pit-lane pass", raw_xy)
    # garage detour / U-turn: the direction reverses, or two parts of the path far apart
    # along it come close in space
    rs = resample(path, STEP)
    for i in range(2, len(rs) - 2):
        ax, ay = rs[i][0] - rs[i - 2][0], rs[i][1] - rs[i - 2][1]
        bx, by = rs[i + 2][0] - rs[i][0], rs[i + 2][1] - rs[i][1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la > 0 and lb > 0 and (ax * bx + ay * by) / (la * lb) < math.cos(math.radians(135)):
            return reject("the path turns back on itself (garage visit / reversing)", raw_xy)
    for i in range(len(rs)):
        for j in range(i + 12, len(rs)):                # > 60 m apart along the path
            if _dist(rs[i], rs[j]) < 3 * M:
                return reject("the path turns back on itself (garage visit)", raw_xy)
    # arc length of the timing lines on the path
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + _dist(path[i - 1], path[i]))

    def s_at(t: float) -> float:
        k = bisect.bisect_left(t_of, t)
        return cum[min(max(k, 0), len(cum) - 1)]

    return Traversal(win.num, win.t_in, win.t_out, rs, raw_xy, entry_joined, exit_joined,
                     s_at(win.t_in), s_at(win.t_out), win.how, notes=notes)


# --------------------------------------------------------------------------- centerline
def _lateral(ref: list, normals: list, line: SegIndex) -> list:
    """Signed lateral offset of ``line`` at every reference station (None if not covered)."""
    out = []
    for p, nv in zip(ref, normals):
        d, i, t = line.nearest(p, LATERAL_WINDOW)
        if d > LATERAL_WINDOW:
            out.append(None)
            continue
        a, b = line.segs[i]
        q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        out.append((q[0] - p[0]) * nv[0] + (q[1] - p[1]) * nv[1])
    return out


def _consensus(ref: list, lines: list) -> tuple[list, list]:
    normals = _normals(ref)
    idx = [SegIndex(l) for l in lines]
    lat = [_lateral(ref, normals, ix) for ix in idx]
    new = []
    for s, (p, nv) in enumerate(zip(ref, normals)):
        vals = [l[s] for l in lat if l[s] is not None]
        if not vals:
            new.append(p)
            continue
        m = median(vals)
        kept = [v for v in vals if abs(v - m) <= TRIM] or vals
        m = median(kept)
        new.append((p[0] + nv[0] * m, p[1] + nv[1] * m))
    return new, lat


def _rms_dev(center: list, line: list) -> Optional[float]:
    ix = SegIndex(line)
    ds = [ix.nearest(p, LATERAL_WINDOW)[0] for p in center]
    ds = [d for d in ds if d <= LATERAL_WINDOW]
    if len(ds) < max(5, len(center) // 3):
        return None                                   # barely overlapping - not comparable
    return math.sqrt(sum(d * d for d in ds) / len(ds))


@dataclass
class Reconstruction:
    centerline: list
    confidence: str               # HIGH | MEDIUM | LOW
    used: list                    # accepted traversals that built the line
    rejected: list                # (traversal, reason)
    spread_m: Optional[float]
    entry_joined: bool
    exit_joined: bool
    pit_in_frac: Optional[float]
    pit_out_frac: Optional[float]
    note: str = ""

    @property
    def drawable(self) -> bool:
        return self.confidence in ("HIGH", "MEDIUM") and len(self.centerline) >= 4


def reconstruct(traversals: list, have_track: bool) -> Optional[Reconstruction]:
    """Robust centerline of accepted traversals (arc-length resampled polylines)."""
    good = [t for t in traversals if t.accepted and len(t.pts) >= 4]
    rejected = [(t, t.reason) for t in traversals if not t.accepted]
    if not good:
        return None
    # reference: joined at both ends first, then the median length
    pool = [t for t in good if t.entry_joined and t.exit_joined] or good
    pool = sorted(pool, key=lambda t: path_length(t.pts))
    ref = pool[len(pool) // 2].pts
    lines = [t.pts for t in good]
    center = ref
    for _ in range(3):
        center, _lat = _consensus(center, lines)
        center = resample(center, STEP)
    # trajectory-level outliers
    devs = [(_rms_dev(center, t.pts), t) for t in good]
    if len(good) >= 3:
        vals = [d for d, _ in devs if d is not None]
        med = median(vals) if vals else 0.0
        limit = max(3 * med, 3 * M)
        keep = []
        for d, t in devs:
            if d is None or d > limit:
                t.accepted, t.reason = False, (f"outlier: {d / M:.1f} m from the other passes" if d is not None
                                               else "does not overlap the other passes")
                rejected.append((t, t.reason))
            else:
                keep.append(t)
        if len(keep) < len(good) and keep:
            good = keep
            pool = sorted([t for t in good if t.entry_joined and t.exit_joined] or good,
                          key=lambda t: path_length(t.pts))
            center = pool[len(pool) // 2].pts
            for _ in range(3):
                center, _lat = _consensus(center, [t.pts for t in good])
                center = resample(center, STEP)
        devs = [(_rms_dev(center, t.pts), t) for t in good]
    center = catmull_rom(smooth(center, SMOOTH_SIGMA, STEP), OUT_STEP)
    spread = None
    if len(good) >= 2:
        vals = [d for d, _ in devs if d is not None]
        spread = round(median(vals) / M, 2) if vals else None
    entry_j = sum(t.entry_joined for t in good) * 2 >= len(good)
    exit_j = sum(t.exit_joined for t in good) * 2 >= len(good)
    # where the pit entry / exit lines lie on the centerline (fraction of its length)
    total = path_length(center)
    ix = SegIndex(center)
    cum = [0.0]
    for i in range(1, len(center)):
        cum.append(cum[-1] + _dist(center[i - 1], center[i]))

    def frac_of(t: Traversal, s: float) -> Optional[float]:
        pts = t.pts
        k = min(len(pts) - 1, max(0, int(round(s / STEP))))
        d, i, tt = ix.nearest(pts[k], LATERAL_WINDOW)
        if d > LATERAL_WINDOW or total <= 0:
            return None
        return (cum[i] + tt * _dist(center[i], center[min(i + 1, len(center) - 1)])) / total
    fin = [f for f in (frac_of(t, t.s_in) for t in good) if f is not None]
    fout = [f for f in (frac_of(t, t.s_out) for t in good) if f is not None]
    n = len(good)
    if n >= 3 and spread is not None and spread <= 2.5 and entry_j and exit_j and have_track:
        conf = "HIGH"
    elif (n >= 2 and (spread is None or spread <= 5.0)) or (n == 1 and (entry_j and exit_j or not have_track)):
        conf = "MEDIUM"
    else:
        conf = "LOW"
    note = f"{n} complete pass(es)" + (f", spread {spread} m" if spread is not None else "")
    return Reconstruction([(round(x, 1), round(y, 1)) for x, y in center], conf, good, rejected, spread,
                          entry_j, exit_j, round(median(fin), 4) if fin else None,
                          round(median(fout), 4) if fout else None, note)


# --------------------------------------------------------------------------- collector (live / replay / VOD)
class PitLaneCollector:
    """Timing signals + positions -> traversals, incrementally (live) or in one go (VOD)."""

    def __init__(self, track_points: Optional[list] = None, keep_ms: Optional[float] = 300_000) -> None:
        self.signals = PitSignals()
        self.hist = PositionHistory(keep_ms)
        self.track = SegIndex(track_points, closed=True) if track_points else None
        self.done: set = set()
        self.traversals: list[Traversal] = []

    def set_track(self, track_points: Optional[list]) -> None:
        self.track = SegIndex(track_points, closed=True) if track_points else None

    def timing(self, topic: str, data: Any, t_ms: float) -> None:
        self.signals.feed(topic, data, t_ms)

    def position_entry(self, entry: dict) -> None:
        self.hist.add_entry(entry)

    def position_z(self, payload: Any, t_ms: float) -> None:
        obj = decode_z(payload)
        for entry in (obj or {}).get("Position") or []:
            self.hist.add_entry(entry)

    def poll(self, until_ms: Optional[float] = None) -> list[Traversal]:
        """New traversals whose pit window is complete (positions up to exit + padding present)."""
        new = []
        for w in self.signals.windows(until_ms):
            key = (w.num, round(w.t_out / 1000))
            if key in self.done:
                continue
            last = (self.hist.cars.get(w.num) or [(-math.inf,)])[-1][0]
            if last < w.t_out + PAD_MAX_MS and (until_ms is None or until_ms < w.t_out + PAD_MAX_MS + 30_000):
                continue                               # positions after the exit not there yet
            self.done.add(key)
            tr = extract_traversal(w, self.hist, self.track)
            self.traversals.append(tr)
            new.append(tr)
        return new


def collect_from_events(events: Iterable, track_points: Optional[list]) -> list[Traversal]:
    """VOD: the complete session is available - find every pass through the pit lane.
    Positions are decoded only around the pit windows (fast)."""
    col = PitLaneCollector(track_points, keep_ms=None)
    pos_events = []
    for e in events:
        t = e.t.timestamp() * 1000
        if e.topic in ("TimingData", "TimingDataF1", "PitLaneTimeCollection"):
            col.timing(e.topic, e.data, t)
        elif e.topic in ("Position.z", "Position"):
            pos_events.append((t, e))
    wins = col.signals.windows(math.inf)
    if not wins:
        return []
    spans = sorted((w.t_in - PAD_MAX_MS - 5_000, w.t_out + PAD_MAX_MS + 5_000) for w in wins)
    times = [t for t, _ in pos_events]
    for a, b in spans:
        for k in range(bisect.bisect_left(times, a), bisect.bisect_right(times, b)):
            e = pos_events[k][1]
            try:
                obj = decode_z(e.data) if e.topic.endswith(".z") else e.data
            except Exception:  # noqa: BLE001 - one broken packet
                continue
            for entry in (obj or {}).get("Position") or []:
                col.hist.add_entry(entry)
    return col.poll(math.inf)


# --------------------------------------------------------------------------- cache
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


class PitLaneCache:
    """``data/tracks/pitlane_geometry_<circuit_key>.json`` - one file per circuit, shared by
    every session and every recording of it; variants for seasons with a different layout."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, circuit_key: int) -> Path:
        return self.dir / f"pitlane_geometry_{int(circuit_key)}.json"

    def read(self, circuit_key: int) -> dict:
        p = self.path(circuit_key)
        if not p.exists():
            return {}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Pit lane cache %s is corrupt - ignored (will be rebuilt)", p.name)
            return {}
        if d.get("format") != CACHE_FORMAT or d.get("algo_version") != ALGO_VERSION:
            log.info("Pit lane cache %s is from another algorithm version (%s) - ignored, will be rebuilt",
                     p.name, d.get("algo_version"))
            return {}
        if int(d.get("track_key", -1)) != int(circuit_key):
            return {}
        return d

    def write(self, circuit_key: int, data: dict) -> None:
        p = self.path(circuit_key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def select(data: dict, year: Optional[int]) -> Optional[dict]:
        """Variant for this season: one that was seen in it; else the newest one not
        superseded before it. Outdated/invalid variants are never returned."""
        vs = [v for v in data.get("variants") or [] if v.get("status") in ("verified", "provisional")
              and len(v.get("centerline") or []) >= 4]
        if not vs:
            return None
        rank = {"verified": 0, "provisional": 1}
        if year is not None:
            same = [v for v in vs if year in (v.get("seasons") or [])]
            if same:
                return sorted(same, key=lambda v: (rank[v["status"]], -v.get("traversals", 0)))[0]
            ok = [v for v in vs if v.get("valid_until") is None or v["valid_until"] >= year]
            before = [v for v in ok if max(v.get("seasons") or [0]) <= year]
            cand = before or ok
            if not cand:
                return None
            return sorted(cand, key=lambda v: (-max(v.get("seasons") or [0]), rank[v["status"]]))[0]
        return sorted(vs, key=lambda v: (rank[v["status"]], -max(v.get("seasons") or [0])))[0]

    def lookup(self, circuit_key: Optional[int], year: Optional[int]) -> Optional[dict]:
        if circuit_key is None:
            return None
        return self.select(self.read(circuit_key), year)

    def store(self, circuit_key: int, name: str, year: Optional[int], session_key: Optional[int],
              rec: Reconstruction, source: str) -> tuple[Optional[dict], str]:
        """Merge a reconstruction into the cache. Returns (variant used for drawing, what happened).
        LOW results are never stored; MEDIUM is stored as provisional; HIGH as verified.
        A result that differs from a cached variant is never written over it."""
        data = self.read(circuit_key) or {"format": CACHE_FORMAT, "algo_version": ALGO_VERSION,
                                          "track_key": int(circuit_key), "name": name, "variants": []}
        if not rec.drawable:
            return self.select(data, year), "not stored (LOW confidence)"
        samples = [[[round(x), round(y)] for x, y in t.pts] for t in rec.used][:MAX_TRAVERSALS]
        status = "verified" if rec.confidence == "HIGH" else "provisional"
        match = None
        for v in data["variants"]:
            if v.get("status") == "outdated":
                continue
            dev = _rms_dev([tuple(p) for p in v["centerline"]], rec.centerline)
            if dev is not None and dev <= LAYOUT_CHANGE:
                match = v
                break
        if match is not None:
            if year is not None and year not in match.setdefault("seasons", []):
                match["seasons"] = sorted(match["seasons"] + [year])
            if session_key is not None and session_key not in match.setdefault("sessions", []):
                match["sessions"] = (match["sessions"] + [session_key])[-20:]
            what = "confirmed the cached pit lane"
            if match["status"] == "provisional":
                # a provisional line may be replaced by a better estimate from more passes
                if rec.confidence == "HIGH" or len(rec.used) > match.get("traversals", 0):
                    match.update(self._geom(rec, samples, status, source))
                    what = f"cached pit lane improved ({match['confidence']})"
            match["updated"] = _now()
            self.write(circuit_key, data)
            return match, what
        variant = {"id": max([v.get("id", 0) for v in data["variants"]] + [0]) + 1,
                   "seasons": [year] if year is not None else [], "sessions": [session_key] if session_key else [],
                   "created": _now(), "updated": _now(), "valid_until": None,
                   **self._geom(rec, samples, status, source)}
        what = "new pit lane stored"
        others = [v for v in data["variants"] if v.get("status") != "outdated"]
        if others:
            # different from what is cached: a changed layout. The old variant stays for its own
            # seasons; it is only closed for this season onwards when the new one is verified.
            what = f"pit lane differs from the cached one - stored as a new {status} variant"
            if status == "verified" and year is not None:
                for v in others:
                    if max(v.get("seasons") or [0]) < year:
                        v["valid_until"] = year - 1
                    elif year in (v.get("seasons") or []) and v["status"] == "provisional":
                        v["status"] = "outdated"
        data["variants"].append(variant)
        self.write(circuit_key, data)
        return self.select(data, year), what

    @staticmethod
    def _geom(rec: Reconstruction, samples: list, status: str, source: str) -> dict:
        return {"status": status, "confidence": rec.confidence,
                "centerline": [[x, y] for x, y in rec.centerline],
                "entry": list(rec.centerline[0]), "exit": list(rec.centerline[-1]),
                "entry_joined": rec.entry_joined, "exit_joined": rec.exit_joined,
                "pit_in_frac": rec.pit_in_frac, "pit_out_frac": rec.pit_out_frac,
                "traversals": len(rec.used), "spread_m": rec.spread_m, "source": source,
                "samples": samples}

    def samples(self, circuit_key: int, variant_id: int) -> list:
        for v in self.read(circuit_key).get("variants") or []:
            if v.get("id") == variant_id:
                return v.get("samples") or []
        return []


def needs_reconstruction(cache: PitLaneCache, circuit_key: Optional[int], year: Optional[int]) -> tuple[bool, Optional[dict]]:
    """Cache-first: a verified pit lane seen in this season is used as it is - no reconstruction.
    Missing, provisional, outdated (algorithm version) or only known from another season -> learn."""
    variant = cache.lookup(circuit_key, year)
    if variant and variant.get("status") == "verified" and (year is None or year in (variant.get("seasons") or [])):
        return False, variant
    return True, variant


def traversals_from_samples(samples: list) -> list[Traversal]:
    """Cached passes of a provisional variant, to be combined with new ones."""
    out = []
    for s in samples:
        pts = [tuple(p) for p in s]
        if len(pts) >= 4:
            out.append(Traversal("cache", 0, 0, pts, [], True, True, 0, path_length(pts), "cache"))
    return out


def deviation_from(variant: dict, traversal: Traversal) -> Optional[float]:
    """RMS distance (m) of a new pass from a cached centerline (layout-change check)."""
    d = _rms_dev(traversal.pts, [tuple(p) for p in variant.get("centerline") or []])
    return None if d is None else d / M


def pit_info(variant: Optional[dict], state: str, extra: Optional[dict] = None) -> dict:
    """What the dashboard shows about the pit lane ("cached", "learning", "reconstructed", ...)."""
    info = {"state": state}
    if variant:
        info.update({k: variant.get(k) for k in ("status", "confidence", "traversals", "spread_m",
                                                  "entry", "exit", "pit_in_frac", "pit_out_frac",
                                                  "seasons", "id", "source", "entry_joined", "exit_joined")})
    if extra:
        info.update(extra)
    return info
