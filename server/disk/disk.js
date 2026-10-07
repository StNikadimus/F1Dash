/* F1 recorder / disk page - reads /api/disk/* of the dashboard server (main/server/app.py).
   Changing settings, deleting and playing video need the remote token when one is set. */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TOKEN_KEY = "f1dash-remote-token";
let token = "";
try { token = localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { /* private window */ }
let status = null, recordings = [], recFilter = "all", logLevel = "INFO", openId = null, serverOffset = 0;
const details = {};

function bytes(n) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n >= 100 ? 0 : 1) : n.toFixed(0)) + " " + u[i];
}
function dur(s) {
  if (s == null || isNaN(s)) return "–";
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : m ? `${m} min` : `${s} s`;
}
const pad = (n) => String(n).padStart(2, "0");
function when(iso) {
  if (!iso) return "–";
  const d = new Date(iso); if (isNaN(d)) return "–";
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function tile(k, v, note = "", cls = "") {
  return `<div class="tile ${cls}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>${note ? `<div class="note">${esc(note)}</div>` : ""}</div>`;
}
function kv(rows) { return rows.filter(Boolean).map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${v}</div>`).join(""); }
const withToken = (url) => token ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : url;

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (token) headers["X-Remote-Token"] = token;
  const r = await fetch(path, Object.assign({}, opts, { headers, cache: "no-store" }));
  let body = null; try { body = await r.json(); } catch (e) { /* not json */ }
  if (r.status === 401) { askToken("This needs the remote token (F1DASH_REMOTE_TOKEN in server/.env)."); }
  if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
  return body;
}
function askToken(why) {
  const t = prompt((why ? why + "\n\n" : "") + "Remote token:", token);
  if (t === null) return;
  token = t.trim();
  try { localStorage.setItem(TOKEN_KEY, token); } catch (e) { /* ignore */ }
  renderTokenBtn();
}
function renderTokenBtn() {
  const b = $("tok-btn");
  b.textContent = token ? "TOKEN ✓" : "TOKEN";
  b.classList.toggle("primary", !!(status && status.token_required && !token));
}

/* ------------------------------------------------------------------ status: top bar, disk, now, retention */
async function loadStatus() {
  try { status = await api("/api/disk/status"); }
  catch (e) { setState("NO CONNECTION", "the dashboard server is not reachable", "bad"); return; }
  serverOffset = status.now * 1000 - Date.now();
  const st = status.state;
  setState(st.state, st.detail, st.level);
  renderDisk(status.disk); renderNow(st, status.player); renderRetention(status.retention, status.config);
  renderTokenBtn();
}
function setState(main, sub, level) {
  $("state").className = "tb-state st-" + (level || "idle");
  $("st-main").textContent = main; $("st-sub").textContent = sub || "";
}
function renderDisk(d) {
  $("disk-path").textContent = d.path || "";
  const tot = d.total || 0, rec = d.recordings_bytes || 0, used = d.used || 0;
  $("bar-rec").style.width = tot ? Math.min(100, rec / tot * 100) + "%" : "0";
  $("bar-other").style.width = tot ? Math.max(0, (used - rec) / tot * 100) + "%" : "0";
  const freeCls = d.free == null ? "" : d.free < (d.min_free_bytes || 0) * 2 ? "bad" : d.percent > 85 ? "warn" : "good";
  $("disk-tiles").innerHTML =
    tile("USED", bytes(d.used), d.percent != null ? d.percent + " %" : "") +
    tile("FREE", bytes(d.free), "", freeCls) +
    tile("TOTAL", bytes(d.total)) +
    tile("RECORDINGS", bytes(d.recordings_bytes), "video " + bytes(d.video_bytes)) +
    tile("≈ TIME LEFT", d.hours_left != null ? d.hours_left + " h" : "–",
      d.bytes_per_hour ? bytes(d.bytes_per_hour) + " / hour of video" : "no video recorded yet");
  $("disk-kv").innerHTML = kv([
    ["STATUS", d.ok ? '<span class="ok-t">writable - recording possible</span>' : `<span class="bad-t">${esc(d.error || "off")}</span>`],
    d.require_mount && ["DISK", esc(d.require_mount) + (d.mount_marker ? ` (marker ${esc(d.mount_marker)})` : "")],
    ["RESERVE", bytes(d.min_free_bytes) + " always left free"],
  ]);
}
function renderNow(st, player) {
  const big = $("now-big");
  big.textContent = st.state; big.className = "now-big " + (st.level || "idle");
  $("now-detail").textContent = st.detail || "";
  let tiles = "";
  if (st.state === "RECORDING") {
    tiles = tile("RECORDING FOR", dur(st.elapsed_s)) + tile("VIDEO", bytes(st.video_bytes), (st.segments || 0) + " segments") +
      tile("SINCE", when(st.since).slice(-5));
  }
  const hb = player && player.heartbeat, nxt = hb && hb.next;
  if (nxt && nxt.open_from) {
    const secs = nxt.open_from - (Date.now() + serverOffset) / 1000;
    tiles += tile("NEXT SESSION", secs > 0 ? "in " + dur(secs) : "now", `${nxt.meeting || ""} · ${nxt.session_name || ""}`);
  }
  $("now-tiles").innerHTML = tiles;
  $("now-hint").textContent = player && player.enabled ? "SERVER VOYO PLAYER" : "";
  if (!player || !player.enabled) { $("player-kv").innerHTML = ""; return; }
  const age = player.heartbeat_age_s;
  $("player-kv").innerHTML = kv([
    ["PLAYER", age == null ? '<span class="warn-t">no news yet (service running?)</span>'
      : age > 90 ? `<span class="warn-t">silent for ${dur(age)}</span>` : `<span class="ok-t">running</span> · ${esc(hb.state)}`],
    hb && ["CHROME", hb.browser ? (hb.widevine ? '<span class="ok-t">ok, DRM (Widevine) ok</span>' : '<span class="bad-t">no Widevine - VOYO will not play</span>') : '<span class="bad-t">not installed</span>'],
    hb && ["VOYO PAGE", hb.stream_url_set ? '<span class="ok-t">set</span>' : '<span class="warn-t">not set - voyo-player.sh login</span>'],
    hb && hb.note && ["PAGE", esc(hb.note)],
    nxt && ["NEXT", `${esc(nxt.meeting)} ${esc(nxt.session_name)} · ${esc(when(nxt.start))} · recording opens ${esc(when(new Date(nxt.open_from * 1000).toISOString()))}`],
  ]);
}
let retDirty = false;
function renderRetention(rows, cfg) {
  if (!retDirty) {
    $("ret-table").innerHTML = rows.map((r) =>
      `<tr><td class="k">${esc(r.label)}</td><td class="v"><input type="number" min="0" max="3650" step="1" data-key="${esc(r.key)}" data-orig="${r.days}" value="${r.days}"><span class="unit">days</span>${r.changed_on_page ? '<span class="pg">PAGE</span>' : ""}</td></tr>`).join("");
  }
  $("cfg-kv").innerHTML = kv([
    ["PATH", esc(cfg.path)],
    ["VIDEO", `server ${cfg.record_video_server == null ? "–" : cfg.record_video_server ? "on" : "off"} · PC ${cfg.record_video_pc ? "on" : "off"}`],
    ["ENCODER", esc(cfg.capture_encoder || "x264") + (cfg.capture_encoder === "vaapi" ? " (Intel Quick Sync)" : " (CPU)")],
    ["PICTURE", `${esc(cfg.resolution || "–")} · ${esc(cfg.capture_fps)} fps · ${esc(cfg.capture_segment_seconds)} s segments`],
    cfg.record_sessions && ["SESSIONS", esc(cfg.record_sessions.join(", "))],
    cfg.lead_minutes != null && ["WINDOW", `${esc(cfg.lead_minutes)} min before - ${esc(cfg.trail_minutes)} min after`],
    ["LOG", `kept ${cfg.log_keep_hours} h`],
  ]);
}
$("ret-table").addEventListener("input", (e) => {
  if (e.target.tagName !== "INPUT") return;
  e.target.classList.toggle("changed", e.target.value !== e.target.dataset.orig);
  retDirty = [...document.querySelectorAll("#ret-table input")].some((i) => i.value !== i.dataset.orig);
  $("ret-msg").textContent = retDirty ? "not saved" : "";
});
$("ret-save").addEventListener("click", async () => {
  const changed = {};
  document.querySelectorAll("#ret-table input").forEach((i) => { if (i.value !== i.dataset.orig) changed[i.dataset.key] = Number(i.value); });
  if (!Object.keys(changed).length) { $("ret-msg").textContent = "nothing changed"; return; }
  const shorter = Object.entries(changed).some(([k, v]) => {
    const i = document.querySelector(`#ret-table input[data-key="${k}"]`); return v > 0 && (Number(i.dataset.orig) === 0 || v < Number(i.dataset.orig));
  });
  if (shorter && !confirm("Shorter times: video older than the new limit is deleted NOW. Continue?")) return;
  try {
    const r = await api("/api/disk/settings", { method: "POST", body: JSON.stringify(changed) });
    retDirty = false;
    $("ret-msg").textContent = "saved" + (r.video_files_deleted ? ` · ${r.video_files_deleted} old video file(s) deleted` : "");
    loadStatus(); loadRecordings(true);
  } catch (e) { $("ret-msg").textContent = "not saved: " + e.message; }
});

