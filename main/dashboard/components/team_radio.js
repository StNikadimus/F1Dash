/* TEAM RADIO panel - the radio clips F1 published for the session shown (server/team_radio.py).
 *
 * Data: the "radio" section of the dashboard state (the existing WebSocket - no extra connection):
 *   [{id, utc, driver, src: "feed"|"openf1", playable, file, transcript?}, ...] newest first.
 *   In LIVE mode the list grows as F1 publishes clips to live timing (if it sends them to this connection);
 *   in VOD / replay it holds what was published up to the video's time (the archive, no spoilers).
 * Audio: /api/radio/audio/<id> - the server fetches the MP3 from the F1 archive (never a URL from here).
 * Driver names / teams / colours come from the dashboard's own driver list (no second mapping).
 *
 * API:  TeamRadio.update(clips, drivers, mode, session)   // after every state change (cheap if unchanged)
 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const panel = $("radio-panel"), list = $("radio-list"), audio = $("radio-audio");
  if (!panel || !list || !audio) return;
  const selTeam = $("radio-team"), selDrv = $("radio-driver"), search = $("radio-search");
  const btnPlay = $("radio-play"), seek = $("radio-seek"), nowEl = $("radio-now"), timeEl = $("radio-time");
  const newPill = $("radio-new"), badge = $("radio-badge"), countEl = $("radio-count");

  let clips = [], drivers = {}, mode = null, session = {};
  let filt = { team: "", driver: "", q: "" };
  try { filt = Object.assign(filt, JSON.parse(localStorage.getItem("f1dash-radio-filter") || "{}")); } catch (e) { /* ignore */ }
  let current = null;                      // id of the clip in the player
  const state = {};                        // id -> "loading" | "playing" | "paused" | "error"
  const errors = {};                       // id -> message
  let renderedIds = [], unseen = 0, lastKey = "", seeking = false;

  function fmtClock(utc) {
    if (!utc) return "--:--:--";
    const d = new Date(utc);
    return isNaN(d) ? "--:--:--" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  function fmtDur(s) {
    if (!isFinite(s) || s < 0) return "0:00";
    s = Math.floor(s);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  function driverOf(c) { return (c && c.driver && drivers[c.driver]) || null; }
  // what a clip is: LIVE = published to the live timing feed during this live session (a recorded clip,
  // not a live audio channel); ARCHIVE = the session's archive (VOD / replay), OPENF1 = OpenF1's list
  function kind(c) {
    if (c.src === "openf1") return { cls: "arch", text: "ARCHIVE · OPENF1", title: "From OpenF1's team radio list (a selection of the F1 recordings)" };
    if (mode === "live") return { cls: "live", text: "LIVE FEED", title: "Published to F1 live timing during this session (a recorded clip, delayed)" };
    return { cls: "arch", text: "ARCHIVE", title: "From the session's F1 live-timing archive" };
  }
  function panelBadge() {
    if (mode === "live") return ["live", "LIVE FEED"];
    if (mode === "vod") return ["arch", "ARCHIVE · WITH THE VIDEO"];
    if (mode === "replay") return ["arch", "REPLAY · ARCHIVE"];
    if (mode === "test") return ["test", "TEST MODE"];
    return ["", ""];
  }
  function emptyText() {
    if (mode === "test") return "Team radio is not simulated in TEST MODE.";
    if (mode === "live") return "No team radio clip received on this connection yet. F1 publishes selected clips to live timing; " +
      "with F1 TV Access live team radio may not be delivered at all (see README \"Team radio\").";
    if (mode === "vod") return "No team radio published up to this point of the session (or none in the archive for it).";
    return "No team radio for this session.";
  }

  function matches(c) {
    const d = driverOf(c);
    if (filt.driver && c.driver !== filt.driver) return false;
    if (filt.team && (!d || d.team !== filt.team)) return false;
    if (filt.q) {
      const q = filt.q.toLowerCase();
      const hay = [c.driver, d && d.tla, d && d.full_name, d && d.team, c.transcript && c.transcript.text].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function fillFilters() {
    const nums = Object.keys(drivers).filter((n) => drivers[n] && (drivers[n].tla || drivers[n].full_name));
    const teams = [...new Set(nums.map((n) => drivers[n].team).filter(Boolean))].sort();
    if (filt.team && !teams.includes(filt.team)) teams.push(filt.team);
    selTeam.innerHTML = '<option value="">ALL TEAMS</option>' +
      teams.map((t) => `<option value="${esc(t)}"${t === filt.team ? " selected" : ""}>${esc(t)}</option>`).join("");
    const inTeam = nums.filter((n) => !filt.team || drivers[n].team === filt.team)
      .sort((a, b) => String(drivers[a].tla || a).localeCompare(String(drivers[b].tla || b)));
    if (filt.driver && nums.length && !inTeam.includes(filt.driver)) filt.driver = "";   // (keep it until the list arrives)
    selDrv.innerHTML = '<option value="">ALL DRIVERS</option>' + inTeam.map((n) =>
      `<option value="${esc(n)}"${n === filt.driver ? " selected" : ""}>${esc(drivers[n].tla || n)} · ${esc(n)}</option>`).join("");
    if (document.activeElement !== search) search.value = filt.q || "";
  }

  function item(c) {
    const d = driverOf(c), k = kind(c), st = state[c.id];
    const icon = !c.playable ? "—" : st === "loading" ? "…" : st === "playing" ? "❚❚" : st === "error" ? "✕" : "▶";
    const t = c.transcript;
    const tx = t && t.text ? `<div class="rd-tx" title="AI transcript (${esc(t.model || "local model")}) - may be wrong">` +
      `<em>AI</em> ${esc(t.text)}${t.confidence != null ? ` <small>≈${Math.round(t.confidence * 100)}%</small>` : ""}</div>` : "";
    const err = st === "error" ? `<div class="rd-err">${esc(errors[c.id] || "cannot be played")}</div>` : "";
    return `<li class="rd-item${c.id === current ? " sel" : ""}${st === "playing" ? " playing" : ""}${st === "error" ? " err" : ""}${c.playable ? "" : " na"}" data-id="${esc(c.id)}"` +
      ` title="${c.playable ? "play / pause" : "no recording file for this message"}">` +
      `<i class="rd-bar" style="background:${esc((d && d.team_color) || "#6d7c8b")}"></i>` +
      `<span class="rd-t">${esc(fmtClock(c.utc))}</span>` +
      `<span class="rd-drv"><b>${esc(d ? d.tla || c.driver : c.driver || "?")}</b> <small>${esc(c.driver || "")}</small></span>` +
      `<span class="rd-team">${esc(d && d.team ? d.team : d ? "" : "driver not in this session's list")}</span>` +
      `<span class="rd-st">${icon}</span>` +
      `<span class="rd-kind ${k.cls}" title="${esc(k.title)}">${esc(k.text)}</span>${tx}${err}</li>`;
  }

  function render(force) {
    const shown = clips.filter(matches);
    // keep what the user is looking at: anchor on the first visible row unless they are at the top
    const atTop = list.scrollTop < 6;
    let anchor = null, delta = 0;
    if (!atTop) {
      for (const li of list.children) {
        if (li.offsetTop + li.offsetHeight > list.scrollTop) { anchor = li.dataset.id; delta = li.offsetTop - list.scrollTop; break; }
      }
    }
    const fresh = shown.filter((c) => !renderedIds.includes(c.id)).length;
    if (!atTop && renderedIds.length && fresh) unseen += fresh;
    if (atTop) unseen = 0;
    list.innerHTML = shown.length ? shown.map(item).join("") :
      `<li class="rd-empty">${clips.length ? "No clip for this filter." : esc(emptyText())}</li>`;
    renderedIds = shown.map((c) => c.id);
    if (anchor) {
      const el = list.querySelector(`[data-id="${CSS.escape(anchor)}"]`);
      if (el) list.scrollTop = el.offsetTop - delta;
    } else if (atTop || force) {
      list.scrollTop = 0;
    }
    newPill.hidden = !unseen;
    newPill.textContent = `${unseen} NEW ↑`;
    const [bcls, btxt] = panelBadge();
    badge.className = "rd-badge " + bcls; badge.textContent = btxt;
    countEl.textContent = clips.length ? (shown.length === clips.length ? `${clips.length} CLIPS` : `${shown.length} / ${clips.length}`) : "";
    renderPlayer();
  }

  function renderPlayer() {
    const c = clips.find((x) => x.id === current);
    const d = driverOf(c);
    btnPlay.disabled = !c || !c.playable;
    btnPlay.textContent = c && state[c.id] === "playing" ? "❚❚" : "▶";
    seek.disabled = !c || !isFinite(audio.duration);
    if (!c) { nowEl.textContent = clips.length ? "Select a clip to play it" : "—"; timeEl.textContent = ""; return; }
    const st = state[c.id];
    nowEl.textContent = `${d ? d.tla || c.driver : c.driver || "?"} · ${fmtClock(c.utc)}` +
      (st === "loading" ? " · LOADING…" : st === "error" ? " · " + (errors[c.id] || "CANNOT BE PLAYED") : "");
    timeEl.textContent = `${fmtDur(audio.currentTime)} / ${fmtDur(audio.duration)}`;
  }

  function stop() {
    if (current) delete state[current];
    current = null;
    audio.pause(); audio.removeAttribute("src"); audio.load();
  }

  function play(id) {
    const c = clips.find((x) => x.id === id);
    if (!c || !c.playable) return;
    if (current === id && state[id] && state[id] !== "error") {           // the same clip: pause / resume
      if (audio.paused) audio.play().catch(() => {}); else audio.pause();
      return;
    }
    if (current && state[current] !== "error") delete state[current];       // only one clip at a time
    current = id; state[id] = "loading"; delete errors[id];
    audio.src = "/api/radio/audio/" + encodeURIComponent(id);
    audio.play().catch(() => {});
    render();
  }

  async function explainError(id) {
    // the server answers JSON with a reason ("recording not available ...") - show it
    try {
      const r = await fetch("/api/radio/audio/" + encodeURIComponent(id), { method: "GET", cache: "no-store", headers: { Range: "bytes=0-0" } });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        return String(b.error || `HTTP ${r.status}`).slice(0, 120);
      }
      return "this browser cannot play the recording";
    } catch (e) { return "the dashboard server is not reachable"; }
  }

  audio.addEventListener("playing", () => { if (current) { state[current] = "playing"; render(); } });
  audio.addEventListener("pause", () => { if (current && state[current] === "playing") { state[current] = "paused"; render(); } });
  audio.addEventListener("waiting", () => { if (current && state[current] !== "error") { state[current] = "loading"; renderPlayer(); } });
  audio.addEventListener("ended", () => { if (current) { state[current] = "paused"; audio.currentTime = 0; render(); } });
  audio.addEventListener("error", async () => {
    const id = current;
    if (!id || !audio.getAttribute("src")) return;
    state[id] = "error"; errors[id] = "loading…"; render();
    errors[id] = await explainError(id);
    if (current === id) render();
  });
  audio.addEventListener("timeupdate", () => {
    if (!seeking && isFinite(audio.duration) && audio.duration > 0) seek.value = String(Math.round(audio.currentTime / audio.duration * 1000));
    renderPlayer();
  });
  audio.addEventListener("loadedmetadata", renderPlayer);
  seek.addEventListener("input", () => { seeking = true; });
  seek.addEventListener("change", () => {
    if (isFinite(audio.duration)) audio.currentTime = Number(seek.value) / 1000 * audio.duration;
    seeking = false;
  });
  btnPlay.addEventListener("click", () => { if (current) play(current); });
  list.addEventListener("click", (e) => {
    const li = e.target.closest(".rd-item");
    if (li && li.dataset.id) play(li.dataset.id);
  });
  list.addEventListener("scroll", () => { if (list.scrollTop < 6 && unseen) { unseen = 0; newPill.hidden = true; } });
  newPill.addEventListener("click", () => { list.scrollTop = 0; unseen = 0; newPill.hidden = true; });
  function saveFilt() { try { localStorage.setItem("f1dash-radio-filter", JSON.stringify(filt)); } catch (e) { /* ignore */ } }
  selTeam.addEventListener("change", () => { filt.team = selTeam.value; filt.driver = ""; saveFilt(); fillFilters(); render(true); });
  selDrv.addEventListener("change", () => { filt.driver = selDrv.value; saveFilt(); render(true); });
  search.addEventListener("input", () => { filt.q = search.value.trim().slice(0, 40); saveFilt(); render(true); });
  // the TV remote / keyboard drive the dashboard: keys typed in the panel's fields stay in the panel
  for (const el of [selTeam, selDrv, search]) el.addEventListener("keydown", (e) => e.stopPropagation());

  window.TeamRadio = {
    update(newClips, newDrivers, newMode, newSession) {
      clips = Array.isArray(newClips) ? newClips.filter((c) => c && typeof c.id === "string") : [];
      drivers = newDrivers || {}; mode = newMode; session = newSession || {};
      // re-render only when something the panel shows changed (drivers update many times a second)
      const meta = Object.keys(drivers).sort().map((n) => { const d = drivers[n] || {}; return `${n}|${d.tla}|${d.team}|${d.team_color}`; }).join(",");
      const key = `${mode}|${session.path || ""}|${meta}|` + clips.map((c) => c.id + (c.transcript ? "t" + c.transcript.created : "")).join(",");
      if (key === lastKey) return;
      lastKey = key;
      if (current && !clips.some((c) => c.id === current)) stop();          // e.g. the video jumped back
      fillFilters();
      render();
    },
  };
})();
