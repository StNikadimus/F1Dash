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
  if (!r) { if (!video.paused && layout === "FULL_DASHBOARD") video.pause(); placeCtl(); return; }
  const f = dash.getBoundingClientRect();
  Object.assign(vbox.style, { left: f.left + r.left + "px", top: f.top + r.top + "px", width: r.width + "px", height: r.height + "px" });
  if (loaded && !P.replay && video.paused) video.play().catch(() => {});
  placePanel();
  placeCtl();
}
// the controls sit in the video window's top-right corner when there is one (they cover no timing data
// there); without it (DASHBOARD layout, a small window) in the screen's top-right corner; phones: CSS
function placeCtl() {
  const c = $("ctl");
  c.classList.remove("narrow"); c.style.top = c.style.right = c.style.maxWidth = "";
  if (window.innerWidth <= 700) return;
  const r = vbox.classList.contains("shown") ? vbox.getBoundingClientRect() : null;
  if (!r || r.width < 340) return;
  if (r.width < c.offsetWidth + 24) {                              // a small video window: the rows wrap to fit it
    c.classList.add("narrow"); c.style.maxWidth = Math.floor(r.width - 24) + "px";
  }
  if (r.height < c.offsetHeight + 24) { c.classList.remove("narrow"); c.style.maxWidth = ""; return; }
  c.style.top = Math.round(r.top + 12) + "px";
  c.style.right = Math.round(Math.max(12, window.innerWidth - r.right + 12)) + "px";
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
video.addEventListener("playing", () => { playing = true; streamErr = ""; $("offair").hidden = true; $("unmute").hidden = !video.muted; renderOsd(); });
for (const ev of ["waiting", "emptied", "error", "pause"]) video.addEventListener(ev, () => { playing = false; });

/* ------------------------------------------------------------------ the player: LIVE / REPLAYS
   One <video>: the live stream (above) or a recording from the disk (/tv/replay/<id>/index.m3u8 - the
   recorded segments as they are, server/replays.py). The panel (LIVE, the recordings, transport, volume)
   is opened and driven by the remote like everything else: keys -> server (remote.py, its PLAYER layer)
   -> the dashboard in the iframe -> postMessage here. Keys pressed on this page go the same way. */
const P = { replay: null, rhls: null, open: false, cursor: 0, recs: [], recsAt: 0, recsErr: "", lastCmd: null,
  osdTimer: null, err: "", scrolled: -1, recovered: false };
const pmenu = $("pmenu"), posd = $("posd");
function hms(s) {
  if (!isFinite(s) || s < 0) return "--:--";
  s = Math.floor(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(x).padStart(2, "0");
}
const KIND = { practice1: "Practice 1", practice2: "Practice 2", practice3: "Practice 3", sprint_qualifying: "Sprint Qualifying",
  sprint: "Sprint", qualifying: "Qualifying", race: "Race" };
function recLabel(r) {
  return [r.meeting || r.title || "Recording", r.session_name || KIND[r.session_kind] || ""].filter(Boolean).join(" · ");
}
function recWhen(r) {
  const d = r.start ? new Date(r.start) : null;
  return d && !isNaN(d) ? d.toLocaleString([], { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
}
const STATUS_TXT = { closed: "COMPLETE", recording: "RECORDING NOW", interrupted: "INTERRUPTED" };

function rows() {                                   // what the panel shows, in order; sel = can be highlighted
  const out = [];
  if (P.replay) out.push({ kind: "transport", sel: true });
  out.push({ kind: "volume", sel: true });
  out.push({ kind: "live", sel: true });
  out.push({ kind: "head" });
  if (!P.recs.length) out.push({ kind: "empty" });
  for (const r of P.recs) out.push({ kind: "rec", sel: true, rec: r });
  return out;
}
async function loadRecs(force) {
  if (!force && Date.now() - P.recsAt < 30000) return;
  P.recsAt = Date.now();
  try {
    const r = await tvFetch("/api/tv/replays");
    if (r.status === 401) { ended(); return; }
    const b = await r.json();
    P.recs = Array.isArray(b.recordings) ? b.recordings : [];
    P.recsErr = b.recorder && b.recorder.ok === false ? "the recording disk is not available" : "";
  } catch (e) { P.recsErr = "recordings not reachable"; }
  renderPanel();
}
function selRows() { return rows().map((r, i) => (r.sel ? i : -1)).filter((i) => i >= 0); }
function moveCursor(step) {
  const sel = selRows();
  if (!sel.length) return;
  let k = sel.indexOf(P.cursor);
  if (k < 0) k = 0; else k = Math.max(0, Math.min(sel.length - 1, k + step));
  P.cursor = sel[k];
}
function cursorTo(kind, id) {
  const i = rows().findIndex((r) => r.kind === kind && (!id || (r.rec && r.rec.id === id)));
  if (i >= 0) P.cursor = i;
}
function renderPanel() {
  pmenu.hidden = !P.open;
  if (P.open) $("ctl").classList.add("hide");                     // one overlay at a time
  renderOsd();
  if (!P.open) return;
  const rs = rows();
  if (!rs[P.cursor] || !rs[P.cursor].sel) moveCursor(0);
  const lv = (status && status.live) || {};
  const html = rs.map((r, i) => {
    const cur = i === P.cursor ? " cur" : "";
    if (r.kind === "transport") {
      const d = video.duration, t = video.currentTime, pct = isFinite(d) && d > 0 ? Math.min(100, t / d * 100) : 0;
      return `<div class="pr tr${cur}" data-i="${i}"><span class="pi">${video.paused ? "▶" : "❚❚"}</span>` +
        `<span class="pt">${esc(hms(t))} / ${esc(hms(d))}</span><span class="pbar"><i style="width:${pct.toFixed(2)}%"></i></span>` +
        `<span class="ph">◀ ▶ 10 s · OK ${video.paused ? "play" : "pause"}</span></div>`;
    }
    if (r.kind === "volume") {
      const v = Math.round((video.muted ? 0 : video.volume) * 100);
      return `<div class="pr vol${cur}" data-i="${i}"><span class="pi">${video.muted ? "🔇" : "🔊"}</span>` +
        `<span class="pt">VOLUME ${video.muted ? "MUTED" : v + " %"}</span><span class="pbar"><i style="width:${v}%"></i></span>` +
        `<span class="ph">◀ ▶ volume · OK ${video.muted ? "sound on" : "mute"}</span></div>`;
    }
    if (r.kind === "live") {
      const on = !!lv.on_air, act = !P.replay ? " act" : "";
      const sess = status && status.session ? [status.session.meeting, status.session.session_name].filter(Boolean).join(" · ") : "";
      return `<div class="pr live${cur}${act}" data-i="${i}"><span class="pi ${on ? "on" : ""}">●</span>` +
        `<span class="pt">LIVE</span><span class="pd">${on ? "on air" + (sess ? " · " + esc(sess) : "") : "no live stream now"}</span>` +
        `${act ? '<span class="pa">PLAYING</span>' : ""}</div>`;
    }
    if (r.kind === "head") return `<div class="phd">REPLAYS · RECORDINGS ON THE DISK${P.recsErr ? " · " + esc(P.recsErr) : ""}</div>`;
    if (r.kind === "empty") return `<div class="pe">No recordings with video yet.</div>`;
    const x = r.rec, act = P.replay && P.replay.id === x.id ? " act" : "";
    return `<div class="pr rec${cur}${act}" data-i="${i}"><span class="pi">▶</span>` +
      `<span class="pt">${esc(recLabel(x))}</span><span class="pd">${esc(recWhen(x))}${x.duration_s ? " · " + esc(hms(x.duration_s)) : ""}</span>` +
      `<span class="ps s-${esc(x.status || "")}">${esc(STATUS_TXT[x.status] || (x.status || "").toUpperCase())}</span>` +
      `${act ? '<span class="pa">PLAYING</span>' : ""}</div>`;
  }).join("");
  $("pm-list").innerHTML = html;
  $("pm-src").textContent = P.replay ? "REPLAY · " + recLabel(P.replay) : "LIVE";
  $("pm-src").className = P.replay ? "rp" : "lv";
  $("pm-err").textContent = P.err || "";
  const c = $("pm-list").querySelector(".cur");
  if (c && P.scrolled !== P.cursor) { P.scrolled = P.cursor; c.scrollIntoView({ block: "nearest" }); }
  placePanel();
}
function placePanel() {
  if (!P.open) return;
  const shown = vbox.classList.contains("shown"), r = vbox.getBoundingClientRect();
  if (shown && r.width > 520 && r.height > 300) {
    const w = Math.min(r.width - 32, 860);
    Object.assign(pmenu.style, { left: r.left + 16 + "px", top: r.top + 16 + "px", width: w + "px", maxHeight: r.height - 32 + "px",
      transform: "none" });
  } else {
    Object.assign(pmenu.style, { left: "50%", top: "50%", width: "min(860px, 94vw)", maxHeight: "86vh", transform: "translate(-50%, -50%)" });
  }
}
function renderOsd() {
  posd.hidden = P.open || !(P.replay || (loaded && playing));        // the panel says it itself while open
  $("po-src").textContent = P.replay ? "▶ REPLAY · " + recLabel(P.replay) : "● LIVE";
  $("po-src").className = P.replay ? "rp" : "lv";
  const d = video.duration, t = video.currentTime;
  $("po-time").textContent = P.replay ? hms(t) + " / " + hms(d) : "";
  $("po-bar").style.width = P.replay && isFinite(d) && d > 0 ? Math.min(100, t / d * 100).toFixed(2) + "%" : "0%";
  $("po-prog").hidden = !P.replay;
}
function flashOsd() {
  posd.classList.add("flash"); clearTimeout(P.osdTimer);
  P.osdTimer = setTimeout(() => posd.classList.remove("flash"), 2500);
}

function stopReplay() {
  if (P.rhls) { P.rhls.destroy(); P.rhls = null; }
  if (P.replay) { video.removeAttribute("src"); video.load(); }
  P.replay = null;
}
function playReplay(rec) {
  if (layout === "FULL_DASHBOARD") setLayout("RACE_VIEW");          // the video needs its window
  detach();                                                         // the live stream goes out
  stopReplay();
  P.replay = rec; P.err = "";
  $("offair").hidden = true;
  const url = `/tv/replay/${encodeURIComponent(rec.id)}/index.m3u8`;
  if (window.Hls && Hls.isSupported()) {
    const h = new Hls({ startPosition: 0, backBufferLength: 60, maxBufferLength: 60,
      xhrSetup: (xhr) => { xhr.withCredentials = true; xhr.setRequestHeader("X-F1-TV-Page", PAGE || ""); } });
    P.rhls = h;
    h.on(Hls.Events.ERROR, (_e, d) => {
      if (d.response && d.response.code === 401) { ended(); return; }
      if (!d.fatal) return;
      if (d.type === Hls.ErrorTypes.MEDIA_ERROR && !/codec|buffer(Add|Append)/i.test(d.details || "") && !P.recovered) {
        P.recovered = true; h.recoverMediaError(); return;
      }
      P.err = /codec|buffer(Add|Append)/i.test(d.details || "") ? "this browser cannot decode the recording (H.264) - use Chrome, Edge, Firefox or Safari"
        : `the recording cannot be played (${d.details || d.type})`;
      renderPanel(); flashOsd();
    });
    h.loadSource(url);
    h.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url + "?p=" + encodeURIComponent(PAGE || "");
  } else {
    P.err = "this browser cannot play recordings (no HLS)"; P.replay = null; renderPanel(); return;
  }
  P.recovered = false;
  video.play().catch(() => {});
  cursorTo("transport");
  renderPanel(); flashOsd();
}
function goLive() {
  stopReplay(); P.err = "";
  if (layout === "FULL_DASHBOARD") setLayout("RACE_VIEW");
  cursorTo("live");
  renderPanel(); flashOsd();
  poll();                                                           // attaches the live stream when it is on air
}
function seekBy(sec) {
  if (!P.replay) return;
  const d = isFinite(video.duration) ? video.duration : Infinity;
  video.currentTime = Math.max(0, Math.min(d - 1, video.currentTime + sec));
  flashOsd();
}
function playPause() {
  if (video.paused) video.play().catch(() => {}); else video.pause();
  flashOsd();
}
function volumeBy(step) {
  const v = Math.max(0, Math.min(1, Math.round(((video.muted ? 0 : video.volume) + step) * 10) / 10));
  video.volume = v; video.muted = v === 0;
  if (!video.muted) video.play().catch(() => {});
  $("unmute").hidden = !video.muted;
}
function activate(r) {
  if (!r) return;
  if (r.kind === "transport") playPause();
  else if (r.kind === "volume") { video.muted = !video.muted; if (!video.muted && video.volume === 0) video.volume = 0.5; $("unmute").hidden = !video.muted; }
  else if (r.kind === "live") goLive();
  else if (r.kind === "rec") playReplay(r.rec);
}
function playerCmd(c) {                                // one command of the remote's PLAYER layer
  const a = c.action, rs = rows();
  if (a === "menu") {
    P.open = c.arg === "open";
    if (P.open) { P.scrolled = -1; loadRecs(true); cursorTo(P.replay ? "transport" : "live"); }
  } else if (a === "nav" && P.open) {
    const r = rs[P.cursor] || {};
    if (c.arg === "up") moveCursor(-1);
    else if (c.arg === "down") moveCursor(1);
    else if (r.kind === "transport") seekBy(c.arg === "left" ? -10 : 10);
    else if (r.kind === "volume") volumeBy(c.arg === "left" ? -0.1 : 0.1);
    else moveCursor(c.arg === "left" ? -5 : 5);                    // the list: a page at a time
  } else if (a === "ok" && P.open) activate(rs[P.cursor]);
  else if (a === "play_pause") { if (P.replay || loaded) playPause(); }
  else if (a === "seek") seekBy(parseInt(c.arg, 10) || 0);
  renderPanel();
}
// the dashboard in the iframe hands over the remote's player state (same origin, that frame only)
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || e.source !== dash.contentWindow || !PAGE) return;
  const d = e.data || {};
  if (d.type !== "f1-player") return;
  const c = d.player_cmd || {};
  if (P.lastCmd === null) {                                        // never replay an old command after a (re)load
    P.lastCmd = c.n || 0;
    if (!!d.player_menu !== P.open) playerCmd({ action: "menu", arg: d.player_menu ? "open" : "close" });
    return;
  }
  if (c.n && c.n !== P.lastCmd) { P.lastCmd = c.n; playerCmd(c); }
  else if (!!d.player_menu !== P.open) playerCmd({ action: "menu", arg: d.player_menu ? "open" : "close" });
});
dash.addEventListener("load", () => { P.lastCmd = null; });       // a new dashboard connection: a new baseline
$("pm-list").addEventListener("click", (e) => {
  const el = e.target.closest("[data-i]");
  if (!el) return;
  P.cursor = Number(el.dataset.i);
  activate(rows()[P.cursor]); renderPanel();
});
function updateTransport() {                           // the clock and bar only (4x a second while playing)
  const row = $("pm-list").querySelector(".pr.tr");
  if (!row) return;
  const d = video.duration, t = video.currentTime;
  row.querySelector(".pt").textContent = hms(t) + " / " + hms(d);
  row.querySelector(".pbar i").style.width = (isFinite(d) && d > 0 ? Math.min(100, t / d * 100) : 0).toFixed(2) + "%";
}
video.addEventListener("timeupdate", () => { if (P.replay) { renderOsd(); if (P.open) updateTransport(); } });
for (const ev of ["durationchange", "play", "pause", "volumechange"]) {
  video.addEventListener(ev, () => { if (P.open) renderPanel(); else if (P.replay) renderOsd(); });
}
video.addEventListener("ended", () => { if (P.replay) { renderPanel(); flashOsd(); } });

/* ------------------------------------------------------------------ server status (every 3 s) */
async function poll() {
  try {
    const r = await tvFetch("/api/tv/status");
    if (r.status === 401) { ended(); return; }                   // revoked / expired / server restarted: approve again
    status = await r.json();
  } catch (e) { showOff("NO CONNECTION", "the F1 server is not reachable", "warn"); setCtl("● NO CONNECTION", "off", ""); return; }
  serverOffset = status.now * 1000 - Date.now();
  const lv = status.live || {}, st = status.state || {};
  if (P.replay) {                                              // a recording plays: the live stream stays out
    setCtl("▶ REPLAY", "rp", recLabel(P.replay) + (lv.on_air ? " · live stream on air" : ""));
    renderPanel();
    return;
  }
  if (lv.on_air) {
    attach();
    const sess = status.session ? [status.session.meeting, status.session.session_name].filter(Boolean).join(" · ") : "";
    let lag = "";
    if (hls && hls.playingDate) lag = ` · picture ${((Date.now() + serverOffset - hls.playingDate.getTime()) / 1000).toFixed(0)} s behind the capture`;
    setCtl("● ON AIR", "on", sess + lag);
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
    setCtl("● OFF AIR", "off", st.state || "");
  }
}
// the status row (a label): the stream state as a badge + what it means / the session
function setCtl(badge, cls, detail) {
  $("ctl-status").innerHTML = `<span class="ctl-badge ${esc(cls)}">${esc(badge)}</span>` +
    (detail ? `<span class="ctl-detail">${esc(detail)}</span>` : "");
}

/* ------------------------------------------------------------------ controls */
let hideTimer = null;
function showCtl() { if (P.open) return; placeCtl(); $("ctl").classList.remove("hide"); clearTimeout(hideTimer); hideTimer = setTimeout(() => $("ctl").classList.add("hide"), 4000); }
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
// keys pressed on this page (not inside the dashboard): arrows / OK / BACK / B / P go the remote's way - the
// dashboard's connection to the server, which decides (player panel open: the player; else the dashboard)
const FWD = { ArrowUp: "KEY_UP", ArrowDown: "KEY_DOWN", ArrowLeft: "KEY_LEFT", ArrowRight: "KEY_RIGHT", Enter: "KEY_ENTER",
  Escape: "KEY_ESC", Backspace: "KEY_BACK", BrowserBack: "KEY_BACK", b: "KEY_B", B: "KEY_B", ContextMenu: "KEY_B" };
// play / pause of THIS page's video stays here (the remote's Space would switch every screen's TV mode)
const LOCAL_PLAY = new Set([" ", "p", "P", "MediaPlayPause"]);
function sendKey(key) {
  try { dash.contentWindow.postMessage({ type: "f1-key", key }, location.origin); } catch (e) { /* not loaded yet */ }
}
document.addEventListener("keydown", (e) => {
  if (!PAGE || e.target.tagName === "INPUT") return;
  if (FWD[e.key] && !e.altKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); sendKey(FWD[e.key]); return; }
  if (LOCAL_PLAY.has(e.key) && !e.altKey && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    if (P.replay || loaded) { playPause(); if (P.open) renderPanel(); }
    return;
  }
  const k = e.key.toLowerCase();
  if (k === "1") setLayout("RACE_VIEW"); else if (k === "2") setLayout("VIDEO_FOCUS"); else if (k === "3") setLayout("FULL_DASHBOARD");
  else if (k === "m") toggleSound(); else if (k === "f") toggleFull(); else return;
  showCtl();
});
$("b-player").addEventListener("click", () => sendKey("KEY_B"));

/* ------------------------------------------------------------------ start: only after THIS load was approved */
function start() {
  document.body.classList.remove("locked");
  $("auth").hidden = true;
  setLayout(layout); showCtl();
  setInterval(poll, 3000); poll();
  setInterval(place, 1000);                               // the dashboard re-lays itself out (TV mode, banners)
}
ask();
