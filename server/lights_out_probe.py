"""LIGHTS OUT fallback for LIVE: the public F1 SignalR feed.

When the primary live connection has no actual race start (the F1 TV / authenticated socket did
not deliver it, or it is down), a second, minimal connection to the PUBLIC F1 live timing feed is
opened - the very client the dashboard used before F1 TV access (server/sources/f1_live.py,
anonymous: SignalR Core, legacy ``/signalr`` as its fallback transport). It subscribes only to
the topics that carry the start and reports it as source "F1 SignalR":

* ``SessionData.StatusSeries`` {"SessionStatus": "Started", "Utc": ...}  (also in the snapshot,
  so a start that happened before this connection is still found)
* ``SessionStatus`` {"Status": "Started"}
* ``ExtrapolatedClock`` (the clock starting to run: +-1 s fallback)
* ``SessionInfo`` - the reports are used only while its Key is the dashboard's session key

Its reports go into the same reference events as the primary feed's (one report per source and
moment: a reconnect snapshot repeating the start adds nothing); server/lights_out.py decides.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("sync")

TOPICS = ["Heartbeat", "SessionInfo", "SessionStatus", "SessionData", "ExtrapolatedClock"]


class PublicStartProbe:
    """A Sink for a minimal anonymous F1LiveSource; copies the session's start reports into
    ``sync.feed_ref.ref``."""

    def __init__(self, sync, live_cfg: dict, session_key: Any,
                 source_factory: Optional[Callable[[dict], Any]] = None) -> None:
        from .sync import FeedRefCollector
        self.sync = sync
        self.key = session_key
        self.collector = FeedRefCollector()
        self.collector.family = lambda: "F1 SignalR"
        self.session_seen: Any = None
        self.state = "starting"
        self.reports = 0
        self.task: Optional[asyncio.Task] = None
        cfg = {"topics": list(TOPICS), "archive_follow": False, "helpers": False,
               "transport": (live_cfg or {}).get("transport", "auto"),
               "reconnect_min": (live_cfg or {}).get("reconnect_min", 2.0),
               "reconnect_max": (live_cfg or {}).get("reconnect_max", 60.0),
               "silence_timeout": (live_cfg or {}).get("silence_timeout", 120.0)}
        if source_factory is None:
            from .sources.f1_live import F1LiveSource

            def source_factory(c):                     # anonymous: no F1 TV sign-in on this connection
                return F1LiveSource(c, None, auth=None)
        self.source = source_factory(cfg)
        self._publish()

    def _publish(self) -> None:
        self.sync.lo_fallback = {"state": self.state, "source": "F1 SignalR (public live timing)",
                                 "session_key": self.key, "reports": self.reports,
                                 "session_seen": self.session_seen}

    def start(self) -> "PublicStartProbe":
        log.info("[SYNC] Lights Out fallback: opening the public F1 SignalR feed (session %s)", self.key)
        self.task = asyncio.get_event_loop().create_task(self._run())
        return self

    async def _run(self) -> None:
        try:
            await self.source.run(self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a fallback never takes the dashboard down
            log.warning("[SYNC] Lights Out fallback (public F1 SignalR) stopped: %s", exc)
            self.state = "error"
            self._publish()

    async def stop(self, why: str) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.state = "off"
        self._publish()
        log.info("[SYNC] Lights Out fallback closed: %s", why)

    # ---- Sink interface (server/sources/base.py) ------------------------------------------
    async def begin_snapshot(self) -> None:
        return None

    def set_schedule(self, schedule: Any) -> None:
        return None

    def set_status(self, **status: Any) -> None:
        self.state = str(status.get("state") or self.state)
        self._publish()

    async def feed(self, topic: str, data: Any, ts: Optional[datetime], snapshot: bool = False,
                   origin: str = "feed") -> None:
        ms = (ts or datetime.now(timezone.utc)).timestamp() * 1000
        if topic == "SessionInfo" and isinstance(data, dict) and data.get("Key") is not None:
            if data.get("Key") != self.session_seen:
                self.session_seen = data.get("Key")
                if self.session_seen != self.key:
                    log.warning("[SYNC] Lights Out fallback: the public feed is on session %s, the dashboard on "
                                "%s - its reports are not used", self.session_seen, self.key)
        self.collector.observe(topic, data, ms, snapshot)
        if self.session_seen is None or self.session_seen != self.key:
            return                                      # never another session's start
        main = self.sync.feed_ref.ref
        before = len(main.start_obs)
        for o in list(self.collector.ref.start_obs):
            main.add_start(*o)                          # duplicates (reconnect snapshots) are ignored
        if len(main.start_obs) > before:
            self.reports += len(main.start_obs) - before
            log.info("[SYNC] Lights Out fallback: public F1 SignalR reports a start (%s)",
                     ", ".join(f"{o[1]} {datetime.fromtimestamp(o[0] / 1000, timezone.utc):%H:%M:%S.%f}"[:-3]
                               for o in main.start_obs[before:]))
            self._publish()
