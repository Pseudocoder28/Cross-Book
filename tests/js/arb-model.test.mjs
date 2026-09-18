/* tests/js/arb-model.test.mjs — pages/arb-model.js, what /arb says about how
   current each leg's price is.

   The column this replaced printed K:QUIET over a Kalshi book that matched
   the venue to the tick, and P:OK over a Polymarket quote a minute old. Each
   test here is one way of pointing at the wrong leg. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl } from "./shim.mjs";

const m = await import(jsUrl("pages/arb-model.js"));

const ok = { has_book: true, valid: true, reason: null };
const stale = { has_book: true, valid: false, reason: "stale" };
const CYCLE = 64_000;

test("a Kalshi book nobody has touched for seven minutes is LIVE, not a warning", () => {
  const r = m.kalshiLeg(stale, 408_300, "live");
  assert.equal(r.text, "LIVE");
  assert.equal(r.level, "ok");
  assert.match(r.detail, /NO CHANGE FOR 6m 48s/);
});

test("a Kalshi book that just changed says LIVE and nothing about quiet", () => {
  const r = m.kalshiLeg(ok, 1200, "live");
  assert.equal(r.text, "LIVE");
  assert.doesNotMatch(r.detail, /NO CHANGE/);
});

test("LIVE is never claimed over a socket that is not live", () => {
  for (const feed of ["connecting", "down"]) {
    const r = m.kalshiLeg(ok, 3000, feed);
    assert.equal(r.text, "FROZEN", feed);
    assert.equal(r.level, "bad", feed);
  }
  // before the first status frame the page does not know: it says so
  assert.equal(m.kalshiLeg(ok, 3000, null).level, "wait");
  assert.notEqual(m.kalshiLeg(ok, 3000, null).text, "LIVE");
});

test("a structural problem outranks everything, on either venue", () => {
  const gap = { has_book: true, valid: false, reason: "seq_gap" };
  assert.equal(m.kalshiLeg(gap, 100, "live").level, "bad");
  assert.equal(m.kalshiLeg(gap, 100, "live").text, "SEQ GAP");
  assert.equal(m.polymarketLeg({ has_book: true, valid: false, reason: "crossed" }, 100, CYCLE).text, "CROSSED");
  assert.equal(m.kalshiLeg({ has_book: false }, null, "live").text, "NONE");
  assert.equal(m.polymarketLeg(null, null, CYCLE).text, "NONE");
});

test("a Polymarket quote shows its age, because for a polled venue age is the freshness", () => {
  const r = m.polymarketLeg(ok, 34_300, CYCLE);
  assert.equal(r.text, "34s");
  assert.equal(r.level, "ok");
  assert.match(r.detail, /POLLED 34s AGO/);
  assert.match(r.detail, /RE-READ EVERY 1m 04s/);
});

test("a quote inside one cycle is on schedule; past a cycle and a half it is overdue", () => {
  assert.equal(m.polymarketLeg(ok, CYCLE, CYCLE).level, "ok");
  assert.equal(m.polymarketLeg(ok, CYCLE * 1.5, CYCLE).level, "ok");
  const late = m.polymarketLeg(ok, CYCLE * 1.5 + 1, CYCLE);
  assert.equal(late.level, "bad");
  assert.match(late.detail, /OVERDUE/);
});

test("the server calling a Polymarket book stale is overdue whatever the cycle says", () => {
  assert.equal(m.polymarketLeg(stale, 10_000, CYCLE).level, "bad");
  assert.equal(m.polymarketLeg(stale, 10_000, null).level, "bad");
});

test("an unknown cycle never invents an overdue, and an unknown age is not a number", () => {
  assert.equal(m.polymarketLeg(ok, 500_000, null).level, "ok");
  const r = m.polymarketLeg(ok, null, CYCLE);
  assert.equal(r.level, "wait");
  assert.equal(r.text, "—");
});

test("the cell stays narrow as the age grows", () => {
  assert.equal(m.polymarketLeg(ok, 99_900, null).text, "99s");
  assert.equal(m.polymarketLeg(ok, 100_000, null).text, "1m");
  assert.equal(m.polymarketLeg(ok, 3 * 3600_000, null).text, "3h");
});

test("fmtElapsed keeps two units", () => {
  assert.equal(m.fmtElapsed(0), "0s");
  assert.equal(m.fmtElapsed(59_999), "59s");
  assert.equal(m.fmtElapsed(60_000), "1m 00s");
  assert.equal(m.fmtElapsed(7_505_000), "2h 05m");
});

test("the poll cycle comes from the stats frame, and is unknown without one", () => {
  assert.equal(Math.round(m.pollCycleMs({ targets: 29, rate_per_s: 0.45 })), 64444);
  assert.equal(m.pollCycleMs(null), null);
  assert.equal(m.pollCycleMs({ targets: 0, rate_per_s: 0.45 }), null);
});

test("a book frame keeps ageing between frames", () => {
  assert.equal(m.ageNow({ age_ms: 2000, recvAt: 10_000 }, 13_000), 5000);
  assert.equal(m.ageNow(undefined, 13_000), null);
  // a clock that steps backwards never makes a quote younger than measured
  assert.equal(m.ageNow({ age_ms: 2000, recvAt: 10_000 }, 9_000), 2000);
});
