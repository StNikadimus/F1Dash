/* /tv - one page for the TV: the dashboard (iframe, layout forced for this window) and the server's live
   VOYO stream (HLS from tools/voyo_capture.py, only while the server VOYO player records) laid exactly over
   the dashboard's video slot. Keys: 1 race view, 2 video, 3 dashboard, M sound, F fullscreen. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// access: the approved /tv session (HttpOnly cookie set by the server after the trusted remote approved)
let layout = "RACE_VIEW", hls = null, loaded = false, status = null, serverOffset = 0, playing = false, streamErr = "";
try { layout = localStorage.getItem("f1tv-layout") || layout; } catch (e) { /* ignore */ }

const video = $("live"), dash = $("dash"), vbox = $("vbox");
const LIVE_URL = "/tv/live/index.m3u8";

function dur(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : m ? `${m} min` : `${s} s`; }

/* ------------------------------------------------------------------ layout: the dashboard + the video over its slot */
function setLayout(l) {
  layout = l;
  try { localStorage.setItem("f1tv-layout", l); } catch (e) { /* ignore */ }
  document.querySelectorAll("#layouts button").forEach((b) => b.classList.toggle("sel", b.dataset.l === l));
  dash.src = "/?layout=" + l;
  place();
}
function slotRect() {
  try {
    const slot = dash.contentDocument && dash.contentDocument.getElementById("video-slot");
    if (!slot) return null;
    const r = slot.getBoundingClientRect();
    return r.width > 10 && r.height > 10 ? r : null;
  } catch (e) { return null; }
}
function place() {
  const r = layout === "FULL_DASHBOARD" ? null : slotRect();
  vbox.classList.toggle("shown", !!r);
  if (!r) { if (!video.paused && layout === "FULL_DASHBOARD") video.pause(); return; }
  const f = dash.getBoundingClientRect();
  Object.assign(vbox.style, { left: f.left + r.left + "px", top: f.top + r.top + "px", width: r.width + "px", height: r.height + "px" });
  if (loaded && video.paused) video.play().catch(() => {});
}
dash.addEventListener("load", () => { setTimeout(place, 300); setTimeout(place, 1500); });
window.addEventListener("resize", () => setTimeout(place, 100));
setInterval(place, 1000);                               // the dashboard re-lays itself out (TV mode, banners)

/* ------------------------------------------------------------------ the live stream */
function attach() {
  if (loaded) return;
  loaded = true;
  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({
      liveSyncDurationCount: 3, maxLiveSyncPlaybackRate: 1.1, lowLatencyMode: false, backBufferLength: 30,
      xhrSetup: (xhr) => { xhr.withCredentials = true; },
    });
    hls.on(Hls.Events.ERROR, (_e, d) => {
      if (d.response && d.response.code === 401) { location.reload(); return; }     // session ended -> request page
      if (d.fatal) {
        streamErr = /codec|buffer(Add|Append)/i.test(d.details || "") ? "this browser cannot decode the stream (H.264) - use Chrome, Edge, Firefox or Safari"
          : `stream error (${d.details || d.type}) - retrying`;
        detach();                                      // the next status poll tries again
      }
    });
    hls.loadSource(LIVE_URL);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {          // Safari / iOS: native HLS
    video.src = LIVE_URL;
  } else {
    loaded = false; showOff("THIS BROWSER CANNOT PLAY THE STREAM", "use Chrome, Edge, Firefox or Safari", "warn");
    return;
  }
  video.play().catch(() => {});
}
function detach() {
  loaded = false; playing = false;
  if (hls) { hls.destroy(); hls = null; }
  video.removeAttribute("src"); video.load();
}
function showOff(main, sub, cls) {
  $("offair").hidden = false;
  $("oa-main").textContent = main; $("oa-main").className = "oa-main " + (cls || "");
  $("oa-sub").textContent = sub || "";
}
video.addEventListener("playing", () => { playing = true; streamErr = ""; $("offair").hidden = true; $("unmute").hidden = !video.muted; });
for (const ev of ["waiting", "emptied", "error", "pause"]) video.addEventListener(ev, () => { playing = false; });

/* ------------------------------------------------------------------ server status (every 3 s) */
async function poll() {
  try {
    const r = await fetch("/api/tv/status", { cache: "no-store" });
    if (r.status === 401) { location.reload(); return; }        // revoked / expired: the server shows the request page
    status = await r.json();
  } catch (e) { showOff("NO CONNECTION", "the F1 server is not reachable", "warn"); setCtl('<span class="off">● NO CONNECTION</span>'); return; }
  serverOffset = status.now * 1000 - Date.now();
  const lv = status.live || {}, st = status.state || {};
  if (lv.on_air) {
    attach();
    const sess = status.session ? [status.session.meeting, status.session.session_name].filter(Boolean).join(" · ") : "";
    let lag = "";
    if (hls && hls.playingDate) lag = ` · picture ${((Date.now() + serverOffset - hls.playingDate.getTime()) / 1000).toFixed(0)} s behind the capture`;
    setCtl(`<span class="on">● ON AIR</span> ${esc(sess)}${esc(lag)}`);
    if (!playing) showOff(streamErr ? "STREAM PROBLEM" : "STARTING THE STREAM…", streamErr || sess, streamErr ? "warn" : "rec");
  } else {
    if (loaded) detach();
    const nxt = status.next;
    let sub = st.detail || "";
    if (nxt && nxt.open_from) {
      const secs = nxt.open_from - (Date.now() + serverOffset) / 1000;
      sub = `next: ${nxt.meeting || ""} ${nxt.session_name || ""} - the stream starts ${secs > 0 ? "in " + dur(secs) : "soon"}`;
    }
    const main = st.state === "RECORDING" ? "RECORDING - LIVE STREAM STARTING" :
      st.state === "OPENING" ? "VOYO IS OPENING…" : "NO LIVE STREAM";
    showOff(main, sub, st.state === "RECORDING" || st.state === "OPENING" ? "rec" : st.level === "warn" || st.level === "bad" ? "warn" : "");
    setCtl(`<span class="off">● OFF AIR</span> ${esc(st.state || "")}`);
  }
}
function setCtl(html) { $("ctl-status").innerHTML = html; }
setInterval(poll, 3000); poll();

/* ------------------------------------------------------------------ controls */
let hideTimer = null;
function showCtl() { $("ctl").classList.remove("hide"); clearTimeout(hideTimer); hideTimer = setTimeout(() => $("ctl").classList.add("hide"), 4000); }
document.addEventListener("mousemove", showCtl); document.addEventListener("touchstart", showCtl, { passive: true });
document.querySelectorAll("#layouts button").forEach((b) => b.addEventListener("click", () => setLayout(b.dataset.l)));
function toggleSound() { video.muted = !video.muted; if (!video.muted) video.play().catch(() => {}); $("unmute").hidden = !video.muted; }
$("b-sound").addEventListener("click", toggleSound);
$("unmute").addEventListener("click", toggleSound);
function toggleFull() { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {}); }
$("b-full").addEventListener("click", toggleFull);
$("b-tok").addEventListener("click", async () => {
  if (!confirm("Log this TV out? It will need approval again.")) return;
  await fetch("/api/tv/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
  location.reload();
});
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  const k = e.key.toLowerCase();
  if (k === "1") setLayout("RACE_VIEW"); else if (k === "2") setLayout("VIDEO_FOCUS"); else if (k === "3") setLayout("FULL_DASHBOARD");
  else if (k === "m") toggleSound(); else if (k === "f") toggleFull(); else return;
  showCtl();
});
setLayout(layout); showCtl();
