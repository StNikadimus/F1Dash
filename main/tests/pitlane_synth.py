"""Synthetic pit-lane passes for the geometry tests.

The recordings in this project contain no car positions, so the geometry of a pass is
generated here - on a *real* circuit outline (data/test_tracks, same decimetre units as the
feed) with a pit lane of known shape next to its start/finish straight. The tests then
check that the reconstruction recovers that known shape. (Pit timing signal tests use
the real 2026 Japanese GP timing recording.)
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server.pitlane import M, resample
from server.track import geometry_from_geojson
from server.config import DATA_DIR

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 9, 26, 11, 30, tzinfo=timezone.utc)


def track(name: str = "az-2016") -> list:
    return [tuple(p) for p in geometry_from_geojson(DATA_DIR / "test_tracks" / f"{name}.geojson").points]


def _straight_start(pts: list, need: float) -> int:
    """Index where the longest straight of the outline starts."""
    best, best_i = 0, 0
    n = len(pts)
    for i in range(n):
        j, run = i, 0.0
        while run < need * 2 and j < i + n:
            a, b, c = pts[j % n], pts[(j + 1) % n], pts[(j + 2) % n]
            h1 = math.atan2(b[1] - a[1], b[0] - a[0])
            h2 = math.atan2(c[1] - b[1], c[0] - b[0])
            if abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi) > 0.05:
                break
            run += math.dist(a, b)
            j += 1
        if run > best:
            best, best_i = run, i
    return best_i


def pit_lane(pts: list, offset_m: float = 22.0, lane_m: float = 420.0, blend_m: float = 90.0,
             side: int = 1, shift: float = 0.0):
    """Known pit lane along the longest straight: returns (centerline, i_branch, i_join)
    ``shift`` moves the whole lane along the track (a changed layout for season tests)."""
    total = lane_m + 2 * blend_m
    i0 = _straight_start(pts, total * M)
    n = len(pts)
    seq, s = [], 0.0
    i = i0 + int(shift * M / 50)
    while s <= total * M:
        a, b = pts[i % n], pts[(i + 1) % n]
        seq.append((a, b, s))
        s += math.dist(a, b)
        i += 1
    lane = []
    for a, b, s in seq:
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L * side, dx / L * side
        u = s / M
        if u < blend_m:
            k = 0.5 - 0.5 * math.cos(math.pi * u / blend_m)
        elif u > total - blend_m:
            k = 0.5 - 0.5 * math.cos(math.pi * (total - u) / blend_m)
        else:
            k = 1.0
        lane.append((a[0] + nx * offset_m * M * k, a[1] + ny * offset_m * M * k))
    return lane, i0


def pass_samples(pts: list, lane: list, i_branch: int, t_start: datetime, noise_m: float = 0.5,
                 seed: int = 1, lateral_m: float = 0.0, stop_s: float = 2.5, dt: float = 0.27,
                 gap: tuple = None):
    """Positions of one car: 25 s on the track before the branch, the lane at 22 m/s with a box
    stop, 20 s on the track after it. Returns (entries, t_in, t_out) - entries in the feed's
    Position format, t_in/t_out = pit entry/exit line (60 m after the branch / before the join)."""
    rnd = random.Random(seed)
    n = len(pts)
    path, speeds = [], []
    back = []
    j = i_branch
    run = 0.0
    while run < 25 * 60 * M:                      # 25 s at 60 m/s before the branch
        a, b = pts[(j - 1) % n], pts[j % n]
        back.append(a)
        run += math.dist(a, b)
        j -= 1
    back.reverse()
    path += back
    speeds += [60 * M] * len(back)
    lane_pts = resample(lane, 2 * M)
    if lateral_m:
        shifted = []
        for k, p in enumerate(lane_pts):
            a, b = lane_pts[max(0, k - 1)], lane_pts[min(len(lane_pts) - 1, k + 1)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1
            w = min(1.0, k / 20, (len(lane_pts) - 1 - k) / 20)
            shifted.append((p[0] - dy / L * lateral_m * M * w, p[1] + dx / L * lateral_m * M * w))
        lane_pts = shifted
    path += lane_pts
    speeds += [22 * M] * len(lane_pts)
    j = i_branch + len(lane) - 1
    after, run = [], 0.0
    while run < 20 * 60 * M:
        a, b = pts[j % n], pts[(j + 1) % n]
        after.append(b)
        run += math.dist(a, b)
        j += 1
    path += after
    speeds += [60 * M] * len(after)
    # time along the path
    t_of = [0.0]
    for k in range(1, len(path)):
        t_of.append(t_of[-1] + math.dist(path[k - 1], path[k]) / speeds[k])
    lane_start = len(back)
    lane_len = [0.0]
    for k in range(1, len(lane_pts)):
        lane_len.append(lane_len[-1] + math.dist(lane_pts[k - 1], lane_pts[k]))
    box_k = lane_start + len(lane_pts) // 2
    for k in range(box_k, len(t_of)):
        t_of[k] += stop_s
    k_in = lane_start + next(i for i, s in enumerate(lane_len) if s >= 60 * M)
    k_out = lane_start + next(i for i, s in enumerate(lane_len) if s >= lane_len[-1] - 60 * M)
    t_in, t_out = t_of[k_in], t_of[k_out]
    entries = []
    t = 0.0
    k = 0
    while t <= t_of[-1]:
        while k < len(t_of) - 2 and t_of[k + 1] < t:
            k += 1
        if gap and gap[0] <= t - t_in <= gap[1]:
            t += dt
            continue
        a, b = path[k], path[k + 1]
        span = t_of[k + 1] - t_of[k]
        u = 0 if span <= 0 else min(1, max(0, (t - t_of[k]) / span))
        x = a[0] + (b[0] - a[0]) * u + rnd.gauss(0, noise_m * M)
        y = a[1] + (b[1] - a[1]) * u + rnd.gauss(0, noise_m * M)
        ts = (t_start + timedelta(seconds=t)).isoformat().replace("+00:00", "Z")
        entries.append({"Timestamp": ts, "Entries": {"__NUM__": {"Status": "OnTrack", "X": round(x), "Y": round(y), "Z": 0}}})
        t += dt
    return entries, (t_start + timedelta(seconds=t_in)).timestamp() * 1000, \
        (t_start + timedelta(seconds=t_out)).timestamp() * 1000


def iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
