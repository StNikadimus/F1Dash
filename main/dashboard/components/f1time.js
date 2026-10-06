/* Converting a clock time the user reads in the video into UTC (Manual Exact Time).
 * Shared by the dashboard SYNC menu and the phone remote.
 *
 *   zone:   "slo"   = Slovenia (Europe/Ljubljana, CET/CEST with the correct DST for that day)
 *           "track" = the circuit's local time (OpenF1 / F1 gmt_offset of the session)
 *   format: "24" or "12" (then AM/PM, or "pm"/"am" typed after the time)
 * The day is the one (±1) that puts the time nearest to the session start.
 */
(function (root) {
  "use strict";
  const SLO = "Europe/Ljubljana";

  function tzOffsetMin(ms, tz) {
    // offset of the named zone at instant ms, in minutes (local = UTC + offset)
    const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const p = {};
    for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
    const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
    return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 60000);
  }

  function gmtOffsetMin(gmt) {
    const g = String(gmt || "").match(/^([+-])?(\d{1,2}):(\d{2})/);
    return g ? (g[1] === "-" ? -1 : 1) * (+g[2] * 60 + +g[3]) : null;
  }

  /** "14:48:32", "14.48.32", "14 48 32", "14:48", "2:48:32 pm" -> {h, mi, se} or {error} */
  function parseClock(text, format, ampm) {
    let t = String(text || "").trim().toLowerCase();
    let ap = null;
    const apm = t.match(/\s*(a\.?m\.?|p\.?m\.?|dop\.?|pop\.?)$/);
    if (apm) { ap = /^(p|pop)/.test(apm[1]) ? "pm" : "am"; t = t.slice(0, apm.index).trim(); }
    const m = t.match(/^(\d{1,2})[:.\s](\d{2})(?:[:.\s](\d{2})(?:[.,](\d{1,3}))?)?$/);
    if (!m) return { error: "Enter the time as HH:MM:SS, e.g. 14:48:32" };
    let h = +m[1];
    const mi = +m[2], se = +(m[3] || 0) + (m[4] ? +("0." + m[4]) : 0);
    if (mi > 59 || se >= 60) return { error: "Minutes and seconds must be 00-59" };
    if (format === "12" || ap) {
      ap = ap || (ampm || "am").toLowerCase();
      if (h < 1 || h > 12) return { error: "12-hour clock: the hour must be 1-12 (with AM/PM)" };
      h = (h % 12) + (ap === "pm" ? 12 : 0);
    } else if (h > 23) return { error: "24-hour clock: the hour must be 0-23" };
    return { h, mi, se };
  }

  /** wall-clock time in the zone -> UTC ms, on the day nearest to the session start */
  function toUtcMs(parts, zone, sessionStartIso, gmtOffset) {
    const start = new Date(sessionStartIso).getTime();
    if (!isFinite(start)) return { error: "The session start is not known yet" };
    const fixed = zone === "track" ? gmtOffsetMin(gmtOffset) : null;
    if (zone === "track" && fixed === null) return { error: "The track's time zone is not known for this session" };
    let best = null;
    for (const dd of [-1, 0, 1]) {
      const off0 = zone === "track" ? fixed : tzOffsetMin(start, SLO);
      const day = new Date(start + off0 * 60000);                       // the session day in that zone
      const wall = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate() + dd, parts.h, parts.mi, 0) +
        Math.round(parts.se * 1000);
      let t = wall - off0 * 60000;
      if (zone !== "track") t = wall - tzOffsetMin(t, SLO) * 60000;      // DST of that very instant
      if (best === null || Math.abs(t - start) < Math.abs(best - start)) best = t;
    }
    return { ms: best };
  }

  function fmtIn(ms, zone, gmtOffset) {
    const off = zone === "utc" ? 0 : zone === "track" ? gmtOffsetMin(gmtOffset) : tzOffsetMin(ms, SLO);
    if (off === null) return "?";
    const d = new Date(ms + off * 60000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
  }

  /** everything the UI needs: {iso, preview, warn} or {error} */
  function exactTime(text, zone, format, ampm, sessionStartIso, gmtOffset) {
    const parts = parseClock(text, format, ampm);
    if (parts.error) return parts;
    const r = toUtcMs(parts, zone, sessionStartIso, gmtOffset);
    if (r.error) return r;
    const start = new Date(sessionStartIso).getTime();
    const hoursAway = (r.ms - start) / 3600000;
    const d = new Date(r.ms);
    const preview = `= ${fmtIn(r.ms, "utc")} UTC · ${fmtIn(r.ms, "slo")} Slovenia · ${fmtIn(r.ms, "track", gmtOffset)} track` +
      ` (${d.getUTCDate()}. ${d.getUTCMonth() + 1}.)`;
    const warn = Math.abs(hoursAway) > 3 ? `This is ${hoursAway > 0 ? "+" : ""}${hoursAway.toFixed(1)} h from the session start - no data there. Check Slovenia/track and 24/12-hour.` : null;
    return { iso: d.toISOString(), preview, warn };
  }

  /** The session clock between two server states. The server sends the remaining time at the
   *  shown F1 moment and the replay rate (0 = video paused / buffering). Only the replay may move
   *  it: paused -> frozen; never below 0:00. elapsedWallMs = ms since that state arrived. */
  function clockNow(clock, elapsedWallMs) {
    if (!clock || clock.remaining_ms === null || clock.remaining_ms === undefined) return null;
    let ms = clock.remaining_ms;
    const rate = clock.speed === null || clock.speed === undefined ? 1 : clock.speed;   // 0 must stay 0
    if (clock.running) ms -= Math.max(0, elapsedWallMs) * rate;
    return Math.max(0, ms);
  }

  /** F1 time of the shown moment between two server states (same rule: paused -> frozen). */
  function f1Now(nowMs, speed, elapsedWallMs) {
    if (nowMs === null || nowMs === undefined) return null;
    const rate = speed === null || speed === undefined ? 1 : speed;
    return nowMs + Math.max(0, elapsedWallMs) * rate;
  }

  root.F1Time = { exactTime, parseClock, toUtcMs, tzOffsetMin, fmtIn, clockNow, f1Now };
})(typeof window !== "undefined" ? window : globalThis);
