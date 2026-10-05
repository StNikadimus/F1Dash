/* F1 Timing Wall - dashboard client.
 * Talks to the server over one WebSocket. Only renders normalized data;
 * any value that is null/undefined is shown as N/A. Nothing is estimated here
 * except the smooth interpolation between two REAL position samples. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ state
  const S = {
    mode: null,
    cfg: { interp_delay_ms: 1200, map_fps: 30, animations: "full", pulse_period_ms: 2400, reorder_ms: 450 },
    keymap: {},
    session: {}, track_status: {}, weather: {}, drivers: {}, order: [],
    race_control: [], radio: [], availability: {}, timeline: null, map: {},
    status: {}, ui: { view: "overview", selected: null, help: false },
    track: null,
    tel: {},
    clockAt: 0,
    wsOpen: false,
    sync: null,
    modeSel: null,          // MODE selector: selected / detected / effective (server/mode.py)
  };
  const $ = (id) => document.getElementById(id);
  const stage = $("stage");
  const NA = '<span class="na">N/A</span>';

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const has = (v) => v !== null && v !== undefined && v !== "";
  const na = (v, f) => (has(v) ? (f ? f(v) : esc(v)) : NA);
  const COMP_LETTER = { SOFT: "S", MEDIUM: "M", HARD: "H", INTERMEDIATE: "I", WET: "W" };

  // ------------------------------------------------------------------ scaling
  function fit() {
    const portrait = window.innerHeight > window.innerWidth * 1.15;
    stage.classList.toggle("portrait", portrait);
    const w = portrait ? 1080 : 1920, h = portrait ? 1920 : 1080;
    const s = Math.min(window.innerWidth / w, window.innerHeight / h);
    stage.style.transform = `scale(${s}) translate(-50%, -50%)`;
    stage.style.transformOrigin = "0 0";
    S.scale = s;
    layoutBoard();
    Map2D.resize();
  }
  window.addEventListener("resize", fit);

  // ------------------------------------------------------------------ websocket
  let ws = null, retry = 0;
  function connect() {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    ws = new WebSocket(url);
    ws.onopen = () => { S.wsOpen = true; retry = 0; $("conn-overlay").hidden = true; renderMode(); };
    ws.onclose = () => {
      S.wsOpen = false;
      $("conn-overlay").hidden = false;
      renderMode();
      const d = Math.min(15000, 1000 * 2 ** Math.min(retry++, 4));
      $("conn-detail").textContent = `RECONNECTING IN ${Math.round(d / 1000)} s…`;
      setTimeout(connect, d);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      try { handle(m); } catch (e) { console.error("render error", e); }
    };
  }
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

  function handle(m) {
    switch (m.type) {
      case "hello":
        // another data source now (MODE switched LIVE <-> VOD): nothing of the old one stays on screen
        if (S.mode !== null && m.mode !== S.mode) resetData();
        S.mode = m.mode; S.keymap = m.keymap || {};
        S.keymapVideo = m.keymap_video || {}; S.keymapVideoFocus = m.keymap_video_focus || {};
        Object.assign(S.cfg, m.config || {});
        stage.classList.remove("anim-full", "anim-reduced", "anim-off");
        stage.classList.add("anim-" + (S.cfg.animations || "full"));
        stage.style.setProperty("--reorder-ms", (S.cfg.reorder_ms || 0) + "ms");
        stage.style.setProperty("--pulse-ms", (S.cfg.pulse_period_ms || 2400) + "ms");
        renderHelp(); renderMode();
        break;
      case "state":
        if (m.full) { S.drivers = {}; }
        for (const k of ["session", "track_status", "weather", "order", "race_control", "radio", "availability", "timeline", "map"]) {
          if (k in m) S[k] = m[k];
        }
        if ("session" in m) S.clockAt = performance.now();
        if (m.drivers) Object.assign(S.drivers, m.drivers);
        for (const n of m.removed || []) delete S.drivers[n];
        renderAll(m);
        break;
      case "track":
        S.track = m.track; Map2D.setTrack(m.track); renderMapInfo();
        break;
      case "mode":
        S.modeSel = m; renderModeSel();
        break;
      case "weather_report":
        showWeatherReport(m);
        break;
      case "pitlane_debug":
        Map2D.setPitDebug(m);
        break;
      case "pos":
        Map2D.addSamples(m.t, m.cars);
        break;
      case "pos_reset":            // the sync target jumped (video seek, resync): drop old samples
        Map2D.reset();
        break;
      case "clock":                // presentation clock of the sync engine (VOYO / DELAY / LIVE)
        Map2D.setClock(m);
        break;
      case "sync":
        S.sync = m; renderSync();
        break;
      case "sync_result":             // answer to this screen's own SYNC menu action
        if (m.action === "capture") syncCapture = { video_time: m.video_time, paused: m.paused };
        else syncMsg = { ok: !!m.ok, text: m.result || m.error || "" };
        if (m.state) S.sync = m.state;
        renderSync();
        break;
      case "tel":
        S.tel = m.cars || {};
        if (S.ui.view === "overview" || S.ui.view === "telemetry") renderTelemetryLive();
        break;
      case "status":
        S.status = m; renderStatus();
        break;
      case "ui": {
        const prevNotice = S.ui.video_notice;
        const prevToast = (S.ui.toast || {}).n;
        S.ui = m; renderUI(); renderSync();
        if (m.toast && m.toast.text && prevToast !== undefined && m.toast.n !== prevToast) showToast(m.toast.text, 4000);
        window.VoyoPlayer && VoyoPlayer.setUI(m);
        if (m.video_notice && m.video_notice !== prevNotice && m.tv_mode !== "FULL_DASHBOARD") showToast("VIDEO: " + m.video_notice, 6000);
        break;
      }
      case "video":
        S.video = m;
        window.VoyoPlayer && VoyoPlayer.setInfo(m);
        renderRaceInfo();
        break;
    }
  }

  // ------------------------------------------------------------------ keyboard
  const KEYS = {
    ArrowUp: "KEY_UP", ArrowDown: "KEY_DOWN", ArrowLeft: "KEY_LEFT", ArrowRight: "KEY_RIGHT",
    Enter: "KEY_ENTER", Escape: "KEY_ESC", Backspace: "KEY_BACK", " ": "KEY_SPACE",
    i: "KEY_I", I: "KEY_I", h: "KEY_H", H: "KEY_H", "?": "KEY_H",
    1: "KEY_1", 2: "KEY_2", 3: "KEY_3", 4: "KEY_4", 5: "KEY_5",
    PageUp: "KEY_PAGEUP", PageDown: "KEY_PAGEDOWN",
    MediaPlayPause: "KEY_PLAYPAUSE", BrowserBack: "KEY_BACK",
    p: "KEY_P", P: "KEY_P", v: "KEY_V", V: "KEY_V", m: "KEY_M", M: "KEY_M",
    f: "KEY_F", F: "KEY_F", a: "KEY_A", A: "KEY_A", t: "KEY_T", T: "KEY_T",
    s: "KEY_S", S: "KEY_S", r: "KEY_R", R: "KEY_R", d: "KEY_D", D: "KEY_D",
    y: "KEY_Y", Y: "KEY_Y", l: "KEY_L", L: "KEY_L", c: "KEY_C", C: "KEY_C", x: "KEY_X", X: "KEY_X", k: "KEY_K", K: "KEY_K",
    o: "KEY_O", O: "KEY_O", n: "KEY_N", N: "KEY_N", g: "KEY_G", G: "KEY_G", w: "KEY_W", W: "KEY_W",
    e: "KEY_E", E: "KEY_E", u: "KEY_U", U: "KEY_U",
    "+": "KEY_KPPLUS", "=": "KEY_EQUAL", "-": "KEY_MINUS", "_": "KEY_MINUS",
  };
  document.addEventListener("keydown", (e) => {
    const field = e.target && e.target.closest && e.target.closest("input, select, textarea");
    if (field) {
      if (field.closest("#track-menu")) {             // the "choose circuit" list: arrows pick, Enter uses
        if (e.key === "Enter") { e.preventDefault(); document.getElementById("track-ok").click(); }
        else if (e.key === "Escape") { e.preventDefault(); document.getElementById("track-menu").remove(); }
        return;
      }
      if (S.ui && S.ui.sync_menu && field.closest("#sync-menu")) {
        if (e.key === "Escape") field.blur();
        return;                                       // typing in the open SYNC menu
      }
      field.blur();                                   // a leftover focus (menu closed) must not eat the keys
    }
    if (e.key === "Escape" && S.ui && S.ui.sync_menu) {
      e.preventDefault();
      send({ type: "command", command: "SYNC_MENU", arg: "close" });
      return;
    }
    const k = KEYS[e.key];
    if (!k) return;
    e.preventDefault();
    send({ type: "key", key: k });
  });

  // ------------------------------------------------------------------ helpers
  function fmtClock(ms) {
    if (!has(ms)) return null;
    const t = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(s).padStart(2, "0");
  }
  function utcDate(s) {
    if (!s) return null;
    const iso = /[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + "Z";
    const d = new Date(iso);
    return isNaN(d) ? null : d;
  }
  function hhmm(s) {
    const d = utcDate(s);
    return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  }
  function tyreDot(comp, big) {
    const c = comp || "UNK";
    return `<span class="tyre-dot ${esc(c)}${big ? " big" : ""}">${COMP_LETTER[c] || "?"}</span>`;
  }
  function timeClass(tv) { return tv && tv.overall_best ? "ob" : tv && tv.personal_best ? "pb" : ""; }
  function secClass(tv) {
    if (!tv || !has(tv.value)) return "";
    return tv.overall_best ? "s-ob" : tv.personal_best ? "s-pb" : "s-n";
  }
  function badges(rc) {
    if (!rc) return "";
    let h = "";
    if (rc.disqualified) h += '<span class="badge b-dsq">DSQ</span>';
    for (const p of rc.penalties || []) {
      h += p.served ? `<span class="badge b-served" title="served">${esc(p.label)}✓</span>`
                    : `<span class="badge b-pen">${esc(p.label)}</span>`;
    }
    if (rc.investigation === "UNDER INVESTIGATION") h += '<span class="badge b-inv" title="under investigation">⚠</span>';
    else if (rc.investigation === "AFTER RACE") h += '<span class="badge b-after" title="investigated after the race">⚠PR</span>';
    else if (rc.investigation === "NOTED") h += '<span class="badge b-noted" title="incident noted">N</span>';
    if (rc.black_white) h += '<span class="badge b-bw" title="black and white flag"></span>';
    return h;
  }
  function selectedNum() {
    const sel = S.ui.selected;
    if (sel && S.drivers[sel]) return sel;
    return S.order[0] || null;
  }
  // ---- qualifying / practice (session-aware board)
  const isTimed = (s) => ["qualifying", "practice"].includes((s || S.session || {}).session_kind);
  function f1Now() {
    // F1 time of the shown moment, advanced smoothly between two states (0 while the video is paused)
    const s = S.session || {};
    return F1Time.f1Now(s.now_ms, s.clock ? s.clock.speed : null, performance.now() - S.clockAt);
  }
  function fmtRun(ms, tenths) {
    if (!has(ms) || ms < 0) return null;
    const t = Math.floor(ms / 100) / 10;                 // truncated like a stopwatch
    const m = Math.floor(t / 60), sec = t - m * 60;
    const ss = tenths === false ? String(Math.floor(sec)) : sec.toFixed(1);
    return m ? `${m}:${ss.padStart(tenths === false ? 2 : 4, "0")}` : ss;
  }
  function runSpan(startMs) {
    // running time since startMs (lap / sector being driven) - refreshed by the ticker
    if (!has(startMs)) return NA;
    const now = f1Now();
    return `<span class="run" data-run="${startMs}">${now === null ? "" : esc(fmtRun(now - startMs) || "")}</span>`;
  }
  // qualifying: what the car is doing (server/lap_state.py); LOW confidence arrives as UNKNOWN
  const LS_TAG = { "OUT LAP": ["OUT", "ls-out"], PREP: ["PREP", "ls-prep"], "HOT LAP": ["HOT", "ls-hot"],
    COOLDOWN: ["COOL", "ls-cool"] };
  function lapStateTag(d, long) {
    const t = LS_TAG[d.lap_state];
    if (!t || !has(d.lap_now)) return "";
    return `<span class="ls ${t[1]}" title="${esc(d.lap_state)} · ${esc(d.lap_state_conf || "")}: ${esc(d.lap_state_why || "")}">${long ? esc(d.lap_state) : t[0]}</span>`;
  }
  function nowText(d) {
    // what the car does now: lap being driven + sector (never the last completed one)
    if (d.dnf || d.retired) return "";
    if (d.in_garage || d.in_pit) return "PIT";
    if (!has(d.lap_now)) return "--";
    return `L${d.lap_now}·${has(d.sector_now) ? "S" + d.sector_now : "S–"}`;
  }
  function gapText(d, kind) {
    const g = d.gap;
    if (!has(g)) return d.position === 1 && kind !== "race" ? "—" : null;
    if (/^LAP/i.test(g)) return "LEADER";
    return g;
  }

  // ------------------------------------------------------------------ top bar
  function renderTop() {
    const s = S.session || {};
    if (S.mode === "live") renderMode();               // FINISHED comes with the session state
    $("meeting").textContent = s.meeting_name || (S.mode === "live" ? "F1 LIVE TIMING" : "—");
    // live and connected, but the feed has not said which session: never guessed
    const connected = S.mode === "live" && ["connected", "stale"].includes((S.status || {}).state);
    let sname = s.session_name || (connected ? "SESSION UNKNOWN" : "WAITING FOR SESSION");
    if (connected && s.session_name && s.session_kind === "unknown") sname = `${s.session_name} · SESSION TYPE UNKNOWN`;
    if (s.session_kind === "qualifying" && s.session_part) {
      sname = /sprint/i.test(s.session_name || "") ? `${s.session_name} · SQ${s.session_part}` : `${s.session_name} · Q${s.session_part}`;
    }
    $("session-name").textContent = sname;
    if (isTimed(s)) {
      $("lap-label").textContent = s.session_kind === "qualifying" ? "PHASE" : "SESSION";
      $("lap").innerHTML = (s.phase ? esc(s.phase) : NA) + (s.phase_state ? ` <small class="ph-st">${esc(s.phase_state)}</small>` : "");
    } else if (s.session_kind === "race" || has(s.total_laps)) {
      $("lap-label").textContent = "LAP";
      $("lap").innerHTML = has(s.lap) ? `${s.lap}<span class="tot"> / ${has(s.total_laps) ? s.total_laps : "N/A"}</span>` : NA;
    } else {
      $("lap-label").textContent = "SESSION";
      $("lap").innerHTML = s.status ? esc(String(s.status).toUpperCase()) : NA;
    }
    renderClock();

    const ts = S.track_status || {};
    const labels = { GREEN: "GREEN", YELLOW: "YELLOW", SC: "SAFETY CAR", VSC: "VSC", VSC_ENDING: "VSC ENDING", RED: "RED FLAG", CHEQUERED: "CHEQUERED", UNKNOWN: "TRACK STATUS N/A" };
    const pill = $("track-status");
    pill.className = "ts-pill ts-" + (ts.status || "UNKNOWN");
    $("ts-main").textContent = labels[ts.status] || ts.status || "N/A";
    let sub = "";
    if (ts.status === "VSC") sub = "VIRTUAL SAFETY CAR";
    if (ts.sc_phase && /IN THIS LAP|ENDING/.test(ts.sc_phase)) sub = ts.sc_phase;
    const nY = Object.keys(ts.sector_flags || {}).length;
    if (ts.status === "YELLOW" || (nY && ts.status === "GREEN")) sub = nY ? `${nY} SECTOR${nY > 1 ? "S" : ""} YELLOW` : "LOCAL YELLOW";
    if (s.red_flag) sub = "SESSION SUSPENDED";
    if (ts.source === "RaceControl") sub = (sub ? sub + " · " : "") + "from race control";
    $("ts-sub").textContent = sub;
    setFlagState(ts);

    const pe = $("pitexit-chip"), peHtml = pitExitHTML("pitexit-chip");
    if (pe.dataset.h !== peHtml) { pe.outerHTML = peHtml; $("pitexit-chip").dataset.h = peHtml; }
    const chip = $("rule-chip");
    if (has(ts.overtake)) { chip.textContent = "OVERTAKE " + ts.overtake; chip.className = "rule-chip" + (ts.overtake === "ENABLED" ? " on" : ""); }
    else if (has(ts.drs)) { chip.textContent = "DRS " + ts.drs; chip.className = "rule-chip" + (ts.drs === "ENABLED" ? " on" : ""); }
    else { chip.textContent = ""; }

    const w = S.weather || {};
    const arrow = has(w.wind_direction) ? `<span class="wind-arrow" style="transform:rotate(${w.wind_direction + 180}deg)">↑</span>` : "";
    $("weather-mini").innerHTML =
      `<div class="wrow"><span>AIR <b>${na(w.air_temp, (v) => v.toFixed(1) + "°")}</b></span>` +
      `<span>TRACK <b>${na(w.track_temp, (v) => v.toFixed(1) + "°")}</b></span>` +
      `<span>HUM <b>${na(w.humidity, (v) => Math.round(v) + "%")}</b></span></div>` +
      `<div class="wrow"><span>WIND <b>${na(w.wind_speed, (v) => v.toFixed(1) + " m/s")}</b> ${arrow} <b>${na(w.wind_direction, (v) => v + "°")}</b></span>` +
      `<span>RAIN <b class="${w.rainfall ? "rain-yes" : ""}">${w.rainfall === null || w.rainfall === undefined ? NA : w.rainfall ? "YES" : "NO"}</b></span></div>`;
  }
  function clockRemaining() {
    // the replay moves the clock, nothing else: speed 0 (video paused) keeps it frozen, 0:00 stays 0:00
    return F1Time.clockNow((S.session || {}).clock, performance.now() - S.clockAt);
  }
  function renderClock() {
    const ms = clockRemaining();
    let t = fmtClock(ms);
    const sy = S.sync;
    if (sy && sy.vod && t !== null) {
      if (sy.confidence === "UNSYNCED") t = null;
      else if (sy.confidence === "LOW") t = "≈" + t.replace(/:\d\d$/, "");      // minutes only
    }
    $("clock").innerHTML = t === null ? NA : t;
    for (const id of ["ri-clock", "fb-clock"]) { const e = document.getElementById(id); if (e) e.innerHTML = t === null ? NA : t; }
  }
  function feedAuthLabel() {
    // AUTHENTICATED (F1 TV sign-in in use) / ANONYMOUS (public feed) - never anything secret
    const st = S.status || {}, f = st.f1tv || {};
    if (st.auth === "AUTHENTICATED") return "F1 TV AUTHENTICATED" + (f.product ? " (" + f.product + ")" : "");
    if (f.subscription) return "ANONYMOUS - F1 TV sign-in " + (f.state || "?").toLowerCase().replace("_", " ");
    return "ANONYMOUS";
  }
  function liveState() {
    // LIVE connection state: CONNECTING / LIVE / DELAYED / DISCONNECTED / RECONNECTING / FINISHED
    if (S.mode && S.mode !== "live") return null;
    if (!S.wsOpen) return "DISCONNECTED";                   // this screen lost the dashboard server
    const st = S.status || {};
    if (st.state === "reconnecting" || (st.state === "connecting" && st.attempt > 0)) return "RECONNECTING";
    if (st.state === "connecting" || st.state === "starting" || !st.state) return "CONNECTING";
    if (st.state === "stale") return "DELAYED";               // socket open, no data from F1
    if (st.state === "error") return "DISCONNECTED";
    if ((S.session || {}).state === "FINISHED") return "FINISHED";
    return "LIVE";
  }
  // ------------------------------------------------------------------ weather report popup (server/weather.py)
  let wxTimer = null;
  function showWeatherReport(r) {
    const c = r.current || {}, f = r.forecast || {}, ri = r.race_impact || {}, rd = r.radar || {};
    const u = (v, unit, dp = 1) => has(v) ? `${Number(v).toFixed(dp)}<small>${unit}</small>` : NA;
    const tile = (k, v, cls) => `<div class="tile ${cls || ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    const tr = has(c.track_temp_trend_c) && Math.abs(c.track_temp_trend_c) >= 0.5
      ? ` <small>${c.track_temp_trend_c > 0 ? "▲" : "▼"}${Math.abs(c.track_temp_trend_c).toFixed(1)}</small>` : "";
    // wind arrow points where the wind blows TO (F1 gives where it comes from)
    const arrow = has(c.wind_direction_deg) ? `<span style="display:inline-block;transform:rotate(${(c.wind_direction_deg + 180) % 360}deg)">↑</span> ` : "";
    const rainCur = c.rainfall === true ? "YES" : c.rainfall === false ? "NO" : NA;
    let rainF, inten = NA, dur = NA, conf = NA;
    if (!f.available) rainF = '<span class="na">N/A</span>';
    else if (f.rain_expected === false) rainF = "NOT EXPECTED";
    else {
      rainF = esc(f.expected_lap || f.expected_time || "LATER") + (f.rain_expected === "POSSIBLE" ? " <small>POSSIBLE</small>" : "");
      inten = esc((f.intensity_range && f.intensity_range.length > 1) ? f.intensity_range.join("–") : f.intensity || "N/A");
      dur = has(f.duration_min) ? `~${esc(f.duration_min)}<small> min</small>` : NA;
    }
    if (f.available) conf = esc(f.confidence || "N/A");
    // RADAR: ETA (an estimate from the radar / nowcast data - never shown as exact)
    let eta = NA;
    if (rd.available) {
      if (has(rd.rain_eta_minutes) && rd.rain_eta_minutes > 0) {
        eta = `~${esc(rd.rain_eta_minutes)}<small> MIN</small>${has(rd.rain_eta_lap) ? ` / <small>LAP</small> ${esc(rd.rain_eta_lap)}` : ""}`;
      } else eta = esc(rd.eta_label || (rd.rain_eta_minutes === 0 ? "NOW" : "—"));
    }
    const mv = rd.available ? esc(rd.movement || "—") + (has(rd.direction_deg) && rd.speed_kmh ? ` <small>${esc(compass(rd.direction_deg))} ${esc(rd.speed_kmh)} km/h</small>` : "") : NA;
    const lap = has(r.session_lap) ? `LAP ${esc(r.session_lap)}${has(r.total_laps) ? " / " + esc(r.total_laps) : ""}` : "";
    const hhmm = (ms) => { const d = new Date(ms); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
    const rsrc = !rd.available ? `RADAR UNAVAILABLE${rd.reason ? " · " + esc(rd.reason) : ""}`
      : `RADAR: ${esc(rd.provider || "")}${has(rd.timestamp_ms) ? " · " + hhmm(rd.timestamp_ms) : ""}` +
        (rd.age_s > 60 ? ` · updated ${Math.round(rd.age_s / 60)} min ago` : "") + (rd.stale ? " · STALE" : "");
    $("wx-box").innerHTML =
      `<h2>🌦 WEATHER REPORT ${r.simulated ? '<span class="wx-sim">SIMULATED</span>' : ""}<span class="wx-lap">${lap}</span></h2>` +
      `<div class="wx-cols"><div class="wx-radar">` +
        `<div class="wx-sec">RADAR</div><div class="wx-rmap"><canvas id="wx-canvas"></canvas>` +
          `<div class="wx-rlabel" id="wx-rlabel"></div>${rd.available ? "" : '<div class="wx-roff">RADAR UNAVAILABLE</div>'}</div>` +
        `<div class="wx-legend">${["DRIZZLE", "LIGHT", "MEDIUM", "HEAVY"].map((k) => `<span><i style="background:${RADAR_COL[k]}"></i>${k}</span>`).join("")}</div>` +
        `<div class="wx-tiles">${tile("RAIN ETA", eta, "wx-eta")}${tile("AT CIRCUIT", rd.available ? esc(rd.current_intensity || "N/A") : NA, "wx-i-" + esc(rd.current_intensity || ""))}` +
        `${tile("MOVEMENT", mv)}</div>` +
        `<div class="wx-rsrc">${rsrc}</div>` +
      `</div><div class="wx-info">` +
        `<div class="wx-sec">CURRENT${c.available ? " · F1 TIMING" : " · F1 SESSION WEATHER UNAVAILABLE"}</div>` +
        `<div class="wx-tiles">${tile("AIR", u(c.air_temperature, "°C"))}${tile("TRACK", u(c.track_temperature, "°C") + tr)}` +
        `${tile("HUMIDITY", u(c.humidity, "%", 0))}${tile("WIND", has(c.wind_speed_kmh) ? arrow + u(c.wind_speed_kmh, " km/h", 0) + (c.wind_direction ? ` <small>${esc(c.wind_direction)}</small>` : "") : NA)}` +
        `${tile("RAIN", rainCur, c.rainfall ? "wx-rain" : "")}${tile("CONDITION", c.condition ? esc(c.condition) : NA)}</div>` +
        `<div class="wx-sec">FORECAST</div>` +
        `<div class="wx-tiles f2">${tile("RAIN", rainF, f.rain_expected ? "wx-rain" : "")}${tile("INTENSITY", inten, "wx-i-" + esc(f.intensity || ""))}` +
        `${tile("DURATION", dur)}${tile("CONFIDENCE", conf)}</div>` +
        `<div class="wx-sec">EXPECTED</div><div class="wx-text">${esc(f.text || "Forecast unavailable.")}</div>` +
      `</div></div>` +
      `<div class="wx-sec">RACE IMPACT</div><div class="wx-text">${esc(ri.summary || "")}</div>` +
      (ri.details || []).map((x) => `<div class="wx-text dim">${esc(x)}</div>`).join("") +
      `<div class="wx-foot">SOURCES: ${esc((r.sources || []).join(" · ") || "none")}` +
      (f.available && f.rain_expected ? (f.lap_estimate ? ` · laps estimated from the leader's pace (${esc(f.lap_time_s)} s/lap), not an official F1 prediction` : " · no lap estimate (race pace unknown)") : "") +
      (rd.available ? " · radar ETA = estimate from radar / nowcast data" : "") + "</div>";
    const box = $("wx-report");
    box.hidden = false;
    WxRadar.start(rd);
    clearTimeout(wxTimer);
    wxTimer = setTimeout(() => { box.hidden = true; WxRadar.stop(); }, Math.max(5, r.display_seconds || 15) * 1000);
  }
  const RADAR_COL = { DRIZZLE: "rgba(120,200,255,.55)", LIGHT: "rgba(40,140,255,.7)", MEDIUM: "rgba(255,200,40,.8)", HEAVY: "rgba(240,60,60,.85)" };
  function compass(deg) {
    const d = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
    return d[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
  }
  // The radar map: RainViewer radar tiles (observed) and / or the numeric precipitation grid, centred on
  // the circuit; range rings, the circuit marker above the precipitation, frames PAST -> NOW -> FORECAST.
  const WxRadar = (() => {
    let timer = null, data = null, idx = 0, seq = [];
    const imgs = new Map();
    function img(url) {
      if (!imgs.has(url)) { const im = new Image(); im.crossOrigin = "anonymous"; im.onload = () => draw(); im.src = url; imgs.set(url, im); }
      return imgs.get(url);
    }
    function cat(v, th) {
      if (!has(v) || v < th.drizzle_mm_h) return null;
      return v >= th.heavy_mm_h ? "HEAVY" : v >= th.medium_mm_h ? "MEDIUM" : v >= th.light_mm_h ? "LIGHT" : "DRIZZLE";
    }
    function draw() {
      const cv = document.getElementById("wx-canvas");
      if (!cv || !data) return;
      const S = cv.clientWidth || 300, dpr = window.devicePixelRatio || 1;
      if (cv.width !== Math.round(S * dpr)) { cv.width = Math.round(S * dpr); cv.height = Math.round(S * dpr); }
      const g = cv.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, S, S);
      g.fillStyle = "#0b1118"; g.fillRect(0, 0, S, S);
      if (!data.available) return;
      const R = data.radius_km, k = S / 2 / R, lat0 = data.center.lat, lon0 = data.center.lon;
      const kmLon = 111.32 * Math.cos(lat0 * Math.PI / 180);
      const P = (lat, lon) => [S / 2 + (lon - lon0) * kmLon * k, S / 2 - (lat - lat0) * 111.32 * k];
      const fr = seq[idx] || seq[seq.length - 1];
      if (fr && fr.src === "rv") {
        // RainViewer web-mercator tiles at zoom z, scaled to the km scale of the map
        const rv = data.rainviewer, z = rv.zoom, n = 2 ** z, ts = 256;
        const wx = (lon0 + 180) / 360 * n * ts;
        const lr = lat0 * Math.PI / 180, wy = (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n * ts;
        const kmPx = 40075 * Math.cos(lr) / (n * ts), sc = kmPx * k;
        const half = S / 2 / sc;
        for (let tx = Math.floor((wx - half) / ts); tx <= Math.floor((wx + half) / ts); tx++) {
          for (let ty = Math.floor((wy - half) / ts); ty <= Math.floor((wy + half) / ts); ty++) {
            if (ty < 0 || ty >= n) continue;
            const im = img(`${rv.host}${fr.path}/256/${z}/${((tx % n) + n) % n}/${ty}/${rv.color}/${rv.options}.png`);
            if (im.complete && im.naturalWidth) g.drawImage(im, S / 2 + (tx * ts - wx) * sc, S / 2 + (ty * ts - wy) * sc, ts * sc, ts * sc);
          }
        }
      } else if (fr) {
        const N = data.grid.n, step = 2 * R / (N - 1);
        fr.cells.forEach((v, i) => {
          const c = cat(v, data.thresholds || {});
          if (!c) return;
          const e = -R + (i % N) * step, no = R - Math.floor(i / N) * step;
          const x = S / 2 + e * k, y = S / 2 - no * k, rr = step * k * 0.85;
          const gr = g.createRadialGradient(x, y, 0, x, y, rr);
          gr.addColorStop(0, RADAR_COL[c]); gr.addColorStop(1, "rgba(0,0,0,0)");
          g.fillStyle = gr; g.beginPath(); g.arc(x, y, rr, 0, Math.PI * 2); g.fill();
        });
      }
      // range rings + north
      g.strokeStyle = "rgba(255,255,255,.18)"; g.lineWidth = 1;
      for (const f of [0.5, 1]) { g.beginPath(); g.arc(S / 2, S / 2, S / 2 * f - 1, 0, Math.PI * 2); g.stroke(); }
      g.fillStyle = "rgba(255,255,255,.5)"; g.font = "700 10px sans-serif";
      g.fillText(`${Math.round(R / 2)} km`, S / 2 + 3, S / 4 - 3); g.fillText("N", S / 2 - 3, 11);
      // circuit marker (above the precipitation): ring, the real outline magnified, name
      g.strokeStyle = "#fff"; g.lineWidth = 2; g.beginPath(); g.arc(S / 2, S / 2, 13, 0, Math.PI * 2); g.stroke();
      const ol = data.outline || [];
      if (ol.length > 3) {
        let mx = 0; for (const p of ol) { const q = P(p[0], p[1]); mx = Math.max(mx, Math.abs(q[0] - S / 2), Math.abs(q[1] - S / 2)); }
        const m = mx > 0 ? 9 / mx : 1;
        g.strokeStyle = "#ff1e28"; g.lineWidth = 2; g.beginPath();
        ol.forEach((p, i) => { const q = P(p[0], p[1]); const x = S / 2 + (q[0] - S / 2) * m, y = S / 2 + (q[1] - S / 2) * m; i ? g.lineTo(x, y) : g.moveTo(x, y); });
        g.closePath(); g.stroke();
      } else { g.fillStyle = "#ff1e28"; g.beginPath(); g.arc(S / 2, S / 2, 4, 0, Math.PI * 2); g.fill(); }
      if (data.circuit) {
        g.font = "800 11px sans-serif";
        let name = data.circuit.toUpperCase();
        while (name.length > 4 && g.measureText(name).width > S - 24) name = name.slice(0, -2).trimEnd() + "…";
        const w = g.measureText(name).width;
        g.fillStyle = "rgba(0,0,0,.65)"; g.fillRect(S / 2 - w / 2 - 4, S / 2 + 16, w + 8, 15);
        g.fillStyle = "#fff"; g.fillText(name, S / 2 - w / 2, S / 2 + 27);
      }
      // movement arrow (from the analysis), bottom right
      if (has(data.direction_deg) && data.speed_kmh) {
        g.save(); g.translate(S - 24, S - 24); g.rotate(data.direction_deg * Math.PI / 180);
        g.strokeStyle = "#fff"; g.lineWidth = 2.5; g.beginPath(); g.moveTo(0, 12); g.lineTo(0, -12); g.moveTo(-6, -5); g.lineTo(0, -12); g.lineTo(6, -5); g.stroke();
        g.restore();
      }
      const lab = document.getElementById("wx-rlabel");
      if (lab && fr) {
        const rel = Math.round((fr.t - Date.now()) / 60000);
        lab.textContent = (fr.kind === "forecast" || fr.kind === "nowcast" ? "FORECAST " : fr.kind === "now" || Math.abs(rel) <= 5 ? "NOW " : "PAST ") +
          (Math.abs(rel) <= 5 ? "" : (rel > 0 ? "+" : "−") + Math.abs(rel) + " min") + (fr.src === "rv" ? " · RADAR" : data.simulated ? " · SIMULATED" : " · MODEL GRID");
      }
    }
    function start(rd) {
      stop(); data = rd; idx = 0; seq = [];
      if (rd && rd.available) {
        const rv = rd.rainviewer;
        if (rv && rv.frames && rv.frames.length) {
          seq = rv.frames.map((x) => ({ src: "rv", t: x.t, path: x.path, kind: x.kind }));
          // future: RainViewer nowcast if it has one, else the numeric forecast frames (labelled)
          if (!rv.frames.some((x) => x.kind === "nowcast")) seq = seq.concat((rd.frames || []).filter((x) => x.kind === "forecast").map((x) => ({ ...x, src: "grid" })));
        } else seq = (rd.frames || []).map((x) => ({ ...x, src: "grid" }));
        const nowI = Math.max(0, seq.reduce((a, x, i) => (x.t <= Date.now() ? i : a), 0));
        idx = rd.animation ? 0 : nowI;
        if (rd.animation && seq.length > 1) {
          const tick = () => { idx = (idx + 1) % seq.length; draw(); timer = setTimeout(tick, idx === nowI ? 1600 : idx === seq.length - 1 ? 1400 : 700); };
          timer = setTimeout(tick, 700);
        }
      }
      requestAnimationFrame(draw);
    }
    function stop() { clearTimeout(timer); timer = null; }
    return { start, stop };
  })();
  $("wx-report").addEventListener("click", () => { $("wx-report").hidden = true; clearTimeout(wxTimer); WxRadar.stop(); });

  // ------------------------------------------------------------------ MODE selector (AUTO / LIVE / VOD)
  function resetData() {
    Object.assign(S, { session: {}, track_status: {}, weather: {}, drivers: {}, order: [], race_control: [], radio: [],
      availability: {}, timeline: null, map: {}, status: {}, tel: {}, sync: null, track: null });
    Map2D.reset(); Map2D.setTrack(null);
  }
  function modeSelHTML(label) {
    const m = S.modeSel;
    if (!m) return "";
    const eff = m.effective_mode;
    const tip = `Selected: ${m.selected_mode} · detected: ${m.detected_mode} (${m.detected_reason || ""}) · running: ${eff}` +
      " - AUTO follows the detection, LIVE / VOD override it (key E / remote MENU)";
    return `<span class="mode-sel${m.switching ? " busy" : ""}" title="${esc(tip)}">` +
      (label ? '<span class="ms-label">MODE</span>' : "") +
      (m.options || ["AUTO", "LIVE", "VOD"]).map((o) =>
        `<button type="button" data-mode="${esc(o)}" class="m-${esc(o)}${m.selected_mode === o ? " sel" : ""}` +
        `${m.selected_mode === "AUTO" && eff === o ? " eff" : ""}">${esc(o)}</button>`).join("") + "</span>";
  }
  function modeDetail() {
    const m = S.modeSel;
    if (!m) return "";
    if (m.switching) return `SWITCHING TO <b>${esc(m.effective_mode)}</b>…`;
    // the selected button shows AUTO / LIVE / VOD; here: what the detection says (and MANUAL when overridden)
    let t = m.selected_mode === "AUTO" ? `DETECTED: <b>${esc(m.detected_mode)}</b>`
      : `<b>MANUAL</b> · DETECTED: ${esc(m.detected_mode)}`;
    if (m.notice) t += ` · <span class="warn">NO LIVE SESSION</span>`;
    if (m.error) t += ` · <span class="warn">${esc(m.error)}</span>`;
    return t;
  }
  function renderModeSel() {
    const tip = S.modeSel ? (S.modeSel.detected_reason || "") : "";
    $("mode-sel").innerHTML = modeSelHTML(true);
    $("mode-detected").innerHTML = modeDetail();
    $("mode-detected").title = tip;
    const ri = $("ri-mode");
    if (ri) { ri.innerHTML = modeSelHTML(false) + `<span class="mode-detected">${modeDetail()}</span>`; ri.title = tip; }
    const fb = $("fb-modesel");
    if (fb) fb.innerHTML = modeSelHTML(false);
  }
  document.addEventListener("click", (e) => {
    const b = e.target && e.target.closest && e.target.closest(".mode-sel button[data-mode]");
    if (!b) return;
    e.preventDefault();
    send({ type: "mode", value: b.dataset.mode });
  });

  // ---- session-wide flag state: stage classes (CSS layers under the mini-map), the map's flag pill and
  // one short takeover animation per real change (RED > SC > VSC > DOUBLE YELLOW in intensity)
  let flagNow = null;
  const FLAG_PILL = { RED: "RED FLAG", SC: "SAFETY CAR", VSC: "VIRTUAL SAFETY CAR", VSC_ENDING: "VSC ENDING",
    DY: "DOUBLE YELLOW", CHEQUERED: "CHEQUERED FLAG" };
  function setFlagState(ts) {
    const st = ts.status;
    const f = ts.state === "DOUBLE_YELLOW" && st === "YELLOW" ? "DY" : st;
    stage.classList.remove("flag-RED", "flag-SC", "flag-VSC", "flag-VSC_ENDING", "flag-YELLOW", "flag-DY", "flag-CHEQUERED");
    if (["RED", "SC", "VSC", "VSC_ENDING", "YELLOW", "CHEQUERED"].includes(st)) stage.classList.add("flag-" + st);
    if (f === "DY") stage.classList.add("flag-DY");
    const pill = $("map-flagpill");
    if (FLAG_PILL[f]) {
      if (pill.dataset.f !== f) { pill.textContent = FLAG_PILL[f]; pill.className = "map-flagpill f-" + f; pill.dataset.f = f; }
      pill.hidden = false;
    } else { pill.hidden = true; pill.dataset.f = ""; }
    if (flagNow !== null && f !== flagNow && f) flash($("map-flag"), f === "RED" ? "ev-flag-red" : "ev-flag", f === "RED" ? 1100 : 800);
    flagNow = f || flagNow;
  }

  // pit exit OPEN / CLOSED: race control's literal "PIT EXIT OPEN / CLOSED" (also "GREEN LIGHT - PIT
  // EXIT OPEN", "PIT LANE CLOSED") up to the shown moment - server: race_control.py -> track_status.pit_exit.
  // No such message yet: "PIT EXIT —" (never guessed)
  function pitExitHTML(id) {
    const v = (S.track_status || {}).pit_exit;
    const cls = v === "OPEN" ? " open" : v === "CLOSED" ? " closed" : "";
    return `<div${id ? ` id="${id}"` : ""} class="pitexit${cls}" title="from race control messages">PIT EXIT <b>${v === "OPEN" ? "OPEN" : v === "CLOSED" ? "CLOSED" : "—"}</b></div>`;
  }

  // phone remote: its URL (LAN / Tailscale, from the server) and a QR code in the HELP panel
  let phoneRemote = null;
  function renderPhoneRemote() {
    const box = $("help-remote");
    if (!phoneRemote) {
      fetch("/api/remote/info").then((r) => r.json()).then((j) => { phoneRemote = j; renderPhoneRemote(); }).catch(() => {});
      return;
    }
    const u = phoneRemote.url;
    let qr = "";
    try {
      if (window.qrcode) { const q = qrcode(0, "M"); q.addData(u); q.make(); qr = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true }); }
    } catch (e) { qr = ""; }
    const alt = (phoneRemote.lan || []).slice(1).concat(phoneRemote.tailscale || []).map((x) => x.split("?")[0]);
    box.innerHTML = (qr ? `<div class="qr">${qr}</div>` : "") +
      `<div><h3>PHONE REMOTE</h3><div class="url">${esc(u.split("?")[0])}</div>` +
      (alt.length ? `<div class="alt">${alt.map(esc).join("<br>")}${(phoneRemote.tailscale || []).length ? " (Tailscale)" : ""}</div>` : "") +
      `<div class="note">Scan with the phone camera (same Wi-Fi). ${esc(phoneRemote.note || "")}</div></div>`;
  }

  function renderMode() {
    const b = $("mode-badge");
    const m = S.mode;
    const live = liveState();
    b.className = "mode-badge mode-" + (m || "live") + (live && live !== "LIVE" ? " live-" + live.toLowerCase() : "");
    b.textContent = m === "test" ? "TEST MODE" : m === "replay" ? "REPLAY" + (S.status.detail && /x[\d.]+/.test(S.status.detail) ? " " + S.status.detail.match(/x[\d.]+/)[0] : "") : m === "vod" ? "RECORDING" : live === "LIVE" ? "LIVE" : "LIVE · " + live;
    $("test-watermark").hidden = m !== "test";
  }

  // ------------------------------------------------------------------ status / banners
  function renderStatus() {
    renderMode();
    const st = S.status || {};
    const banner = $("feed-banner");
    let html = "";
    if (S.mode === "live" && (st.state === "reconnecting" || st.state === "connecting") && st.attempt > 0) {
      html = `LIVE DATA DISCONNECTED · RECONNECTING…<small>attempt ${st.attempt}` +
        (has(st.retry_in) ? ` · next try in ${st.retry_in} s` : "") + (st.detail ? ` · ${esc(st.detail)}` : "") + "</small>";
    } else if (S.mode === "live" && st.state === "stale") {
      html = `LIVE DATA DELAYED · NO DATA FROM F1 FOR ${has(st.feed_age_s) ? st.feed_age_s : "?"} s` +
        "<small>connection still open · the board shows the last data received and may be outdated</small>";
    } else if (st.state === "error") {
      html = `DATA SOURCE ERROR<small>${esc(st.detail || "")}</small>`;
    }
    banner.innerHTML = html;
    banner.hidden = !html;
    stage.classList.toggle("stale", !!html);
    bannerSpace("has-fbanner", !!html);
    renderMapInfo();
  }

  // ------------------------------------------------------------------ leaderboard
  const rowEls = {};
  let rowH = 33;
  function bannerSpace(cls, on) {
    // banners take their own space (the panels shrink): re-fit the leaderboard rows when that changes
    if (stage.classList.contains(cls) === on) return;
    stage.classList.toggle(cls, on);
    requestAnimationFrame(layoutBoard);            // (the ResizeObservers re-fit board and map as well)
  }
  function layoutBoard() {
    const rows = $("lb-rows");
    const n = Math.max(S.order.length, 20);
    const title = $("lb-title");
    const avail = $("board").clientHeight - $("lb-head").offsetHeight - (title && !title.hidden ? title.offsetHeight : 0) - 2;
    rowH = Math.max(22, Math.min(44, Math.floor(avail / n)));
    stage.style.setProperty("--row-h", rowH + "px");
    rows.style.height = rowH * S.order.length + "px";
    S.order.forEach((num, i) => { if (rowEls[num]) rowEls[num].el.style.transform = `translate3d(0, ${i * rowH}px, 0)`; });
  }
  function renderBoardHead() {
    const kind = (S.session || {}).session_kind;
    const q = kind === "qualifying";
    const timed = isTimed();
    stage.classList.toggle("kind-timed", timed);
    if (timed) {
      // ranking by the best valid lap of the current phase (not race positions)
      $("lb-head").innerHTML =
        `<div class="pos c">P</div><div class="bar"></div><div class="num r">NO</div><div class="drv">DRIVER</div><div class="team">TEAM</div>` +
        `<div class="gap r">GAP</div><div class="int r" title="lap being driven now · sector">NOW</div><div class="last r">LAST</div>` +
        `<div class="best r">${q ? "BEST " + esc((S.session || {}).phase || "") : "BEST LAP"}</div><div class="sect c">SEC</div><div class="tyre">TYRE</div><div class="pit c">PIT</div>`;
      renderBoardTitle();
      return;
    }
    renderBoardTitle();
    $("lb-head").innerHTML =
      `<div class="pos c">P</div><div class="bar"></div><div class="num r">NO</div><div class="drv">DRIVER</div><div class="team">TEAM</div>` +
      `<div class="gap r">GAP</div><div class="int r">INT</div><div class="last r">LAST</div>` +
      `<div class="best r">${q ? "BEST Q" + ((S.session || {}).session_part || "") : "BEST"}</div><div class="sect c">SEC</div><div class="tyre">TYRE</div><div class="pit c">PIT</div>`;
  }
  // ---- board title for qualifying / practice: "QUALIFYING — Q2" + phase clock + session timeline
  function renderBoardTitle() {
    const el = $("lb-title");
    const s = S.session || {};
    const timed = isTimed(s);
    const was = !el.hidden;
    el.hidden = !timed;
    if (was !== timed) requestAnimationFrame(layoutBoard);
    if (!timed) { el.innerHTML = ""; return; }
    const st = s.phase_state ? `<span class="lbt-state st-${esc(s.phase_state.replace(/ /g, "_"))}">${esc(s.phase_state)}</span>` : "";
    const html = `<div class="lbt-row"><b>${esc(s.title || "")}</b>${st}<span class="lbt-clock" id="lbt-clock"></span></div>` + stripHtml();
    if (el.dataset.html !== html) { el.dataset.html = html; el.innerHTML = html; }
    tickTimed();
  }
  function stripHtml() {
    // phases (Q1 / Q2 / Q3 or the practice session) on F1's clock with their START / END markers;
    // red flags etc. only once passed (the server filters them); ticks = completed laps of the selected car
    const tl = S.timeline;
    const ph = ((tl && tl.phases) || []).map((p) => {
      const a = has(p.start_ms) ? p.start_ms : has(p.end_ms) && has(p.duration_ms) ? p.end_ms - p.duration_ms : null;
      const b = has(p.end_ms) ? p.end_ms : has(a) && has(p.duration_ms) ? a + p.duration_ms : null;
      return { ...p, a, b };
    }).filter((p) => has(p.a) && has(p.b));
    if (!ph.length) { S.strip = null; return ""; }
    const t0 = Math.min(...ph.map((p) => p.a)) - 60000, t1 = Math.max(...ph.map((p) => p.b)) + 60000;
    S.strip = [t0, t1];
    const x = (t) => Math.max(0, Math.min(100, (t - t0) / (t1 - t0) * 100));
    const cur = (S.session || {}).phase;
    let h = ph.map((p) => `<div class="lbt-seg${p.id === cur ? " cur" : ""}" style="left:${x(p.a).toFixed(2)}%;width:${(x(p.b) - x(p.a)).toFixed(2)}%">` +
      `<span>${esc(p.label)}</span></div>`).join("");
    h += (tl.markers || []).map((m) => `<i class="lbt-m k-${esc(m.kind)}" style="left:${x(m.ms).toFixed(2)}%" title="${esc(m.label)}"></i>`).join("");
    const d = S.drivers[selectedNum()];
    if (d) h += (d.lap_marks || []).map((lm) => `<i class="lbt-lap" style="left:${x(lm[0]).toFixed(2)}%" title="${esc(d.tla || "")} lap ${esc(lm[1])}"></i>`).join("");
    return `<div class="lbt-strip"><div class="lbt-fill" id="lbt-fill"></div>${h}<i class="lbt-now" id="lbt-now"></i></div>`;
  }
  function tickTimed() {
    // running lap / sector times, the phase clock and the timeline playhead (between two server states)
    const now = f1Now();
    for (const e of document.querySelectorAll("[data-run]")) {
      const v = now === null ? null : fmtRun(now - +e.dataset.run);
      e.textContent = v === null ? "--" : v;
    }
    const s = S.session || {};
    const ck = document.getElementById("lbt-clock");
    if (ck) {
      const sy = S.sync;
      const hide = sy && sy.vod && ["UNSYNCED", "LOW"].includes(sy.confidence);
      const rem = hide ? null : clockRemaining();
      const dur = s.phase_duration_ms;
      ck.innerHTML = rem === null ? "" : `<b>${fmtClock(rem)}</b> LEFT` +
        (has(dur) && rem <= dur ? ` · <b>${fmtClock(dur - rem)}</b> ELAPSED` : "") +
        (has(dur) ? ` <small>of ${fmtClock(dur)}</small>` : "");
    }
    const np = document.getElementById("lbt-now"), fl = document.getElementById("lbt-fill");
    if (np && S.strip && now !== null) {
      const [t0, t1] = S.strip;
      const p = Math.max(0, Math.min(100, (now - t0) / (t1 - t0) * 100));
      np.style.left = p + "%"; if (fl) fl.style.width = p + "%";
      np.hidden = false;
    } else if (np) np.hidden = true;
  }
  function setCell(r, key, html) {
    if (r.cache[key] !== html) { r.cache[key] = html; r.cells[key].innerHTML = html; }
  }
  // ---- leaderboard motion: rows glide (GPU transform), position changes are emphasised briefly.
  // Driven only by the real order the server sends; a bulk re-order (seek, new session, reconnect)
  // just moves without emphasis.
  function flash(el, cls, ms) {
    if (!el || stage.classList.contains("anim-off")) return;
    el.classList.remove(cls); void el.offsetWidth;            // restart if it is already running
    el.classList.add(cls);
    clearTimeout(el["_t" + cls]);
    el["_t" + cls] = setTimeout(() => el.classList.remove(cls), ms);
  }
  function animateMoves(moves) {
    const changed = moves.filter((m) => m[2] !== undefined && m[2] !== m[3]);
    if (!changed.length || changed.length > Math.max(6, moves.length * 0.4)) return;
    const race = (S.session || {}).session_kind === "race";
    for (const [, r, from, to] of changed) {
      const up = to < from;
      r.cells.drv.dataset.delta = (up ? "▲" : "▼") + Math.abs(from - to);   // shown after the TLA (in its own cell)
      // the gainer travels above the others while the rows glide
      r.el.style.zIndex = up ? 3 : 2;
      clearTimeout(r.zt); r.zt = setTimeout(() => { r.el.style.zIndex = ""; }, 900);
      flash(r.el, up ? (race ? "ev-overtake" : "ev-gain") : "ev-loss", up ? 900 : 700);
    }
  }
  function renderBoard() {
    const rows = $("lb-rows");
    const kind = (S.session || {}).session_kind;
    const cutoff = kind === "qualifying" ? (S.session || {}).quali_cutoff : null;
    const timed = isTimed();
    const sel = selectedNum();
    for (const num of Object.keys(rowEls)) {
      if (!S.drivers[num]) { rowEls[num].el.remove(); delete rowEls[num]; }
    }
    const moves = [];
    S.order.forEach((num, i) => {
      const d = S.drivers[num];
      if (!d) return;
      let r = rowEls[num];
      if (!r) {
        const el = document.createElement("div");
        el.className = "lb-row";
        const keys = ["pos", "bar", "num", "drv", "team", "gap", "int", "last", "best", "sect", "tyre", "pit"];
        const cells = {};
        for (const k of keys) { const c = document.createElement("div"); c.className = k; el.appendChild(c); cells[k] = c; }
        el.style.transform = `translate3d(0, ${i * rowH}px, 0)`;
        el.addEventListener("click", () => send({ type: "command", command: "SELECT_DRIVER", arg: num }));
        rows.appendChild(el);
        r = rowEls[num] = { el, cells, cache: {}, idx: i };
      }
      moves.push([num, r, r.idx, i]);
      r.idx = i;
      r.el.style.transform = `translate3d(0, ${i * rowH}px, 0)`;
      r.el.classList.toggle("selected", num === sel);
      r.el.classList.toggle("retired", !!(d.retired || d.stopped));
      r.el.classList.toggle("ko", !!d.knocked_out);
      r.el.classList.toggle("cutoff-line", has(cutoff) && d.position === cutoff);

      setCell(r, "pos", has(d.position) ? String(d.position) : "—");
      if (r.cache.color !== d.team_color) { r.cache.color = d.team_color; r.cells.bar.style.background = d.team_color || "#444"; }
      setCell(r, "num", esc(d.number));
      // DNF (crashed / broken down) and cars in the garage are named next to the driver
      const out = d.dnf || d.retired;
      const state = out ? '<span class="badge b-dnf">DNF</span>' : d.in_garage ? '<span class="badge b-garage">IN PIT</span>' : "";
      setCell(r, "drv", `<span class="tla">${esc(d.tla || d.number)}</span>${state}${badges(d.rc)}`);
      setCell(r, "team", esc(d.team || ""));
      if (timed) {
        // qualifying / practice: BEST (valid, current phase) · GAP to P1 · LAST · NOW (lap · sector)
        const bl = d.best_lap || {};
        const bestTxt = has(bl.value) ? `<span class="${bl.overall_best ? "ob" : ""}">${esc(bl.value)}</span>` +
          (d.best_deleted ? '<i class="del" title="F1 best lap deleted by race control - not counted">✕</i>' : "")
          : d.out_phase ? '<span class="na">--</span>' : '<span class="nt">NO TIME</span>';
        setCell(r, "best", bestTxt);
        setCell(r, "gap", d.out_phase ? `<span class="koq">${d.out_phase === "OUT" ? "OUT" : "OUT " + esc(d.out_phase)}</span>` : has(d.gap) ? esc(d.gap) :
          d.position === 1 && has(bl.value) ? "—" : "");
        setCell(r, "int", d.out_phase ? "" : esc(nowText(d)) + lapStateTag(d));
        const ll = d.last_lap || {};
        setCell(r, "last", has(ll.value) ? `<span class="${d.last_deleted ? "" : timeClass(ll)}">${esc(ll.value)}</span>` +
          (d.last_deleted ? '<i class="del" title="the last lap was deleted - this is the last valid one">✕</i>' : "") :
          d.last_deleted ? '<span class="nt">DEL</span>' : '<span class="na">--</span>');
        r.cells.int.classList.remove("catching");
      } else {
      const g = gapText(d, kind);
      setCell(r, "gap", g === null ? NA : esc(g));
      const iv = d.interval && /^LAP/i.test(d.interval) ? "—" : d.interval;
      setCell(r, "int", has(iv) ? esc(iv) : (d.position === 1 ? "—" : NA));
      r.cells.int.classList.toggle("catching", !!d.catching);
      setCell(r, "last", has(d.last_lap && d.last_lap.value) ? `<span class="${timeClass(d.last_lap)}">${esc(d.last_lap.value)}</span>` : NA);
      setCell(r, "best", has(d.best_lap && d.best_lap.value) ? `<span class="${d.best_lap.overall_best ? "ob" : ""}">${esc(d.best_lap.value)}</span>` : NA);
      }
      const sec = [0, 1, 2].map((k) => `<i class="${secClass((d.sectors || [])[k])}"></i>`).join("");
      setCell(r, "sect", sec);
      const t = d.tyre || {};
      setCell(r, "tyre", t.compound || has(t.tyre_age)
        ? `${tyreDot(t.compound)}<span class="age">${has(t.tyre_age) ? t.tyre_age : "N/A"}</span>` : NA);
      let pit = has(d.pit_stops) ? String(d.pit_stops) : "";
      const inLane = d.in_pit && !d.in_garage && !out;             // a pit stop in progress
      const leaving = d.pit_out && !out && !d.in_garage;            // (a car in the garage is not leaving)
      if (inLane) pit = "IN"; else if (leaving) pit = "OUT";
      const pitCls = "pit" + (inLane ? " in" : leaving ? " out" : "");
      setCell(r, "pit", pit);
      if (r.cache.pitCls !== pitCls) {
        r.cells.pit.className = pitCls;
        if (r.cache.pitCls !== undefined && (inLane || leaving)) flash(r.cells.pit, "ev-pit", 700);
        r.cache.pitCls = pitCls;
      }
      // meaningful changes only: a new compound (pit stop), a new overall fastest lap, a new PB / overall-best sector
      if (r.cache.comp !== undefined && t.compound && r.cache.comp !== t.compound) flash(r.cells.tyre, "ev-tyre", 600);
      r.cache.comp = t.compound;
      const bl = d.best_lap || {};
      if (bl.overall_best && has(bl.value) && r.cache.ob !== undefined && r.cache.ob !== bl.value) flash(r.cells.best, "ev-fastest", 1100);
      r.cache.ob = bl.overall_best ? bl.value : null;
      const sc = [0, 1, 2].map((k) => secClass((d.sectors || [])[k]));
      if (r.cache.secs) sc.forEach((c, k) => { if ((c === "s-pb" || c === "s-ob") && r.cache.secs[k] !== c) flash(r.cells.sect.children[k], "ev-sector", 650); });
      r.cache.secs = sc;
    });
    rows.style.height = rowH * S.order.length + "px";
    animateMoves(moves);
    $("lb-empty").hidden = S.order.length > 0;
  }

  // ------------------------------------------------------------------ race control
  let lastRcTop = null;
  function rcItems(list, full) {
    return list.map((m) => `<li class="rc-item sev-${esc(m.severity)}"><span class="t">${esc(hhmm(m.utc))}</span>` +
      `<span class="l">${has(m.lap) ? "L" + m.lap : ""}</span><span class="m">${esc(m.text)}</span></li>`).join("");
  }
  function renderRC() {
    const list = S.race_control || [];
    const ul = $("rc-list");
    ul.innerHTML = list.length ? rcItems(list.slice(0, 6)) : '<li class="rc-item"><span></span><span></span><span class="m na">No race control messages</span></li>';
    const top = list[0] ? String(list[0].utc || "") + "|" + list[0].text : null;
    // highlight only a genuinely newer message - not the older one that is on top after a rewind
    if (top && lastRcTop !== null && top !== lastRcTop && top > lastRcTop && ul.firstElementChild) {
      ul.firstElementChild.classList.add("fresh");
      // a major race-control event (deduplicated by the check above): the mini-map reacts with it
      const t = String(list[0].text || "").toUpperCase();
      if (/RED FLAG|SAFETY CAR DEPLOYED|CHEQUERED FLAG/.test(t)) flash($("map-flag"), /RED FLAG/.test(t) ? "ev-flag-red" : "ev-flag", 1000);
    }
    lastRcTop = top;
  }

  // ------------------------------------------------------------------ driver detail / telemetry
  function renderDetail() {
    const num = selectedNum();
    const panel = $("detail-panel");
    const d = num ? S.drivers[num] : null;
    if (!d) { panel.innerHTML = '<div class="map-notice">NO DRIVER DATA</div>'; return; }
    const t = d.tyre || {};
    const name = d.first_name && d.last_name ? `${esc(d.first_name)} <span>${esc(d.last_name)}</span>` : esc(d.full_name || d.tla || num);
    const lapNow = d.dnf || d.retired ? "OUT" : d.in_garage || d.in_pit ? "IN PIT" :
      has(d.lap_now) ? `LAP ${esc(d.lap_now)}${has(d.sector_now) ? " · S" + esc(d.sector_now) : ""}` +
        (lapStateTag(d, true) || (d.lap_how === "pit" ? " <small>OUT LAP</small>" : "")) : "--";
    const stints = (d.stints || []).map((st, i) =>
      `<div class="stint">${tyreDot(st.compound)}${has(st.laps) ? st.laps + " L" : "N/A"}${i === (d.stints.length - 1) ? " ●" : ""}</div>`).join("") || NA;
    const pens = (d.rc && d.rc.penalties || []).map((p) => `<div>${p.served ? "✓ " : "• "}${esc(p.text)}</div>`).join("");
    const inv = d.rc && d.rc.investigation ? `<div>⚠ ${esc(d.rc.investigation)}</div>` : "";
    panel.innerHTML = `
      <div class="dt">
        <div class="dt-head">
          <div class="bar" style="background:${esc(d.team_color || "#444")}"></div>
          <div class="pos">${has(d.position) ? "P" + d.position : "—"}</div>
          <div><div class="name">${name}</div>
            <div class="sub">#${esc(d.number)} · ${esc(d.team || "N/A")}${has(d.grid_position) ? " · GRID P" + d.grid_position : ""}</div></div>
          <div class="grow"></div>
          <div class="badges">${badges(d.rc)}</div>
          ${tyreDot(t.compound, true)}
          <div class="kv" style="grid-template-columns:auto"><div class="v">${has(t.tyre_age) ? t.tyre_age + " <small>LAPS</small>" : NA}</div></div>
        </div>
        <div class="dt-lap">${lapNow}${has(d.lap_now) && has(d.lap_start_ms) ? ` <span class="dl-k">CURRENT</span> ${runSpan(d.lap_start_ms)}` : ""}` +
          `${has(d.sector_now) && has(d.sector_start_ms) && d.sector_start_ms !== d.lap_start_ms ? ` <span class="dl-k">S${esc(d.sector_now)}</span> ${runSpan(d.sector_start_ms)}` : ""}` +
          ` <span class="dl-k">BEST</span> ${has(d.best_lap && d.best_lap.value) ? esc(d.best_lap.value) : "--"}` +
          ` <span class="dl-k">LAST</span> ${has(d.last_lap && d.last_lap.value) ? esc(d.last_lap.value) : "--"}</div>
        <div class="dt-body">
          <div class="dt-col" id="tel-col"></div>
          <div class="dt-col">
            <div class="kv"><div class="k">NOW</div><div class="v lapnow">${lapNow}${has(d.lap_now) && has(d.lap_start_ms) ? ` <span class="cur-run">${runSpan(d.lap_start_ms)}</span>` : ""}</div></div>
            <div class="kv"><div class="k">BEST LAP</div><div class="v ${d.best_lap && d.best_lap.overall_best ? "ob" : ""}">${has(d.best_lap && d.best_lap.value) ? esc(d.best_lap.value) : isTimed() && !d.out_phase ? '<span class="na">NO TIME</span>' : NA}${d.best_deleted ? " <small>F1 best deleted</small>" : ""}</div></div>
            <div class="kv"><div class="k">LAST LAP</div><div class="v ${timeClass(d.last_lap)}">${has(d.last_lap && d.last_lap.value) ? esc(d.last_lap.value) : '<span class="na">--</span>'}${d.last_deleted ? " <small>last lap deleted</small>" : ""}</div></div>
            ${sectorsHTML(d)}
            <div class="kv"><div class="k">GAP / INT</div><div class="v" style="font-size:22px">${na(gapText(d, S.session.session_kind))} / ${na(d.interval && /^LAP/i.test(d.interval) ? "—" : d.interval)}</div></div>
            <div class="kv"><div class="k">STINTS</div><div class="stints">${stints}</div></div>
            <div class="dt-extra" style="flex-direction:column;gap:8px">
              <div class="kv"><div class="k">SPEED TRAP</div><div class="v">${na(d.speed_trap, (v) => v + " <small>km/h</small>")}</div></div>
              <div class="kv"><div class="k">PIT STOPS</div><div class="v">${na(d.pit_stops)}</div></div>
              <div class="kv"><div class="k">LAPS</div><div class="v">${na(d.laps)}</div></div>
              <div class="kv"><div class="k">TYRE</div><div class="v" style="font-size:22px">${na(t.compound)}${t.new === false ? " <small>USED</small>" : t.new ? " <small>NEW</small>" : ""}</div></div>
              <div class="pen-list">${inv}${pens}${d.rc && d.rc.deleted_laps ? `<div>Lap times deleted: ${d.rc.deleted_laps}</div>` : ""}
                ${(d.rc && d.rc.messages || []).slice(-3).reverse().map((x) => `<div class="fia-m"><b>${esc(hhmm(x.utc))}</b> ${esc(x.text)}</div>`).join("")}</div>
            </div>
          </div>
        </div>
      </div>`;
    renderTelemetryLive();
  }

  // S1 / S2 / S3 of a driver: the one being driven now (running time), the last time of each, the
  // personal best - a completed sector is never shown as the current one. One implementation for the
  // full dashboard (stats column) and the VOYO + data view (telemetry column).
  // OUT LAP / IN LAP / IN PIT / RETIRED / STOPPED (server: normalizer._lap_phase, from the official
  // pit / lap data) take the place of S1-S3: never "N/A" sectors for a car that is not on a timed lap
  const PHASE_SUB = { "OUT LAP": "from the pit exit", "IN LAP": "in the pit lane", "IN PIT": "pit lane / garage",
    RETIRED: "", STOPPED: "car stopped" };
  // RACE: CHASING | POSITION | TYRE AGE in place of S1-S3 (same boxes). Practice / qualifying: sectors.
  function raceStatsHTML(d) {
    const box = (k, v, b, cls) => `<div class="sec rs ${cls || ""}"><div class="k">${k}</div><div class="v">${v}</div>` +
      `<div class="b">${b || "&nbsp;"}</div></div>`;
    // CHASING: consecutive laps within the configured gap of the same car ahead (server/chase.py)
    const c = d.chase, ph = d.lap_phase;
    let cv = "—", cb = "no lap completed yet";
    if (ph === "IN LAP" || ph === "OUT LAP" || ph === "IN PIT") cb = ph;
    else if (c && c.laps > 0) {
      cv = `${c.laps} LAP${c.laps === 1 ? "" : "S"}`;
      cb = `${c.ahead_tla ? esc(c.ahead_tla) : "car ahead"}${has(c.gap) ? " · " + Number(c.gap).toFixed(1) + " s" : ""}`;
    } else if (c) {
      cb = { leader: "LEADING", neutral: "SC / VSC lap", pit: "PIT STOP", ahead: "new car ahead" }[c.why] ||
        (has(c.gap) ? `gap ${Number(c.gap).toFixed(1)} s > ${esc(c.threshold)} s` : "no gap data");
    }
    // POSITION: gain / loss against the grid position (TimingAppData GridPos), never the last lap
    let pv = "—", pb = "no grid position", pcls = "";
    if (has(d.position) && has(d.grid_position) && d.grid_position > 0) {
      const g = d.grid_position - d.position;
      pv = `P ${g > 0 ? "+" + g : g < 0 ? "−" + Math.abs(g) : "0"}`;
      pb = `P${esc(d.position)} · GRID P${esc(d.grid_position)}`;
      pcls = g > 0 ? "rs-up" : g < 0 ? "rs-down" : "";
    }
    // TYRE AGE: age of the current set (TotalLaps of the stint), not the race laps
    const t = d.tyre || {};
    const tv = has(t.tyre_age) ? `${esc(t.tyre_age)} LAP${t.tyre_age === 1 ? "" : "S"}` : "—";
    const tb = has(t.compound) ? `${tyreDot(t.compound)} ${esc(t.compound)}${t.new === false ? " · USED" : ""}` : "compound unknown";
    return `<div class="sectors3">${box("CHASING", cv, cb, c && c.laps > 0 ? "rs-chase" : "")}` +
      `${box("POSITION", pv, pb, pcls)}${box("TYRE AGE", tv, tb)}</div>`;
  }
  function sectorsHTML(d) {
    const ph = d.lap_phase;
    if ((S.session || {}).session_kind === "race" && ph !== "RETIRED" && ph !== "STOPPED") return raceStatsHTML(d);
    if (ph && ph in PHASE_SUB) {
      const run = ph === "OUT LAP" && has(d.sector_now)
        ? `S${esc(d.sector_now)}${has(d.sector_start_ms) ? " " + runSpan(d.sector_start_ms) : ""}` : PHASE_SUB[ph];
      return `<div class="sectors3"><div class="sec sec-phase ph-${esc(ph.replace(" ", "-"))}">` +
        `<div class="v">${esc(ph)}</div>${run ? `<div class="b">${run}</div>` : ""}</div></div>`;
    }
    const secs = [0, 1, 2].map((k) => {
      const s = (d.sectors || [])[k] || {};
      const b = (d.best_sectors || [])[k] || {};
      const cur = d.sector_now === k + 1;
      const run = cur && has(d.sector_start_ms) ? runSpan(d.sector_start_ms) : cur ? "…" : "";
      const lastDone = d.last_sector && d.last_sector.n === k + 1 && !cur;
      return `<div class="sec ${secClass(s)}${cur ? " now" : ""}"><div class="k">S${k + 1}${cur ? ' <em>NOW</em>' : lastDone ? ' <em class="ls">LAST</em>' : ""}</div>` +
        `<div class="v">${cur ? `<span class="srun">${run}</span>` : na(s.value)}</div>` +
        `<div class="b">BEST <span class="${b.overall_best ? "ob" : ""}">${has(b.value) ? esc(b.value) : "--"}</span></div></div>`;
    }).join("");
    const lastSec = d.last_sector ? `S${esc(d.last_sector.n)} ${esc(d.last_sector.value)}` : "--";
    return `<div class="sectors3" title="last completed sector: ${lastSec}">${secs}</div>`;
  }

  function renderTelemetryLive() {
    const col = document.getElementById("tel-col");
    if (!col) return;
    const num = selectedNum();
    const a = S.tel[num];                 // {speed, rpm, gear, throttle, brake, drs, drs_raw, ers, channels, age_ms, fresh}
    const avail = S.availability || {};
    const v = a || {};
    const year = (S.session || {}).year;
    const thr = v.throttle, brk = v.brake;
    const drs = has(v.drs) ? v.drs : null;
    let drsNote = "";
    if (!has(drs)) drsNote = year >= 2026 ? "no DRS in 2026" : "";
    const meter = (val, cls) => has(val)
      ? `<div class="meter ${cls}"><i style="width:${val}%"></i><span>${val}%</span></div>` : NA;
    const tile = (k, val, note) => `<div class="tile"><div class="k">${k}</div><div class="v">${val}</div>${note ? `<div class="note">${note}</div>` : ""}</div>`;
    const extra = Object.entries(v.channels || {}).map(([k, x]) => `ch${esc(k)} ${esc(x)}`).join(" · ");
    // VOYO + data view: S1 / S2 / S3 (same component as the stats column) instead of DRS / ERS /
    // OVERTAKE, which the feed does not deliver reliably
    const video = (S.ui.tv_mode_effective || "FULL_DASHBOARD") !== "FULL_DASHBOARD";
    const d = num ? S.drivers[num] : null;
    const stale = a && !a.fresh;
    const age = a && has(a.age_ms) ? (a.age_ms >= 10000 ? Math.round(a.age_ms / 1000) + " s" : (a.age_ms / 1000).toFixed(1) + " s") : null;
    col.innerHTML = `
      <div class="${stale ? "tel-stale" : ""}">
      <div class="kv big"><div class="k">SPEED</div><div class="v">${na(v.speed, (x) => x + "<small>km/h</small>")}</div></div>
      <div class="kv"><div class="k">THROTTLE</div><div>${meter(thr, "")}</div></div>
      <div class="tiles">
        ${tile("GEAR", na(v.gear, (x) => (x === 0 ? "N" : x)))}
        ${tile("RPM", na(v.rpm, (x) => x.toLocaleString("en-US")))}
        ${tile("BRAKE", has(brk) ? (brk ? '<span style="color:var(--red)">ON</span>' : "OFF") : NA, "on / off only")}
        ${video ? "" : `${tile("DRS", drs ? esc(drs) : NA, drsNote)}
        ${tile("ERS", has(v.ers) ? esc(JSON.stringify(v.ers)) : NA, has(v.ers) ? "" : "not in F1 feed")}
        ${tile("OVERTAKE", NA, "not in F1 feed")}`}
      </div>
      ${video && d ? sectorsHTML(d) : ""}
      ${extra ? `<div class="notes">other CarData channels: ${extra}</div>` : ""}
      </div>
      ${a && stale ? `<div class="notes">STALE · last telemetry ${esc(age || "?")} ago${has(v.speed) ? "" : " - not shown"}</div>` : ""}
      ${!a ? `<div class="notes">${avail.car_data ? "No telemetry for this car" : S.mode === "live" && S.session && S.session.live ? "CarData.z not delivered to this connection (" + esc(feedAuthLabel()) + ") and the public archive stream is not readable (yet)" : "No car telemetry received"}</div>` : ""}`;
  }

  // ------------------------------------------------------------------ views
  function renderView() {
    const v = S.ui.view;
    const panel = $("view-panel");
    const big = v === "strategy" || v === "racecontrol" || v === "weather";
    panel.hidden = !big;
    if (!big) return;
    if (v === "strategy") {
      const maxL = Math.max(1, (S.session || {}).total_laps || 0, ...S.order.map((n) => {
        const d = S.drivers[n]; return (d && (d.stints || []).reduce((a, s) => a + (s.laps || 0), 0)) || 0;
      }));
      const sel = selectedNum();
      const rows = S.order.map((n) => {
        const d = S.drivers[n]; if (!d) return "";
        const bars = (d.stints || []).map((s) => {
          const w = Math.max(0.6, ((s.laps || 0) / maxL) * 100);
          return `<div class="sb-${esc(s.compound || "UNK")}" style="width:${w}%">${COMP_LETTER[s.compound] || "?"} ${has(s.laps) ? s.laps : ""}</div>`;
        }).join("");
        return `<div class="strat-row${n === sel ? " selected" : ""}"><div>${has(d.position) ? d.position : "—"}</div>` +
          `<div style="color:${esc(d.team_color || "#fff")}">${esc(d.tla || n)}</div><div class="strat-bars">${bars || '<span class="na">N/A</span>'}</div>` +
          `<div class="r">${has(d.pit_stops) ? d.pit_stops : ""}</div></div>`;
      }).join("");
      panel.innerHTML = `<div class="view-title">TYRE STRATEGY <small>STINT LAPS · 3</small></div>${rows}` +
        `<div class="strat-legend">Bar length = laps driven on each set (from TimingAppData). Right column = pit stops.</div>`;
    } else if (v === "racecontrol") {
      const radio = (S.radio || []).map((r) => {
        const d = S.drivers[r.driver];
        return `<div>${esc(hhmm(r.utc))} · TEAM RADIO · ${esc(d ? d.tla : r.driver || "")}</div>`;
      }).join("");
      panel.innerHTML = `<div class="view-title">RACE CONTROL / FIA <small>NEWEST FIRST · 4</small></div>` +
        `<ul id="rc-full" class="rc-full" style="list-style:none;margin:0;padding:4px 0">${rcItems((S.race_control || []).slice(0, 26))}</ul>` +
        (radio ? `<div class="view-title" style="font-size:17px">TEAM RADIO</div><div class="radio-list">${radio}</div>` : "");
    } else if (v === "weather") {
      const w = S.weather || {}, s = S.session || {}, a = S.availability || {}, tr = S.track || {};
      panel.innerHTML = `<div class="view-title">WEATHER & SESSION <small>5</small></div>
        <div class="wx-grid">
          <div class="wx"><div class="k">AIR</div><div class="v">${na(w.air_temp, (x) => x.toFixed(1) + "<small>°C</small>")}</div></div>
          <div class="wx"><div class="k">TRACK</div><div class="v">${na(w.track_temp, (x) => x.toFixed(1) + "<small>°C</small>")}</div></div>
          <div class="wx"><div class="k">HUMIDITY</div><div class="v">${na(w.humidity, (x) => Math.round(x) + "<small>%</small>")}</div></div>
          <div class="wx"><div class="k">WIND</div><div class="v">${na(w.wind_speed, (x) => x.toFixed(1) + "<small>m/s</small>")}
            ${has(w.wind_direction) ? `<span class="wind-arrow" style="transform:rotate(${w.wind_direction + 180}deg)">↑</span><small>${w.wind_direction}°</small>` : ""}</div></div>
          <div class="wx"><div class="k">PRESSURE</div><div class="v">${na(w.pressure, (x) => x.toFixed(0) + "<small>hPa</small>")}</div></div>
          <div class="wx"><div class="k">RAIN</div><div class="v">${w.rainfall === null || w.rainfall === undefined ? NA : w.rainfall ? '<span style="color:var(--blue)">YES</span>' : "NO"}</div></div>
        </div>
        <div class="info-list">
          <div><b>EVENT</b><span>${na(s.official_name || s.meeting_name)}</span></div>
          <div><b>CIRCUIT</b><span>${na(s.circuit_name)}${s.country ? " · " + esc(s.country) : ""}</span></div>
          <div><b>SESSION</b><span>${na(s.session_name)} · ${na(s.status)} · ${esc(s.state || "UNKNOWN")}${s.red_flag ? " (red flag)" : ""}</span></div>
          <div><b>RACE CONTROL DATA</b><span>coverage ${esc(s.rc_coverage || "NONE")}${S.track_status && S.track_status.source ? " · track status from " + esc(S.track_status.source) : " · track status N/A"}</span></div>
          <div><b>DATA SOURCE</b><span>${esc((S.mode || "").toUpperCase())}${S.mode === "live" ? " · " + esc(feedAuthLabel()) : ""} · ${esc(S.status.detail || S.status.state || "")}${S.status.delay ? ` · delayed ${S.status.delay} s` : ""}</span></div>
          <div><b>CAR POSITIONS</b><span>${a.positions ? "receiving" : NA}</span></div>
          <div><b>CAR TELEMETRY</b><span>${a.car_data ? "receiving" : NA}</span></div>
          <div><b>TRACK GEOMETRY</b><span>${tr.source ? esc(tr.source) : NA}</span></div>
          <div><b>PIT LANE GEOMETRY</b><span>${tr.pitlane_source ? esc(tr.pitlane_source) : NA}</span></div>
        </div>`;
    }
  }

  function renderUI() {
    for (const v of ["overview", "telemetry", "strategy", "racecontrol", "weather"]) stage.classList.remove("view-" + v);
    stage.classList.add("view-" + (S.ui.view || "overview"));
    const tv = S.ui.tv_mode_effective || "FULL_DASHBOARD";
    if (!stage.classList.contains("tv-" + tv)) {
      stage.classList.remove("tv-FULL_DASHBOARD", "tv-RACE_VIEW", "tv-VIDEO_FOCUS");
      stage.classList.add("tv-" + tv);
      renderBoardHead();
    }
    renderRaceInfo();
    $("help").hidden = !S.ui.help;
    renderBoard(); renderBoardTitle(); renderDetail(); renderView();
    requestAnimationFrame(() => { layoutBoard(); Map2D.resize(); });
  }

  function renderHelp() {
    const names = {
      MOVE_UP: "Move up", MOVE_DOWN: "Move down", NEXT_DRIVER: "Next driver", PREVIOUS_DRIVER: "Previous driver",
      OPEN_TELEMETRY: "Select / telemetry", CLOSE_PANEL: "Back to overview", OPEN_RACE_CONTROL: "Race control",
      TOGGLE_HELP: "This help", TOGGLE_AUTO_CYCLE: "Auto-rotate views", SELECT_DRIVER: "Select driver",
      CYCLE_TV_MODE: "TV mode: dashboard / race view / video focus", VIDEO_PLAY_PAUSE: "Video play / pause",
      VIDEO_FOCUS: "Focus video", VIDEO_UNFOCUS: "Back / leave video focus", VIDEO_MUTE: "Video mute",
      VIDEO_FULLSCREEN: "Video fullscreen", VIDEO_SEEK: "Video seek", VIDEO_VOLUME: "Video volume", SET_TV_MODE: "TV mode",
      SYNC_PLUS: "Sync + (more delay)", SYNC_MINUS: "Sync − (less delay)", SYNC_ADJUST: "Sync adjust",
      SYNC_MARK: "Sync MARK: press when the selected car / leader crosses the line on video",
      SYNC_RESYNC: "Force resync", SYNC_DEBUG: "SYNC menu", SYNC_MENU: "SYNC menu",
      SYNC_START: "Sync: lights out / session start now", SYNC_CONFIRM: "Sync: dashboard lap matches the TV",
      SYNC_CLEAR: "Sync: clear", SYNC_PIN: "Sync: pin the shown time",
      SYNC_STREAM_START: "Sync: MARK STREAM START (the video shows the actual start now)",
      SYNC_STREAM_RESET: "Sync: reset the stream start mark",
      SYNC_KEEP_OLD: "Sync drift: keep the old sync", SYNC_USE_NEW: "Sync drift: use the new anchor",
      PITLANE_DEBUG: "Show pit lane reconstruction debug",
      CYCLE_MODE: "Mode: AUTO → LIVE → VOD", SET_MODE: "Mode", MODE_AUTO: "Mode AUTO", MODE_LIVE: "Mode LIVE",
      MODE_VOD: "Mode VOD", WEATHER_REPORT: "Weather report now", TRACK_REPORT: "Track map wrong (press twice)", SIM_EVENT: "TEST: simulated race event",
    };
    const kb = { KEY_UP: "↑", KEY_DOWN: "↓", KEY_LEFT: "←", KEY_RIGHT: "→", KEY_ENTER: "Enter", KEY_ESC: "Esc", KEY_I: "I",
      KEY_SPACE: "Space", KEY_H: "H", KEY_1: "1", KEY_2: "2", KEY_3: "3", KEY_4: "4", KEY_5: "5", KEY_BACK: "Backspace",
      KEY_P: "P", KEY_V: "V", KEY_M: "M", KEY_F: "F", KEY_A: "A", KEY_T: "T",
      KEY_S: "S", KEY_R: "R", KEY_D: "D", KEY_EQUAL: "+", KEY_MINUS: "−",
      KEY_Y: "Y", KEY_L: "L", KEY_C: "C", KEY_X: "X", KEY_K: "K", KEY_O: "O", KEY_N: "N", KEY_G: "G", KEY_E: "E", KEY_U: "U",
      KEY_W: "W" };
    const rows = [];
    for (const [k, cmd] of Object.entries(S.keymap)) {
      if (!kb[k]) continue;
      const [c, arg] = cmd.split(":");
      const label = c === "CHANGE_VIEW" ? (arg === "next" ? "Next view" : arg === "prev" ? "Previous view" : "View: " + arg) : (names[c] || c);
      rows.push(`<div><kbd>${esc(kb[k])}</kbd><span>${esc(label)}</span></div>`);
    }
    rows.push('<div><kbd>Click row</kbd><span>Select driver</span></div>');
    const remoteNames = { KEY_OK: "OK", KEY_BACK: "BACK", KEY_LEFT: "←", KEY_RIGHT: "→", KEY_UP: "↑", KEY_DOWN: "↓", KEY_ENTER: "Enter", KEY_ESC: "Esc" };
    const irNames = { KEY_CHANNELUP: "CH+", KEY_CHANNELDOWN: "CH−", KEY_RED: "RED", KEY_GREEN: "GREEN", KEY_YELLOW: "YELLOW", KEY_BLUE: "BLUE", KEY_MENU: "MENU" };
    const ir = Object.entries(S.keymap).filter(([k]) => irNames[k]).map(([k, cmd]) => {
      const [c, arg] = cmd.split(":");
      return `<div><kbd>${esc(irNames[k])}</kbd><span>${esc((names[c] || c) + (arg ? " " + arg : ""))}</span></div>`;
    });
    const layer = (title, map) => {
      const items = Object.entries(map || {}).filter(([k]) => remoteNames[k]).map(([k, cmd]) => {
        const [c, arg] = cmd.split(":");
        return `<div><kbd>${esc(remoteNames[k])}</kbd><span>${esc((names[c] || c) + (arg ? " " + arg : ""))}</span></div>`;
      });
      return items.length ? `<div class="help-sub">${title}</div>` + items.join("") : "";
    };
    renderPhoneRemote();
    $("help-body").innerHTML = rows.join("") + (ir.length ? '<div class="help-sub">REMOTE (IR)</div>' + ir.join("") : "") +
      layer("WITH VIDEO (RACE VIEW / VIDEO FOCUS)", S.keymapVideo) +
      layer("WHILE THE VIDEO HAS FOCUS", S.keymapVideoFocus);
  }

  // ------------------------------------------------------------------ map info
  // "choose the circuit": the known layouts; the choice is fitted onto the car positions server-side
  function openTrackMenu() {
    let box = document.getElementById("track-menu");
    if (box) { box.remove(); return; }
    box = document.createElement("div");
    box.id = "track-menu";
    box.className = "track-menu";
    box.innerHTML = "<b>CHOOSE CIRCUIT</b><div>loading…</div>";
    $("map-panel").appendChild(box);
    fetch("/api/track/layouts").then((r) => r.json()).then((d) => {
      const opts = ['<option value="auto">Automatic (MultiViewer / detected from the car positions)</option>']
        .concat((d.layouts || []).map((l) => `<option value="${esc(l.id)}"${d.choice === l.id ? " selected" : ""}>` +
          `${esc(l.location || "")} – ${esc(l.name || l.id)}</option>`));
      box.innerHTML = `<b>CHOOSE CIRCUIT</b><small>${esc(d.circuit || "")} · now: ${esc(d.source || "none")}` +
        `${d.info && d.info.ref_id ? " " + esc(d.info.ref_id) : ""}${d.note ? " · " + esc(d.note) : ""}</small>` +
        `<select id="track-select" size="10">${opts.join("")}</select>` +
        `<div class="tm-row"><button id="track-ok">USE</button><button id="track-cancel">CLOSE</button></div>` +
        `<small>The layout is fitted onto the car positions (needs cars all around the lap). The pit lane is not changed.</small>`;
      const sel = document.getElementById("track-select");
      if (!d.choice) sel.value = "auto";
      document.getElementById("track-ok").onclick = () => { send({ type: "track_choice", value: sel.value }); box.remove(); };
      document.getElementById("track-cancel").onclick = () => box.remove();
      sel.focus();
    }).catch(() => { box.innerHTML = "<b>CHOOSE CIRCUIT</b><div>not available</div>"; });
  }
  function renderMapInfo() {
    // Everything here goes to the MESSAGE_RECT under the map (#map-footer) - never over the map.
    const tr = S.track;
    const s = S.session || {};
    $("map-title").textContent = tr ? tr.name : s.circuit_name || "";
    const a = S.availability || {};
    const ts = S.track_status || {};

    // ---- line 1: what matters during the session (flags, safety car, data problems)
    const status = [];
    const sc = (S.map || {}).safety_car || {};
    if (ts.status === "RED") status.push('<span class="st-red">RED FLAG</span>');
    if (["SC", "VSC", "VSC_ENDING"].includes(ts.status)) {
      const label = ts.status === "SC" ? "SAFETY CAR" : ts.status === "VSC" ? "VIRTUAL SAFETY CAR" : "VSC ENDING";
      status.push(`<span class="st-sc">${label}</span>` + (ts.status === "SC"
        ? ` <small>· position ${sc.available ? (sc.fresh ? "on the map" : "STALE") : "not provided by the feed"}</small>` : ""));
    }
    const ys = Object.entries(ts.sector_flags || {});
    if (ys.length) status.push(`<span class="flag-y">${ys.map(([k, f]) => (f === "DOUBLE YELLOW" ? "DY" : "Y") + " MS" + k).join("  ")}</span>`);
    let dataMsg = "";
    if (tr && !a.positions) {
      dataMsg = S.mode === "live" && s.live
        ? (a.token_configured ? "Live car positions: not received yet" : "Live car positions: N/A for anonymous connections")
        : "No car positions received";
    } else if (a.positions && a.positions_source === "archive") {
      dataMsg = `Positions from the public archive stream${has(a.positions_age_s) ? ` · ${Math.round(a.positions_age_s)} s behind` : ""}`;
    }
    if (S.mode === "live" && !s.live && s.status && S.status.next_session && tr) {
      const ns = S.status.next_session, d = utcDate(ns.start_utc);
      dataMsg = `No live session · next: ${esc(ns.meeting || "")} ${esc(ns.session || "")}` +
        (d ? " " + d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "");
    }
    if (dataMsg) status.push(`<small>${dataMsg}</small>`);
    $("map-status").innerHTML = status.join(" · ");

    // ---- line 2: compact map status (details: tooltip, /api/diagnostics, the server log)
    const legend = [];
    if (tr && tr.source !== "test") {
      const inf = tr.info || {};
      const src = { multiviewer: "MultiViewer", reference: (inf.chosen ? "your choice " : "known layout ") + (inf.ref_id || ""),
                    learned: "learned lap" }[tr.source] || tr.source;
      const title = inf.unaligned ? "not aligned to the car positions yet" : (inf.note || inf.fit || inf.check || "");
      legend.push(`<span title="${esc(title)}">Track <span class="ok">✓</span> ${esc(src)}${inf.unaligned ? " (not aligned)" : ""}</span>`);
    } else if (!tr && s.circuit_key) {
      legend.push('Track <span class="bad">✗</span>');
    }
    if (tr && tr.source !== "test") {
      const pi = tr.pitlane_info || {};
      if (tr.pitlane) {
        legend.push(`<span title="${esc((pi.source || "") + " · " + (pi.confidence || "") + " · " + (pi.traversals || "?") + " passes")}">` +
          `Pit lane <span class="ok">✓</span></span>`);
      } else if (pi.state === "loading" || pi.state === "learning") {
        legend.push(`<span title="${esc(pi.note || "")}">Pit lane: loading…</span>`);
      } else {
        legend.push(`<span title="${esc(pi.note || "")}">Pit lane <span class="bad">✗</span> unavailable</span>`);
      }
    }
    if (tr && tr.source !== "test") legend.push('<a href="#" id="track-report" class="track-report" title="Rebuild the outline of this circuit (the pit lane is kept). Key W twice.">MAP WRONG?</a>');
    if (S.mode !== "test" && (tr || s.circuit_key)) {
      legend.push('<a href="#" id="track-choose" class="track-report" title="Override the automatically detected circuit (debug / fallback)">CIRCUIT…</a>');
    }
    $("map-legend").innerHTML = legend.join(" · ");
    const rep = document.getElementById("track-report");
    if (rep) rep.onclick = (e) => { e.preventDefault(); send({ type: "command", command: "TRACK_REPORT" }); };
    const ch = document.getElementById("track-choose");
    if (ch) ch.onclick = (e) => { e.preventDefault(); openTrackMenu(); };

    // ---- only when there is no map at all, a notice may use the map area (nothing to cover)
    const notice = $("map-notice");
    let msg = "";
    if (!tr) {
      msg = s.circuit_key ? "TRACK MAP UNAVAILABLE<small>Circuit geometry not loaded yet (see the status below the map)</small>"
        : S.mode === "live" && S.status.state !== "connected" ? "CONNECTING TO F1 LIVE TIMING…" : "WAITING FOR SESSION";
    }
    notice.innerHTML = msg;
    notice.hidden = !msg;

    // cars in the pit lane now (cars in the garage are marked IN PIT on the leaderboard instead)
    const inPit = S.order.filter((n) => S.drivers[n] && S.drivers[n].in_pit && !S.drivers[n].in_garage).map((n) => S.drivers[n].tla || n);
    const pb = $("pit-box");
    pb.hidden = !inPit.length;
    pb.innerHTML = `<span class="lbl">PIT</span> ${inPit.map(esc).join(" ")}`;
  }

  // ------------------------------------------------------------------ TV modes: compact info + focus bar
  let toastTimer = null;
  function showToast(text, ms) {
    const t = $("toast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms || 2500);
  }
  const TS_LABEL = { GREEN: "GREEN", YELLOW: "YELLOW", SC: "SAFETY CAR", VSC: "VSC", VSC_ENDING: "VSC ENDING", RED: "RED FLAG", CHEQUERED: "CHEQUERED", UNKNOWN: "N/A" };
  function renderRaceInfo() {
    const tv = (S.ui || {}).tv_mode_effective;
    if (tv !== "RACE_VIEW" && tv !== "VIDEO_FOCUS") return;
    const s = S.session || {}, ts = S.track_status || {}, w = S.weather || {};
    const timedS = isTimed(s);
    const lap = timedS ? (s.phase ? esc(s.phase) : NA) :
      has(s.lap) ? `${s.lap}<span class="tot"> / ${has(s.total_laps) ? s.total_laps : "N/A"}</span>` : NA;
    const lapK = timedS ? (s.session_kind === "qualifying" ? "PHASE" : "SESSION") : "LAP";
    const flag = `<span class="ri-flag ts-${esc(ts.status || "UNKNOWN")}">${esc(TS_LABEL[ts.status] || ts.status || "N/A")}</span>`;
    const nY = Object.keys(ts.sector_flags || {}).length;
    const flagSub = ts.sc_phase && /IN THIS LAP|ENDING/.test(ts.sc_phase) ? ts.sc_phase : nY ? `${nY} sector${nY > 1 ? "s" : ""} yellow` : "";
    const video = S.video || {};
    let vline = !S.ui.video_available ? `VIDEO: ${esc(S.ui.video_notice || "off")}` :
      `VIDEO: VOYO ${esc(video.mode || "")}${S.ui.video_focus ? " · FOCUSED" : ""}`;
    const md = (S.sync || {}).media;
    if (md) {
      const sy = S.sync, ok = ["HIGH", "MEDIUM", "MANUAL"].includes(sy.confidence) && sy.synced;
      vline = `MEDIA: ${esc(md.label || "Unknown F1 session")}<br>SYNC: ${md.data !== "loaded" ? "—" : ok ? "SYNCED — " + esc(sy.confidence) : sy.confidence === "LOW" ? "ESTIMATED" : "SYNC REQUIRED"}`;
    }
    if (tv === "RACE_VIEW") {
      $("race-info").innerHTML = `
        <div class="ri-session">${esc(s.meeting_name || "")}</div>
        <div class="ri-name">${esc(s.session_name || "WAITING FOR SESSION")}</div>
        <div class="ri-row"><div><div class="ri-k">${lapK}</div><div class="ri-v">${lap}</div></div>
          <div><div class="ri-k">REMAINING</div><div class="ri-v" id="ri-clock"></div></div></div>
        <div class="ri-flagrow">${flag}<span class="ri-flagsub">${esc(flagSub)}</span>${pitExitHTML()}</div>
        <div class="ri-wx">AIR <b>${na(w.air_temp, (v) => v.toFixed(1) + "°")}</b> · TRACK <b>${na(w.track_temp, (v) => v.toFixed(1) + "°")}</b><br>
          RAIN <b>${w.rainfall === null || w.rainfall === undefined ? NA : w.rainfall ? "YES" : "NO"}</b> · WIND <b>${na(w.wind_speed, (v) => v.toFixed(1) + " m/s")}</b></div>
        <div class="ri-foot"><div class="ri-mode" id="ri-mode"></div><span class="mode-badge mode-${esc(S.mode || "live")}">${S.mode === "test" ? "TEST MODE" : S.mode === "replay" ? "REPLAY" : S.mode === "vod" ? "RECORDING" : "LIVE"}</span>
          ${has(ts.overtake) ? "OVERTAKE " + esc(ts.overtake) : ""}<span id="ri-sync"></span><br><span class="ri-video">${vline}</span></div>`;
    } else {
      const leaderNum = S.order[0], lead = leaderNum ? S.drivers[leaderNum] : null;
      const sel = selectedNum(), d = sel ? S.drivers[sel] : null;
      const t = d ? d.tyre || {} : {};
      $("focus-bar").innerHTML = `
        <div class="fb-item"><span class="fb-k">${lapK}</span><span class="fb-v">${lap}</span></div>
        <div class="fb-item">${flag}<span class="ri-flagsub">${esc(flagSub)}</span></div>
        <div class="fb-item"><span class="fb-k">LEADER</span><span class="fb-v">${lead ? `<i class="fb-bar" style="background:${esc(lead.team_color || "#555")}"></i>${esc(lead.tla || leaderNum)}` : NA}</span></div>
        <div class="fb-item fb-sel"><span class="fb-k">SELECTED</span><span class="fb-v">${d ? `<i class="fb-bar" style="background:${esc(d.team_color || "#555")}"></i>P${esc(d.position ?? "—")} ${esc(d.tla || sel)} <small>${esc(gapText(d, s.session_kind) || "")}</small> ${tyreDot(t.compound)} ${has(t.tyre_age) ? t.tyre_age : ""}` : NA}</span></div>
        <div class="fb-item"><span class="fb-k">REMAINING</span><span class="fb-v" id="fb-clock"></span></div>
        <div class="fb-item fb-mode"><span id="fb-sync"></span><span id="fb-modesel"></span><span class="mode-badge mode-${esc(S.mode || "live")}">${S.mode === "test" ? "TEST" : S.mode === "replay" ? "REPLAY" : S.mode === "vod" ? "REC" : "LIVE"}</span></div>`;
    }
    renderClock();
    renderSync();
    renderModeSel();
  }

  // ------------------------------------------------------------------ VOYO <-> F1 sync: chip + SYNC menu
  const CONF_TEXT = {
    HIGH: "Several independent anchors agree.", MEDIUM: "One good anchor, not verified independently.",
    LOW: "Estimated - not an exact synchronisation.", MANUAL: "Set or adjusted by you.",
    UNSYNCED: "Not enough data for a reliable synchronisation.", LIVE: "Live, no delay.",
  };
  const ISO3 = { JPN: "JP", AUS: "AU", CHN: "CN", BRN: "BH", KSA: "SA", SAU: "SA", USA: "US", CAN: "CA", MON: "MC", MCO: "MC",
    ESP: "ES", AUT: "AT", GBR: "GB", BEL: "BE", HUN: "HU", NED: "NL", NLD: "NL", ITA: "IT", AZE: "AZ", SGP: "SG", MEX: "MX",
    BRA: "BR", QAT: "QA", UAE: "AE", ARE: "AE", MYS: "MY", MAL: "MY" };
  function flagEmoji(code3) {
    const c = ISO3[(code3 || "").toUpperCase()];
    return c ? String.fromCodePoint(...[...c].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65)) : "";
  }
  const syncOk = (sy) => sy && ["HIGH", "MEDIUM", "MANUAL"].includes(sy.confidence) && sy.synced;
  function localTime(iso, withSec) {
    if (!iso) return null;
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: withSec ? "2-digit" : undefined, hour12: false });
  }
  function dataRow(sy) {
    // what the server has loaded for this session and whether the shown moment is inside it
    const a = +sy.dataSpan[0], b = +sy.dataSpan[1];
    if (!isFinite(a) || !isFinite(b)) return NA;
    const span = `${F1Time.fmtIn(a, "slo").slice(0, 5)}–${F1Time.fmtIn(b, "slo").slice(0, 5)} Slovenia`;
    const t = sy.dataShownMs;
    if (!sy.synced || !has(t) || !isFinite(t)) return `${esc(span)} <small>not synchronised yet</small>`;
    const where = t < a - 60000 ? `<span class="sm-warn">shown time is ${Math.round((a - t) / 60000)} min BEFORE the data starts</span>` :
      t > b + 60000 ? `<span class="sm-warn">shown time is ${Math.round((t - b) / 60000)} min AFTER the data ends</span>` : "shown time is inside";
    const drv = `${sy.dataDrivers || 0} drivers · ${sy.dataTimingLines || 0} timing lines`;
    return `${esc(span)} <small>${where} · ${esc(drv)}</small>`;
  }
  function fmtVid(s) {
    if (!has(s)) return NA;
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(x).padStart(2, "0");
  }
  function syncChip(sy) {
    if (!sy || !sy.enabled) return "";
    if (sy.mode === "LIVE" && !sy.voyo && !sy.vod) return "";                // plain live dashboard stays clean
    const lost = (sy.flags || []).includes("VOYO_CLOCK_LOST");
    const st = sy.voyo && sy.mode === "VOYO" ? sy.voyo.state : null;
    const icon = st === "PAUSED" ? "❚❚ " : st === "BUFFERING" || st === "SEEKING" ? "… " : lost ? "! " : "";
    let val;
    if (sy.drift) val = "DRIFT?";
    else if (sy.vod && sy.media && ["ambiguous", "failed"].includes(sy.media.state) && sy.media.data !== "loaded") val = "SESSION?";
    else if (sy.vod && sy.confidence === "UNSYNCED" && sy.media && (sy.media.data === "loaded" || sy.media.data === "waiting_sync")) val = "SYNC REQUIRED";
    else if (sy.confidence === "UNSYNCED") val = "NOT SYNCED";
    else if (sy.confidence === "LOW") val = "ESTIMATED";
    else if (sy.confidence === "MANUAL") val = "MANUAL";
    else if (!sy.vod && has(sy.tv_delay)) val = sy.tv_delay.toFixed(2) + "s";
    else val = sy.healthError ? sy.healthError.replace(" (anchors disagree)", "").replace(" s", "s") : sy.confidence;
    return `<button class="sync-chip conf-${esc(sy.confidence)}${lost ? " lost" : ""}" data-sync-open title="${esc(sy.confidence)}: ${esc(CONF_TEXT[sy.confidence] || "")}">` +
      `${val === "NOT SYNCED" || val === "SYNC REQUIRED" || val === "SESSION?" ? "" : "SYNC "}${icon}${esc(val)} <i class="sync-dot"></i></button>`;
  }

  // ---- menu (skeleton built once so typing is never interrupted by the 2 Hz updates)
  let syncMethod = null;
  let syncPhase = null;                 // Q1 / Q2 / Q3 / FP2 chosen for the session clock
  let syncCapture = null;               // {video_time, paused} of the moment a typed value refers to
  let syncMsg = null;
  let syncShowAnchors = false;
  let syncShowSelect = false;
  let catalog = null;
  async function loadCatalog(year) {
    $("sm-sel-hint").textContent = "Loading the season from OpenF1…";
    try {
      const r = await fetch("/api/media/catalog?year=" + year);
      catalog = await r.json();
    } catch (e) { catalog = { meetings: [], error: "server not reachable" }; }
    const gp = $("sm-gp");
    gp.innerHTML = `<option value="">Grand Prix…</option>` + (catalog.meetings || []).map((m, i) =>
      `<option value="${i}">${esc(m.name)} (${esc(m.date)})</option>`).join("");
    $("sm-sess").innerHTML = `<option value="">Session…</option>`;
    $("sm-sel-hint").textContent = catalog.error ? "Not available: " + catalog.error :
      `${(catalog.meetings || []).length} Grands Prix with finished sessions (${catalog.source || ""})`;
  }
  function openSelector() {
    syncShowSelect = true;
    const ys = $("sm-year"), now = new Date().getUTCFullYear();
    if (!ys.options.length) {
      for (let y = now; y >= 2023; y--) ys.insertAdjacentHTML("beforeend", `<option>${y}</option>`);
      ys.addEventListener("change", () => loadCatalog(ys.value));
      $("sm-gp").addEventListener("change", () => {
        const m = (catalog && catalog.meetings || [])[+$("sm-gp").value];
        $("sm-sess").innerHTML = `<option value="">Session…</option>` + (m ? m.sessions.map((x) =>
          `<option value="${esc(x.session_key)}">${esc(x.session_name)}</option>`).join("") : "");
      });
    }
    const md = (S.sync || {}).media || {};
    if (md.year) ys.value = md.year;
    loadCatalog(ys.value);
    renderSyncMenu();
  }
  function syncAction(action, value) {
    const field = { countdown: "countdown", exact: "f1_time", estimate: "lead_seconds", clock: "clock", marker: "marker",
      event_set: "id", event_remove: "id" }[action];
    send({ type: "sync_action", action, value: field ? value : undefined });
  }
  function buildSyncMenu() {
    const el = $("sync-menu");
    if (el.dataset.built) return;
    el.dataset.built = "1";
    el.innerHTML = `
      <div class="sm-head"><span>SYNC VIDEO</span><button class="sm-x" data-sync-close title="Close (Y / Esc)">×</button></div>
      <div class="sm-media" id="sm-media"></div>
      <div class="sm-select" id="sm-select" hidden>
        <div class="sm-sub">SELECT SESSION</div>
        <div class="sm-row"><select id="sm-year"></select><select id="sm-gp"><option value="">Grand Prix…</option></select>
          <select id="sm-sess"><option value="">Session…</option></select></div>
        <div class="sm-row"><button class="pri" data-select-use>Use this session</button><button data-select-cancel>Cancel</button></div>
        <div class="sm-hint" id="sm-sel-hint"></div>
      </div>
      <div class="sm-info" id="sm-info"></div>
      <div class="sm-events" id="sm-events" hidden></div>
      <div class="sm-q">How do you want to sync?</div>
      <div class="sm-methods">
        <button data-m="clock">Session Clock<small>time remaining / elapsed</small></button>
        <button data-m="marker">Phase Marker<small>SYNC HERE · most precise</small></button>
        <button data-m="stream">Mark Stream Start<small>VOYO at 0:00 · origin of the stream</small></button>
        <button data-ev-open>Event Sync<small>real F1 events ↔ VOYO · several points</small></button>
        <button data-m="countdown">VOYO Countdown<small>recommended</small></button>
        <button data-m="exact">Manual Exact Time<small>time shown in the video</small></button>
        <button data-m="auto">Restore Saved Sync<small>automatic · only if reliable</small></button>
        <button data-m="estimate">Session Start Estimate<small>marked Estimated</small></button>
      </div>
      <div class="sm-panel" id="sm-p-clock" hidden>
        <p>Pause VOYO on a frame that shows the <b>session clock</b> (or read it the moment you press <b>Capture</b>). Which phase is it, and what does the clock show?</p>
        <div class="sm-cap" id="sm-cap-clock"></div>
        <div class="sm-row sm-phases" id="sm-clock-phases"></div>
        <div class="sm-row"><select id="sm-clock-mode"><option value="remaining">Time remaining</option><option value="elapsed">Time elapsed</option></select>
          <input id="sm-in-clock" placeholder="07:32" autocomplete="off">
          <button data-cap>Capture</button><button class="pri" data-apply="clock">Apply</button></div>
        <div class="sm-hint" id="sm-clock-hint"></div>
      </div>
      <div class="sm-panel" id="sm-p-marker" hidden>
        <p>Pause VOYO <b>exactly</b> on the moment (e.g. the session clock turning to 0:00) and press <b>SYNC HERE</b> - the dashboard uses the official F1 timing of that moment.</p>
        <div class="sm-markers" id="sm-markers"></div>
      </div>
      <div class="sm-panel" id="sm-p-stream" hidden>
        <p>Position VOYO at the <b>absolute beginning of the stream (0:00)</b>, then press <b>MARK STREAM START</b>.
          This is the origin of this VOYO broadcast - <b>not</b> the race start, lights out or the schedule. Its F1
          time comes from an F1 reference of this video - best: press <b>LIGHTS OUT (L)</b> when the video shows it -
          and is saved for this session + video (reopening the VOD restores it). LIVE: estimated from the moment
          0:00 airs until lights out confirms it.</p>
        <div class="sm-row"><button class="pri" data-apply="stream_start">MARK STREAM START</button>
          <button data-lights>LIGHTS OUT (L)</button>
          <button data-apply="stream_reset">RESET STREAM START</button></div>
        <div class="sm-hint" id="sm-stream-hint"></div>
      </div>
      <div class="sm-panel" id="sm-p-countdown" hidden>
        <p>Pause VOYO on a frame that shows the countdown to the start of <b id="sm-sess-cd">this session</b> (or read it the moment you press <b>Capture</b>). How long is it until the start?</p>
        <div class="sm-cap" id="sm-cap-countdown"></div>
        <div class="sm-row"><label class="sm-lab">Counts to</label><select id="sm-cd-target">
          <option value="auto">Auto (scheduled start; asks if the start was delayed)</option>
          <option value="scheduled">Scheduled start</option>
          <option value="announced">Announced new start (race control)</option>
          <option value="actual">Actual start (lights out)</option></select></div>
        <div class="sm-row"><input id="sm-in-countdown" placeholder="23:47  ·  00:23:47  ·  23m 47s" autocomplete="off">
          <button data-cap>Capture</button><button class="pri" data-apply="countdown">Apply</button></div>
      </div>
      <div class="sm-panel" id="sm-p-exact" hidden>
        <p>Which time does the video show at the captured moment? (e.g. a clock in the broadcast)</p>
        <div class="sm-cap" id="sm-cap-exact"></div>
        <div class="sm-row"><input id="sm-in-exact" placeholder="14:48:32" autocomplete="off">
          <select id="sm-tz"><option value="slo">Slovenia time</option><option value="track">Track time</option></select>
          <select id="sm-fmt"><option value="24">24-hour</option><option value="12">12-hour</option></select>
          <select id="sm-ampm" hidden><option value="am">AM</option><option value="pm">PM</option></select></div>
        <div class="sm-hint" id="sm-exact-preview"></div>
        <div class="sm-row"><button data-cap>Capture</button><button class="pri" data-apply="exact">Apply</button></div>
      </div>
      <div class="sm-panel" id="sm-p-auto" hidden>
        <p>Uses only what is reliable without your input (a sync saved for this video and session). If that is not enough it says so and changes nothing.</p>
        <div class="sm-row"><button class="pri" data-apply="auto">Run automatic sync</button></div>
      </div>
      <div class="sm-panel" id="sm-p-estimate" hidden>
        <p>How long does the VOYO video run before the official session start? This only gives an <b>estimate</b> (confidence LOW) - real starts differ from the schedule.</p>
        <div class="sm-row"><input id="sm-in-estimate" placeholder="e.g. 28:50" autocomplete="off">
          <button class="pri" data-apply="estimate">Apply estimate</button><button data-apply="estimate-remove">Remove</button></div>
        <div class="sm-hint" id="sm-est-hint"></div>
      </div>
      <div class="sm-msg" id="sm-msg" hidden></div>
      <div class="sm-drift" id="sm-drift" hidden></div>
      <div class="sm-result" id="sm-result"></div>
      <div class="sm-foot">
        <button data-view-anchors>View anchors</button>
        <button data-apply="resync" title="Re-sync from the current anchors (drops manual adjustments)">Re-sync</button>
        <button data-apply="clear">Clear</button>
        <button data-adj="-0.25">−0.25 s</button><button data-adj="0.25">+0.25 s</button>
      </div>
      <div class="sm-anchors" id="sm-anchors" hidden></div>
      <div class="sm-keys" id="sm-keys"></div>
      <details class="sm-details"><summary>Details</summary><div id="sm-details"></div></details>`;
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.syncClose !== undefined) { send({ type: "command", command: "SYNC_MENU", arg: "close" }); return; }
      if (b.dataset.m) {
        syncMethod = b.dataset.m; syncMsg = null;
        if (syncMethod === "countdown" || syncMethod === "exact" || syncMethod === "clock") syncAction("capture");
        if (syncMethod === "estimate") {
          const sy = S.sync || {};
          const v = has(sy.estimateLead) ? sy.estimateLead : sy.learnedLead;
          if (has(v) && !$("sm-in-estimate").value) $("sm-in-estimate").value = fmtVid(v);
        }
        renderSyncMenu();
        const inp = $("sm-in-" + syncMethod); if (inp) inp.focus();
        return;
      }
      if (b.dataset.cap !== undefined) { syncAction("capture"); return; }
      if (b.dataset.lights !== undefined) { send({ type: "command", command: "SYNC_START" }); return; }
      // EVENT SYNC sub-menu (server state: the TV remote's UP / DOWN / OK / BACK drive it too)
      if (b.dataset.evOpen !== undefined) { send({ type: "command", command: "SYNC_EVENT_MENU", arg: "open" }); return; }
      if (b.dataset.evBack !== undefined) { send({ type: "command", command: "SYNC_EVENT_MENU", arg: "close" }); return; }
      if (b.dataset.evSet) { syncAction("event_set", b.dataset.evSet); return; }
      if (b.dataset.evRm) { syncAction("event_remove", b.dataset.evRm); return; }
      if (b.dataset.evClear !== undefined) { syncAction("event_clear"); return; }
      if (b.dataset.viewAnchors !== undefined) { syncShowAnchors = !syncShowAnchors; renderSyncMenu(); return; }
      if (b.dataset.selectOpen !== undefined) { openSelector(); return; }
      if (b.dataset.selectCancel !== undefined) { syncShowSelect = false; renderSyncMenu(); return; }
      if (b.dataset.selectUse !== undefined) {
        const k = $("sm-sess").value;
        if (!k) { $("sm-sel-hint").textContent = "Choose a Grand Prix and a session."; return; }
        send({ type: "sync_action", action: "select_session", value: k });
        syncShowSelect = false; renderSyncMenu(); return;
      }
      if (b.dataset.pick) { send({ type: "sync_action", action: "select_session", value: b.dataset.pick }); return; }
      if (b.dataset.adj) { send({ type: "command", command: "SYNC_ADJUST", arg: b.dataset.adj }); return; }
      if (b.dataset.phase) { syncPhase = b.dataset.phase; renderSyncMenu(); return; }
      if (b.dataset.marker) { syncAction("marker", b.dataset.marker); return; }
      const a = b.dataset.apply;
      if (!a) return;
      if (a === "countdown") {
        const tgt = $("sm-cd-target").value;
        syncAction("countdown", $("sm-in-countdown").value.trim() + (tgt && tgt !== "auto" ? "|" + tgt : ""));
      }
      else if (a === "stream_start" || a === "stream_reset") syncAction(a);
      else if (a === "clock") {
        const v = $("sm-in-clock").value.trim();
        if (parseDur(v) === null) { syncMsg = { ok: false, text: "Enter the clock as MM:SS (e.g. 07:32) or H:MM:SS." }; renderSyncMenu(); return; }
        if (!syncPhase) { syncMsg = { ok: false, text: "Choose the phase first." }; renderSyncMenu(); return; }
        syncAction("clock", `${syncPhase}|${$("sm-clock-mode").value}|${v}`);
      }
      else if (a === "exact") {
        const r = exactInput();
        if (r.error) { syncMsg = { ok: false, text: r.error }; renderSyncMenu(); return; }
        syncAction("exact", r.iso);
      } else if (a === "auto") syncAction("auto");
      else if (a === "estimate") {
        const secs = parseDur($("sm-in-estimate").value);
        if (secs === null) { syncMsg = { ok: false, text: "Enter the lead as MM:SS or 28m 50s." }; renderSyncMenu(); return; }
        syncAction("estimate", secs);
      } else if (a === "estimate-remove") syncAction("estimate", "");
      else if (a === "clear") syncAction("clear");
      else if (a === "resync" || a === "keep_old" || a === "use_new") syncAction(a);
    });
    $("sm-clock-mode").addEventListener("change", renderSyncMenu);
    $("sm-in-clock").addEventListener("input", clockHint);
    for (const id of ["sm-in-exact", "sm-tz", "sm-fmt", "sm-ampm"]) {
      $(id).addEventListener(id === "sm-in-exact" ? "input" : "change", exactPreview);
    }
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.id && e.target.id.startsWith("sm-in-")) {
        const b = el.querySelector(`[data-apply="${e.target.id.slice(6)}"]`); if (b) b.click();
      }
    });
  }
  function renderClockPanel(sy, tl) {
    const phases = ((tl && tl.phases) || []).filter((p) => p.clock);
    const box = $("sm-clock-phases");
    if (!phases.length) {
      box.innerHTML = "";
      $("sm-clock-hint").textContent = "The session clock is not known yet (the session timing loads with the session details).";
      return;
    }
    const cur = (tl.current && tl.current.phase) || (S.session || {}).phase;
    if (!syncPhase || !phases.some((p) => p.id === syncPhase)) syncPhase = phases.some((p) => p.id === cur) ? cur : phases[0].id;
    const html = phases.map((p) => `<button data-phase="${esc(p.id)}" class="${p.id === syncPhase ? "on" : ""}">${esc(p.label)}` +
      `<small>${has(p.duration_ms) ? fmtClock(p.duration_ms) : "length ?"}</small></button>`).join("");
    if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
    clockHint();
  }
  function clockHint() {
    const sy = S.sync || {}, tl = sy.sessionTimeline;
    const p = ((tl && tl.phases) || []).find((x) => x.id === syncPhase);
    const el = $("sm-clock-hint");
    if (!p) { el.textContent = ""; return; }
    const mode = $("sm-clock-mode").value;
    const v = parseDur($("sm-in-clock").value);
    const dur = p.duration_ms;
    let t = `${p.label}: ${has(dur) ? "official length " + fmtClock(dur) + " (F1 timing clock)" : "length not in the data - use Time remaining"}`;
    if (v !== null && has(dur)) {
      const other = dur - v * 1000;
      t += other > 0 && other < dur ? ` · ${fmtClock(v * 1000)} ${mode} = ${fmtClock(other)} ${mode === "remaining" ? "elapsed" : "remaining"}` :
        ` · ${fmtClock(v * 1000)} is outside ${p.label}`;
    }
    el.textContent = t;
    el.classList.toggle("bad", mode === "elapsed" && !has(dur));
  }
  function renderMarkerPanel(sy, tl) {
    const ms = ((tl && tl.markers) || []).filter((m) => m.sync);
    const html = ms.length ? ms.map((m) => `<div class="sm-mk k-${esc(m.kind)}"><div><b>${esc(m.label)}</b>` +
      `<small>${esc(m.how)} · ${esc(F1Time.fmtIn(m.ms, "slo"))} Slovenia · ${esc(new Date(m.ms).toISOString().slice(11, 23))} UTC</small></div>` +
      `<button class="pri" data-marker="${esc(m.id)}">SYNC HERE</button></div>`).join("")
      : `<div class="sm-hint">No phase markers known for this session yet.</div>`;
    const box = $("sm-markers");
    if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
  }
  function parseDur(t) {
    t = (t || "").trim().toLowerCase();
    let m = t.match(/^(?:(\d{1,2}):)?(\d{1,3}):(\d{1,2})$/);
    if (m) return (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]);
    m = t.match(/^(?:(\d{1,2})\s*h)?\s*(?:(\d{1,3})\s*m(?:in)?)?\s*(?:(\d{1,2})\s*s)?$/);
    if (m && (m[1] || m[2] || m[3])) return (+(m[1] || 0)) * 3600 + (+(m[2] || 0)) * 60 + (+(m[3] || 0));
    return null;
  }
  function exactInput() {
    const sy = S.sync || {};
    if (!sy.sessionStart) {
      const md = sy.media || {};
      return { error: md.session_key ? "Reading the session details… (a moment)" :
        "Which F1 session is this video? Not known yet - press SELECT SESSION above." };
    }
    return F1Time.exactTime($("sm-in-exact").value, $("sm-tz").value, $("sm-fmt").value, $("sm-ampm").value,
      sy.sessionStart, sy.gmtOffset);
  }
  function exactPreview() {
    $("sm-ampm").hidden = $("sm-fmt").value !== "12";
    const el = $("sm-exact-preview");
    if (!$("sm-in-exact").value.trim()) { el.textContent = ""; return; }
    const r = exactInput();
    el.textContent = r.error || (r.preview + (r.warn ? "  ·  " + r.warn : ""));
    el.classList.toggle("bad", !!(r.error || r.warn));
  }
  // scheduled vs ACTUAL start and the two delays - never mixed: the F1 START DELAY is how late the
  // event started; the STREAM DELAY is how far the video is behind the F1 events
  const START_STATE = { PRE_START: "PRE-START", DELAYED: "DELAYED START", STARTED: "STARTED", RUNNING: "RUNNING", UNKNOWN: "—" };
  function fmtSigned(v) { return (v >= 0 ? "+" : "−") + fmtVid(Math.abs(v)); }
  function startRows(sy, row) {
    const si = sy.startInfo;
    if (!si) return "";
    const hm = (u) => u ? esc(u.slice(0, 8)) + " UTC" : "—";
    let st = `<b class="${si.state === "DELAYED" ? "sm-warn" : ""}">${esc(START_STATE[si.state] || si.state)}</b>`;
    if (si.state === "DELAYED") st += ` <small>${si.notice ? esc(si.notice) : has(si.waitingSeconds) ? "scheduled start passed " + fmtVid(si.waitingSeconds) + " ago" : ""} · scheduled start NOT used as the race-time anchor</small>`;
    let out = row("F1 start", st) +
      row("Scheduled → actual", `${hm(si.scheduledUtc)} → ${si.actualUtc ? hm(si.actualUtc) : "<small>waiting for the actual start</small>"}` +
        (si.announcedUtc && !si.actualUtc ? ` <small>announced ${hm(si.announcedUtc)}</small>` : ""));
    if (has(si.f1DelaySeconds)) out += row("F1 start delay", `${fmtSigned(si.f1DelaySeconds)} <small>actual start − scheduled start (the event)</small>`);
    if (has(sy.streamDelaySeconds)) out += row("Stream delay", `${fmtSigned(sy.streamDelaySeconds)} <small>video behind the F1 events (the broadcast)</small>`);
    else if (has(si.actualStartVideo)) out += row("Actual start in video", `${fmtVid(si.actualStartVideo)} <small>recording position of lights out / session start</small>`);
    // LIGHTS OUT: the start event in the F1 data (time + topic) and the last L result - or why not
    const lo = sy.lightsOut, ll = lo && lo.last;
    if (ll && ll.found) {
      out += row("Lights out ✓", `F1 EVENT ${hm(ll.f1Utc)}` + (ll.voyoUtc ? ` · VOYO ${hm(ll.voyoUtc)}` :
        has(ll.videoTime) ? ` · VIDEO ${fmtVid(ll.videoTime)}` : "") +
        (has(ll.streamDelaySeconds) ? ` · STREAM DELAY ${fmtSigned(ll.streamDelaySeconds)}` : "") +
        ` <small>${esc(ll.source || "")}${ll.grounded === false ? " · start not confirmed" : ""}</small>`);
    } else if (ll && !ll.found) {
      out += row("Lights out", `<span class="sm-warn">NOT FOUND</span> <small>${esc(ll.reason || "")}</small>`);
    } else if (lo && lo.available) {
      out += row("Lights out", `${hm(lo.f1Utc)} <small>${esc(lo.source || "")}${lo.approx ? " (±1 s)" : ""} · press L when the video shows it</small>`);
    } else if (lo) {
      out += row("Lights out", `<span class="sm-warn">not available</span> <small>${esc(lo.reason || "")}</small>`);
    }
    const ss = sy.streamStart;
    if (ss) {
      out += row("Stream start", `0:00 · ANCHOR SET` + (ss.originUtc ? ` · = ${hm(ss.originUtc)} <small>${esc(ss.method || "")}${ss.restored ? " · saved" : ""}</small>` :
        ` <span class="sm-warn">no F1 time yet</span> <small>${esc(ss.reason || "")}</small>`));
      if (has(ss.actualAtVideo) || has(ss.scheduledAtVideo))
        out += row("Offset from 0:00", (has(ss.actualAtVideo) ? `actual start at ${fmtSigned(ss.actualAtVideo)}` : "") +
          (has(ss.scheduledAtVideo) ? ` <small>scheduled start at ${fmtSigned(ss.scheduledAtVideo)}</small>` : ""));
    }
    return out;
  }
  function streamHint(sy) {
    if (!sy) return "";
    const ss = sy.streamStart;
    if (!ss) return "STREAM START: not set.";
    return `STREAM START: 0:00 · ANCHOR: SET` + (ss.originUtc ?
      ` · OFFSET: ${has(ss.actualAtVideo) ? "actual start at " + fmtSigned(ss.actualAtVideo) : "0:00 = " + esc(ss.originUtc.slice(0, 8)) + " UTC"} · CONFIDENCE: ${esc(ss.confidence || "—")}` :
      ` · ${esc(ss.reason || "")}`);
  }
  // EVENT SYNC sub-menu: the real timestamped F1 events of this session and the points set on them
  function fmtVidMs(v) { return has(v) ? fmtVid(v) + "." + String(Math.round((v % 1) * 1000) % 1000).padStart(3, "0") : NA; }
  function renderEventSync(sy) {
    const el = $("sm-events"), ev = (sy && sy.eventSync) || {};
    const evs = ev.events || [], pts = (ev.points || []).filter((p) => p.state !== "replaced");
    const sgn2 = (v) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2) + " s";
    const cur = !sy.vod && has(sy.streamDelaySeconds) ? `${fmtSigned(sy.streamDelaySeconds)} <small>video behind the F1 events</small>` :
      ev.current ? `VOYO 0:00 = ${esc(ev.current.videoZeroUtc)} UTC` : `<span class="sm-warn">not synchronised</span>`;
    const mark = { valid: "✓", outlier: "⚠", unconfirmed: "?" };
    el.innerHTML = `
      <div class="sm-sub">EVENT SYNC</div>
      <p class="sm-hint">Select an F1 event, move VOYO to that exact moment (pause on it), then <b>SET</b>. Repeat with more
        events - they are combined. <b>↑ / ↓</b> select · <b>OK / Enter</b> SET · <b>BACK / Backspace</b> back.</p>
      <div class="sm-rrow"><span>Current offset</span><b>${cur}</b></div>
      <div class="sm-rrow"><span>Confidence</span><b><span class="sd-conf conf-${esc(sy.confidence)}">${esc(sy.confidence)}</span>
        ${sy.healthError ? ` <small>${esc(sy.healthError)}</small>` : ""}</b></div>
      <div class="sm-sub">KNOWN EVENTS</div>
      <div class="sm-markers se-list" id="se-list">${evs.length ? evs.map((e) => `
        <div class="sm-mk se-ev${e.id === ev.sel ? " sel" : ""}" data-ev="${esc(e.id)}"><div><b>${esc(e.label)}</b>
          <small>${esc((e.utc || "").slice(0, 12))} UTC${e.precise ? "" : " · ±1 s"} · ${esc(e.source || "")}${e.confidence ?
            ` · confidence ${esc(e.confidence)}` : ""}${e.conflict ? ` · <span class="sm-warn">sources disagree: ${esc(e.conflict)}</span>` : ""}</small></div>
          <button data-ev-set="${esc(e.id)}">SET</button></div>`).join("") :
        `<div class="sm-hint">${esc(ev.reason || "No timestamped F1 events for this session yet.")}</div>`}</div>
      ${sy.sessionKind === "race" && ev.lightsOut && !has(ev.lightsOut.timestamp_ms) ? `<div class="sm-mk"><div><b>LIGHTS OUT</b>
        <small><span class="sm-warn">Unavailable</span> · ${esc(ev.lightsOut.reason || "")}${(sy.lightsOut || {}).fallback &&
          sy.lightsOut.fallback.state !== "off" ? ` · public F1 SignalR fallback: ${esc(sy.lightsOut.fallback.state)}` : ""}</small></div></div>` : ""}
      ${ev.hiddenIncidents ? `<div class="sm-hint">${ev.hiddenIncidents} later race-control event(s) (safety car, flags …) are listed once the synchronised video reaches them - no spoilers.</div>` : ""}
      <div class="sm-sub">SYNC POINTS</div>
      ${pts.length ? pts.map((p) => `<div class="sm-an st-${esc(p.state)}"><em>${mark[p.state] || esc(p.state)}${p.state === "outlier" ? " OUTLIER" : ""}</em>
        <span>${esc(p.label)}${p.precise === false ? " <small>±1 s</small>" : ""}</span>
        <b><small>F1</small> ${esc((p.f1Utc || "").slice(0, 12))} <small>VOYO</small> ${fmtVidMs(p.videoTime)}</b>
        <i>${has(p.residual) ? sgn2(p.residual) : ""} <button class="se-rm" data-ev-rm="${esc(p.id)}" title="Remove this point">✕</button></i></div>`).join("") :
        `<div class="sm-hint">No sync points yet.</div>`}
      <div class="sm-row"><button data-ev-clear>CLEAR EVENT SYNC POINTS</button><button class="pri" data-ev-back>BACK</button></div>`;
    const sel = el.querySelector(".se-ev.sel");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }
  function renderSyncMenu() {
    const sy = S.sync;
    const el = $("sync-menu");
    const show = !!(S.ui && S.ui.sync_menu);
    el.hidden = !show;
    if (!show) {
      if (document.activeElement && el.contains(document.activeElement)) document.activeElement.blur();
      return;
    }
    buildSyncMenu();
    const evMode = !!(S.ui && S.ui.sync_events);
    $("sync-menu").classList.toggle("ev-mode", evMode);
    $("sm-events").hidden = !evMode;
    if (evMode && sy) renderEventSync(sy);
    const v = sy && sy.voyo;
    const sess = sy && sy.sessionName ? `${flagEmoji(sy.countryCode)} ${esc(sy.meetingName || "")} — ${esc(sy.sessionName)}` :
      `<span class="sm-warn">not identified</span>${sy && sy.detection ? ` <small>${esc(sy.detection)}</small>` : ""}`;
    // ---- MEDIA: which session is this video (auto media sync) - separate from the time sync below
    const md = sy && sy.media;
    $("sm-select").hidden = !syncShowSelect;
    if (md) {
      const lab = md.label ? `${flagEmoji(md.country_code)} ${esc(md.label)}${md.year ? " " + esc(md.year) : ""}` :
        md.session_key && sy.sessionName ? `${flagEmoji(sy.countryCode)} ${esc(sy.meetingName || "")} — ${esc(sy.sessionName)}` +
          `${sy.sessionStart ? " " + esc(String(sy.sessionStart).slice(0, 4)) : ""}` : "Unknown F1 session";
      const head = { waiting: "WAITING FOR A VOYO VIDEO", detecting: "AUTO MEDIA SYNC…", detected: "AUTO MEDIA SYNC",
        manual: "MANUAL SESSION", ambiguous: "AUTO MEDIA SYNC FAILED", failed: "AUTO MEDIA SYNC FAILED" }[md.state] || md.state;
      const known = md.data === "loaded" || md.data === "waiting_sync";
      const dataLine = md.data === "loaded" ? "OpenF1 session loaded" : md.data === "waiting_sync" ? "data loads after SYNC" :
        md.data === "loading" ? (md.data_reason || "loading the session data…") :
        md.data === "error" ? "session data could not be loaded: " + (md.data_reason || "") : "";
      const detLine = md.state === "detected" ? `Session detected automatically (${md.how || md.reason}).` :
        md.state === "manual" ? `Manual session selected (${md.reason}).` :
        md.state === "ambiguous" || md.state === "failed" ? `Not detected: ${md.reason}.` : md.reason || "";
      const synced = sy && ["HIGH", "MEDIUM", "MANUAL"].includes(sy.confidence) && sy.synced;
      $("sm-media").innerHTML = `
        <div class="sm-mrow"><span>MEDIA</span><b>${lab}</b></div>
        <div class="sm-mstate st-${esc(md.state)}">${esc(head)}${dataLine ? ` · <small>${esc(dataLine)}</small>` : ""}</div>
        <div class="sm-hint">${esc(detLine)}</div>
        ${(md.candidates || []).length && (md.state === "ambiguous" || md.state === "failed") ? `<div class="sm-cands">` +
          md.candidates.map((c) => `<button data-pick="${esc(c.session_key)}">${esc(c.meeting || "")} — ${esc(c.session_name)} <small>${esc(c.date)}</small></button>`).join("") + `</div>` : ""}
        <div class="sm-row"><button data-select-open>SELECT SESSION</button></div>
        <div class="sm-mrow"><span>SYNC</span><b>${known || md.data === "loading" ? (synced ? `SYNCED — ${esc(sy.confidence)}` :
          sy && sy.confidence === "LOW" ? "ESTIMATED — not exact" : "SYNC REQUIRED") : "—"}</b></div>
        ${known && !synced ? `<div class="sm-hint">Set the video time with one of the methods below - the session data is loaded after that.</div>` : ""}
        ${sy.dataSpan ? `<div class="sm-mrow"><span>DATA</span><b>${dataRow(sy)}</b></div>` : ""}`;
    } else $("sm-media").innerHTML = "";
    $("sm-info").innerHTML = `
      ${md ? "" : `<div><span>Current session</span><b>${sess}</b></div>`}
      <div><span>Current video position</span><b>${v ? fmtVid(v.playback) + ` <small>${esc(v.state)}</small>` : `${NA} <small>no VOYO clock</small>`}</b></div>
      ${v && v.page_title ? `<div><span>VOYO title</span><b><small>${esc(v.page_title)}</small></b></div>` : ""}`;
    // session-aware methods: qualifying / practice use the session clock and the phase markers;
    // the countdown to the start and the start estimate only make sense for a race
    const kind = (sy && sy.sessionKind) || (S.session || {}).session_kind || "unknown";
    const tl = sy && sy.sessionTimeline;
    const timedK = kind === "qualifying" || kind === "practice";
    const offer = timedK ? ["clock", "marker", "stream", "exact", "auto"] :
      ["stream", "countdown", "exact", "auto", "estimate"].concat(tl && (tl.phases || []).length ? ["clock", "marker"] : []);
    if (syncMethod && !offer.includes(syncMethod)) syncMethod = null;
    for (const m of ["clock", "marker", "stream", "countdown", "exact", "auto", "estimate"]) {
      $("sm-p-" + m).hidden = syncMethod !== m;
      const b = $("sync-menu").querySelector(`[data-m="${m}"]`);
      if (b) { b.classList.toggle("on", syncMethod === m); b.hidden = !offer.includes(m); b.style.order = offer.indexOf(m); }
    }
    renderClockPanel(sy, tl);
    renderMarkerPanel(sy, tl);
    $("sm-keys").innerHTML = "Keys: " + (timedK ? "" : "<b>L</b> lights out / session start · ") +
      "<b>S</b> selected car crosses the line · <b>C</b> lap matches TV · <b>O</b> / <b>N</b> keep old / use new after a drift warning · <b>Y</b> menu";
    const sname = sy && sy.sessionName ? sy.sessionName : "this session";
    $("sm-sess-cd").textContent = sname;
    for (const id of ["sm-cap-countdown", "sm-cap-exact", "sm-cap-clock"]) {
      $(id).innerHTML = syncCapture ? `Captured video position <b>${fmtVid(syncCapture.video_time)}</b>${syncCapture.paused ? " (paused)" : " - the value you enter must be what VOYO showed at that moment"}` : "Capturing…";
    }
    $("sm-stream-hint").innerHTML = streamHint(sy);
    $("sm-est-hint").innerHTML = sy && has(sy.learnedLead) ? `Learned from ${sy.learnedLeadCount} earlier sync(s): ${fmtVid(sy.learnedLead)} (not applied automatically).` : "No learned value yet.";
    const msg = $("sm-msg");
    msg.hidden = !syncMsg;
    if (syncMsg) { msg.textContent = syncMsg.text; msg.className = "sm-msg " + (syncMsg.ok ? "ok" : "bad"); }
    // ---- SYNC STATUS: health (measured / stated error), method, anchor, offset, confidence, reason
    if (!sy) { $("sm-result").innerHTML = ""; return; }
    const ok = syncOk(sy);
    const row = (k, val) => `<div class="sm-rrow"><span>${k}</span><b>${val}</b></div>`;
    const sgn = (v, d) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d === undefined ? 2 : d);
    const f1ms = ok ? new Date(sy.absoluteTime).getTime() : null;
    const f1 = ok ? `${F1Time.fmtIn(f1ms, "slo")} <small>Slovenia · ${esc(F1Time.fmtIn(f1ms, "track", sy.gmtOffset))} track · ${esc(sy.absoluteTime.slice(11, 23))} UTC</small>`
      : sy.confidence === "LOW" && sy.approxTime ? `≈ ${esc(sy.approxTime)} <small>estimate - not exact</small>` : NA;
    let offset = NA;
    if (has(sy.offsetDisplay) && sy.confidence !== "LOW") {
      offset = sy.vod || sy.mode === "VOYO"
        ? `${sgn(sy.offsetDisplay)} s <small>session start at video ${fmtVid(sy.offsetDisplay)}${sy.videoZeroUtc ? " · video 0:00 = " + esc(sy.videoZeroUtc.slice(0, 8)) + " UTC" : ""}</small>`
        : `${sgn(sy.offsetDisplay)} s <small>delay</small>`;
    } else if (sy.confidence === "LOW" && has(sy.offsetDisplay)) {
      offset = `≈ ${fmtVid(sy.offsetDisplay)} <small>session start in the video (estimate)</small>`;
    }
    const hl = sy.health || "UNSYNCED";
    const errLabel = sy.errorMeasured ? "Error" : "Estimated error";
    const counts = sy.anchorsValid || sy.anchorsOutliers
      ? `${sy.anchorsValid} valid${sy.anchorsOutliers ? ` · ${sy.anchorsOutliers} outlier${sy.anchorsOutliers > 1 ? "s" : ""}` : ""}` +
        (sy.anchorsIndependent ? ` <small>${sy.anchorsIndependent} independent</small>` : "") : "none";
    $("sm-result").innerHTML = `
      ${sy.modeNote ? `<div class="sm-warnbox">RECORDING IN LIVE MODE<br><span>${esc(sy.modeNote)}</span></div>` : ""}
      <div class="sm-sub">SYNC STATUS</div>
      <div class="sm-health"><span class="sm-status st-${esc(hl)}">${esc(hl === "LIVE" ? "LIVE" : hl)}</span>
        <span class="sm-herr">${sy.healthError ? `<small>${errLabel}</small> ${esc(sy.healthError)}` : `<small>${hl === "UNSYNCED" ? "no time can be determined" : "error cannot be measured"}</small>`}</span></div>
      ${row("F1 time", f1)}
      ${row("Confidence", `<span class="sd-conf conf-${esc(sy.confidence)}">${esc(sy.confidence)}</span> <small>${esc(CONF_TEXT[sy.confidence] || "")}</small>`)}
      ${row("Method", esc(sy.method || "—"))}
      ${sy.anchor ? row("Anchor", esc(sy.anchor)) : ""}
      ${row("Offset", offset)}
      ${startRows(sy, row)}
      ${row("Anchors", counts)}
      <div class="sm-reason">${esc(sy.reason || "")}</div>
      ${sy.deviationNote ? `<div class="sm-warnbox">MINOR DEVIATION<br><span>${esc(sy.deviationNote)}</span></div>` : ""}
      ${sy.note ? `<div class="sd-note">${esc(sy.note)}</div>` : ""}`;
    // ---- drift warning: a new anchor that differs > drift_warning_seconds is never applied silently
    const dr = sy.drift, dEl = $("sm-drift");
    dEl.hidden = !dr;
    if (dr) {
      const moveTxt = has(dr.shift) ? `the dashboard would jump ${Math.abs(dr.shift).toFixed(2)} s ${dr.shift > 0 ? "forward" : "back"} in F1 time` : "";
      dEl.innerHTML = `<div class="sm-dhead">POSSIBLE SYNC DRIFT</div>
        ${has(dr.current) ? row("Current", sgn(dr.current) + " s") : ""}
        ${has(dr.new) ? row("New", sgn(dr.new) + " s") : ""}
        ${row("Drift", has(dr.current) && has(dr.new) ? sgn(dr.new - dr.current) + " s" : sgn(-dr.shift) + " s")}
        <p>The new anchor (${esc(dr.anchors.map((x) => x.label).join(", "))}) differs significantly from the current
        synchronization - ${moveTxt}. It is not used until you decide.${dr.agree > 1 ? ` ${dr.agree} new anchors agree with each other.` : ""}</p>
        <div class="sm-row"><button data-apply="keep_old">Keep old (O)</button><button class="pri" data-apply="use_new">Use new (N)</button></div>`;
    }
    // ---- anchors list (text markers only)
    const an = sy.anchors || [], hist = sy.history || [];
    const aEl = $("sm-anchors");
    aEl.hidden = !syncShowAnchors;
    const mark = { valid: "OK", outlier: "OUTLIER", unconfirmed: "UNCONFIRMED", rejected: "REJECTED", replaced: "OLD" };
    const line = (x) => `<div class="sm-an st-${esc(x.state)}"><em>${esc(mark[x.state] || x.state)}</em><span>${esc(x.label)}${x.driver ? " #" + esc(x.driver) : ""}</span>` +
      `<b>${has(x.offset) ? sgn(x.offset) + " s" : esc(x.text)}</b><i>${x.state === "outlier" ? "outlier" : has(x.residual) && x.state === "valid" ? sgn(-x.residual) : ""}${x.restored ? " saved" : ""}</i></div>`;
    aEl.innerHTML = `<div class="sm-sub">SYNC ANCHORS</div>` + (an.length ? an.map(line).join("") : `<div class="sm-hint">No anchors.</div>`) +
      (has(sy.offsetDisplay) && an.length ? `<div class="sm-rrow"><span>Calculated offset</span><b>${sgn(sy.offsetDisplay)} s <small>median of the valid anchors</small></b></div>` : "") +
      (hist.length ? `<div class="sm-sub">HISTORY</div>` + hist.map(line).join("") : "");
    $("sync-menu").querySelector("[data-view-anchors]").textContent = syncShowAnchors ? "Hide anchors" : `View anchors (${an.length})`;
    const flags = (sy.flags || []).map((f) => `<span class="sd-flag">${esc(f.replace(/_/g, " "))}</span>`).join(" ");
    $("sm-details").innerHTML =
      row("Mode", `${esc(sy.mode_cfg)} → ${esc(sy.mode)}${sy.vod ? " · VOD" : ""}`) +
      row("Source", esc(sy.source || "—")) +
      row("Reference events", `${esc(sy.ref_events || 0)} <small>${esc(sy.ref_source || "—")}</small>`) +
      row("Race Control coverage", `${esc((S.session || {}).rc_coverage || "NONE")} <small>race control · track status · session status</small>`) +
      (v ? row("VOYO", `${fmtVid(v.playback)} <small>${esc(v.state)} ×${esc(v.rate)} · rs ${esc(v.ready_state)} · age ${esc(v.age_s)} s</small>`) : "") +
      (has(sy.tv_delay) ? row("TV delay", `${sy.tv_delay.toFixed(2)} s`) : "") +
      (has(sy.receive_latency) ? row("F1 receive latency", `${sy.receive_latency.toFixed(2)} s`) : "") +
      (has(sy.buffer) ? row("Buffer", `${sy.buffer.toFixed(1)} s`) : "") +
      (v && v.meta && Object.keys(v.meta).length ? row("VOYO meta", `<small>${Object.entries(v.meta).map(([k, x]) => esc(k) + " " + esc(x)).join(" · ")} (not used for sync)</small>`) : "") +
      (flags ? `<div class="sd-flags">${flags}</div>` : "");
  }
  function renderSync() {
    const sy = S.sync;
    const chip = syncChip(sy);
    for (const id of ["sync-chip", "ri-sync", "fb-sync"]) { const e = document.getElementById(id); if (e) e.innerHTML = chip; }
    // clear warning on the dashboard itself when a recording is not (exactly) synchronised
    const ban = $("sync-banner");
    const md = sy && sy.media;
    const mediaBad = md && ["ambiguous", "failed", "waiting", "detecting"].includes(md.state) && md.data !== "loaded";
    const outside = sy && sy.dataNote;
    const dataBad = md && (md.data === "loading" || md.data === "error");
    const warn = sy && ((sy.vod && (sy.confidence === "UNSYNCED" || sy.confidence === "LOW" || sy.mode === "HOLD" || mediaBad || outside || dataBad)) || sy.drift || sy.modeNote);
    ban.hidden = !warn || !!(S.ui && S.ui.sync_menu);
    bannerSpace("has-sbanner", !ban.hidden);
    if (warn) {
      let what, why, btn = "SYNC";
      const syncedNow = ["HIGH", "MEDIUM", "MANUAL"].includes(sy.confidence) && sy.synced;
      if (sy.modeNote) { what = "RECORDING IN LIVE MODE"; why = sy.modeNote; }
      else if (dataBad && !sy.drift) {
        what = md.data === "error" ? "SESSION DATA NOT LOADED" : "DOWNLOADING SESSION DATA";
        why = (md.data_reason || "") + (md.data === "loading" && syncedNow ? " · sync is set - the data appears when the download finishes" : "");
      } else if (outside && !sy.drift) { what = "NO SESSION DATA AT THIS TIME"; why = sy.dataNote; }
      else if (sy.drift) { what = "POSSIBLE SYNC DRIFT"; why = `A new anchor differs by ${Math.abs(sy.drift.shift).toFixed(2)} s - not applied. O = keep old, N = use new.`; }
      else if (mediaBad) {
        what = md.state === "ambiguous" || md.state === "failed" ? "AUTO MEDIA SYNC FAILED" : md.state === "waiting" ? "WAITING FOR VOYO" : "AUTO MEDIA SYNC…";
        why = md.state === "ambiguous" || md.state === "failed" ? `Unknown F1 session: ${md.reason}` : md.reason || "";
        if (md.state === "ambiguous" || md.state === "failed") btn = "SELECT SESSION";
      } else if (sy.mode === "HOLD") { what = "VIDEO CLOCK LOST"; why = sy.reason || ""; }
      else if (sy.confidence === "LOW") { what = "ESTIMATED TIME - NOT EXACT"; why = sy.reason || ""; }
      else if (md && (md.data === "loaded" || md.data === "waiting_sync")) {
        what = "SYNC REQUIRED";
        why = `${md.label || ((sy.meetingName || "") + " — " + (sy.sessionName || ""))} ${md.state === "manual" ? "selected" : "detected"} · ` +
          `set the video time in SYNC (Y) - the session data is shown only after that.`;
      } else if (md && md.data === "loading") { what = "LOADING SESSION"; why = md.data_reason || md.label || ""; }
      else { what = "NOT SYNCHRONISED"; why = sy.reason || ""; }
      ban.innerHTML = `<b>${what}</b><span>${esc(why)}</span><button data-sync-open${btn !== "SYNC" ? " data-open-select" : ""}>${btn}</button>`;
    }
    stage.classList.toggle("unsynced", !!(sy && sy.vod && sy.confidence === "UNSYNCED"));
    renderSyncMenu();
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-sync-open]");
    if (!b) return;
    send({ type: "command", command: "SYNC_MENU", arg: "open" });
    if (b.dataset.openSelect !== undefined) setTimeout(openSelector, 300);
  });
  function selectedTla() {
    const n = selectedNum() || S.order[0];
    const d = n ? S.drivers[n] : null;
    return d ? d.tla || n : "the leader";
  }

  function renderAll(m) {
    renderTop();
    if (!m || m.full || "session" in m || "timeline" in m) renderBoardHead();
    if (!m || m.full || "order" in m) layoutBoard();
    renderBoard();
    if (!m || m.full || "race_control" in m) renderRC();
    renderDetail();
    renderView();
    renderMapInfo();
    Map2D.setFlags(S.track_status || {});
    renderRaceInfo();
  }

  // ------------------------------------------------------------------ map renderer
  const Map2D = (() => {
    const canvas = $("map");
    const ctx = canvas.getContext("2d");
    let track = null, pts = [], pit = null, bounds = null, base = null;
    let pitOffs = null, pitDraw = null;       // screen offsets of the pit lane (readability) + drawn points
    let pitDebug = null;                      // reconstruction details (G = debug overlay)
    let pitNow = [], pitLast = [];            // cars drawn on the pit lane (this / last frame)
    let W = 0, H = 0, dpr = 1, sx = 1, ox = 0, oy = 0;
    let flags = {};
    const buf = new Map();          // num -> [{t,x,y}]
    let offset = null;
    let clock = null;               // sync presentation clock {t, rate, at}; null = live (lag based)
    let lastDraw = 0;

    function rot(p, deg) {
      const a = (deg * Math.PI) / 180;
      const c = Math.cos(a), s = Math.sin(a);
      return [p[0] * c + p[1] * s, -p[0] * s + p[1] * c];
    }
    function toScreen(p) {
      const r = rot(p, track ? track.rotation || 0 : 0);
      return [ox + (r[0] - bounds.x0) * sx, oy + (bounds.y1 - r[1]) * sx];
    }
    function setTrack(t) {
      track = t;
      pts = t ? t.points : [];
      pit = t && t.pitlane ? t.pitlane : null;
      computeBounds(); drawBase();
    }
    function computeBounds() {
      if (!track || !pts.length) { bounds = null; return; }
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const p of pts.concat(pit || [])) {
        const r = rot(p, track.rotation || 0);
        x0 = Math.min(x0, r[0]); x1 = Math.max(x1, r[0]); y0 = Math.min(y0, r[1]); y1 = Math.max(y1, r[1]);
      }
      bounds = { x0, x1, y0, y1 };
      const padX = 50, padTop = 46, padBot = 40;
      sx = Math.min((W - 2 * padX) / (x1 - x0 || 1), (H - padTop - padBot) / (y1 - y0 || 1));
      ox = (W - (x1 - x0) * sx) / 2;
      oy = padTop + (H - padTop - padBot - (y1 - y0) * sx) / 2;
    }
    function resize() {
      const rect = canvas.parentElement;
      const w = rect.clientWidth, h = rect.clientHeight;
      if (!w || !h) return;
      dpr = (window.devicePixelRatio || 1) * (S.scale || 1);
      W = w; H = h;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      computeBounds(); drawBase();
    }
    function path(c, list, from, to) {
      // polyline from index `from` to `to` (wrapping) - whole track when from === to === undefined
      const n = list.length;
      if (!n) return;
      c.beginPath();
      if (from === undefined) {
        list.forEach((p, i) => { const s = toScreen(p); i ? c.lineTo(s[0], s[1]) : c.moveTo(s[0], s[1]); });
        return;
      }
      let i = from, first = true, guard = 0;
      while (guard++ <= n) {
        const s = toScreen(list[i]);
        first ? c.moveTo(s[0], s[1]) : c.lineTo(s[0], s[1]);
        first = false;
        if (i === to) break;
        i = (i + 1) % n;
      }
    }
    function drawBase() {
      base = document.createElement("canvas");
      base.width = canvas.width; base.height = canvas.height;
      const c = base.getContext("2d");
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (!bounds) return;
      c.lineJoin = c.lineCap = "round";
      // pit lane first: a smaller road that branches off the track and rejoins it (drawn under it)
      pitOffs = pitDraw = null;
      if (pit && pit.length > 3) {
        const pitS = pit.map(toScreen);
        pitOffs = window.PitLane ? PitLane.separation(pitS, pts.map(toScreen), 20, 17, 48) : pitS.map(() => [0, 0]);
        pitDraw = pitS.map((q, i) => [q[0] + pitOffs[i][0], q[1] + pitOffs[i][1]]);
        const road = (w, col) => {
          c.beginPath(); pitDraw.forEach((q, i) => (i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])));
          c.strokeStyle = col; c.lineWidth = w; c.stroke();
        };
        road(11, "#161c24"); road(5, "#6b7888");
        const info = track.pitlane_info || {};
        const tick = (frac, label) => {
          if (frac === null || frac === undefined) return;
          const k = Math.max(1, Math.min(pitDraw.length - 2, Math.round(frac * (pitDraw.length - 1))));
          const a = pitDraw[k - 1], b = pitDraw[k + 1], m = pitDraw[k];
          const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1, nx = -(b[1] - a[1]) / L, ny = (b[0] - a[0]) / L;
          c.strokeStyle = "#dfe6ee"; c.lineWidth = 2;
          c.beginPath(); c.moveTo(m[0] - nx * 5, m[1] - ny * 5); c.lineTo(m[0] + nx * 5, m[1] + ny * 5); c.stroke();
        };
        tick(info.pit_in_frac, "IN"); tick(info.pit_out_frac, "OUT");
        const k = Math.floor(pitDraw.length / 2), mid = pitDraw[k];
        const off = pitOffs[k], ol = Math.hypot(off[0], off[1]);
        const a = pitDraw[Math.max(0, k - 2)], b = pitDraw[Math.min(pitDraw.length - 1, k + 2)];
        const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
        let nx = -(b[1] - a[1]) / L, ny = (b[0] - a[0]) / L;
        if (ol > 0.5 && nx * off[0] + ny * off[1] < 0) { nx = -nx; ny = -ny; }     // label outside, away from the track
        c.fillStyle = info.status === "provisional" ? "#6f7b88" : "#8795a3";
        c.font = "800 13px " + getComputedStyle(document.body).fontFamily;
        c.textAlign = "center"; c.textBaseline = "middle";
        c.fillText(info.status === "provisional" ? "PIT?" : "PIT", mid[0] + nx * 20, mid[1] + ny * 20);
      }
      path(c, pts); c.strokeStyle = "#1a212b"; c.lineWidth = 17; c.stroke();
      path(c, pts); c.strokeStyle = "#4a5666"; c.lineWidth = 9; c.stroke();
      // start / finish line
      if (pts.length > 2) {
        const a = toScreen(pts[0]), b = toScreen(pts[2]);
        const ang = Math.atan2(b[1] - a[1], b[0] - a[0]) + Math.PI / 2;
        c.strokeStyle = "#ffffff"; c.lineWidth = 4;
        c.beginPath(); c.moveTo(a[0] + Math.cos(ang) * 12, a[1] + Math.sin(ang) * 12);
        c.lineTo(a[0] - Math.cos(ang) * 12, a[1] - Math.sin(ang) * 12); c.stroke();
      }
      // corner numbers
      c.fillStyle = "#6f7d8c"; c.font = "700 13px " + getComputedStyle(document.body).fontFamily;
      c.textAlign = "center"; c.textBaseline = "middle";
      for (const cn of track.corners || []) {
        const s = toScreen([cn.x, cn.y]);
        const a = ((cn.angle || 0) - (track.rotation || 0)) * Math.PI / 180;
        c.fillText(cn.n, s[0] + Math.cos(a) * 22, s[1] - Math.sin(a) * 22);
      }
    }
    function setFlags(f) { flags = f || {}; }
    function setPitDebug(d) { pitDebug = d; }
    function probe(x, y, inPit) {
      // how a car at raw feed position (x, y) is drawn: screen point, pit lane or track, distances (px)
      const s = toScreen([x, y]);
      let k = -1;
      if (pitOffs && window.PitLane) { k = PitLane.carOnPit([x, y], pit, pts, !!inPit, 150); if (k >= 0) { s[0] += pitOffs[k][0]; s[1] += pitOffs[k][1]; } }
      const dist = (poly, closed) => (poly && poly.length > 1 ? PitLane.nearest(s, poly, closed).d : null);
      return { s, onPit: k >= 0, dPit: dist(pitDraw, false), dTrack: dist(pts.map(toScreen), true) };
    }
    function drawPitDebug() {
      // raw positions of every pass, the reconstructed and the cached centerline, entry/exit, confidence
      const d = pitDebug || {};
      const font = getComputedStyle(document.body).fontFamily;
      const line = (list, col, w, dash) => {
        if (!list || list.length < 2) return;
        ctx.beginPath(); list.forEach((q, i) => { const s2 = toScreen(q); i ? ctx.lineTo(s2[0], s2[1]) : ctx.moveTo(s2[0], s2[1]); });
        ctx.strokeStyle = col; ctx.lineWidth = w; ctx.setLineDash(dash || []); ctx.stroke(); ctx.setLineDash([]);
      };
      for (const ps of d.passes || []) {
        ctx.fillStyle = ps.accepted ? "rgba(61,200,255,.75)" : "rgba(255,70,70,.8)";
        for (const q of ps.raw || []) { const s2 = toScreen(q); ctx.fillRect(s2[0] - 1.5, s2[1] - 1.5, 3, 3); }
      }
      line(d.cached, "rgba(255,80,220,.9)", 2, [6, 4]);
      line(d.centerline, "#ffd21f", 2);
      const cl = d.centerline || d.cached;
      if (cl && cl.length > 1) {
        for (const [q, col] of [[cl[0], "#2bd576"], [cl[cl.length - 1], "#ff4d5e"]]) {
          const s2 = toScreen(q); ctx.beginPath(); ctx.arc(s2[0], s2[1], 6, 0, Math.PI * 2);
          ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.stroke();
        }
        for (const f of [d.pit_in_frac, d.pit_out_frac]) {
          if (f === null || f === undefined) continue;
          const s2 = toScreen(cl[Math.round(f * (cl.length - 1))]);
          ctx.fillStyle = "#ffffff"; ctx.fillRect(s2[0] - 3, s2[1] - 3, 6, 6);
        }
      }
      const acc = (d.passes || []).filter((x) => x.accepted).length, rej = (d.passes || []).length - acc;
      const c = d.cache;
      const xy = (q) => (q ? `(${Math.round(q[0])}, ${Math.round(q[1])})` : "–");
      const inPitFlag = Object.entries(S.drivers || {}).filter(([, v]) => v.in_pit).map(([n, v]) => v.tla || n);
      const rows = [`PIT LANE DEBUG · ${d.circuit_name || "circuit"} (${d.circuit ?? "?"}) · season ${d.season ?? "?"}`,
        c ? `cache: ${c.status} ${c.confidence} · variant ${c.id} · seasons ${(c.seasons || []).join(",") || "–"} · ${c.traversals} passes`
          : "cache: none for this circuit / season",
        `now: ${(d.state || "none").toUpperCase()} · ${d.confidence || "no confidence"} · passes ${d.used || acc} used · ${rej} rejected` +
          `${d.spread_m !== null && d.spread_m !== undefined ? ` · spread ${d.spread_m} m` : ""}`,
        d.note || "",
        `entry ${xy(d.entry)} · exit ${xy(d.exit)} · pit lines at ${d.pit_in_frac ?? "–"} / ${d.pit_out_frac ?? "–"}`,
        `drawn in pit lane: ${pitLast.join(", ") || "–"} · timing InPit: ${inPitFlag.join(", ") || "–"}`];
      for (const ps of (d.passes || []).filter((x) => !x.accepted).slice(0, 3)) rows.push(`✗ #${ps.num}: ${ps.reason}`);
      for (const ps of (d.passes || []).filter((x) => x.accepted && (x.notes || []).length).slice(0, 2)) rows.push(`✓ #${ps.num}: ${ps.notes.join("; ")}`);
      rows.push("cyan = used · red = rejected · yellow = new · magenta = cached · green/red ring = entry/exit");
      ctx.font = `700 12px ${font}`; ctx.textAlign = "left"; ctx.textBaseline = "top";
      const wBox = Math.min(W - 16, Math.max(...rows.map((r) => ctx.measureText(r).width)) + 16);
      const top = 38;                                   // under the map title, clear of the legend
      ctx.fillStyle = "rgba(5,7,10,.85)"; ctx.fillRect(8, top, wBox, rows.length * 16 + 8);
      ctx.fillStyle = "#e6ebf0";
      rows.forEach((r, i) => ctx.fillText(r, 16, top + 4 + i * 16));
    }

    function setClock(m) {
      clock = m.mode === "LIVE" || !has(m.t) ? null : { t: m.t, rate: m.rate || 0, at: performance.now() };
    }
    function reset() { buf.clear(); offset = null; }
    function addSamples(t, cars) {
      const now = Date.now();
      const lag = now - t;
      if (offset === null || Math.abs(lag - offset) > 4000) offset = lag;
      else if (clock && lag < offset) offset = lag;          // synced: track the freshest sample
      else offset = offset * 0.97 + lag * 0.03;
      for (const [num, x, y] of cars) {
        let b = buf.get(num);
        if (!b) { b = []; buf.set(num, b); }
        if (b.length && t <= b[b.length - 1].t) continue;
        b.push({ t, x, y });
        if (b.length > 60) b.splice(0, b.length - 60);
      }
    }
    function interp(b, rt) {
      if (!b || !b.length) return null;
      if (rt <= b[0].t) return b[0];
      const last = b[b.length - 1];
      if (rt >= last.t) return rt - last.t > 15000 ? null : { x: last.x, y: last.y, stale: rt - last.t > 5000 };  // hold, never extrapolate
      let lo = 0, hi = b.length - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (b[mid].t <= rt) lo = mid; else hi = mid; }
      const a = b[lo], c = b[hi];
      if (c.t - a.t > 5000) return { x: a.x, y: a.y, stale: rt - a.t > 5000 };   // gap in data: no fake motion
      const k = (rt - a.t) / (c.t - a.t);
      return { x: a.x + (c.x - a.x) * k, y: a.y + (c.y - a.y) * k };
    }
    function pulse(t) {
      if (S.cfg.animations !== "full") return 1;
      const p = S.cfg.pulse_period_ms || 2400;
      return 0.55 + 0.45 * (0.5 + 0.5 * Math.sin((t / p) * Math.PI * 2));
    }
    function frame(ts) {
      requestAnimationFrame(frame);
      if (ts - lastDraw < 1000 / (S.cfg.map_fps || 30) - 2) return;
      lastDraw = ts;
      if (!W || stage.classList.contains("view-strategy") || stage.classList.contains("view-racecontrol") || stage.classList.contains("view-weather")) return;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (base) ctx.drawImage(base, 0, 0);
      if (!bounds) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.lineJoin = ctx.lineCap = "round";
      const st = flags.status;
      const pv = pulse(ts);
      if (st === "SC" || st === "VSC" || st === "VSC_ENDING") {
        path(ctx, pts); ctx.strokeStyle = `rgba(255,176,0,${0.35 + 0.6 * pv})`; ctx.lineWidth = 9; ctx.stroke();
      } else if (st === "RED") {
        path(ctx, pts); ctx.strokeStyle = `rgba(255,45,61,${0.35 + 0.6 * pv})`; ctx.lineWidth = 9; ctx.stroke();
      }
      // local yellow flags: only the affected marshal sectors
      const ms = (track && track.marshal_sectors) || [];
      for (const [k, f] of Object.entries(flags.sector_flags || {})) {
        const sec = ms.find((m) => String(m.n) === String(k));
        if (!sec) continue;
        path(ctx, pts, sec.start, sec.end);
        ctx.strokeStyle = f === "DOUBLE YELLOW" ? "#ffae00" : "#ffd21f";
        ctx.lineWidth = f === "DOUBLE YELLOW" ? 13 : 10;
        ctx.globalAlpha = S.cfg.animations === "full" ? 0.7 + 0.3 * pv : 1;
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
      // cars
      let rt;
      if (clock) {
        // synced to the video: draw exactly at the sync clock (the server streams samples ahead of it);
        // if samples arrive late (e.g. archive stream) fall back to the lag-based buffer
        const ct = clock.t + (performance.now() - clock.at) * clock.rate;
        rt = offset === null ? ct : Math.min(ct, Date.now() - offset - (S.cfg.interp_delay_ms || 1200));
      } else {
        if (offset === null) return;
        // archive data arrives in bursts every few seconds -> larger buffer so cars keep moving smoothly
        const archive = (S.availability || {}).positions_source === "archive";
        rt = Date.now() - offset - Math.max(S.cfg.interp_delay_ms || 1200, archive ? 7000 : 0);
      }
      const sel = selectedNum();
      const font = getComputedStyle(document.body).fontFamily;
      const order = S.order.slice().reverse();      // leader drawn last (on top)
      const drawn = new Set();
      // a layout you chose that is not aligned to the car positions yet: no cars on it (they would be misplaced)
      if (track && track.info && track.info.unaligned) return;
      const drawCar = (num) => {
        const d = S.drivers[num] || {};
        // in the garage (a long stay in the pit, or retired there): not on the map
        if (d.in_garage) { drawn.add(num); return; }
        const p = interp(buf.get(num), rt);
        if (!p) return;
        drawn.add(num);
        const s = toScreen([p.x, p.y]);
        if (pitOffs && window.PitLane) {
          // in the pit lane: drawn on the (readability-shifted) pit lane, never on the main straight
          const k = PitLane.carOnPit([p.x, p.y], pit, pts, !!d.in_pit, 150);
          if (k >= 0) { s[0] += pitOffs[k][0]; s[1] += pitOffs[k][1]; pitNow.push(d.tla || num); }
        }
        const isSel = num === sel;
        const r = isSel ? 13 : 11;
        // no position for > 5 s: drawn faded where it was last seen (never moved on a guess)
        ctx.globalAlpha = d.retired || d.stopped || p.stale ? 0.4 : 1;
        if (isSel) { ctx.beginPath(); ctx.arc(s[0], s[1], r + 6, 0, Math.PI * 2); ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 3; ctx.stroke(); }
        ctx.beginPath(); ctx.arc(s[0], s[1], r, 0, Math.PI * 2);
        ctx.fillStyle = d.team_color || "#9aa4ae"; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = "#05070a"; ctx.stroke();
        ctx.fillStyle = lum(d.team_color) > 0.6 ? "#05070a" : "#ffffff";
        ctx.font = `900 ${isSel ? 13 : 12}px ${font}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(num, s[0], s[1] + 0.5);
        const label = d.tla || num;
        ctx.font = `900 ${isSel ? 19 : 16}px ${font}`; ctx.textAlign = "left";
        ctx.lineWidth = 4; ctx.strokeStyle = "rgba(5,7,10,.9)";
        ctx.strokeText(label, s[0] + r + 4, s[1] - 1);
        ctx.fillStyle = isSel ? "#ffffff" : "#e6ebf0";
        ctx.fillText(label, s[0] + r + 4, s[1] - 1);
        ctx.globalAlpha = 1;
      };
      if (S.ui.pit_debug) drawPitDebug();
      pitLast = pitNow; pitNow = [];
      for (const num of order) if (num !== sel) drawCar(num);
      // objects in Position.z that are not on the driver list are never drawn as cars; the safety car
      // only when the server identified it (a configured key that is actually in the feed)
      const mi = S.map || {}, others = new Set(mi.non_driver_objects || []);
      const scKey = mi.safety_car && mi.safety_car.available ? String(mi.safety_car.key) : null;
      for (const num of buf.keys()) if (!drawn.has(num) && num !== sel && !S.drivers[num] && !others.has(num) && num !== scKey) drawCar(num);
      if (sel) drawCar(sel);
      if (scKey) {
        const p = interp(buf.get(scKey), rt);
        if (p) {
          const s = toScreen([p.x, p.y]);
          ctx.globalAlpha = p.stale ? 0.4 : 1;
          if (!p.stale && !stage.classList.contains("anim-off")) {      // soft halo on the REAL safety car only
            ctx.beginPath(); ctx.arc(s[0], s[1], 16 + 8 * pv, 0, Math.PI * 2);
            ctx.strokeStyle = `rgba(255,176,0,${0.55 * (1 - pv)})`; ctx.lineWidth = 3; ctx.stroke();
          }
          ctx.fillStyle = "#ffb000"; ctx.strokeStyle = "#05070a"; ctx.lineWidth = 2;
          ctx.fillRect(s[0] - 13, s[1] - 9, 26, 18); ctx.strokeRect(s[0] - 13, s[1] - 9, 26, 18);
          ctx.fillStyle = "#05070a"; ctx.font = `900 12px ${font}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText("SC", s[0], s[1] + 0.5);
          ctx.globalAlpha = 1;
        }
      }
    }
    function lum(hex) {
      if (!hex || hex.length < 7) return 0.5;
      const r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    }
    requestAnimationFrame(frame);
    return { setTrack, resize, addSamples, setFlags, setClock, reset, setPitDebug, probe };
  })();
  window.F1Map = Map2D;           // for the browser tests (inject samples); no effect on normal use

  // ------------------------------------------------------------------ timers
  setInterval(tickTimed, 200);
  setInterval(() => {
    renderClock();
    $("wallclock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }, 250);
  setInterval(() => send({ type: "ping" }), 30000);

  new ResizeObserver(() => { layoutBoard(); Map2D.resize(); }).observe($("board"));
  new ResizeObserver(() => Map2D.resize()).observe($("map-panel"));
  new ResizeObserver(() => Map2D.resize()).observe($("map-area"));

  window.VoyoPlayer && VoyoPlayer.init($("video-slot"), (t) => showToast(t));
  fit();
  renderAll();
  connect();
})();
