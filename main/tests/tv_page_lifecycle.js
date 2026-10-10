/* The /tv page's approval-request lifecycle (server/tv/tv.js) in node - no browser: tv.js runs in a vm with a
   minimal fake DOM, fake timers, a fake fetch the scenarios answer by hand and a recorded sendBeacon.

     node tests/tv_page_lifecycle.js [path/to/tv.js]      -> one JSON line per scenario: {name, ok, why}

   Run by tests/test_tv_approval_lifecycle.py. */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const TV_JS = process.argv[2] || path.resolve(__dirname, "../../server/tv/tv.js");
const SRC = fs.readFileSync(TV_JS, "utf8");

function element(id) {
  const listeners = {};
  const el = {
    id, hidden: false, textContent: "", innerHTML: "", className: "", value: "", src: "", muted: true, paused: true,
    style: {}, dataset: {}, tagName: "DIV",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    removeAttribute() {}, load() {}, play() { return Promise.resolve(); }, pause() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; },
    canPlayType() { return ""; },
    click() { if (el.hidden) return; for (const fn of listeners.click || []) fn({}); },   // a user cannot click a hidden button
  };
  return el;
}

/* one page load: its own timers, its own fetch queue, its own beacons */
function loadPage(name, opts = {}) {
  const els = {};
  const $ = (id) => (els[id] = els[id] || element(id));
  let now = 0, seq = 0;
  const timers = new Map();
  const calls = [];          // every fetch: {url, opts, resolve(json, status), fail(), aborted}
  const beacons = [];
  const winListeners = {};
  const reloads = [];
  const ignoreAbort = !!opts.ignoreAbort;     // a response that already arrived when the page aborted the call

  function fetchFn(url, o = {}) {
    return new Promise((resolve, reject) => {
      const c = { url, opts: o, aborted: false, settled: false };
      c.resolve = (json, status = 200) => {
        if (c.settled) return; c.settled = true;
        resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(json) });
      };
      c.fail = () => { if (c.settled) return; c.settled = true; reject(new TypeError("Failed to fetch")); };
      if (o.signal && !ignoreAbort) {
        if (o.signal.aborted) { c.aborted = true; c.settled = true; reject(new Error("AbortError")); return; }
        o.signal.addEventListener("abort", () => {
          c.aborted = true;
          if (!c.settled) { c.settled = true; reject(Object.assign(new Error("aborted"), { name: "AbortError" })); }
        });
      }
      calls.push(c);
    });
  }
  const ctx = {
    console,
    URLSearchParams, AbortController, Promise, Object, Math, String, Number, JSON, Date, Error, TypeError,
    location: { search: opts.search || "", href: "/tv", reload() { reloads.push(now); } },
    document: {
      getElementById: $, addEventListener() {}, querySelectorAll() { return []; },
      body: { classList: { add() {}, remove() {}, toggle() {} } }, documentElement: { requestFullscreen() { return Promise.resolve(); } },
      fullscreenElement: null,
    },
    navigator: { sendBeacon(url, body) { beacons.push({ url, body: String(body), at: now }); return true; } },
    localStorage: { getItem() { return null; }, setItem() {} },
    fetch: fetchFn,
    confirm: () => true,
    setTimeout(fn, ms) { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0), every: 0 }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms) { const id = ++seq; timers.set(id, { fn, at: now + ms, every: ms }); return id; },
    clearInterval(id) { timers.delete(id); },
  };
  ctx.window = ctx;
  ctx.addEventListener = (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx, { filename: "tv.js" });

  const page = {
    name, els: $, calls, beacons, reloads, ctx,
    get now() { return now; },
    pendingTimers: () => timers.size,
    async settle() { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); },
    async advance(ms) {                                   // run the timers due in the next `ms` milliseconds
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
        if (!next) break;
        const [id, t] = next;
        now = t.at;
        if (t.every) t.at += t.every; else timers.delete(id);
        t.fn();
        await page.settle();
      }
      now = end;
    },
    open: () => calls.filter((c) => !c.settled),
    last: (url) => [...calls].reverse().find((c) => c.url === url),
    requests: () => calls.filter((c) => c.url === "/api/tv/auth/request").length,
    fire(ev, e = {}) { for (const fn of winListeners[ev] || []) fn(e); },
    text: () => $("a-title").textContent + " | " + $("a-msg").textContent,
  };
  return page;
}

