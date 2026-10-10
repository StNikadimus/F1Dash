/* /tv - one page for the TV: the dashboard (iframe, layout forced for this window) and the server's live
   VOYO stream (HLS from tools/voyo_capture.py, only while the server VOYO player records) laid exactly over
   the dashboard's video slot. Keys: 1 race view, 2 video, 3 dashboard, M sound, F fullscreen.

   ACCESS - every load of this page needs a NEW approval on the trusted phone (/remote), checked by the
   server: the page asks for a challenge, the phone approves exactly that one, and the server then gives
   THIS page its session (HttpOnly cookie + a page secret that exists only in the variable PAGE below -
   never in storage). A reload, a new tab, another browser start from zero; the server ends the earlier
   authorization of this browser whenever /tv is loaded. Nothing of the TV starts before the approval. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ authorization of this page load
   One approval request at a time, and only while this page exists:
   * every asynchronous step (request, status poll, retry timer) belongs to one flow; a step whose flow was
     replaced, denied or abandoned does nothing - a late answer can neither restart polling nor log in;
   * DENIED ends it for this page instance: nothing asks again (no button, no retry, no back/forward
     restore) until the page is loaded again;
   * leaving / closing the page (pagehide) stops every timer and call and withdraws its open request on the
     server (best effort - sendBeacon with its own challenge; otherwise the request just expires). */
let PAGE = null, challenge = null, authTimer = null, authTotal = 120, leaving = false;
let flow = 0, denied = false, gone = false, inflight = null;
const next = (() => { const n = new URLSearchParams(location.search).get("next") || "/tv";
  return /^\/[A-Za-z0-9_\-\/]*$/.test(n) && !n.startsWith("//") ? n : "/tv"; })();
function authShow(title, msg, cls) { $("a-title").textContent = title; $("a-msg").textContent = msg; $("a-msg").className = "msg " + (cls || ""); }
function stopFlow() {                                   // the current flow's timers and call end; its late answers are ignored
  flow++; clearTimeout(authTimer); authTimer = null;
  if (inflight) { inflight.abort(); inflight = null; }
}
function current(id) { return id === flow && !denied && !gone; }
function authPost(url, headers) {
  const ac = new AbortController(); inflight = ac;
  return fetch(url, { method: "POST", credentials: "same-origin", cache: "no-store", headers: headers || {}, signal: ac.signal })
    .finally(() => { if (inflight === ac) inflight = null; });
}
function withdraw(ch) {                                 // only this page can: its request cookie + its own challenge
  if (!ch) return;
  try { navigator.sendBeacon("/api/tv/logout", "challenge:" + ch); } catch (e) { /* it expires on its own */ }
}
async function ask() {
  if (denied || gone) return;                           // denied: only a new load of the page asks again
  stopFlow(); const id = flow;
  $("a-again").hidden = true; challenge = null;
  let r, b;
  try { r = await authPost("/api/tv/auth/request"); b = await r.json(); }
  catch (e) {
    if (!current(id)) return;
    authShow("NO CONNECTION", "the F1 server is not reachable - retrying", "bad"); authTimer = setTimeout(ask, 5000); return;
  }
  if (!current(id)) { if (b && b.challenge) withdraw(b.challenge); return; }   // abandoned meanwhile: end it there too
  if (!r.ok || !b.challenge) { authShow("NOT NOW", b.error || "the request was refused", "bad"); $("a-again").hidden = false; return; }
  challenge = b.challenge; authTotal = b.expires_in || 120;
  $("a-code").textContent = b.code;
  if (!b.approver_set) authShow("APPROVAL NEEDED", "No phone is set up to approve /tv yet: in /disk → SECURITY choose your /remote device (USE FOR AUTH), then ask again.", "bad");
  else authShow("APPROVE ON YOUR PHONE", `Open /remote on the trusted phone${b.approver_online ? "" : " (it is not connected right now)"} and approve the request with this code.`);
  authPoll(id);
}
async function authPoll(id) {
  if (!current(id)) return;
  const ch = challenge;
  let b;
  try { b = await (await authPost("/api/tv/auth/status", { "X-F1-TV-Challenge": ch })).json(); }
  catch (e) { if (current(id)) authTimer = setTimeout(() => authPoll(id), 3000); return; }
  if (!current(id) || ch !== challenge) return;         // a late answer of an ended flow
  if (b.status === "authenticated" && b.page) {
    stopFlow(); PAGE = b.page; challenge = null;
    authShow("APPROVED", "opening…", "ok");
    if (next !== "/tv") { leaving = true; location.href = next; return; }      // the plain dashboard ([security] protect_dashboard)
    start(); return;
  }
  if (b.status === "pending") {
    $("a-bar").style.width = Math.max(0, b.expires_in / authTotal * 100) + "%";
    authTimer = setTimeout(() => authPoll(id), 1500); return;
  }
  challenge = null; $("a-bar").style.width = "0";
  if (b.status === "denied") {
    stopFlow(); denied = true;
    authShow("DENIED", "the request was denied on the phone - this page will not ask again; reload it to start a new request", "bad");
    return;
  }
  const why = { expired: ["EXPIRED", "nobody answered in time"],
    superseded: ["REPLACED", "/tv was opened again in this browser - this request ended"],
    withdrawn: ["WITHDRAWN", "this request was withdrawn"],
    consumed: ["ALREADY USED", "this approval was already used"], none: ["NO REQUEST", "the request is gone"] }[b.status] || ["NO ACCESS", b.status];
  authShow(why[0], why[1], "bad"); $("a-again").hidden = false;
}
$("a-again").addEventListener("click", ask);
/* every protected request carries this page's secret; 401 = this page's authorization ended -> start again */
function tvFetch(url) {
  return fetch(url, { cache: "no-store", credentials: "same-origin", headers: { "X-F1-TV-Page": PAGE || "" } });
}
function ended() { PAGE = null; leaving = true; detach(); location.reload(); }
window.addEventListener("pagehide", () => {                 // leaving / closing / reloading: end it on the server too
  gone = true;
  const ch = challenge; challenge = null;
  stopFlow();                                              // no timer, poll or retry outlives the page
  withdraw(ch);                                            // an open request disappears from the phone
  if (PAGE && !leaving) { try { navigator.sendBeacon("/api/tv/logout", PAGE); } catch (e) { /* the next load ends it */ } }
  PAGE = null;
});
// back/forward cache: approve again (a new load) - but a denied page stays denied until it is reloaded by hand
window.addEventListener("pageshow", (e) => { if (e.persisted && !denied) location.reload(); });

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

