/* tests/js/format.test.mjs — core/format.js, the tick and Qty formatters.

   These are correctness tests, not cosmetics. Prices are integer ticks of
   $0.0001 and quantities are integer units of 1e-4 contracts (CLAUDE.md), so
   every one of these functions is a unit conversion: a misplaced factor of
   100 renders a 55.50¢ quote as $55.50 and nobody notices until sizing does.

   core/format.js imports cleanly with no DOM, but the shim is imported anyway
   so a future formatter that reaches for `document` fails loudly here rather
   than only in a browser. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl } from "./shim.mjs";

const f = await import(jsUrl("core/format.js"));

test("fmtCents: ticks are $0.0001, so a cent is 100 ticks", () => {
  assert.equal(f.fmtCents(5550), "55.50"); // $0.555 == 55.50¢
  assert.equal(f.fmtCents(100), "1.00"); // 1¢
  assert.equal(f.fmtCents(1), "0.01"); // one tick == one hundredth of a cent
  assert.equal(f.fmtCents(10000), "100.00"); // $1.00, the top of the book
  assert.equal(f.fmtCents(0), "0.00");
  assert.equal(f.fmtCents(-100), "-1.00"); // an edge can be negative
  assert.equal(f.fmtCents(null), "—");
  assert.equal(f.fmtCents(undefined), "—");
});

test("fmtMid: a whole tick gets 2dp, a half-tick mid gets 3", () => {
  assert.equal(f.fmtMid(5550), "55.50");
  assert.equal(f.fmtMid(5550.5), "55.505");
  assert.equal(f.fmtMid(0), "0.00");
  assert.equal(f.fmtMid(-50.5), "-0.505");
  assert.equal(f.fmtMid(null), "—");
});

test("fmtQty: 1e-4 contract units, grouped, trailing zeros trimmed", () => {
  assert.equal(f.fmtQty(10000), "1"); // one contract
  assert.equal(f.fmtQty(0), "0");
  assert.equal(f.fmtQty(1), "0.0001"); // the smallest unit
  assert.equal(f.fmtQty(1000), "0.1");
  assert.equal(f.fmtQty(10010), "1.001"); // 1.0010 -> trailing zero trimmed
  assert.equal(f.fmtQty(12345), "1.2345");
  assert.equal(f.fmtQty(12340000), "1,234"); // grouping separator
  assert.equal(f.fmtQty(-12345), "-1.2345"); // sign applied outside the padding
  assert.equal(f.fmtQty(-1), "-0.0001");
  assert.equal(f.fmtQty(null), "—");
});

test("fmtSignedQty: zero reads as +0, negatives keep the minus", () => {
  assert.equal(f.fmtSignedQty(0), "+0");
  assert.equal(f.fmtSignedQty(12345), "+1.2345");
  assert.equal(f.fmtSignedQty(-1), "-0.0001");
  assert.equal(f.fmtSignedQty(null), "—");
});

test("fmtMs: 1dp under 10ms, rounded above, and sign-blind about it", () => {
  assert.equal(f.fmtMs(9.94), "9.9ms");
  assert.equal(f.fmtMs(0), "0.0ms");
  assert.equal(f.fmtMs(10), "10ms");
  assert.equal(f.fmtMs(10.6), "11ms");
  assert.equal(f.fmtMs(1500), "1500ms");
  // The `v < 10` test is a magnitude test written as a signed comparison, so
  // EVERY negative value takes the 1dp branch no matter how large. Pinned as
  // the current contract because it surprised a reader once; if the clock-skew
  // display ever needs -1234ms to read as "-1234ms", change the source and
  // this line together.
  assert.equal(f.fmtMs(-0.4), "-0.4ms");
  assert.equal(f.fmtMs(-1234), "-1234.0ms");
  assert.equal(f.fmtMs(null), "—");
  assert.equal(f.fmtMs(Infinity), "—");
  assert.equal(f.fmtMs(NaN), "—");
});

test("fmtAge / fmtAgo: sub-second in ms, then seconds; ago never goes negative", () => {
  assert.equal(f.fmtAge(0), "0ms");
  assert.equal(f.fmtAge(999), "999ms");
  assert.equal(f.fmtAge(1000), "1.0s");
  assert.equal(f.fmtAge(1500), "1.5s");
  assert.equal(f.fmtAgo(1500), "2s");
  assert.equal(f.fmtAgo(0), "0s");
  // A clock that runs backwards must not print "-1s" next to a live book.
  assert.equal(f.fmtAgo(-500), "0s");
});

test("fmtRate: three bands, all suffixed /s", () => {
  assert.equal(f.fmtRate(0), "0.0/s");
  assert.equal(f.fmtRate(9.94), "9.9/s");
  assert.equal(f.fmtRate(99.9), "100/s"); // rounds up across the band edge
  assert.equal(f.fmtRate(150), "150/s");
  assert.equal(f.fmtRate(null), "—");
  assert.equal(f.fmtRate(Infinity), "—");
});

test("fmtUptime: d hh:mm:ss, zero-padded, days only when there are days", () => {
  assert.equal(f.fmtUptime(0), "00:00:00");
  assert.equal(f.fmtUptime(59), "00:00:59");
  assert.equal(f.fmtUptime(61.9), "00:01:01"); // fractional seconds floored
  assert.equal(f.fmtUptime(3600), "01:00:00");
  assert.equal(f.fmtUptime(90061), "1d 01:01:01");
  assert.equal(f.fmtUptime(null), "—");
  assert.equal(f.fmtUptime(Infinity), "—");
});

test("fmtCentsPct: ticks are cents AND implied probability", () => {
  assert.equal(f.fmtCentsPct(5550), "55.50¢ · 55.50%");
  assert.equal(f.fmtCentsPct(50), "0.50¢ · 0.50%");
  assert.equal(f.fmtCentsPct(null), "—");
});

test("fmtCount rounds and groups", () => {
  assert.equal(f.fmtCount(1234.6), "1,235");
  assert.equal(f.fmtCount(0), "0");
  assert.equal(f.fmtCount(null), "—");
  assert.equal(f.fmtCount(NaN), "—");
});

test("fmtSignedCents: + only for strictly positive; zero is bare", () => {
  assert.equal(f.fmtSignedCents(100), "+1.00¢");
  assert.equal(f.fmtSignedCents(0), "0.00¢");
  assert.equal(f.fmtSignedCents(-100), "-1.00¢");
  assert.equal(f.fmtSignedCents(null), "—");
});

test("fmtDollarsFromTicks: 10000 ticks is one dollar, minus outside the $", () => {
  assert.equal(f.fmtDollarsFromTicks(10000), "$1.00");
  assert.equal(f.fmtDollarsFromTicks(0), "$0.00");
  assert.equal(f.fmtDollarsFromTicks(-12345), "-$1.23");
  assert.equal(f.fmtDollarsFromTicks(null), "—");
});

test("fmtClockUtc / fmtTapeTime read UTC, never local time", () => {
  assert.equal(f.fmtClockUtc(0), "00:00:00");
  assert.equal(f.fmtClockUtc(Date.UTC(2024, 0, 1, 13, 4, 5)), "13:04:05");
  assert.equal(f.fmtClockUtc(null), "—");
  assert.equal(f.fmtClockUtc(NaN), "—");
  assert.equal(f.fmtTapeTime(0), "00:00.0");
  assert.equal(f.fmtTapeTime(Date.UTC(2024, 0, 1, 3, 4, 5, 678)), "04:05.6");
});

test("fmtWhen: UTC first, ET in the same string (ET is display only)", () => {
  const s = f.fmtWhen("2026-09-16T03:04:05Z");
  assert.equal(s, "2026-09-16 03:04Z · 2026-09-15 23:04 ET");
  assert.equal(f.fmtWhen(null), "—");
  assert.equal(f.fmtWhen(""), "—");
  assert.equal(f.fmtWhen("not a date"), "—");
});

test("percentile: nearest-rank, sorted numerically, input untouched", () => {
  assert.equal(f.percentile([], 0.5), null);
  assert.equal(f.percentile([3, 1, 2, 4], 0.5), 3);
  assert.equal(f.percentile([3, 1, 2], 1), 3);
  assert.equal(f.percentile([3, 1, 2], 0), 1);
  // A default lexicographic sort would answer 100 for p0 here.
  assert.equal(f.percentile([100, 9, 20], 0), 9);
  const src = [3, 1, 2];
  f.percentile(src, 0.5);
  assert.deepEqual(src, [3, 1, 2]);
});

test("perContractTicks: the 10000 goes back in, and qty 0 is guarded", () => {
  // net_ticks is a $0.0001 total; qty is in 1e-4 contracts.
  assert.equal(f.perContractTicks(5000, 10000), 5000); // 1 contract
  assert.equal(f.perContractTicks(5000, 20000), 2500); // 2 contracts
  assert.equal(f.perContractTicks(-5000, 10000), -5000);
  assert.equal(f.perContractTicks(null, 10000), null);
  assert.equal(f.perContractTicks(5000, 0), null); // no division by zero
  assert.equal(f.perContractTicks(5000, null), null);
  assert.equal(f.perContractTicks(5000, -10000), null);
});
