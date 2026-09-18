/* pages/system-model.js — what SYSTEM says, decided without a DOM.

   The old page printed counters and left the judgement to the reader: SEQ GAPS
   36, in amber, with no way to know whether 36 was a disaster or a Tuesday.
   This module makes the judgement. It turns the stats frame, /api/status, the
   control frame and the client's own book and quote state into a list of
   CHECKS, each of which says four things in plain words:

     level    ok | warn | fail | off | wait
     reading  the one line you read at a glance
     what     what this part does and what its state means right now
     fix      what to do about it, when there is something to do

   and a VERDICT over all of them. Pure functions of their input, so
   tests/js/system-model.test.mjs can pin every threshold.

   Levels, in the order the list sorts them:
     fail  broken now; data or results are wrong or missing
     warn  working, but something an operator should look at
     wait  no data yet (just started) — not a judgement either way
     off   deliberately not running; never counts against the verdict
     ok    fine                                                          */

export const LEVELS = ["fail", "warn", "wait", "off", "ok"];

export const SKEW_WARN_MS = 25;       // matches arb doctor's ntp check
export const P95_HOT_MS = 250;
export const RECENT_S = 120;          // "recently" = the last two minutes
export const PM_BOOK_OLD_MS = 30000;  // no Polymarket book for this long
export const PM_CYCLE_SLOW_S = 60;    // a quote this old is history, not a price
export const STATUS_OLD_MS = 15000;
export const BOOK_NEARLY_FULL = 0.95;
export const RECONNECT_LOOP = 3;      // this many reconnects in two minutes is a loop

const nf = new Intl.NumberFormat("en-US");
const n = (v) => (v == null ? "—" : nf.format(v));
const usd = (ticks) => "$" + (ticks / 10000).toFixed(2);
const plural = (c, one, many) => c + " " + (c === 1 ? one : many || one + "S");

