/* VOYO playback clock probe - evaluated by tools/voyo_clock.py in the VOYO page
 * through the browser's LOCAL DevTools port (Runtime.evaluate, returnByValue).
 *
 * READ-ONLY. It reads what every web page may read from its own <video> element:
 *   currentTime, paused, playbackRate, readyState, seeking, ended, duration,
 *   buffered, seekable  (+ the expando values length / startAt / drmProtected /
 *   mediaId / title that VOYO puts on the element, and the page title - used for
 *   display and to recognise which F1 session the recording shows)
 * and listens to the standard media events (play, pause, seeking, seeked,
 * ratechange, waiting, playing, stalled, emptied, loadstart, ended).
 *
 * It does NOT touch the media stream, MediaSource/EME, licences, keys, network
 * requests or frames; it does not capture, copy or record anything, and it does
 * not change the player's state.
 *
 * Also reported (diagnostics): document.visibilityState, whether a text field has
 * the focus (so the TV agent leaves typing keys to the page) and the element's
 * standard MediaError code.
 *
 * Result: one plain JSON object, or {found:false} when the page has no video.
 */
(() => {
  const num = (x) => (typeof x === "number" && isFinite(x) ? x : null);
  const S = window.__f1dashClock2 || (window.__f1dashClock2 = { v: null, events: [], videos: 0, scanAt: 0, metaAt: 0 });
  const now = Date.now();
  // The full document / shadow-root scan runs at most every 2 s (or when the cached
  // element left the page) - not on every 5 Hz poll, so VOYO's own player keeps the
  // page's main thread.
  let v = S.v;
  if (!v || !v.isConnected || !(now - S.scanAt <= 2000)) {
    const videos = [];
    const collect = (root) => {
      // documents, same-origin iframes and open shadow roots (web-component players)
      try {
        videos.push(...root.querySelectorAll("video"));
        for (const f of root.querySelectorAll("iframe")) {
          try { if (f.contentDocument) collect(f.contentDocument); } catch (e) { /* cross-origin frame */ }
        }
        const w = (root.ownerDocument || root).createTreeWalker(root, 1 /* SHOW_ELEMENT */);
        for (let n = w.nextNode(), k = 0; n && k < 20000; n = w.nextNode(), k++) {
          if (n.shadowRoot) collect(n.shadowRoot);
        }
      } catch (e) { /* ignore */ }
    };
    collect(document);
    S.scanAt = now;
    S.videos = videos.length;
    // the main player: VOYO's own player element (it carries mediaId / length), a loaded
    // stream, playing and large beat a small preview / promo / placeholder video
    v = null;
    let best = -1;
    for (const c of videos) {
      const r = c.getBoundingClientRect();
      const voyo = (c.mediaId != null || typeof c.length === "number") ? 1e13 : 0;
      const loaded = isFinite(c.duration) && c.duration > 0 ? 1e11 : 0;
      const score = voyo + loaded + (c.paused ? 0 : 1e10) + c.readyState * 1e9 + Math.min(r.width * r.height, 1e8);
      if (score > best) { best = score; v = c; }
    }
  }
  // the user is typing in the VOYO page (login, PIN, search): the TV agent then does
  // not take letter / digit keys away from the page
  let act = document.activeElement;
  try { while (act && act.shadowRoot && act.shadowRoot.activeElement) act = act.shadowRoot.activeElement; } catch (e) { /* ignore */ }
  const typing = !!act && (/^(INPUT|TEXTAREA|SELECT)$/.test(act.tagName) || act.isContentEditable === true);
  const visibility = document.visibilityState;
  if (!v) { S.v = null; return { found: false, typing, visibility }; }
  if (S.v !== v) {
    S.v = v;
    S.metaAt = 0;
    S.events.length = 0;
    for (const type of ["play", "pause", "seeking", "seeked", "ratechange", "waiting", "playing",
                        "stalled", "emptied", "loadstart", "ended", "durationchange"]) {
      v.addEventListener(type, () => {
        S.events.push({ type, pb: num(v.currentTime) });
        if (S.events.length > 50) S.events.shift();
      }, { passive: true });
    }
  }
  const range = (r) => {
    try { return r && r.length ? [r.start(0), r.end(r.length - 1)] : [null, null]; } catch (e) { return [null, null]; }
  };
  const [ss, se] = range(v.seekable);
  let be = null;
  try {
    for (let i = 0; i < v.buffered.length; i++) {
      if (v.buffered.start(i) <= v.currentTime + 0.5 && v.buffered.end(i) >= v.currentTime) be = v.buffered.end(i);
    }
  } catch (e) { /* ignore */ }
  const len = num(v.length);
  const str = (x) => (typeof x === "string" || typeof x === "number") && String(x).trim() ? String(x).trim().slice(0, 200) : null;
  if (!S.meta || !(now - S.metaAt <= 5000)) {
    S.metaAt = now;
    const og = document.querySelector('meta[property="og:title"]');
    const metaContent = (sel) => { const m = document.querySelector(sel); return m ? str(m.getAttribute("content")) : null; };
    let published = metaContent('meta[property="article:published_time"]') || metaContent('meta[itemprop="uploadDate"]') ||
      metaContent('meta[property="og:video:release_date"]');
    if (!published) {
      for (const sc of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
          const j = JSON.parse(sc.textContent || "{}");
          for (const o of Array.isArray(j) ? j : [j]) {
            const d = o && (o.uploadDate || o.datePublished);
            if (typeof d === "string") { published = d.slice(0, 40); break; }
          }
        } catch (e) { /* not JSON */ }
        if (published) break;
      }
    }
    S.meta = { og: og ? str(og.getAttribute("content")) : null, published };
  }
  // page / recording metadata for session detection (titles and the media id only -
  // never tokens, cookies, storage, URLs with query strings or licence data)
  return {
    found: true,
    playback_time: num(v.currentTime),
    paused: !!v.paused,
    playback_rate: num(v.playbackRate),
    ready_state: v.readyState,
    seeking: !!v.seeking,
    ended: !!v.ended,
    duration: num(v.duration),
    seekable_start: num(ss),
    seekable_end: num(se),
    buffered_end: num(be),
    timestamp_local: Date.now() / 1000,
    // identifies the stream (page + programme length) - used to restore a calibration
    asset: location.host + location.pathname + "|" + (len !== null ? len : ""),
    meta: {
      length: len,
      startAt: num(v.startAt),
      drmProtected: typeof v.drmProtected === "boolean" ? v.drmProtected : null,
    },
    page: {
      title: str(document.title),
      og_title: S.meta.og,
      media_title: str(v.title) || str(v.videoTitle),
      media_id: str(v.mediaId),
      url_path: str(location.pathname),          // path only - never the query string
      published: S.meta.published,               // publish / upload date of the recording, if the page states it
    },
    videos: S.videos,                              // diagnostics: how many <video> elements were seen
    typing,                                        // a text field of the page has the focus
    visibility,                                    // "hidden" = the browser treats the page as not shown
    // the player's own MediaError, if any (code 1-4; diagnostics only)
    error: v.error ? { code: v.error.code, message: str(v.error.message) } : null,
    events: S.events.splice(0),
  };
})()
