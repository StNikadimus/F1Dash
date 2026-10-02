"""What the live feed actually delivers - per topic, per car - for logs, /api/diagnostics and
``python main.py --diagnose``.

Nothing here assumes what an F1 TV tier "should" provide: a topic is listed as received only
when data for it arrived on this connection; Position.z / CarData.z are counted per car from
the samples actually stored. Summaries are rate-limited (no per-sample logging).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("diagnostics")

RECEIVING_S = 60.0          # an update within this = the topic is flowing now
NOT_PROVIDED_AFTER_S = 120.0  # a session running this long without a topic: "not provided by the feed"
STARTUP_REPORT_S = 45.0     # first summary this long after the connection
PERIODIC_REPORT_S = 600.0   # then every 10 minutes (and when the set of received topics changes)


@dataclass
class TopicStat:
    updates: int = 0
    snapshot: bool = False          # content in the subscribe snapshot
    snapshot_empty: bool = False    # F1 answered the topic with an empty value
    first: Optional[float] = None   # monotonic
    last: Optional[float] = None
    origin: str = "feed"
    bytes: int = 0


def _empty(data: Any) -> bool:
    return data is None or data == {} or data == [] or data == ""


class FeedDiagnostics:
    def __init__(self) -> None:
        self.topics: dict[str, TopicStat] = {}
        self.connected_at: Optional[float] = None
        self.session_running_since: Optional[float] = None
        self._last_report = -1e9
        self._reported_set: Optional[frozenset] = None
        self._announced: set[str] = set()

    def reset_connection(self, mono: float) -> None:
        """A new connection: the per-connection view starts over (counts stay for the session)."""
        self.connected_at = mono
        self._reported_set = None
        self._last_report = mono - PERIODIC_REPORT_S + STARTUP_REPORT_S    # first summary soon

    def observe(self, topic: str, data: Any, snapshot: bool, origin: str, mono: float) -> None:
        st = self.topics.get(topic)
        if st is None:
            st = self.topics[topic] = TopicStat()
        if snapshot:
            st.snapshot = not _empty(data)
            st.snapshot_empty = _empty(data)
            if _empty(data):
                return
        else:
            st.updates += 1
        st.origin = origin
        if st.first is None:
            st.first = mono
            if topic not in self._announced:
                self._announced.add(topic)
                log.info("Live feed: first data on topic %s%s", topic,
                         " (public archive stream)" if origin == "archive" else "")
        st.last = mono
        if isinstance(data, str):
            st.bytes += len(data)

    def session_running(self, running: bool, mono: float) -> None:
        if running and self.session_running_since is None:
            self.session_running_since = mono
        elif not running:
            self.session_running_since = None

    def topic_status(self, topic: str, mono: float) -> tuple[bool, str]:
        st = self.topics.get(topic)
        running_long = self.session_running_since is not None and \
            mono - self.session_running_since > NOT_PROVIDED_AFTER_S
        if st is None or st.first is None:
            if st is not None and st.snapshot_empty:
                why = "empty in the subscribe snapshot"
            else:
                why = "no data received"
            return False, why + (" - not provided by the current feed" if running_long
                                 else " (yet - no session running long enough to tell)")
        age = mono - (st.last or mono)
        src = " via the public archive stream" if st.origin == "archive" else ""
        if st.updates and age <= RECEIVING_S:
            return True, f"receiving{src} ({st.updates} updates, last {age:.1f} s ago)"
        if st.updates:
            return True, f"received{src} ({st.updates} updates, none for {age:.0f} s)"
        return True, "in the subscribe snapshot (no updates yet)"

    def topics_table(self, subscribed: list[str], mono: float) -> list[dict]:
        names = list(subscribed) + sorted(t for t in self.topics if t not in subscribed)
        out = []
        for t in names:
            ok, why = self.topic_status(t, mono)
            out.append({"topic": t, "available": ok, "status": why, "subscribed": t in subscribed,
                        "updates": (self.topics.get(t) or TopicStat()).updates})
        return out

    def due(self, mono: float) -> bool:
        if self.connected_at is None:
            return False
        cur = frozenset(t for t, s in self.topics.items() if s.first is not None)
        if self._reported_set is not None and cur != self._reported_set and mono - self._last_report > 30:
            return True
        return mono - self._last_report >= PERIODIC_REPORT_S

    def mark_reported(self, mono: float) -> None:
        self._last_report = mono
        self._reported_set = frozenset(t for t, s in self.topics.items() if s.first is not None)


def format_report(rep: dict) -> str:
    """The report as log / terminal text (✓ / ✗ per topic)."""
    a = rep.get("auth") or {}
    lines = ["===== F1 live feed diagnostics ====="]
    lines.append(f"F1 TV subscription mode: {'ENABLED' if a.get('subscription') else 'DISABLED'}")
    if a.get("subscription"):
        st = a.get("state")
        label = st
        if st == "VALID":
            label = "SUCCESS - accepted by F1" if a.get("confirmed_by_f1") else "SIGNED IN - not confirmed by F1 yet"
        lines.append(f"Authentication: {label} ({a.get('reason')})"
                     + (f" - {a.get('product')}, {a.get('subscription_status')}, valid until {a.get('expires_utc')}"
                        if st == "VALID" else ""))
    c = rep.get("connection") or {}
    lines.append(f"Connection: {c.get('auth_mode', '?')} · {c.get('state', '?')} · transport {c.get('transport', '?')}")
    s = rep.get("session") or {}
    lines.append(f"Session: {s.get('meeting') or '-'} · {s.get('name') or 'SESSION UNKNOWN'} "
                 f"(type {s.get('type') or '?'}, status {s.get('status') or '?'})")
    lines.append("Live feed topics:")
    for t in rep.get("topics") or []:
        mark = "✓" if t["available"] else "✗"
        extra = "" if t["subscribed"] else "  [not subscribed - sent anyway]"
        lines.append(f"  {mark} {t['topic']:<24} {t['status']}{extra}")
    p, cd = rep.get("positions") or {}, rep.get("car_data") or {}
    lines.append(f"Position.z: {p.get('fresh', 0)}/{p.get('drivers', 0)} drivers with a fresh position "
                 f"({p.get('with_data', 0)} with any){' · source ' + p['source'] if p.get('source') else ''}")
    if p.get("non_driver_objects"):
        lines.append(f"  non-driver objects in Position.z: {', '.join(p['non_driver_objects'])} "
                     "(not shown as cars; see [f1_tv] safety_car_position_keys)")
    lines.append(f"CarData.z: {cd.get('fresh', 0)}/{cd.get('drivers', 0)} drivers with fresh telemetry "
                 f"({cd.get('with_data', 0)} with any); channels seen: {', '.join(cd.get('channels') or []) or '-'}")
    sc = rep.get("safety_car_position") or {}
    lines.append(f"Safety car position: {'AVAILABLE' if sc.get('available') else 'not available'} - {sc.get('reason')}")
    for k, label in (("track_geometry", "Track geometry"), ("tyres", "Tyre data"), ("race_control", "Race control"),
                     ("weather", "Weather"), ("track_status", "Track status")):
        v = rep.get(k) or {}
        lines.append(f"{label}: {'✓' if v.get('available') else '✗'} {v.get('detail') or ''}".rstrip())
    lines.append("====================================")
    return "\n".join(lines)


def now_mono() -> float:
    return time.monotonic()