function ago(ms) {
  if (ms == null) return "—";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

/** How much a counter grew over the last RECENT_S stats frames (1/s). A
    counter that reset (a new run) reads as its current value. */
export function recent(statsBuf, get) {
  if (!statsBuf || !statsBuf.length) return 0;
  const last = get(statsBuf[statsBuf.length - 1]);
  const first = get(statsBuf[Math.max(0, statsBuf.length - 1 - RECENT_S)]);
  if (last == null) return 0;
  if (first == null || first > last) return last;
  return last - first;
}

// ---------------------------------------------------------------------------
// the checks
// ---------------------------------------------------------------------------

function checkLink(x) {
  const c = { id: "link", stage: null, name: "THIS SCREEN", goto: null };
  if (x.conn !== "live") {
    return { ...c, level: "fail", reading: "DISCONNECTED FROM THE SERVER — EVERYTHING HERE IS FROZEN",
      what: "This browser tab talks to the arb server over one WebSocket. It is down, so every "
        + "number on every page is the last one received, not the current one.",
      numbers: [["CONNECTION", String(x.conn || "—").toUpperCase()]],
      fix: "It reconnects by itself. If it does not within a minute, the server has stopped: "
        + "check `docker compose ps`, then `uv run arb doctor`." };
  }
  const age = x.statusAt ? x.now - x.statusAt : null;
  if (age != null && age > STATUS_OLD_MS) {
    return { ...c, level: "warn", reading: "LIVE, BUT THE STATUS POLL IS " + ago(age).toUpperCase() + " OLD",
      what: "Prices stream over the WebSocket and are current. The slower health poll "
        + "(/api/status: venues, database) has not answered recently, so those checks may be behind.",
      numbers: [["WEBSOCKET", "LIVE"], ["STATUS POLL", ago(age) + " ago"]],
      fix: null };
  }
  return { ...c, level: "ok", reading: "CONNECTED · DATA IS LIVE",
    what: "This browser tab is connected to the arb server and receiving live frames.",
    numbers: [["WEBSOCKET", "LIVE"], ["STATUS POLL", age == null ? "—" : ago(age) + " ago"],
      ["BROWSER TABS CONNECTED", n(x.stats && x.stats.ws_clients)]],
    fix: null };
}

function checkKalshi(x) {
  const c = { id: "kalshi", stage: "kalshi", name: "KALSHI FEED", goto: null };
  const v = x.status && x.status.venues && x.status.venues.kalshi;
  if (!v) return { ...c, level: "wait", reading: "WAITING FOR THE FIRST STATUS", what: WHAT_KALSHI, numbers: [], fix: null };
  const rate = x.stats ? x.stats.msg_rate_1s : null;
  const numbers = [["STATE", String(v.state).toUpperCase()], ["DETAIL", v.detail || "—"],
    ["MESSAGES / S (ALL FEEDS)", rate == null ? "—" : rate.toFixed(1)],
    ["MESSAGES THIS RUN", n(x.stats && x.stats.msg_total)],
    ["CONNECTIONS THIS RUN", n(x.stats && x.stats.kalshi_connects) + " (1 = never dropped)"]];
  // "Last frame 2s ago" looks healthy in a reconnect loop, because every
  // reconnect delivers a burst of snapshots. The connection count does not lie.
  const reconnects = recent(x.statsBuf, (s) => s.kalshi_connects);
  if (reconnects >= RECONNECT_LOOP) {
    return { ...c, level: "fail", reading: "RECONNECTING OVER AND OVER — " + reconnects + " TIMES IN THE LAST 2 MIN",
      what: WHAT_KALSHI + " The socket keeps dropping and reconnecting, so Kalshi books never "
        + "settle: they are re-fetched, marked untrusted, and re-fetched again. Nothing can be "
        + "priced reliably while this lasts.",
      numbers, fix: "`docker compose logs app | grep kalshi/ws` shows why. HTTP 401 means Kalshi is "
        + "refusing the login (check keys with `uv run arb doctor`); anything else, restart once "
        + "with `docker compose restart app` and leave the watch set alone for a minute." };
  }
  if (v.state === "live") {
    return { ...c, level: "ok", reading: "STREAMING · " + String(v.detail || "").toUpperCase(),
      what: WHAT_KALSHI, numbers, fix: null };
  }
  if (v.state === "connecting") {
    return { ...c, level: "warn", reading: "CONNECTING — NO KALSHI PRICES YET",
      what: WHAT_KALSHI + " It is (re)connecting right now; Kalshi books are frozen until it is back.",
      numbers, fix: "Usually seconds. If it stays here, Kalshi may be rejecting the login: run "
        + "`uv run arb doctor`. Changing the watch set many times in a minute can also get the "
        + "socket refused for a while — wait, then restart the app once." };
  }
  return { ...c, level: "fail", reading: "DOWN — " + String(v.detail || v.state).toUpperCase(),
    what: WHAT_KALSHI + " It is down: every Kalshi book is frozen and no pair can be priced.",
    numbers, fix: "Run `uv run arb doctor` (keys, clock, reachability), then "
      + "`docker compose restart app`." };
}
const WHAT_KALSHI = "Kalshi prices arrive over a WebSocket: every change to a watched book is "
  + "pushed the moment it happens.";

function checkPolymarket(x) {
  const c = { id: "polymarket", stage: "polymarket", name: "POLYMARKET US FEED", goto: { path: "/control", label: "CONTROL" } };
  const v = x.status && x.status.venues && x.status.venues.polymarket_us;
  const ps = x.stats && x.stats.polymarket_us;
  const cycle = x.control && x.control.pairs && x.control.pairs.poll ? x.control.pairs.poll.cycle_s : null;
  if (!v) return { ...c, level: "wait", reading: "WAITING FOR THE FIRST STATUS", what: WHAT_PM, numbers: [], fix: null };
  const numbers = [["STATE", String(v.state).toUpperCase()],
    ["MARKETS POLLED", n(ps && ps.targets)],
    ["ONE FULL CYCLE", cycle == null ? "—" : cycle.toFixed(1) + " s"],
    ["LAST BOOK", ps && ps.last_poll_age_ms != null ? ago(ps.last_poll_age_ms) + " ago" : "—"],
    ["POLLS THIS RUN", n(ps && ps.polls)],
    ["RATE-LIMITED (429)", n(ps && ps.rate_limited)], ["ERRORS", n(ps && ps.errors)]];
  if (v.rest_reachable === false) {
    return { ...c, level: "fail", reading: "UNREACHABLE — NO POLYMARKET PRICES",
      what: WHAT_PM + " The gateway is not answering, so every Polymarket book is frozen.",
      numbers, fix: "Check your network, then `uv run arb doctor`." };
  }
  if (v.state !== "polled" && v.state !== "live") {
    return { ...c, level: v.state === "connecting" ? "warn" : "fail",
      reading: String(v.state).toUpperCase() + " — " + String(v.detail || "").toUpperCase(),
      what: WHAT_PM, numbers, fix: "Run `uv run arb doctor`." };
  }
  const errs = recent(x.statsBuf, (s) => s.polymarket_us && s.polymarket_us.errors);
  const limited = recent(x.statsBuf, (s) => s.polymarket_us && s.polymarket_us.rate_limited);
  if (ps && ps.last_poll_age_ms != null && ps.last_poll_age_ms > PM_BOOK_OLD_MS) {
    return { ...c, level: "warn", reading: "NO BOOK FOR " + ago(ps.last_poll_age_ms).toUpperCase() + " — POLLING HAS STALLED",
      what: WHAT_PM + " Polls should land every couple of seconds and none has.",
      numbers, fix: "If it does not recover within a minute, `docker compose restart app`." };
  }
  if (limited > 0 || errs > 0) {
    return { ...c, level: "warn",
      reading: "POLLING, WITH " + (limited ? plural(limited, "RATE LIMIT") : plural(errs, "ERROR")) + " IN THE LAST 2 MIN",
      what: WHAT_PM + " Some recent polls were refused, so books are refreshing slower than planned.",
      numbers, fix: "Poll fewer markets: untrack pairs on /pairs or trim the Polymarket universe on /control." };
  }
  if (cycle != null && cycle > PM_CYCLE_SLOW_S) {
    return { ...c, level: "warn", reading: "POLLING · EACH BOOK REFRESHES ONLY EVERY " + Math.round(cycle) + "s",
      what: WHAT_PM + " One shared rate budget is split across every polled market, so each "
        + "extra market makes every other quote older. Past a minute, an arb signal is mostly "
        + "measuring how old the Polymarket price is.",
      numbers, fix: "Watch fewer pairs (T on /pairs) or trim the Polymarket universe on /control." };
  }
  return { ...c, level: "ok",
    reading: "POLLING " + n(ps && ps.targets) + " MARKETS" + (cycle != null ? " · EACH BOOK EVERY " + Math.round(cycle) + "s" : ""),
    what: WHAT_PM + " POLLED is this venue's healthy state, not a degraded one.", numbers, fix: null };
}
const WHAT_PM = "Polymarket US has no streaming credentials yet, so its books are fetched one "
  + "market at a time over REST, round-robin.";

function checkBooks(x) {
  const c = { id: "books", stage: "books", name: "ORDER BOOKS", goto: { path: "/", label: "MONITOR" } };
  const b = x.books;
  if (!b || !b.markets) return { ...c, level: "wait", reading: "NO MARKETS YET", what: WHAT_BOOKS, numbers: [], fix: null };
  const bad = Object.entries(b.invalid || {});
  const badN = bad.reduce((a, [, k]) => a + k, 0);
  const gaps = recent(x.statsBuf, (s) => s.seq_gaps);
  const parse = recent(x.statsBuf, (s) => s.parse_errors);
  const numbers = [["MARKETS WATCHED", n(b.markets)], ["WITH A BOOK", n(b.withBook)],
    ["QUIET (NO RECENT UPDATE)", n(b.quiet)],
    ["UNTRUSTED RIGHT NOW", badN ? bad.map(([r, k]) => k + " " + r.replace(/_/g, " ")).join(", ") : "0"],
    ["SEQUENCE GAPS", n(x.stats && x.stats.seq_gaps) + " this run · " + n(gaps) + " in the last 2 min"],
    ["PARSE ERRORS", n(x.stats && x.stats.parse_errors) + " this run · " + n(parse) + " in the last 2 min"]];
  if (parse > 0) {
    return { ...c, level: "warn", reading: plural(parse, "MESSAGE") + " COULD NOT BE READ IN THE LAST 2 MIN",
      what: WHAT_BOOKS + " A venue sent something the parser did not understand. The message is "
        + "skipped and counted, never fatal — but a steady stream means the venue changed its format.",
      numbers, fix: "Check `docker compose logs app | grep -i parse`." };
  }
  if (badN > 0) {
    return { ...c, level: "warn", reading: plural(badN, "BOOK") + " UNTRUSTED RIGHT NOW · RESYNCING",
      what: WHAT_BOOKS + " A book that missed a message or crossed itself is marked untrusted, "
        + "re-fetched, and not traded against until it is whole again.",
      numbers, fix: "Normally clears in seconds. If the same book stays untrusted, reselect it on MONITOR to see why." };
  }
  const missing = b.markets - b.withBook;
  if (missing > 0 && x.stats && x.stats.uptime_s > 90) {
    return { ...c, level: "warn", reading: plural(missing, "MARKET") + " STILL HAVE NO BOOK",
      what: WHAT_BOOKS + " Some watched markets have never delivered a book — usually a market "
        + "that has closed, or a Polymarket market the poller has not reached yet.",
      numbers, fix: "Find them on MONITOR (they show —). Untrack their pairs on /pairs if the market is over." };
  }
  if (gaps > 0) {
    return { ...c, level: "ok", reading: n(b.withBook) + " OF " + n(b.markets) + " BOOKS GOOD · "
      + plural(gaps, "GAP") + " RECOVERED IN THE LAST 2 MIN",
      what: WHAT_BOOKS + " A sequence gap is a missed message. Each one is detected, the book is "
        + "re-fetched, and nothing trades against it meanwhile — so gaps that recover are routine, not damage.",
      numbers, fix: null };
  }
  return { ...c, level: "ok", reading: n(b.withBook) + " OF " + n(b.markets) + " BOOKS GOOD",
    what: WHAT_BOOKS, numbers, fix: null };
}
const WHAT_BOOKS = "Every watched market keeps one order book, rebuilt from the venue's messages.";

function checkWatch(x) {
  const c = { id: "watch", stage: "engine", name: "WATCH SET", goto: { path: "/pairs", label: "PAIRS" } };
  const p = x.control && x.control.pairs;
  if (!p || p.tracked == null) return { ...c, level: "wait", reading: "NOT READ YET", what: WHAT_WATCH, numbers: [], fix: null };
  const numbers = [["PAIRS CONFIRMED", n(p.confirmed)], ["MARKED TO WATCH", n(p.tracked)],
    ["ACTUALLY QUOTING", n(p.live)], ["PROPOSALS IN TOTAL", n(p.total)]];
  if (p.live === 0) {
    return { ...c, level: p.confirmed ? "warn" : "off", reading: "NOTHING IS BEING WATCHED",
      what: WHAT_WATCH + " With no pair watched the engine prices nothing and paper trades nothing.",
      numbers, fix: "On /pairs, focus a confirmed pair and press T — or set a watch set on /control." };
  }
  if (p.live < p.tracked) {
    const idle = p.tracked - p.live;
    return { ...c, level: "warn", reading: n(p.live) + " OF " + n(p.tracked) + " WATCHED PAIRS ARE QUOTING · "
      + idle + " ARE NOT",
      what: WHAT_WATCH + " The " + idle + " that are not quoting are markets a venue says have "
        + "settled or closed. They are skipped safely, but they still clutter the watch set.",
      numbers, fix: "On /pairs, untrack (T) or reject (N) pairs whose market is over." };
  }
  return { ...c, level: "ok", reading: n(p.live) + " PAIRS WATCHED · ALL QUOTING",
    what: WHAT_WATCH, numbers, fix: null };
}
const WHAT_WATCH = "Confirming a pair says the two markets are the same bet; WATCHING one (T on "
  + "/pairs) is what subscribes both legs and puts it on /arb.";

function checkEngine(x) {
  const c = { id: "engine", stage: "engine", name: "ARB ENGINE", goto: { path: "/arb", label: "ARB" } };
  const a = x.arb;
  if (!a || !a.pairs) {
    return { ...c, level: "off", reading: "IDLE — NO PAIR TO PRICE",
      what: WHAT_ENGINE, numbers: [], fix: null };
  }
  const floor = x.control && x.control.paper && x.control.paper.limits ? x.control.paper.limits.min_net_ticks : null;
  const numbers = [["PAIRS PRICED", n(a.pairs)], ["BOTH LEGS HAVE A BOOK", n(a.bothBooks)],
    ["WITH ANY EDGE AFTER FEES", n(a.withEdge)],
    ["CLEARING THE PAPER FLOOR", n(a.overFloor) + (floor != null ? " (≥ " + (floor / 100).toFixed(2) + "¢/ct)" : "")],
    ["BEST EDGE NOW", a.best == null ? "—" : (a.best / 100).toFixed(2) + "¢ per contract"]];
  if (a.bothBooks < a.pairs) {
    return { ...c, level: "warn", reading: "PRICING " + n(a.bothBooks) + " OF " + n(a.pairs) + " PAIRS · "
      + (a.pairs - a.bothBooks) + " MISSING A LEG",
      what: WHAT_ENGINE + " A pair with a missing book on either side cannot be priced.",
      numbers, fix: "Open /arb: the BOOKS column says which leg. A Polymarket leg can take one poll cycle to arrive." };
  }
  return { ...c, level: "ok", reading: "PRICING " + plural(a.pairs, "PAIR") + " · "
    + (a.overFloor ? plural(a.overFloor, "EDGE") + " OVER THE FLOOR" : "NO EDGE OVER THE FLOOR RIGHT NOW"),
    what: WHAT_ENGINE + " No edge most of the time is normal: equivalent markets are usually priced alike.",
    numbers, fix: null };
}
const WHAT_ENGINE = "For every watched pair, on every book change, the engine walks both ladders "
  + "and works out what buying YES on one venue and NO on the other would earn after fees.";

function checkPaper(x) {
  const c = { id: "paper", stage: "paper", name: "PAPER TRADER", goto: { path: "/paper", label: "PAPER" } };
  const p = x.control && x.control.paper;
  if (!p) return { ...c, level: "wait", reading: "NOT READ YET", what: WHAT_PAPER, numbers: [], fix: null };
  if (!p.attached) {
    return { ...c, level: "off", reading: "NOT RUNNING IN THIS PROCESS", what: WHAT_PAPER, numbers: [],
      fix: null };
  }
  const cap = p.limits.max_notional_ticks;
  const used = cap > 0 ? p.notional_ticks / cap : 0;
  const numbers = [["STATE", p.suspended ? "SUSPENDED" : "TRADING"], ["TRADES THIS RUN", n(p.trades)],
    ["PAIRS HELD", n(p.positions)],
    ["DEPLOYED", usd(p.notional_ticks) + " of " + usd(cap) + " (" + Math.round(used * 100) + "%)"],
    ["MIN EDGE TO TRADE", (p.limits.min_net_ticks / 100).toFixed(2) + "¢ per contract"],
    ["MAX PER PAIR", n(p.limits.max_qty_per_pair / 10000) + " contracts"],
    ["DECLINED: BOOK UNTRUSTED", n(p.skipped_invalid)],
    ["DECLINED: WHILE SUSPENDED", n(p.skipped_suspended)],
    ["PRICE LEVELS ALREADY USED UP", n(p.taken_levels)]];
  if (p.suspended) {
    return { ...c, level: "off", reading: "SUSPENDED — TAKING NO TRADES",
      what: WHAT_PAPER + " It is switched off; positions and the ledger are kept.",
      numbers, fix: "RESUME it on /control when you want it trading again." };
  }
  if (used >= BOOK_NEARLY_FULL) {
    return { ...c, level: "warn", reading: "BUDGET " + Math.round(used * 100) + "% USED — NO ROOM FOR NEW TRADES",
      what: WHAT_PAPER + " Its total spend limit is reached, so new edges are being passed up.",
      numbers, fix: "Raise MAX NOTIONAL on /control, or restart the app for a fresh book." };
  }
  return { ...c, level: "ok", reading: "TRADING · " + plural(p.trades || 0, "TRADE") + " · " + usd(p.notional_ticks)
    + " OF " + usd(cap) + " DEPLOYED",
    what: WHAT_PAPER, numbers, fix: null };
}
const WHAT_PAPER = "The paper trader takes every edge that clears its floor, at the size the books "
  + "show, within its limits — simulated, no orders are ever sent. Size it has already taken is "
  + "not taken twice, and a pair whose book is untrusted is declined.";

function checkRecorder(x) {
  const c = { id: "recorder", stage: "recorder", name: "RECORDER", goto: { path: "/control", label: "CONTROL" } };
  if (!x.stats) return { ...c, level: "wait", reading: "WAITING FOR THE FIRST STATS FRAME", what: WHAT_REC, numbers: [], fix: null };
  const r = x.stats.recorder;
  if (!r) {
    return { ...c, level: "warn", reading: "OFF — NOTHING FROM THIS RUN IS BEING SAVED",
      what: WHAT_REC + " It is off, so this session cannot be replayed or studied later.",
      numbers: [["STATE", "OFF"]], fix: "Turn it on from /control (RECORDER → START)." };
  }
  const dropped = recent(x.statsBuf, (s) => s.recorder && s.recorder.dropped);
  const numbers = [["STATE", "ON"], ["MESSAGES QUEUED FOR SAVING", n(r.enqueued)],
    ["DROPPED THIS RUN", n(r.dropped)], ["DROPPED IN THE LAST 2 MIN", n(dropped)]];
  if (dropped > 0) {
    return { ...c, level: "fail", reading: plural(dropped, "MESSAGE") + " LOST IN THE LAST 2 MIN — THE DATABASE CANNOT KEEP UP",
      what: WHAT_REC + " Messages are being thrown away because the save queue is full; the recording has holes.",
      numbers, fix: "Check the database check below and free disk (`uv run arb doctor`)." };
  }
  return { ...c, level: "ok", reading: "SAVING EVERYTHING · " + n(r.enqueued) + " MESSAGES · 0 LOST"
    + (r.dropped ? " RECENTLY (" + n(r.dropped) + " EARLIER)" : ""),
    what: WHAT_REC, numbers, fix: null };
}
const WHAT_REC = "Every raw message from both venues is saved before it is even parsed, so any "
  + "run can be replayed exactly (`arb replay`, or JOBS on /control).";

function checkDatabase(x) {
  const c = { id: "database", stage: "database", name: "DATABASE", goto: null };
  const db = x.status && x.status.database;
  if (!db) return { ...c, level: "wait", reading: "WAITING FOR THE FIRST STATUS", what: WHAT_DB, numbers: [], fix: null };
  const runs = Array.isArray(db.runs) ? db.runs : [];
  const cur = runs.find((r) => r.run_id === x.runId);
  const numbers = [["CONNECTION", db.connected ? "UP" : "DOWN"], ["MESSAGES STORED, ALL RUNS", n(db.raw_messages_total)],
    ["THIS RUN", cur ? n(cur.count) : "—"], ["RUNS ON RECORD", n(runs.length)]];
  if (!db.connected) {
    return { ...c, level: "fail", reading: "DOWN — NOTHING CAN BE SAVED, AND CONTROLS CANNOT BE AUDITED",
      what: WHAT_DB, numbers, fix: "`docker compose ps` — is postgres healthy? Then `uv run arb doctor`." };
  }
  return { ...c, level: "ok", reading: "CONNECTED · " + n(db.raw_messages_total) + " MESSAGES STORED",
    what: WHAT_DB, numbers, fix: null, runs };
}
const WHAT_DB = "Postgres holds the recorded messages, the pair decisions, the paper trades and "
  + "the audit trail of every control action.";

function checkClock(x) {
  const c = { id: "clock", stage: null, name: "CLOCK & LATENCY", goto: null };
  const s = x.stats;
  const lm = (s && s.latency_ms) || {};
  if (!s || !lm.n) return { ...c, level: "wait", reading: "NO TIMED MESSAGES YET", what: WHAT_CLOCK, numbers: [], fix: null };
  const skew = s.clock_skew_ms;
  const ms = (v) => (v == null ? "—" : (Math.abs(v) < 10 ? v.toFixed(1) : String(Math.round(v))) + " ms");
  const numbers = [["VENUE → HERE, TYPICAL", ms(lm.median)], ["VENUE → HERE, SLOWEST 5%", ms(lm.p95)],
    ["ROUND TRIP (KEEPALIVE)", ms(s.rtt_ms)], ["ESTIMATED CLOCK ERROR", skew == null ? "—" : (skew > 0 ? "+" : "") + ms(skew)],
    ["SAMPLES", n(lm.n)]];
  const skewed = (lm.median != null && lm.median < 0) || (skew != null && Math.abs(skew) > SKEW_WARN_MS);
  if (skewed) {
    const dir = (skew != null ? skew : lm.median) < 0 ? "BEHIND" : "AHEAD OF";
    const by = skew != null ? Math.abs(Math.round(skew)) : Math.abs(Math.round(lm.median));
    return { ...c, level: "warn", reading: "THIS MACHINE'S CLOCK IS ~" + by + " ms " + dir + " THE VENUE'S",
      what: WHAT_CLOCK + " The two clocks disagree, which is why latency can read negative. "
        + "Only the latency DISPLAY is affected: prices, edges and paper trades do not use it.",
      numbers, fix: "macOS: `sudo sntp -sS time.apple.com`. Linux VM: `sudo timedatectl set-ntp true`. "
        + "Then `uv run arb doctor` to confirm." };
  }
  if (lm.p95 != null && lm.p95 > P95_HOT_MS) {
    return { ...c, level: "warn", reading: "SLOW: 1 MESSAGE IN 20 TAKES OVER " + Math.round(lm.p95) + " ms TO ARRIVE",
      what: WHAT_CLOCK, numbers, fix: "Usually the network between you and the venue. A VM near the venue fixes it." };
  }
  return { ...c, level: "ok", reading: "MESSAGES ARRIVE IN ~" + ms(lm.median).toUpperCase() + " · CLOCK "
    + (skew == null ? "NOT YET CHECKED" : "AGREES WITH THE VENUE"),
    what: WHAT_CLOCK, numbers, fix: null };
}
const WHAT_CLOCK = "Latency is the venue's timestamp compared with this machine's clock, so it is "
  + "only as good as the two clocks' agreement.";

export function buildChecks(x) {
  const checks = [checkLink(x), checkKalshi(x), checkPolymarket(x), checkBooks(x), checkWatch(x),
    checkEngine(x), checkPaper(x), checkRecorder(x), checkDatabase(x), checkClock(x)];
  checks.forEach((c, i) => { c.order = i; });
  return checks;
}

/** Problems first, then the pipeline's own order. Stable, so a row only moves
    when its level changes. */
export function sortChecks(checks) {
  return checks.slice().sort((a, b) =>
    LEVELS.indexOf(a.level) - LEVELS.indexOf(b.level) || a.order - b.order);
}

export function verdict(checks) {
  const fails = checks.filter((c) => c.level === "fail");
  const warns = checks.filter((c) => c.level === "warn");
  const waits = checks.filter((c) => c.level === "wait");
  const names = (l) => l.map((c) => c.name).join(" · ");
  if (fails.length) {
    return { level: "fail", headline: plural(fails.length, "PROBLEM"),
      line: names(fails) + (warns.length ? " — and " + warns.length + " to look at" : "") };
  }
  if (warns.length) {
    return { level: "warn", headline: plural(warns.length, "THING") + " TO LOOK AT",
      line: names(warns) + " — everything else is working" };
  }
  if (waits.length === checks.length) return { level: "wait", headline: "STARTING UP", line: "waiting for the first data" };
  return { level: "ok", headline: "ALL SYSTEMS NORMAL",
    line: waits.length ? "still waiting on: " + names(waits) : "feeds, books, engine, paper, recorder and database are all healthy" };
}

/** The worst level among the checks belonging to a pipeline stage. */
export function stageLevel(checks, stage) {
  let worst = null;
  for (const c of checks) {
    if (c.stage !== stage) continue;
    if (worst == null || LEVELS.indexOf(c.level) < LEVELS.indexOf(worst)) worst = c.level;
  }
  return worst || "wait";
}

/** Books, summarised from the client's own book map. */
export function summariseBooks(markets, books, now, staleMs) {
  const out = { markets: markets.length, withBook: 0, quiet: 0, invalid: {} };
  for (const m of markets) {
    const b = books.get(m.market_id);
    if (!b) continue;
    out.withBook += 1;
    if (b.valid === false && b.reason && b.reason !== "stale") out.invalid[b.reason] = (out.invalid[b.reason] || 0) + 1;
    else if (b.reason === "stale" || b.age_ms + (now - b.recvAt) > staleMs) out.quiet += 1;
  }
  return out;
}

/** /arb's quotes, summarised. */
export function summariseArb(quotes, floorTicks) {
  const out = { pairs: 0, bothBooks: 0, withEdge: 0, overFloor: 0, best: null };
  for (const q of quotes || []) {
    out.pairs += 1;
    if (q.kalshi && q.kalshi.has_book && q.polymarket_us && q.polymarket_us.has_book) out.bothBooks += 1;
    const net = q.best && q.best.qty > 0 ? q.best.net_per_contract_ticks : 0;
    if (net > 0) out.withEdge += 1;
    if (floorTicks != null && net >= floorTicks && net > 0) out.overFloor += 1;
    if (net > 0 && (out.best == null || net > out.best)) out.best = net;
  }
  return out;
}
