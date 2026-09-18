/* tests/js/system-model.test.mjs — pages/system-model.js, SYSTEM's judgement.

   The page's whole job is to say whether something is wrong, so a wrong
   threshold is the page lying: calling a recovered sequence gap a problem
   trains the operator to ignore amber, and calling a dropped recording fine
   loses data quietly. Each check's levels are pinned here. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl } from "./shim.mjs";

const m = await import(jsUrl("pages/system-model.js"));

const NOW = 1_000_000;

function healthy(over = {}) {
  const stats = {
    msg_total: 5000, msg_rate_1s: 12.5, parse_errors: 0, seq_gaps: 0, ws_clients: 1, uptime_s: 900,
    latency_ms: { last: 40, median: 38, p95: 61, n: 500 }, rtt_ms: 70, clock_skew_ms: 3,
    recorder: { enqueued: 9000, dropped: 0 },
    polymarket_us: { polls: 400, rate_limited: 0, errors: 0, targets: 12, rate_per_s: 0.45, last_poll_age_ms: 1500 },
  };
  return {
    now: NOW, conn: "live", statusAt: NOW - 2000, runId: "run-b", stats, statsBuf: [stats],
    status: {
      venues: { kalshi: { state: "live", detail: "last frame 800ms ago" },
        polymarket_us: { state: "polled", detail: "REST polling 12 markets", rest_reachable: true } },
      database: { connected: true, raw_messages_total: 100753, runs: [{ run_id: "run-b", count: 2160 }] },
    },
    control: {
      paper: { attached: true, enabled: true, suspended: false, notional_ticks: 960000, trades: 7, positions: 1,
        skipped_invalid: 0, skipped_suspended: 0, taken_levels: 2,
        limits: { min_net_ticks: 50, max_qty_per_pair: 1000000, max_notional_ticks: 10000000 } },
      pairs: { confirmed: 69, tracked: 16, live: 16, total: 11967, poll: { cycle_s: 26.7 } },
    },
    books: { markets: 46, withBook: 46, quiet: 30, invalid: {} },
    arb: { pairs: 16, bothBooks: 16, withEdge: 3, overFloor: 1, best: 58 },
    ...over,
  };
}

const level = (x, id) => m.buildChecks(x).find((c) => c.id === id).level;
const check = (x, id) => m.buildChecks(x).find((c) => c.id === id);

test("a healthy system: every check ok, and the verdict says so", () => {
  const checks = m.buildChecks(healthy());
  assert.deepEqual([...new Set(checks.map((c) => c.level))], ["ok"]);
  const v = m.verdict(checks);
  assert.equal(v.level, "ok");
  assert.equal(v.headline, "ALL SYSTEMS NORMAL");
  assert.ok(checks.every((c) => c.reading && c.what), "every check explains itself");
});

test("sequence gaps that recovered are routine, not a warning", () => {
  const x = healthy();
  const before = { ...x.stats, seq_gaps: 30 };
  x.stats = { ...x.stats, seq_gaps: 36 };
  x.statsBuf = [before, x.stats];
  const c = check(x, "books");
  assert.equal(c.level, "ok", "36 gaps that all resynced is a Tuesday");
  assert.match(c.reading, /6 GAPS RECOVERED/);
});

test("an untrusted book, a parse error and a missing book each warn", () => {
  assert.equal(level(healthy({ books: { markets: 46, withBook: 46, quiet: 0, invalid: { seq_gap: 2 } } }), "books"), "warn");
  assert.equal(level(healthy({ books: { markets: 46, withBook: 44, quiet: 0, invalid: {} } }), "books"), "warn");
  const x = healthy();
  x.statsBuf = [{ ...x.stats, parse_errors: 0 }, { ...x.stats, parse_errors: 3 }];
  assert.equal(level(x, "books"), "warn");
});

test("a just-started process is not blamed for books that have not arrived", () => {
  const x = healthy({ books: { markets: 46, withBook: 10, quiet: 0, invalid: {} } });
  x.stats = { ...x.stats, uptime_s: 20 };
  assert.equal(level(x, "books"), "ok");
});

test("recent() is a two-minute window, and survives a counter reset", () => {
  const buf = [];
  for (let i = 0; i < 300; i++) buf.push({ seq_gaps: i });
  assert.equal(m.recent(buf, (s) => s.seq_gaps), m.RECENT_S);
  assert.equal(m.recent([{ seq_gaps: 50 }, { seq_gaps: 4 }], (s) => s.seq_gaps), 4, "a new run: count from zero");
  assert.equal(m.recent([], (s) => s.seq_gaps), 0);
});

test("watch set: pairs tracked but not quoting are called out, nothing watched is a warning", () => {
  const some = healthy();
  some.control.pairs = { ...some.control.pairs, tracked: 41, live: 16 };
  const c = check(some, "watch");
  assert.equal(c.level, "warn");
  assert.match(c.reading, /16 OF 41 WATCHED PAIRS ARE QUOTING · 25 ARE NOT/);
  assert.ok(c.fix);
  const none = healthy();
  none.control.pairs = { ...none.control.pairs, tracked: 0, live: 0 };
  assert.equal(level(none, "watch"), "warn");
});

test("paper: suspended is OFF (a choice), a full budget warns, not attached is off", () => {
  const sus = healthy();
  sus.control.paper = { ...sus.control.paper, suspended: true, enabled: false };
  assert.equal(level(sus, "paper"), "off");
  assert.equal(m.verdict(m.buildChecks(sus)).level, "ok", "a deliberate OFF never fails the verdict");
  const full = healthy();
  full.control.paper = { ...full.control.paper, notional_ticks: 9_600_000 };
  assert.equal(level(full, "paper"), "warn");
  const none = healthy();
  none.control.paper = { ...none.control.paper, attached: false };
  assert.equal(level(none, "paper"), "off");
});

test("paper: the reading carries the money, in dollars", () => {
  assert.match(check(healthy(), "paper").reading, /7 TRADES · \$96\.00 OF \$1000\.00 DEPLOYED/);
});

test("recorder: off warns, recent drops FAIL, old drops do not", () => {
  const off = healthy();
  off.stats = { ...off.stats, recorder: null };
  assert.equal(level(off, "recorder"), "warn");
  const losing = healthy();
  losing.stats = { ...losing.stats, recorder: { enqueued: 9000, dropped: 12 } };
  losing.statsBuf = [{ ...losing.stats, recorder: { enqueued: 100, dropped: 2 } }, losing.stats];
  assert.equal(level(losing, "recorder"), "fail");
  const healed = healthy();
  healed.stats = { ...healed.stats, recorder: { enqueued: 9000, dropped: 12 } };
  healed.statsBuf = [healed.stats, healed.stats];
  assert.equal(level(healed, "recorder"), "ok");
});

test("polymarket: slow cycle, stalled polls and rate limits warn; unreachable fails", () => {
  const slow = healthy();
  slow.control.pairs.poll = { cycle_s: 75 };
  assert.equal(level(slow, "polymarket"), "warn");
  const stalled = healthy();
  stalled.stats = { ...stalled.stats, polymarket_us: { ...stalled.stats.polymarket_us, last_poll_age_ms: 45000 } };
  assert.equal(level(stalled, "polymarket"), "warn");
  const down = healthy();
  down.status.venues.polymarket_us.rest_reachable = false;
  assert.equal(level(down, "polymarket"), "fail");
});

test("kalshi: connecting warns, anything else not live fails", () => {
  const conn = healthy();
  conn.status.venues.kalshi = { state: "connecting", detail: "" };
  assert.equal(level(conn, "kalshi"), "warn");
  const down = healthy();
  down.status.venues.kalshi = { state: "down", detail: "HTTP 401" };
  assert.equal(level(down, "kalshi"), "fail");
});

test("clock: skew past 25ms or a negative median warns, and says which way", () => {
  const behind = healthy();
  behind.stats = { ...behind.stats, clock_skew_ms: -27, latency_ms: { ...behind.stats.latency_ms, median: -14 } };
  const c = check(behind, "clock");
  assert.equal(c.level, "warn");
  assert.match(c.reading, /~27 ms BEHIND/);
  assert.match(c.what, /Only the latency DISPLAY is affected/);
  const none = healthy();
  none.stats = { ...none.stats, latency_ms: { n: 0 } };
  assert.equal(level(none, "clock"), "wait");
});

test("a dead socket and a dead database fail, and lead the verdict", () => {
  const x = healthy({ conn: "reconnecting" });
  x.status.database.connected = false;
  const checks = m.buildChecks(x);
  const v = m.verdict(checks);
  assert.equal(v.level, "fail");
  assert.equal(v.headline, "2 PROBLEMS");
  const sorted = m.sortChecks(checks);
  assert.deepEqual(sorted.slice(0, 2).map((c) => c.id), ["link", "database"]);
});

test("sorting is by level, then by pipeline order — stable", () => {
  const x = healthy();
  x.control.pairs = { ...x.control.pairs, tracked: 41, live: 16 };
  const ids = m.sortChecks(m.buildChecks(x)).map((c) => c.id);
  assert.equal(ids[0], "watch");
  assert.deepEqual(ids.slice(1, 4), ["link", "kalshi", "polymarket"]);
});

test("a stage takes the worst level of its checks", () => {
  const x = healthy();
  x.control.pairs = { ...x.control.pairs, tracked: 41, live: 16 };
  const checks = m.buildChecks(x);
  assert.equal(m.stageLevel(checks, "engine"), "warn", "watch set warns, arb engine ok");
  assert.equal(m.stageLevel(checks, "paper"), "ok");
});

test("before any data: everything waits and the verdict says starting up", () => {
  const checks = m.buildChecks({ now: NOW, conn: "live", statusAt: 0, stats: null, statsBuf: [], status: null,
    control: null, books: null, arb: null });
  assert.equal(m.verdict(checks).level === "wait" || m.verdict(checks).level === "ok", true);
  assert.ok(checks.filter((c) => c.level === "wait").length >= 6);
});

test("summaries: books by validity, quotes against the paper floor", () => {
  const markets = [{ market_id: "a" }, { market_id: "b" }, { market_id: "c" }, { market_id: "d" }];
  const books = new Map([
    ["a", { valid: true, reason: null, age_ms: 0, recvAt: NOW }],
    ["b", { valid: false, reason: "stale", age_ms: 0, recvAt: NOW }],
    ["c", { valid: false, reason: "seq_gap", age_ms: 0, recvAt: NOW }],
  ]);
  assert.deepEqual(m.summariseBooks(markets, books, NOW, 5000),
    { markets: 4, withBook: 3, quiet: 1, invalid: { seq_gap: 1 } });
  const leg = { has_book: true };
  const quotes = [
    { kalshi: leg, polymarket_us: leg, best: { qty: 5, net_per_contract_ticks: 58 } },
    { kalshi: leg, polymarket_us: leg, best: { qty: 5, net_per_contract_ticks: 20 } },
    { kalshi: leg, polymarket_us: { has_book: false }, best: { qty: 0, net_per_contract_ticks: 0 } },
  ];
  assert.deepEqual(m.summariseArb(quotes, 50), { pairs: 3, bothBooks: 2, withEdge: 2, overFloor: 1, best: 58 });
});

test("kalshi: a reconnect loop is a PROBLEM even while frames keep arriving", () => {
  // The live failure: every reconnect delivers snapshots, so "last frame 2s
  // ago" read healthy while the socket dropped nine times in two minutes.
  const x = healthy();
  x.stats = { ...x.stats, kalshi_connects: 10 };
  x.statsBuf = [{ ...x.stats, kalshi_connects: 1 }, x.stats];
  const c = check(x, "kalshi");
  assert.equal(c.level, "fail");
  assert.match(c.reading, /9 TIMES IN THE LAST 2 MIN/);
  // one reconnect (a quiet-market stall) is routine
  const once = healthy();
  once.stats = { ...once.stats, kalshi_connects: 2 };
  once.statsBuf = [{ ...once.stats, kalshi_connects: 1 }, once.stats];
  assert.equal(level(once, "kalshi"), "ok");
});
