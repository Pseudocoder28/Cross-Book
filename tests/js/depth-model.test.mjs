/* tests/js/depth-model.test.mjs — pages/depth-model.js, the arithmetic behind
   the DEPTH panel.

   These are accuracy tests. The panel draws cumulative terrain, per-level
   bars and a price window from these functions; a wrong prefix sum draws
   liquidity that is not there, and a scale without hysteresis makes every bar
   on screen jump on every tick. Units: prices are ticks of $0.0001, quantities
   e4 (1e-4 contracts). */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl } from "./shim.mjs";

const m = await import(jsUrl("pages/depth-model.js"));

const C = 10000; // e4 per contract

function book(bids, asks, extra = {}) {
  return { bids, asks, valid: true, reason: null, ...extra };
}

// ---------------------------------------------------------------------------
// the model
// ---------------------------------------------------------------------------

test("cumulative depth is an exact integer prefix sum, best first", () => {
  const md = m.buildModel(book(
    [[5100, 101 * C + 202], [5000, 4 * C + 9156], [4900, 385 * C]],
    [[5200, 18 * C + 788], [5300, 252 * C]],
  ));
  assert.deepEqual(md.bids.map((l) => l.cum), [101 * C + 202, 105 * C + 9358, 490 * C + 9358]);
  assert.deepEqual(md.asks.map((l) => l.cum), [18 * C + 788, 270 * C + 788]);
  assert.equal(md.totB, 490 * C + 9358);
  assert.equal(md.bb, 5100);
  assert.equal(md.ba, 5200);
  assert.equal(md.mid, 5150);
  assert.equal(md.spr, 100);
});

test("no mid and no spread unless the book is two-sided and uncrossed", () => {
  const one = m.buildModel(book([[5100, C]], []));
  assert.equal(one.mid, null);
  assert.equal(one.spr, null);
  assert.equal(one.oneSided, "bids");

  const crossed = m.buildModel(book([[8800, C]], [[8700, C]]));
  assert.equal(crossed.crossed, true);
  assert.equal(crossed.mid, null, "a crossed book has no mid the market is quoting");
  assert.equal(crossed.spr, null);

  const empty = m.buildModel(book([], []));
  assert.equal(empty.empty, true);
  assert.equal(empty.bb, null);
});

test("imbalance counts only size within 5¢ of the mid, inclusive", () => {
  const md = m.buildModel(book(
    [[5000, 10 * C], [4550, 20 * C], [4549, 999 * C]],
    [[5100, 7 * C], [5550, 3 * C], [5551, 999 * C]],
  ));
  // mid 5050: bids >= 4550, asks <= 5550
  assert.equal(md.imbB, 30 * C);
  assert.equal(md.imbA, 10 * C);
});

test("cumulative depth at a price reads the prefix sum by binary search", () => {
  const md = m.buildModel(book(
    [[5100, 1 * C], [5000, 2 * C], [4800, 4 * C]],
    [[5200, 1 * C], [5400, 2 * C], [5500, 4 * C]],
  ));
  assert.equal(m.cumBidAt(md.bids, 5101), 0);
  assert.equal(m.cumBidAt(md.bids, 5100), 1 * C);
  assert.equal(m.cumBidAt(md.bids, 4900), 3 * C);
  assert.equal(m.cumBidAt(md.bids, 0), 7 * C);
  assert.equal(m.cumAskAt(md.asks, 5199), 0);
  assert.equal(m.cumAskAt(md.asks, 5450), 3 * C);
  assert.equal(m.cumAskAt(md.asks, 10000), 7 * C);
});

// ---------------------------------------------------------------------------
// grid and diff
// ---------------------------------------------------------------------------

test("the grid is detected from the prices and only ever gets finer", () => {
  const cent = m.buildModel(book([[5100, C], [5000, C]], [[5200, C]]));
  assert.equal(m.detectGrid(cent, null), 100);
  const tenth = m.buildModel(book([[8770, C]], [[8810, C]]));
  assert.equal(m.detectGrid(tenth, null), 10);
  assert.equal(m.detectGrid(cent, 10), 10, "sticky: a 0.1¢ book stays 0.1¢");
});