/* ------------------------------------------------------------------ the live stream */
function attach() {
  if (loaded) return;
  loaded = true;
  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({
      liveSyncDurationCount: 3, maxLiveSyncPlaybackRate: 1.1, lowLatencyMode: false, backBufferLength: 30,
      xhrSetup: (xhr) => { xhr.withCredentials = true; xhr.setRequestHeader("X-F1-TV-Page", PAGE || ""); },
    });
    hls.on(Hls.Events.ERROR, (_e, d) => {
      if (d.response && d.response.code === 401) { ended(); return; }              // authorization ended -> approve again
      if (d.fatal) {
        streamErr = /codec|buffer(Add|Append)/i.test(d.details || "") ? "this browser cannot decode the stream (H.264) - use Chrome, Edge, Firefox or Safari"
          : `stream error (${d.details || d.type}) - retrying`;
        detach();                                      // the next status poll tries again
      }
    });
    hls.loadSource(LIVE_URL);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {          // Safari / iOS: native HLS (no headers:
    video.src = LIVE_URL + "?p=" + encodeURIComponent(PAGE || "");          // the page secret in the URL instead)
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
    const r = await tvFetch("/api/tv/status");
    if (r.status === 401) { ended(); return; }                   // revoked / expired / server restarted: approve again
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

/* ------------------------------------------------------------------ controls */
let hideTimer = null;
function showCtl() { $("ctl").classList.remove("hide"); clearTimeout(hideTimer); hideTimer = setTimeout(() => $("ctl").classList.add("hide"), 4000); }
document.addEventListener("mousemove", () => { if (PAGE) showCtl(); });
document.addEventListener("touchstart", () => { if (PAGE) showCtl(); }, { passive: true });
document.querySelectorAll("#layouts button").forEach((b) => b.addEventListener("click", () => setLayout(b.dataset.l)));
function toggleSound() { video.muted = !video.muted; if (!video.muted) video.play().catch(() => {}); $("unmute").hidden = !video.muted; }
$("b-sound").addEventListener("click", toggleSound);
$("unmute").addEventListener("click", toggleSound);
function toggleFull() { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {}); }
$("b-full").addEventListener("click", toggleFull);
$("b-tok").addEventListener("click", async () => {
  if (!confirm("Log this TV out? It will need approval again.")) return;
  await fetch("/api/tv/logout", { method: "POST", credentials: "same-origin", headers: { "X-F1-TV-Page": PAGE || "" } }).catch(() => {});
  ended();
});
document.addEventListener("keydown", (e) => {
  if (!PAGE || e.target.tagName === "INPUT") return;
  const k = e.key.toLowerCase();
  if (k === "1") setLayout("RACE_VIEW"); else if (k === "2") setLayout("VIDEO_FOCUS"); else if (k === "3") setLayout("FULL_DASHBOARD");
  else if (k === "m") toggleSound(); else if (k === "f") toggleFull(); else return;
  showCtl();
});

/* ------------------------------------------------------------------ start: only after THIS load was approved */
function start() {
  document.body.classList.remove("locked");
  $("auth").hidden = true;
  setLayout(layout); showCtl();
  setInterval(poll, 3000); poll();
  setInterval(place, 1000);                               // the dashboard re-lays itself out (TV mode, banners)
}
ask();
