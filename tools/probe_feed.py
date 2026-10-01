#!/usr/bin/env python3
"""Measure what the F1 live feed actually delivers - run it DURING a session.

    python tools/probe_feed.py                 # anonymous, 60 s
    python tools/probe_feed.py --seconds 120
    F1TV_TOKEN=... python tools/probe_feed.py --token     # authenticated connection
    python tools/probe_feed.py --transport legacy

Prints how many messages arrived per topic (snapshot + stream), whether
Position.z / CarData.z contained real samples, and whether the public archive
stream of the running session is readable. This is the evidence for the
question "do we get car positions without F1 TV?" on your own connection.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import load_config  # noqa: E402
from server.sources.archive_follow import ARCHIVE  # noqa: E402
from server.sources.f1_live import F1LiveSource  # noqa: E402
from server.telemetry import decode_z  # noqa: E402


class CountingSink:
    def __init__(self) -> None:
        self.snapshot: Counter = Counter()
        self.stream: Counter = Counter()
        self.samples: Counter = Counter()
        self.path: Optional[str] = None
        self.status: Optional[str] = None
        self.states: list[str] = []

    async def feed(self, topic: str, data: Any, ts, snapshot: bool = False, origin: str = "feed") -> None:
        (self.snapshot if snapshot else self.stream)[topic] += 1
        if topic == "SessionInfo" and isinstance(data, dict):
            self.path = data.get("Path") or self.path
            self.status = data.get("SessionStatus") or self.status
        if topic == "SessionStatus" and isinstance(data, dict):
            self.status = data.get("Status") or self.status
        if topic in ("Position.z", "CarData.z") and data:
            try:
                obj = decode_z(data)
                key = "Position" if topic == "Position.z" else "Entries"
                self.samples[topic] += len(obj.get(key) or [])
            except Exception:  # noqa: BLE001
                self.samples[topic + " (undecodable)"] += 1

    async def begin_snapshot(self) -> None:
        pass

    def set_status(self, **st: Any) -> None:
        line = f"{st.get('state')}: {st.get('detail', '')}"
        if not self.states or self.states[-1] != line:
            self.states.append(line)
            print("  [status]", line)

    def set_schedule(self, schedule) -> None:
        pass


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--token", action="store_true", help="use F1TV_TOKEN / live.f1tv_token")
    ap.add_argument("--transport", choices=["core", "legacy"], default="core")
    args = ap.parse_args()

    cfg = load_config()["live"]
    cfg = dict(cfg, transport=args.transport, record=False, archive_follow=False)
    if not args.token:
        cfg["f1tv_token"] = ""
    elif not (cfg.get("f1tv_token") or os.environ.get("F1TV_TOKEN")):
        sys.exit("--token given but no token configured")

    sink = CountingSink()
    src = F1LiveSource(cfg)
    print(f"Connecting ({args.transport}, {'AUTHENTICATED' if src.token else 'ANONYMOUS'}) for {args.seconds} s ...")
    task = asyncio.create_task(src.run(sink))
    await asyncio.sleep(args.seconds)
    task.cancel()

    print("\nSession:", sink.path, "| status:", sink.status)
    print("\nTopic                     snapshot   stream")
    for topic in sorted(set(sink.snapshot) | set(sink.stream)):
        print(f"  {topic:24} {sink.snapshot[topic]:8} {sink.stream[topic]:8}")
    if not sink.snapshot and not sink.stream:
        print("\nNo data received at all - the connection failed, so nothing can be concluded. "
              "Check network access to livetiming.formula1.com.")
        return
    for topic in ("Position.z", "CarData.z"):
        n = sink.samples[topic]
        print(f"\n{topic}: {n} decoded sample batches "
              f"-> {'DELIVERED' if n else 'NOT DELIVERED on this connection'}")

    if sink.path:
        print("\nPublic archive streams of this session:")
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            for topic in ("Position.z", "CarData.z", "TimingData"):
                url = f"{ARCHIVE}{sink.path}{topic}.jsonStream"
                try:
                    r = await http.get(url, headers={"Range": "bytes=0-2047"})
                    size = r.headers.get("content-range", "").split("/")[-1] or len(r.content)
                    print(f"  {topic:12} HTTP {r.status_code}  size={size}  last-modified={r.headers.get('last-modified')}")
                except httpx.HTTPError as exc:
                    print(f"  {topic:12} error: {exc}")
    if sink.status not in ("Started", "Aborted"):
        print("\nNOTE: no session is running right now - Position.z/CarData.z are only sent while cars are on track.")


if __name__ == "__main__":
    asyncio.run(main())