test("diff reports every changed price, with absent as zero, per side", () => {
  const a = m.buildModel(book([[5100, 5 * C], [5000, 3 * C]], [[5200, 2 * C]]));
  const b = m.buildModel(book([[5100, 2 * C], [4900, 1 * C]], [[5200, 2 * C], [5300, 4 * C]]));
  const d = m.diffLevels(a, b);
  const key = (x) => `${x.side}:${x.p}:${x.oldQ}->${x.newQ}`;
  assert.deepEqual(d.map(key).sort(), [
    `ask:5300:0->${4 * C}`,
    `bid:4900:0->${1 * C}`,
    `bid:5000:${3 * C}->0`,
    `bid:5100:${5 * C}->${2 * C}`,
  ].sort());
  assert.deepEqual(m.diffLevels(null, b), [], "no previous frame: nothing to mark");
});

// ---------------------------------------------------------------------------
// scales
// ---------------------------------------------------------------------------

test("niceCeil lands on a 1-1.2-1.5-2-2.5-3-4-5-6-8 contract step", () => {
  assert.equal(m.niceCeil(0), 1 * C, "never below one contract");
  assert.equal(m.niceCeil(1 * C), 1 * C);
  assert.equal(m.niceCeil(1 * C + 1), 1.2 * C);
  assert.equal(m.niceCeil(2438 * C + 8874), 2500 * C);
  assert.equal(m.niceCeil(8001 * C), 10000 * C);
  assert.equal(m.niceCeil(6694 * C), 8000 * C);
});

test("a scale grows at once and shrinks only after holding for the whole hold", () => {
  const mem = {};
  assert.equal(m.stepYDomain(mem, 7000 * C, 0), 8000 * C);
  // grows immediately
  assert.equal(m.stepYDomain(mem, 9000 * C, 10), 10000 * C);
  // a smaller need that is NOT under half does nothing, ever
  assert.equal(m.stepYDomain(mem, 6000 * C, 20), 10000 * C);
  assert.equal(m.stepYDomain(mem, 6000 * C, 99999), 10000 * C);
  // under half: holds for 4s, then shrinks
  assert.equal(m.stepYDomain(mem, 2000 * C, 100000), 10000 * C);
  assert.equal(m.stepYDomain(mem, 2000 * C, 103999), 10000 * C);
  assert.equal(m.stepYDomain(mem, 2000 * C, 104000), 2500 * C);
});

test("one render back over the threshold resets the shrink timer", () => {
  const mem = {};
  m.stepYDomain(mem, 9000 * C, 0);                    // 10K
  m.stepYDomain(mem, 2000 * C, 1000);                 // starts the timer
  m.stepYDomain(mem, 6000 * C, 3000);                 // back above half: reset
  assert.equal(m.stepYDomain(mem, 2000 * C, 5500), 10000 * C, "timer restarted at 5500");
  assert.equal(m.stepYDomain(mem, 2000 * C, 9499), 10000 * C);
  assert.equal(m.stepYDomain(mem, 2000 * C, 9500), 2500 * C);
});

test("the size scale fits the ordinary levels and lets walls clamp", () => {
  // two walls among ordinary levels: the scale must not be set by either
  const bids = [[5100, 100 * C], [5000, 71694 * C], [4900, 150 * C], [4800, 20846 * C], [4700, 300 * C]];
  const asks = [[5200, 200 * C], [5300, 250 * C], [5400, 120 * C], [5500, 400 * C], [5600, 180 * C]];
  const md = m.buildModel(book(bids, asks));
  // 10 sizes sorted; nearest-rank 85th percentile (rounded down) = index 7
  assert.equal(m.sizeScaleInput(md), 400 * C);
  const mem = {};
  const S = m.stepSizeScale(mem, md, 0);
  assert.equal(S, m.niceCeil((400 * C) / 0.92));
  assert.ok(71694 * C > S && 20846 * C > S, "both walls clamp");
  assert.ok((100 * C) / S >= 0.2, "even the smallest level is a fifth of the scale, not a hairline");
});

test("only the first 20 levels of each side feed the size scale", () => {
  const deep = [];
  for (let i = 0; i < 40; i++) deep.push([9000 - i * 100, (i < 20 ? 10 : 99999) * C]);
  const md = m.buildModel(book(deep, []));
  assert.equal(m.sizeScaleInput(md), 10 * C, "levels 21+ cannot rescale the bars");
});

test("yTickStep lands on a 1-2-2.5-5 step", () => {
  assert.equal(m.yTickStep(10000 * C), 2500 * C);
  assert.equal(m.yTickStep(8000 * C), 2000 * C);
  assert.equal(m.yTickStep(1500 * C), 500 * C);
});

