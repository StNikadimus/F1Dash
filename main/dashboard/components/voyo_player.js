/* VOYO video layer - separate component, independent of the F1 data pipeline.
 *
 * Modes (decided by the server from config [voyo]):
 *   window : VOYO's official player runs in its own browser window placed over this
 *            slot by tools/tv_launcher.py. The slot shows a placeholder only.
 *   embed  : iframe of the configured VOYO page - only when the server verified
 *            that VOYO's headers allow framing (never bypassed).
 *   hls    : <video> for an OFFICIALLY provided external-player stream URL.
 * Nothing here decrypts, records, proxies or re-distributes video.
 *
 * API:  VoyoPlayer.init(slotEl, toastFn)
 *       VoyoPlayer.setInfo(videoMsg)      // {mode, url, hls_url, notice, ...}
 *       VoyoPlayer.setUI(uiMsg)           // tv_mode_effective, video_focus, video_cmd
 */
(() => {
  "use strict";
  let slot = null, toast = () => {}, info = {}, ui = {};
  let lastCmd = null, mode = null, visible = false;
  let video = null, frame = null, hls = null, statusEl = null, retry = 0, retryTimer = null, stallTimer = null;
  const HLS_JS = "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js";

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; }
  function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text || "";
    statusEl.className = "vp-status" + (kind ? " vp-" + kind : "");
    statusEl.hidden = !text;
  }

  // ------------------------------------------------------------------ build per mode
  function build() {
    teardown();
    mode = info.mode;
    slot.innerHTML = "";
    slot.dataset.mode = mode || "";
    const focusHint = el("div", "vp-focus-hint",
      "VIDEO FOCUS · ◀ ▶ seek · ▲ ▼ volume · OK play/pause · BACK exit");
    if (mode === "window") {
      slot.appendChild(el("div", "vp-placeholder",
        `<div class="vp-logo">VOYO</div>
         <div class="vp-line">Official VOYO player window goes here</div>
         <div class="vp-small">Start it with <b>python tools/tv_launcher.py</b> – the window is placed exactly over this area.<br>
         Play/pause, volume and seeking are done in VOYO's own player.</div>`));
    } else if (mode === "embed" && info.url) {
      frame = el("iframe", "vp-frame");
      frame.src = info.url;
      frame.allow = "autoplay; fullscreen; picture-in-picture; encrypted-media";
      frame.referrerPolicy = "strict-origin-when-cross-origin";
      frame.title = "VOYO";
      slot.appendChild(frame);
    } else if (mode === "hls" && info.hls_url) {
      video = el("video", "vp-video");
      video.playsInline = true; video.autoplay = true; video.muted = true; video.controls = false;
      video.addEventListener("playing", () => { retry = 0; setStatus(""); });
      video.addEventListener("waiting", () => { setStatus("BUFFERING…"); armStall(); });
      video.addEventListener("pause", () => { if (!video.ended) setStatus("PAUSED", "info"); });
      video.addEventListener("error", () => offline("media error"));
      slot.appendChild(video);
      loadHls();
    } else {
      slot.appendChild(el("div", "vp-placeholder", `<div class="vp-line">VIDEO NOT CONFIGURED</div>`));
    }
    statusEl = el("div", "vp-status"); statusEl.hidden = true;
    slot.appendChild(statusEl);
    slot.appendChild(focusHint);
  }

  function teardown() {
    clearTimeout(retryTimer); clearTimeout(stallTimer);
    if (hls) { try { hls.destroy(); } catch (e) { /* ignore */ } hls = null; }
    if (video) { try { video.pause(); video.removeAttribute("src"); video.load(); } catch (e) { /* ignore */ } }
    video = null; frame = null;
  }

  // ------------------------------------------------------------------ HLS
  function loadScript(src) {
    return new Promise((res, rej) => {
      if (window.Hls) return res();
      const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  async function loadHls() {
    if (!video) return;
    setStatus("LOADING VIDEO…");
    const url = info.hls_url;
    try {
      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url;
      } else {
        await loadScript(HLS_JS);
        if (!window.Hls || !window.Hls.isSupported()) throw new Error("HLS not supported in this browser");
        hls = new window.Hls({ liveSyncDurationCount: 3, enableWorker: true });
        hls.on(window.Hls.Events.ERROR, (_, d) => { if (d && d.fatal) offline(d.details || "stream error"); });
        hls.loadSource(url);
        hls.attachMedia(video);
      }
      if (visible) await video.play().catch(() => {});
    } catch (e) {
      offline(e.message || "load failed");
    }
  }
  function armStall() {
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => { if (video && video.readyState < 3 && visible) offline("stalled"); }, 15000);
  }
  function offline(reason) {
    retry += 1;
    const d = Math.min(30, 2 ** Math.min(retry, 5));
    setStatus(`VIDEO OFFLINE · reconnecting in ${d} s (${reason})`, "error");
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => { if (mode === "hls") { build(); } }, d * 1000);
  }

  // ------------------------------------------------------------------ commands
  function command(action, arg) {
    if (mode !== "hls" || !video) {
      toast(mode === "window" ? "Video controls: use VOYO's own player window" :
            mode === "embed" ? "Video controls: use the controls inside VOYO's player" : "No video player");
      return;
    }
    switch (action) {
      case "play_pause":
        if (video.paused) video.play().catch(() => toast("Browser blocked playback – click the page once"));
        else video.pause();
        break;
      case "mute": {
        const want = !video.muted;
        video.muted = want;
        if (!want && video.paused) { video.muted = true; video.play().catch(() => {}); toast("Browser blocks sound until you click the page once"); }
        else toast(video.muted ? "Muted" : "Sound on");
        break;
      }
      case "volume": {
        video.muted = false;
        video.volume = Math.max(0, Math.min(1, video.volume + (arg === "up" ? 0.1 : -0.1)));
        toast(`Volume ${Math.round(video.volume * 100)}%`);
        break;
      }
      case "seek": {
        const s = video.seekable;
        const delta = parseInt(arg, 10) || 0;
        if (!s || !s.length || s.end(s.length - 1) - s.start(0) < 20) { toast("Seeking not supported for this live stream"); break; }
        const t = Math.max(s.start(0), Math.min(s.end(s.length - 1) - 2, video.currentTime + delta));
        video.currentTime = t;
        toast(`${delta > 0 ? "+" : ""}${delta} s`);
        break;
      }
      case "fullscreen":
        (slot.requestFullscreen ? slot.requestFullscreen() : Promise.reject())
          .catch(() => toast("Fullscreen needs a click in the browser – use TV mode VIDEO_FOCUS instead"));
        break;
    }
  }

  // ------------------------------------------------------------------ public
  window.VoyoPlayer = {
    init(slotEl, toastFn) { slot = slotEl; toast = toastFn || toast; },
    setInfo(msg) {
      const changed = !info || msg.mode !== info.mode || msg.url !== info.url || msg.hls_url !== info.hls_url;
      info = msg || {};
      if (slot && changed) build();
    },
    setUI(msg) {
      ui = msg || {};
      const show = ui.tv_mode_effective && ui.tv_mode_effective !== "FULL_DASHBOARD";
      if (show !== visible) {
        visible = show;
        if (video) { if (show) video.play().catch(() => {}); else video.pause(); }  // save CPU when hidden
      }
      if (slot) slot.classList.toggle("focused", !!ui.video_focus);
      const cmd = ui.video_cmd || {};
      if (lastCmd === null) { lastCmd = cmd.n || 0; return; }   // never replay an old command after (re)connect
      if (cmd.n && cmd.n !== lastCmd) {
        lastCmd = cmd.n;
        command(cmd.action, cmd.arg);
      }
    },
  };
})();