/* ------------------------------------------------------------------ recordings */
async function loadRecordings(fresh) {
  try { const r = await api("/api/disk/recordings" + (fresh ? "?fresh=1" : "")); recordings = r.recordings || []; }
  catch (e) { return; }
  renderRecordings();
}
function keptUntil(r) {
  if (!r.video_bytes) return r.capture_deleted_at ? "video deleted" : "–";
  if (!r.keep_days) return "forever";
  const ref = r.closed_at || r.updated_at; if (!ref) return "–";
  return when(new Date(new Date(ref).getTime() + r.keep_days * 86400e3).toISOString()).slice(0, 10);
}
function renderRecordings() {
  const list = recordings.filter((r) => recFilter === "all" ? true : recFilter === "video" ? r.video_bytes > 0 : (r.channel || "viewer") === recFilter);
  $("rec-hint").textContent = `${list.length} of ${recordings.length} · ${bytes(recordings.reduce((a, r) => a + (r.video_bytes || 0) + (r.data_bytes || 0), 0))}`;
  $("rec-empty").hidden = list.length > 0;
  const tb = $("rec-table").querySelector("tbody");
  tb.innerHTML = list.map((r) => {
    const sess = [r.meeting, r.session_name].filter(Boolean).join(" · ") || r.title || r.stream_instance_id;
    const sync = r.sync && r.sync.confidence ? `<span class="badge ${esc(r.sync.confidence)}">${esc(r.sync.confidence)}</span>` : '<span class="badge">–</span>';
    const stat = r.status === "recording" ? '<span class="badge rec">● RECORDING</span>' : `<span class="badge ${esc(r.status)}">${esc((r.status || "").toUpperCase())}</span>`;
    const src = (r.channel || "viewer") === "server_player" ? "SERVER" : "PC / TV";
    const row = `<tr class="row ${openId === r.stream_instance_id ? "open" : ""}" data-id="${esc(r.stream_instance_id)}">
      <td class="mono">${esc(when(r.detected_at))}</td>
      <td><div class="sess">${esc(sess)}</div><div class="sub">${esc(r.title && r.title !== sess ? r.title : "")} ${esc(r.stream_instance_id)}</div></td>
      <td>${src}${r.live ? ' <span class="badge">LIVE</span>' : ""}</td>
      <td class="r mono">${esc(dur(r.watched_seconds))}</td>
      <td class="r mono">${r.video_bytes ? bytes(r.video_bytes) + `<div class="sub">${r.segments} seg</div>` : "–"}</td>
      <td class="r mono">${esc(keptUntil(r))}</td><td>${sync}</td><td>${stat}</td></tr>`;
    return row + (openId === r.stream_instance_id ? `<tr class="detail"><td colspan="8" id="det-${esc(r.stream_instance_id)}">${detailHtml(r)}</td></tr>` : "");
  }).join("");
}
function detailHtml(r) {
  const d = details[r.stream_instance_id];
  if (!d) return '<div class="msg">loading…</div>';
  const m = d.manifest || {}, cap = r.video_bytes ? (d.capture || []).filter((c) => c.name) : [];
  const base = `/api/voyo/recordings/${encodeURIComponent(r.stream_instance_id)}/files/`;
  const segs = cap.length ? cap.map((c, i) =>
    `<span class="seg"><button data-play="${i}" title="${esc(c.name)}">▶ ${i + 1}${c.pc_start_epoch ? " · " + esc(when(new Date(c.pc_start_epoch * 1000).toISOString()).slice(-5)) : ""}</button><a href="${esc(withToken(base + "capture/" + c.name))}" download="${esc(c.name)}" title="download ${esc(bytes(c.bytes))}">⬇</a></span>`).join("")
    : '<span class="msg">no video in this recording</span>';
  const cal = d.calibration || {};
  return `<div class="det-grid"><div>
      <div class="sub-title" style="margin-top:0">VIDEO SEGMENTS</div><div class="segs">${segs}</div>
      <div class="det-actions">
        ${cap.length ? `<button class="btn" data-play="0">▶ PLAY ALL</button>` : ""}
        ${["manifest.json", "meta.json", "timeline.jsonl", "anchors.json", "sync_observations.jsonl"].map((f) =>
          `<a class="btn" href="${esc(withToken(base + f))}" target="_blank">${esc(f)}</a>`).join("")}
      </div>
      <div class="det-actions">
        ${r.video_bytes ? `<button class="btn danger" data-del="video">DELETE VIDEO</button>` : ""}
        <button class="btn danger" data-del="all">DELETE RECORDING</button>
      </div></div>
    <div class="kvlist small">${kv([
      ["STARTED", esc(when(m.detected_at))], ["CLOSED", esc(m.closed_at ? when(m.closed_at) : "–") + (m.close_reason ? ` · ${esc(m.close_reason)}` : "")],
      ["STREAM START", esc(when(m.stream_start_wall_time))], ["MODE", esc(m.mode ? `${m.mode.selected || "–"} / ${m.mode.source || "–"}` : "–")],
      ["SESSION", esc(m.session ? `${m.session.meeting || ""} ${m.session.session_name || ""} (${m.session.kind || ""})` : "not identified")],
      ["SYNC", cal.offset != null ? `${esc(cal.state)} · ${cal.n} point(s) · spread ${cal.spread} s` : "no sync points"],
      ["DATA DELAY", m.live_data_delay ? `${m.live_data_delay.seconds} s` : "–"],
      ["SAMPLES", esc(m.counts ? `${m.counts.timeline} timeline · ${m.counts.observations} observations · ${m.counts.gaps} gaps` : "–")],
      ["DATA", bytes(r.data_bytes)],
    ])}</div></div>`;
}
$("rec-table").addEventListener("click", async (e) => {
  const play = e.target.closest("[data-play]"), del = e.target.closest("[data-del]");
  const detRow = e.target.closest("tr.detail");
  if (play && detRow) { openViewer(openId, Number(play.dataset.play)); return; }
  if (del && detRow) { deleteRec(openId, del.dataset.del); return; }
  if (detRow || e.target.closest("a")) return;
  const row = e.target.closest("tr.row"); if (!row) return;
  const id = row.dataset.id;
  openId = openId === id ? null : id;
  renderRecordings();
  if (openId) {
    try { details[id] = await api(`/api/voyo/recordings/${encodeURIComponent(id)}`); } catch (err) { details[id] = { error: err.message }; }
    renderRecordings();
  }
});
async function deleteRec(id, what) {
  const r = recordings.find((x) => x.stream_instance_id === id) || {};
  const name = [r.meeting, r.session_name].filter(Boolean).join(" ") || r.title || id;
  const q = what === "video" ? `Delete the VIDEO of "${name}"?\nThe timeline and sync data stay.` : `Delete the whole recording "${name}"?\nThis cannot be undone.`;
  if (!confirm(q)) return;
  try { const res = await api(`/api/disk/recordings/${encodeURIComponent(id)}/delete?what=${what}`, { method: "POST" });
    delete details[id]; if (what === "all") openId = null; alert(res.message); loadRecordings(true); loadStatus(); }
  catch (e) { alert("Not deleted: " + e.message); }
}
document.querySelectorAll("#rec-filters .chip").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#rec-filters .chip").forEach((x) => x.classList.toggle("sel", x === b));
  recFilter = b.dataset.f; renderRecordings();
}));