const REQ = "/api/tv/auth/request", STATUS = "/api/tv/auth/status";
let nCh = 0;
const granted = (code) => ({ ok: true, code: code || "ABC-DEF", challenge: "ch" + (++nCh), expires_in: 120, approver_set: true, approver_online: true });

/* the page asks at load; the server answers -> the page polls */
async function pendingPage(name, opts) {
  const p = loadPage(name, opts);
  await p.settle();
  p.last(REQ).resolve(granted());
  await p.settle();
  return p;
}

const scenarios = {
  async "closing the page stops polling and retries and withdraws its own request"() {
    const p = await pendingPage("tab");
    const ch = p.last(REQ) && p.calls[1] && p.calls[1].opts.headers["X-F1-TV-Challenge"];
    p.last(STATUS).resolve({ status: "pending", expires_in: 118 });
    await p.settle();
    await p.advance(1500);                                // the next poll is in flight
    p.fire("pagehide", { persisted: false });
    await p.settle();
    const before = p.calls.length;
    for (const c of p.open()) c.resolve({ status: "pending", expires_in: 100 });   // a late answer, if any
    await p.advance(10 * 60 * 1000);
    if (p.calls.length !== before) return `kept calling the server after pagehide (${p.calls.length - before} more calls)`;
    if (p.pendingTimers() !== 0) return `${p.pendingTimers()} timer(s) still armed after pagehide`;
    const w = p.beacons.filter((b) => b.url === "/api/tv/logout" && b.body === "challenge:" + ch);
    if (w.length !== 1) return `expected one withdraw beacon for ${ch}, got ${JSON.stringify(p.beacons)}`;
    return true;
  },

  async "closing the page while the request is still being created asks nothing more"() {
    const p = loadPage("tab");
    await p.settle();
    p.last(REQ).fail();                                   // server not reachable: the page would retry in 5 s
    await p.settle();
    p.fire("pagehide", { persisted: false });
    await p.advance(10 * 60 * 1000);
    if (p.requests() !== 1) return `${p.requests()} approval requests - the retry survived pagehide`;
    if (p.pendingTimers() !== 0) return `${p.pendingTimers()} timer(s) still armed`;
    return true;
  },

  async "a late request answer after pagehide starts nothing (and is withdrawn)"() {
    const p = loadPage("tab", { ignoreAbort: true });
    await p.settle();
    const req = p.last(REQ);
    p.fire("pagehide", { persisted: false });
    req.resolve(granted());                               // the answer was already on its way
    await p.settle();
    await p.advance(10 * 60 * 1000);
    if (p.calls.some((c) => c.url === STATUS)) return "polled a request after the page was gone";
    if (p.requests() !== 1) return `${p.requests()} approval requests`;
    if (!p.beacons.some((b) => b.body.startsWith("challenge:"))) return "the late request was not withdrawn";
    return true;
  },

  async "denial stops polling and the page never asks again by itself"() {
    const p = await pendingPage("tab");
    p.last(STATUS).resolve({ status: "denied", expires_in: 100 });
    await p.settle();
    const before = p.calls.length;
    await p.advance(30 * 60 * 1000);
    if (p.calls.length !== before) return `${p.calls.length - before} more calls after DENIED`;
    if (p.pendingTimers() !== 0) return `${p.pendingTimers()} timer(s) still armed`;
    if (!p.els("a-again").hidden) return "ASK AGAIN is offered after a denial";
    if (!/DENIED/.test(p.text())) return "no DENIED shown: " + p.text();
    // nothing in this page instance may start a new request: the button, a programmatic call, bfcache
    p.els("a-again").click();
    p.ctx.ask();
    p.fire("pageshow", { persisted: true });
    p.fire("pagehide", { persisted: false });
    await p.advance(10 * 60 * 1000);
    if (p.requests() !== 1) return `${p.requests()} approval requests after the denial`;
    if (p.reloads.length) return "the denied page reloaded itself (= a new request)";
    return true;
  },

  async "late answers of an older flow cannot restart a denied page"() {
    const p = await pendingPage("tab", { ignoreAbort: true });
    const stale = p.last(STATUS);                         // poll of flow 1 still on its way
    p.ctx.ask();                                          // a second trigger in the same page (ASK AGAIN / key)
    await p.settle();
    p.last(REQ).resolve(granted());
    await p.settle();
    p.last(STATUS).resolve({ status: "denied", expires_in: 100 });
    await p.settle();
    stale.resolve({ status: "pending", expires_in: 90 });  // the old poll lands after the denial
    await p.settle();
    await p.advance(30 * 60 * 1000);
    const polls = p.calls.filter((c) => c.url === STATUS).length;
    if (polls !== 2) return `${polls} status calls - the stale answer restarted polling`;
    if (p.requests() !== 2) return `${p.requests()} approval requests`;
    if (!/DENIED/.test(p.text())) return "the stale answer overwrote DENIED: " + p.text();
    const p2 = await pendingPage("tab2", { ignoreAbort: true });
    const stale2 = p2.last(STATUS);
    p2.ctx.ask(); await p2.settle();
    p2.last(REQ).resolve(granted()); await p2.settle();
    p2.last(STATUS).resolve({ status: "denied", expires_in: 100 }); await p2.settle();
    stale2.resolve({ status: "authenticated", page: "secret", expires_in: 3600 });       // nor log it in
    await p2.settle();
    if (p2.calls.some((c) => c.url === "/api/tv/status")) return "the TV started after the denial";
    return true;
  },

  async "a fresh page load after a denial asks again"() {
    const p = await pendingPage("tab");
    p.last(STATUS).resolve({ status: "denied", expires_in: 100 });
    await p.settle();
    const fresh = loadPage("tab, reloaded");                // F5 / reopening the URL: a new page instance
    await fresh.settle();
    if (fresh.requests() !== 1) return "a new page load did not ask for approval";
    fresh.last(REQ).resolve(granted());
    await fresh.settle();
    if (!fresh.calls.some((c) => c.url === STATUS)) return "the new request is not followed";
    return true;
  },

  async "two pages are independent: closing one leaves the other's request alone"() {
    const a = await pendingPage("device A");
    const b = await pendingPage("device B");
    const chB = b.last(STATUS).opts.headers["X-F1-TV-Challenge"];
    a.fire("pagehide", { persisted: false });
    await a.settle();
    if (a.beacons.some((x) => x.body === "challenge:" + chB)) return "A withdrew B's request";
    b.last(STATUS).resolve({ status: "pending", expires_in: 100 });
    await b.settle();
    await b.advance(1500);
    if (b.open().length !== 1 || b.last(STATUS).opts.headers["X-F1-TV-Challenge"] !== chB) return "B stopped following its request";
    b.last(STATUS).resolve({ status: "denied", expires_in: 90 });
    await b.settle();
    const c = await pendingPage("device C");            // a denial on B means nothing for C
    if (c.requests() !== 1 || !c.calls.some((x) => x.url === STATUS)) return "C was blocked by B's denial";
    return true;
  },

  async "an expired request is not re-asked automatically, ASK AGAIN does it once"() {
    const p = await pendingPage("tab");
    p.last(STATUS).resolve({ status: "expired", expires_in: 0 });
    await p.settle();
    await p.advance(10 * 60 * 1000);
    if (p.requests() !== 1) return "re-asked by itself after EXPIRED";
    if (p.els("a-again").hidden) return "ASK AGAIN not offered after EXPIRED";
    p.els("a-again").click(); p.els("a-again").click();
    await p.settle();
    if (p.requests() !== 2) return `${p.requests() - 1} requests for one ASK AGAIN`;
    return true;
  },

  async "an approved page still starts the TV and ends its session on pagehide"() {
    const p = await pendingPage("tab");
    p.last(STATUS).resolve({ status: "authenticated", page: "pagesecret", expires_in: 3600 });
    await p.settle();
    if (!p.calls.some((c) => c.url === "/api/tv/status")) return "the TV did not start";
    p.fire("pagehide", { persisted: false });
    if (!p.beacons.some((b) => b.url === "/api/tv/logout" && b.body === "pagesecret")) return "no logout beacon for the approved page";
    return true;
  },
};

(async () => {
  for (const [name, fn] of Object.entries(scenarios)) {
    let res;
    try { res = await fn(); } catch (e) { res = "threw: " + (e && e.stack || e); }
    console.log(JSON.stringify({ name, ok: res === true, why: res === true ? "" : String(res) }));
  }
})();