// ---------------------------------------------------------------------------
// the window
// ---------------------------------------------------------------------------

function ladder(bestBid, bestAsk, n, g = 100) {
  const bids = [];
  const asks = [];
  for (let i = 0; i < n; i++) {
    if (bestBid - i * g > 0) bids.push([bestBid - i * g, 100 * C]);
    if (bestAsk + i * g < 10000) asks.push([bestAsk + i * g, 100 * C]);
  }
  return m.buildModel(book(bids, asks));
}

test("the ideal window holds the Kth level on each side and the touch", () => {
  const md = ladder(5100, 5200, 30);
  const w = m.idealWindow(md, 100, 880); // K = 11
  assert.ok(w.lo <= 5100 - 10 * 100 - 100, "10 levels below the best bid, plus a grid step");
  assert.ok(w.hi >= 5200 + 10 * 100 + 100);
  assert.ok(w.lo >= 0 && w.hi <= 10000);
  assert.equal(w.hi - w.lo, w.span);
});

test("a window is clamped inside 0-100¢ near the edges", () => {
  const md = ladder(100, 200, 30);
  const w = m.idealWindow(md, 100, 880);
  assert.equal(w.lo, 0);
  assert.ok(w.hi <= 10000);
});

test("an empty book gets the whole 0-100 axis", () => {
  const w = m.idealWindow(m.buildModel(book([], [])), 100, 880);
  assert.deepEqual([w.lo, w.hi], [0, 10000]);
});

test("the window widens at once but narrows only after 3s of being enough", () => {
  const mem = {};
  const deep = ladder(5100, 5200, 30);
  const wide = m.stepWindow(mem, deep, 100, 880, 0);
  const thin = ladder(5100, 5200, 3);
  // thin book would fit a narrower window, but not yet
  assert.equal(m.stepWindow(mem, thin, 100, 880, 1000).span, wide.span);
  assert.equal(m.stepWindow(mem, thin, 100, 880, 3999).span, wide.span);
  assert.ok(m.stepWindow(mem, thin, 100, 880, 4000).span < wide.span);
  // and a deep book widens it again instantly
  assert.equal(m.stepWindow(mem, deep, 100, 880, 4001).span, wide.span);
});

test("the window does not move while the book stays in its middle half", () => {
  const mem = {};
  const a = m.stepWindow(mem, ladder(5100, 5200, 30), 100, 880, 0);
  const b = m.stepWindow(mem, ladder(5200, 5300, 30), 100, 880, 10);
  assert.equal(b.lo, a.lo, "a one-cent move inside the middle half: no camera move");
  const far = m.stepWindow(mem, ladder(7100, 7200, 30), 100, 880, 20);
  assert.ok(far.lo > a.lo, "the book left the window: recentre");
});

// ---------------------------------------------------------------------------
// sweep and formatting
// ---------------------------------------------------------------------------

test("a sweep walks the touch through level i: qty, weighted average, worst", () => {
  const md = m.buildModel(book([], [[5200, 10 * C], [5300, 30 * C], [5500, 60 * C]]));
  const s = m.sweep(md.asks, 1);
  assert.equal(s.qty, 40 * C);
  assert.equal(s.levels, 2);
  assert.equal(s.worst, 5300);
  assert.equal(s.avg, (5200 * 10 + 5300 * 30) / 40);
  assert.equal(m.sweep(md.asks, 99).levels, 3, "past the end clamps to the book");
  assert.equal(m.sweep([], 0), null);
});

test("fmtQtyParts splits fmtQty's exact string for decimal alignment", async () => {
  const f = await import(jsUrl("core/format.js"));
  for (const v of [0, 1, 18 * C + 788, 2438 * C + 8874, 1234567 * C, 5, -(3 * C + 5000)]) {
    const p = m.fmtQtyParts(v);
    assert.equal(p.i + p.f, f.fmtQty(v), `parts of ${v} must rejoin to fmtQty`);
  }
  assert.deepEqual(m.fmtQtyParts(2438 * C + 8874), { i: "2,438", f: ".8874" });
});

test("short contract labels for axes", () => {
  assert.equal(m.fmtContractsShort(2500 * C), "2.5K");
  assert.equal(m.fmtContractsShort(10000 * C), "10K");
  assert.equal(m.fmtContractsShort(150 * C), "150");
  assert.equal(m.fmtContractsShort(1200000 * C), "1.2M");
  assert.equal(m.fmtContractsShort(1.5 * C), "1.5");
});