/* ------------------------------------------------------------------ video viewer (segments one after another) */
let vSegs = [], vIdx = 0, vId = null;
function openViewer(id, i) {
  const d = details[id], r = recordings.find((x) => x.stream_instance_id === id) || {};
  vSegs = r.video_bytes ? (d && d.capture || []).filter((c) => c.name) : []; vId = id;
  if (!vSegs.length) return;
  if (!token && status && status.token_required) askToken("Playing video needs the remote token.");
  $("v-label").textContent = (r.channel === "server_player" ? "SERVER RECORDING" : "PC / TV RECORDING") + " · " + when(r.detected_at);
  $("v-title").textContent = [r.meeting, r.session_name].filter(Boolean).join(" · ") || r.title || id;
  $("viewer").showModal(); showSeg(i);
}
function showSeg(i) {
  vIdx = Math.max(0, Math.min(vSegs.length - 1, i));
  const c = vSegs[vIdx], url = withToken(`/api/voyo/recordings/${encodeURIComponent(vId)}/files/capture/${encodeURIComponent(c.name)}`);
  const v = $("v-video"); v.src = url; v.play().catch(() => {});
  $("v-dl").href = url; $("v-dl").setAttribute("download", c.name);
  $("v-pos").textContent = `segment ${vIdx + 1} / ${vSegs.length}` + (c.pc_start_epoch ? ` · ${when(new Date(c.pc_start_epoch * 1000).toISOString())}` : "") + ` · ${bytes(c.bytes)}`;
  $("v-prev").disabled = vIdx === 0; $("v-next").disabled = vIdx >= vSegs.length - 1;
}
$("v-video").addEventListener("ended", () => { if (vIdx < vSegs.length - 1) showSeg(vIdx + 1); });
$("v-prev").addEventListener("click", () => showSeg(vIdx - 1));
$("v-next").addEventListener("click", () => showSeg(vIdx + 1));
$("v-close").addEventListener("click", () => $("viewer").close());
$("viewer").addEventListener("close", () => { const v = $("v-video"); v.pause(); v.removeAttribute("src"); v.load(); });

