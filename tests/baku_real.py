"""Loader for tests/fixtures/openf1_baku2025_pit.txt (real 2025 Azerbaijan GP positions)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "openf1_baku2025_pit.txt"
DAY = "2025-09-21"


def ms(hms: str) -> float:
    return datetime.fromisoformat(f"{DAY}T{hms}+00:00").timestamp() * 1000


def load():
    """-> (track [(x,y)], passes [{num, t_out_ms, lane_s, samples [(t_ms, x, y)]}])"""
    track, passes, cur = [], [], None
    for line in FIX.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            parts = line.strip("[]").split()
            if parts[0] == "track":
                cur = "track"
            else:
                cur = {"num": parts[1], "t_out": ms(parts[2]), "lane_s": float(parts[3]), "samples": []}
                passes.append(cur)
            continue
        t, x, y = line.split(",")
        if cur == "track":
            track.append((float(x), float(y)))
        else:
            cur["samples"].append((ms(t), float(x), float(y)))
    return track, passes


def iso(t_ms: float) -> str:
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
