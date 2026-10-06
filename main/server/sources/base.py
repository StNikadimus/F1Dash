"""Data-source interface.

A source produces raw F1-style topic messages ``(topic, data, timestamp)``
and hands them to a :class:`Sink` (the engine). Replacing the data source
therefore only requires producing the same raw topics.
"""
from __future__ import annotations

import abc
from datetime import datetime, timezone
from typing import Any, Optional, Protocol


class Sink(Protocol):
    async def feed(self, topic: str, data: Any, ts: Optional[datetime], snapshot: bool = False,
                   origin: str = "feed") -> None: ...
    async def begin_snapshot(self) -> None: ...
    def set_status(self, **status: Any) -> None: ...
    def set_schedule(self, schedule: Optional[dict[str, Any]]) -> None: ...


class Source(abc.ABC):
    mode: str = "live"          # live | test | replay
    speed: float = 1.0

    @abc.abstractmethod
    async def run(self, sink: Sink) -> None:
        """Run forever, pushing messages into ``sink``."""

    def now(self) -> datetime:
        """Current time on the feed's clock (virtual time for replays)."""
        return datetime.now(timezone.utc)

    def map_time(self, ts: datetime) -> int:
        """Convert a feed timestamp into presentation epoch milliseconds.

        The browser interpolates car positions on this time base, so replays
        running faster than real time are mapped onto wall-clock time.
        """
        return int(ts.timestamp() * 1000)