/* ------------------------------------------------------------------ log */
async function loadLog() {
  let r; try { r = await api(`/api/disk/log?limit=600&level=${logLevel}`); } catch (e) { return; }
  const box = $("log"), atTop = box.scrollTop < 20;
  let day = "", html = "";
  for (const e of r.entries) {
    const d = new Date(e.t * 1000), dk = `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
    if (dk !== day) { day = dk; html += `<div class="day">${dk}</div>`; }
    html += `<div class="ln ${esc(e.level)}"><span class="t">${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}</span><span class="s">${esc(e.source)}</span><span class="m">${esc(e.text)}</span></div>`;
  }
  box.innerHTML = html || '<div class="empty">Nothing logged in the last 48 hours.</div>';
  if (atTop) box.scrollTop = 0;
}
document.querySelectorAll("#log-filters .chip").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#log-filters .chip").forEach((x) => x.classList.toggle("sel", x === b));
  logLevel = b.dataset.l; loadLog();
}));

/* ------------------------------------------------------------------ clock + polling */
$("tok-btn").addEventListener("click", () => askToken(""));
function tickClock() {
  const d = new Date(Date.now() + serverOffset);
  $("clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
setInterval(tickClock, 1000); tickClock();
loadStatus(); loadRecordings(); loadLog();
setInterval(loadStatus, 3000);
setInterval(() => { if (!openId || !document.querySelector("#rec-table tr.detail")) loadRecordings(); }, 15000);
setInterval(loadLog, 5000);
