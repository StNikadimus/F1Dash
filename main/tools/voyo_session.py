"""The recording of ONE F1 session by the server VOYO player - a state machine without a browser.

tools/voyo_server_player.py runs it: every few seconds it reads the page (PAGE_JS / EPISODES_JS below,
read-only), the screen recorder (tools/voyo_capture.py ``health()``) and the clock, calls
``SessionRecorder.step(obs)`` and carries out the actions it returns (open a page, press play, sign in,
restart the screen recorder, finish). All decisions are here, so they are tested without Chrome.

    DISCOVERING  read the event page -> the recordings on it (server/voyo_episodes.py)
       |  one recording of the session's kind            none / several: NOT_FOUND / AMBIGUOUS (retried,
       v                                                  shown on /disk - never another session instead)
    OPENING      open that recording's page
    VERIFYING    the player plays THAT recording (its id in mediaId / the HLS or DASH manifest) and the
       |         picture moves - only then the recorder starts         wrong / unverifiable: FAILED (retried)
       v
    RECORDING    the screen recorder runs; checked all the time: the video moves (stall -> play, then reload
       |         the page), still signed in (login -> sign in again), the recorder alive and its file growing
       |         (restart it), sound present. A recovery never starts a second recording: the package is the
       v         same (``key``), the server appends to it.
    FINISHING    the recording ended (VOD: at its end; live: the session window is over) -> the recorder
    DONE         stops, the last segment is uploaded, the package is closed with this report.

The ``key`` (session kind + episode id + day) is the package id on the server: one session = one package,
also across a crash or a restart of either side.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit

from server import voyo_episodes as ve

KEY_RE = re.compile(r"^[a-z0-9_]{2,24}-[0-9]{5,12}-[0-9]{8}$")

# the open page: its main video, what it shows, the manifests it loaded (path only - the query string with
# its tokens is cut off IN the page), a visible sign-in form. Read-only.
PAGE_JS = r"""(() => {
  const W = window.__f1rec || (window.__f1rec = {manifests: []});
  if (!W.obs && window.PerformanceObserver) {
    const keep = (name) => { try { const u = new URL(name, location.href);
      if (/\.(mpd|m3u8)$/i.test(u.pathname)) { const p = u.origin + u.pathname;
        if (W.manifests[W.manifests.length - 1] !== p) { W.manifests.push(p); if (W.manifests.length > 20) W.manifests.shift(); } } } catch (e) {} };
    try { performance.getEntriesByType('resource').forEach((e) => keep(e.name));
      W.obs = new PerformanceObserver((l) => l.getEntries().forEach((e) => keep(e.name)));
      W.obs.observe({type: 'resource', buffered: true}); } catch (e) { W.obs = true; }
  }
  const vis = (e) => !!(e && e.offsetParent !== null && e.getBoundingClientRect().width > 0);
  const login = !![...document.querySelectorAll('input[type=password]')].find(vis);
  const og = document.querySelector('meta[property="og:title"]');
  const base = {url_path: location.pathname, origin: location.origin, title: document.title,
                og_title: og ? og.getAttribute('content') : null, login_form: login,
                manifests: W.manifests.slice(-8), visibility: document.visibilityState};
  const vs = [...document.querySelectorAll('video')];
  if (!vs.length) return Object.assign(base, {video: false});
  const area = (v) => v.clientWidth * v.clientHeight;
  const v = vs.sort((a, b) => (b.mediaId != null) - (a.mediaId != null) || area(b) - area(a))[0];
  const num = (x) => typeof x === 'number' && isFinite(x) ? x : null;
  return Object.assign(base, {video: true, t: num(v.currentTime), paused: v.paused, ended: v.ended,
    ready: v.readyState, duration: num(v.duration), live: v.duration === Infinity, muted: v.muted,
    volume: num(v.volume), visible: area(v) > 0, error: v.error ? v.error.code : null,
    media_id: v.mediaId != null ? String(v.mediaId) : null,
    media_title: typeof v.title === 'string' && v.title ? v.title : null});
})()"""

# the event page: every recording card with the texts a person sees around it, and the recordings the page
# describes in JSON-LD. VOYO's own cards are <a class="episode" data-uniq="media/<id>" onclick="playEpisode(..)">
# whose href is the event page itself; older pages linked /episodes/<id>. The attributes are only READ - nothing
# is clicked and no handler is called (the ids are parsed in server/voyo_episodes.py). Scrolls to the end first
# (lazy-loaded rows). Read-only.
EPISODES_JS = r"""(async () => {
  for (let i = 0; i < 4; i++) { window.scrollTo(0, document.body.scrollHeight); await new Promise((r) => setTimeout(r, 400)); }
  window.scrollTo(0, 0);
  const out = [];
  const SEL = 'a[href*="/episode"], a.episode, a[data-uniq]';
  const txt = (e) => (e && (e.innerText || e.textContent) || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const attr = (e, n) => (e.getAttribute(n) || '').slice(0, 400);
  const eid = (h) => { const m = /\/episodes?\/(\d{5,12})/.exec(h || ''); return m ? m[1] : null; };
  const cardId = (a) => { const u = /^\s*(?:media\/)?(\d{5,12})\s*$/.exec(attr(a, 'data-uniq'));
    const h = /(?:playEpisode|onPlayClick)\(\s*["'](\d{5,12})["']/.exec(attr(a, 'onclick'));
    return (u && u[1]) || (h && h[1]) || eid(a.href); };
  for (const a of new Set(document.querySelectorAll(SEL))) {
    // the card of THIS recording: climb while the parent holds cards of this recording only (never up to
    // a row / list that also holds the other recordings - their titles would mix in)
    const id = cardId(a);
    let card = a, k = 0;
    while (id && card.parentElement && k < 5 && txt(card.parentElement).length < 600 &&
           [...card.parentElement.querySelectorAll(SEL)].every((x) => cardId(x) === id)) {
      card = card.parentElement; k++;
    }
    const img = a.querySelector('img');
    out.push({href: a.href, data_uniq: attr(a, 'data-uniq'), onclick: attr(a, 'onclick'), text: txt(a),
              label: a.getAttribute('aria-label') || '', title: a.getAttribute('title') || '',
              alt: img ? img.getAttribute('alt') || '' : '', card: txt(card)});
  }
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const j = JSON.parse(s.textContent || 'null');
      const walk = (o) => { if (!o || typeof o !== 'object') return; if (Array.isArray(o)) return o.forEach(walk);
        if (typeof o.url === 'string' && /\/episodes?\/\d+/.test(o.url)) out.push({href: o.url, ld_name: String(o.name || ''),
          card: String(o.description || '').slice(0, 300)});
        Object.values(o).forEach((x) => typeof x === 'object' && walk(x)); };
      walk(j); } catch (e) {}
  }
  return {url_path: location.pathname, origin: location.origin, title: document.title, items: out.slice(0, 400)};
})()"""


@dataclass
class Target:
    """The session to record (the F1 schedule window, or the ``record`` command)."""
    kind: str
    meeting: Optional[str] = None
    session_name: Optional[str] = None
    start: Optional[float] = None          # scheduled start (epoch s)
    until: Optional[float] = None          # the window closes (epoch s) - live: the recording ends then

    def label(self) -> str:
        return " ".join(x for x in (self.meeting, self.session_name or ve.KIND_LABELS.get(self.kind)) if x)


@dataclass
class Limits:
    discover_retry_s: float = 60.0         # NOT_FOUND / AMBIGUOUS: read the event page again after this
    verify_timeout_s: float = 120.0        # the recording must be verified + moving within this
    retry_after_fail_s: float = 120.0      # FAILED (wrong / unverifiable recording): try again after this
    stall_play_s: float = 20.0             # the video does not move: press play
    stall_reload_s: float = 75.0           # ... still not: open the recording's page again
    grow_restart_s: float = 30.0           # the recorder's file does not grow (it grows every ~2 s): restart it
    no_audio_warn_s: float = 90.0          # no sound from the player this long: shown as a problem
    login_retry_s: float = 300.0
    end_margin_s: float = 2.0              # VOD: this close to its length counts as its end
    vod_from_start: bool = True            # a finished recording is recorded from 0:00
    verification: str = "strict"           # strict = the id must be seen in the player; url = the page address is enough


@dataclass
class Obs:
    """One look at everything (filled by the player; tests build it by hand)."""
    now: float
    page: Optional[dict] = None            # PAGE_JS answer (None: the page could not be read)
    episodes: Optional[dict] = None        # EPISODES_JS answer, when DISCOVERING asked for it
    capture: Optional[dict] = None         # VoyoWindowCapture.health()
    audio_streams: Optional[int] = None    # Chrome's sound streams into the f1voyo sink (None = unknown)
    chrome_alive: bool = True


@dataclass
class SessionRecorder:
    target: Target
    event_url: str
    limits: Limits = field(default_factory=Limits)
    state: str = "DISCOVERING"
    episode: Optional[dict] = None         # the selected recording (Episode.to_json)
    episode_url: Optional[str] = None
    key: Optional[str] = None
    selection: Optional[dict] = None       # the last Selection.to_json (candidates when ambiguous)
    verification: Optional[dict] = None
    issues: list = field(default_factory=list)       # problems seen (time, text) - the report
    problem: str = ""                       # the current problem in words ("" = none)
    started_at: Optional[float] = None      # recording start (epoch s)
    recording_s: float = 0.0                # time spent in RECORDING (the recorder should cover this)
    recoveries: int = 0
    duration: Optional[float] = None        # the recording's length (VOD) - for the report
    live: Optional[bool] = None
    end_reason: str = ""
    _since: float = 0.0
    _next_try: float = 0.0
    _last_t: Optional[float] = None
    _moved_at: float = 0.0
    _verified_moves: int = 0
    _played_from: Optional[float] = None
    _max_t: float = 0.0
    _last_step: Optional[float] = None
    _audio_seen_at: Optional[float] = None
    _login_at: float = -1e18
    _stall_action: int = 0
    _rewound: bool = False
    _grow_restarted_at: float = -1e18
    _cap_err: Optional[tuple] = None

    # ------------------------------------------------------------------ helpers
    def _goto(self, state: str, now: float) -> None:
        self.state, self._since = state, now

    def _issue(self, now: float, text: str) -> None:
        self.problem = text
        if not self.issues or self.issues[-1][1] != text:
            self.issues.append((round(now), text[:200]))
            del self.issues[:-30]

    def recording_wanted(self) -> bool:
        return self.state == "RECORDING"

    def status(self) -> dict:
        """For the heartbeat -> /disk and /tv: what is being recorded and how it goes."""
        ep = self.episode or {}
        return {"state": self.state, "target": {"kind": self.target.kind, "label": self.target.label(),
                                                "meeting": self.target.meeting, "session_name": self.target.session_name},
                "key": self.key, "episode_id": ep.get("id"), "episode_title": ep.get("title"),
                "selection": (self.selection or {}).get("reason"),
                "candidates": [{"id": c.get("id"), "title": c.get("title"), "kind": c.get("kind")}
                               for c in (self.selection or {}).get("candidates", [])][:6],
                "verified": (self.verification or {}).get("reason"), "format": (self.verification or {}).get("format"),
                "live": self.live, "duration": self.duration, "started_at": self.started_at,
                "recording_s": round(self.recording_s), "position": round(self._max_t) if self._max_t else None,
                "recoveries": self.recoveries, "problem": self.problem or None,
                "issues": [{"at": a, "text": t} for a, t in self.issues[-6:]], "end_reason": self.end_reason or None}

    def report(self) -> dict:
        """What the package is closed with (server: the recording's completeness check)."""
        s = self.status()
        s["expected_s"] = round(self.recording_s)
        s["played_from"] = self._played_from
        s["played_to"] = round(self._max_t, 1) if self._max_t else None
        s["issues"] = [{"at": a, "text": t} for a, t in self.issues]
        return s

    # ------------------------------------------------------------------ the step
    def step(self, o: Obs) -> list:
        """-> actions: ("discover",), ("navigate", url), ("play",), ("unmute",), ("seek0",), ("login",),
        ("restart_capture",), ("finish", reason)."""
        now, acts = o.now, []
        if self._last_step is not None and self.state == "RECORDING":
            self.recording_s += max(0.0, min(now - self._last_step, 30.0))
        self._last_step = now
        st = self.state
        if st in ("DONE",):
            return acts
        if self.target.until and now > self.target.until and st != "RECORDING":
            self.end_reason = "the session window closed before the recording could start" if not self.started_at \
                else self.end_reason or "the session window closed"
            self._goto("DONE", now)
            return [("finish", self.end_reason)] if self.started_at else acts
        if st in ("DISCOVERING", "NOT_FOUND", "AMBIGUOUS", "FAILED"):
            if now < self._next_try:
                return acts
            if o.episodes is None:
                return [("discover",)]
            return self._select(o, now)
        if st == "OPENING":
            self._goto("VERIFYING", now)
            return [("navigate", self.episode_url)]
        if st == "VERIFYING":
            return self._verify(o, now)
        if st == "RECORDING":
            return self._watch(o, now)
        return acts

    def _select(self, o: Obs, now: float) -> list:
        page = o.episodes or {}
        eps = ve.episodes_from_page(page.get("items") or [])
        sel = ve.select_episode(eps, self.target.kind, self.target.meeting, self.target.start)
        self.selection = sel.to_json()
        if sel.state != "SELECTED":
            self._goto(sel.state, now)
            self._next_try = now + self.limits.discover_retry_s
            self._issue(now, f"{self.target.label()}: {sel.reason}")
            return []
        if not sel.episode.path:                                  # named by the page, but nowhere to open it
            self._goto("FAILED", now)
            self._next_try = now + self.limits.retry_after_fail_s
            self._issue(now, f"{self.target.label()}: the event page shows recording {sel.episode.id} but not the "
                             "address that opens it - not opened")
            return []
        origin = str(page.get("origin") or "")
        url = urljoin(origin + "/", sel.episode.path)
        if urlsplit(url).scheme not in ("http", "https") or (origin and not url.startswith(origin)):
            self._goto("FAILED", now)
            self._next_try = now + self.limits.retry_after_fail_s
            self._issue(now, "the recording's link leads off the VOYO site - not opened")
            return []
        self.episode, self.episode_url = sel.episode.to_json(), url
        day = datetime.fromtimestamp(self.target.start or now, timezone.utc).strftime("%Y%m%d")
        self.key = f"{self.target.kind}-{sel.episode.id}-{day}"
        self.problem = ""
        self._goto("OPENING", now)
        return [("note", sel.reason)]

    def _verify(self, o: Obs, now: float) -> list:
        p = o.page or {}
        if not o.chrome_alive:
            return []                                             # the player restarts Chrome on the same page
        if p.get("login_form") and now - self._login_at > self.limits.login_retry_s:
            self._login_at = now
            return [("login",), ("navigate", self.episode_url)]
        v = ve.verify_episode(self.episode["id"], self.target.kind, p)
        if v.state == "UNVERIFIED" and self.limits.verification == "url" and \
                ve.episode_id(p.get("url_path")) == self.episode["id"] and p.get("video"):
            v = ve.Verification("VERIFIED", f"episode {self.episode['id']} page open (address only - verification = url)",
                                ["page address"])
        self.verification = {"state": v.state, "reason": v.reason, "format": v.format}
        if v.state == "WRONG":
            return self._fail(now, f"wrong recording: {v.reason}")
        acts = []
        moving = self._moving(p, now)
        if p.get("video") and p.get("paused") and not p.get("ended") and (p.get("ready") or 0) >= 2:
            acts.append(("play",))
        if p.get("video") and p.get("muted"):
            acts.append(("unmute",))
        live = p.get("live") or p.get("duration") is None
        if v.state == "VERIFIED" and moving and self._verified_moves >= 2:
            self.live, self.duration = bool(live), p.get("duration")
            self._played_from = round(p.get("t") or 0.0, 1)
            self.started_at = now
            self._moved_at = now
            self.problem = ""
            self._goto("RECORDING", now)
            return acts + [("note", f"recording {self.target.label()}: {v.reason}")]
        if now - self._since > self.limits.verify_timeout_s:
            why = v.reason if v.state != "VERIFIED" else "the video does not play (no movement)"
            return self._fail(now, f"could not confirm the recording within {self.limits.verify_timeout_s:.0f} s: {why}")
        return acts

    def _moving(self, p: dict, now: float) -> bool:
        t = p.get("t")
        moved = t is not None and self._last_t is not None and t > self._last_t + 0.2 and not p.get("paused")
        if moved:
            self._moved_at = now
            self._verified_moves += 1
            self._max_t = max(self._max_t, t)
        elif t is not None and self._last_t is not None and t < self._last_t - 5:
            self._verified_moves = 0                              # jumped back (seek / reload): count again
        self._last_t = t if t is not None else self._last_t
        return moved

    def _fail(self, now: float, why: str) -> list:
        self._issue(now, why)
        self._goto("FAILED", now)
        self._next_try = now + self.limits.retry_after_fail_s
        self._verified_moves = 0
        self._last_t = None
        return []

    def _watch(self, o: Obs, now: float) -> list:
        p, acts = o.page or {}, []
        if not o.chrome_alive:
            self._issue(now, "Chrome stopped - opened again")
            self.recoveries += 1
            return []
        if p.get("login_form"):
            if now - self._login_at > self.limits.login_retry_s:
                self._login_at = now
                self._issue(now, "VOYO signed the player out - signing in again")
                self.recoveries += 1
                return [("login",), ("navigate", self.episode_url)]
            return []
        if o.page is not None and p.get("video"):
            v = ve.verify_episode(self.episode["id"], self.target.kind, p)
            if v.state == "WRONG":                              # the page went on to another recording
                if self._near_end():
                    return self._end(now, "the recording ended (the page went on to the next one)")
                self._issue(now, f"the player switched away: {v.reason} - opening the recording again")
                self.recoveries += 1
                self._last_t = None
                return [("navigate", self.episode_url)]
        if not self.live and self.limits.vod_from_start and not self._rewound and (self._played_from or 0) > 2:
            # a finished recording is recorded from 0:00: back to its start once the recorder runs (before,
            # the first seconds would be lost to the check that it is the right recording)
            cap = o.capture or {}
            if o.capture is None or cap.get("alive") or now - (self.started_at or now) > 20:
                self._rewound = True
                self._played_from = 0.0
                self._last_t, self._moved_at = None, now
                return [("seek0",)]
        if p.get("video") and p.get("t") is not None and not p.get("seeking"):
            self._max_t = max(self._max_t, float(p["t"]))
        if self._ended(p):
            return self._end(now, "the recording ended")
        if p.get("muted"):
            acts.append(("unmute",))
        moving = self._moving(p, now) if o.page is not None else False
        if moving:
            self._stall_action = 0
            if self.problem.startswith("the video"):
                self.problem = ""
        else:
            idle = now - self._moved_at
            if idle > self.limits.stall_reload_s and self._stall_action < 2:
                self._stall_action = 2
                self.recoveries += 1
                self._issue(now, f"the video has not moved for {idle:.0f} s - opening the recording again")
                self._last_t = None
                self._moved_at = now
                return acts + [("navigate", self.episode_url)]
            if idle > self.limits.stall_play_s and self._stall_action < 1:
                self._stall_action = 1
                self._issue(now, f"the video has not moved for {idle:.0f} s - pressing play")
                acts.append(("play",))
            if idle > self.limits.stall_reload_s * 2 and self._stall_action == 2:
                self._stall_action = 0                       # allow the next round of recovery
        cap = o.capture or {}
        if cap:
            err, fails = cap.get("last_error"), cap.get("failures")
            seen = fails if fails is not None else err
            if self._cap_err is None:
                self._cap_err = ("start", seen)               # what was there before this recording started
            elif seen and ("start", seen) != self._cap_err and seen != self._cap_err:
                self._cap_err = seen                          # the recorder stopped (and was restarted by itself)
                self.recoveries += 1
                self._issue(now, f"the screen recorder stopped and was restarted ({str(err or 'ffmpeg stopped')[:120]})")
            grew = cap.get("grew_age_s")
            if not cap.get("alive"):
                if now - self.started_at > 30:
                    self._issue(now, f"the screen recorder is not running ({cap.get('last_error') or 'starting'})")
            elif grew is not None and grew > self.limits.grow_restart_s and now - self._grow_restarted_at > 120:
                self._grow_restarted_at = now
                self.recoveries += 1
                self._issue(now, f"the recording file has not grown for {grew:.0f} s - restarting the recorder")
                acts.append(("restart_capture",))
            elif self.problem.startswith(("the screen recorder", "the recording file")) and grew is not None and grew < 10:
                self.problem = ""
        if o.audio_streams:
            self._audio_seen_at = now
            if self.problem.startswith("no sound"):
                self.problem = ""
        elif o.audio_streams == 0:
            ref = self._audio_seen_at or self.started_at
            if now - ref > self.limits.no_audio_warn_s:
                self._issue(now, f"no sound from the player for {now - ref:.0f} s")
        if self.target.until and now > self.target.until and (self.live or self.duration is None):
            return acts + self._end(now, "the session window is over")
        return acts

    def _near_end(self) -> bool:
        return bool(self.duration and self._max_t >= self.duration - 30)

    def _ended(self, p: dict) -> bool:
        if not p.get("video"):
            return False
        if p.get("ended"):
            return True
        if self.live:
            return False
        dur = p.get("duration") or self.duration
        return bool(dur and (p.get("t") or 0) >= dur - self.limits.end_margin_s)

    def _end(self, now: float, why: str) -> list:
        self.end_reason = why
        self._goto("DONE", now)
        return [("finish", why)]
