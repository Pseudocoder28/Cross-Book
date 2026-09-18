/* tests/js/control-model.test.mjs — pages/control-model.js.

   /control is the page whose buttons write. These pin what it SENDS: a unit
   slip here is a wrong risk limit on a live trader, applied with a green
   receipt. Wire units are ticks ($0.0001) and Qty (1e-4 contracts); the
   operator types cents, contracts and dollars. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl } from "./shim.mjs";

const m = await import(jsUrl("pages/control-model.js"));

const CUR = { min_net_ticks: 50, max_qty_per_pair: 1000000, max_notional_ticks: 10000000 };
const same = { minEdge: "0.50", maxPair: "100", maxSpend: "1000" };

test("the fields show the trader's limits in the operator's units", () => {
  assert.equal(m.fmtCentsField(50), "0.50");
  assert.equal(m.fmtCentsField(125), "1.25");
  assert.equal(m.fmtContractsField(1000000), "100");
  assert.equal(m.fmtDollarsField(10000000), "1000");
  assert.equal(m.fmtDollarsField(10005000), "1000.50");
  assert.equal(m.usd(960000), "$96.00");
  assert.equal(m.usd(19999), "$2.00", "rounds to the cent first — never $1.100");
  assert.equal(m.usd(12345678900), "$1,234,567.89");
});

test("untouched fields are not dirty and cannot be applied", () => {
  const d = m.limitsDraft(CUR, same, 0);
  assert.equal(d.valid, true);
  assert.equal(d.canApply, false, "an APPLY that changes nothing is a no-op press");
  assert.deepEqual(d.changes, []);
});

test("cents, contracts and dollars reach the wire as ticks and whole contracts", () => {
  const d = m.limitsDraft(CUR, { minEdge: "0.75", maxPair: "250", maxSpend: "$2,500" }, 0);
  assert.equal(d.canApply, true);
  assert.deepEqual(d.params, { min_net_ticks: 75, max_cts_per_pair: 250, max_notional_ticks: 25000000 });
  assert.deepEqual(d.changes, [
    "min edge 0.50¢ → 0.75¢", "max per pair 100 cts → 250 cts", "max spend $1,000.00 → $2,500.00",
  ]);
});

test("floating point never leaks into a limit: 0.57¢ is 57 ticks, not 56", () => {
  assert.equal(m.limitsDraft(CUR, { ...same, minEdge: "0.57" }, 0).params.min_net_ticks, 57);
  assert.equal(m.limitsDraft(CUR, { ...same, minEdge: "0.29" }, 0).params.min_net_ticks, 29);
  assert.equal(m.limitsDraft(CUR, { ...same, maxSpend: "1000.10" }, 0).params.max_notional_ticks, 10001000);
});

test("a value that is not a whole number of ticks is refused, never rounded", () => {
  const d = m.limitsDraft(CUR, { ...same, minEdge: "0.505" }, 0);
  assert.equal(d.valid, false);
  assert.equal(d.canApply, false);
  assert.equal(d.params, null, "an invalid form has nothing to send");
  assert.match(d.fields.minEdge.error, /nearest 0\.01/);
});

test("each field names its own error", () => {
  const bad = (t) => m.limitsDraft(CUR, { ...same, ...t }, 0);
  assert.ok(bad({ minEdge: "" }).fields.minEdge.error);
  assert.ok(bad({ minEdge: "abc" }).fields.minEdge.error);
  assert.ok(bad({ minEdge: "-1" }).fields.minEdge.error);
  assert.match(bad({ minEdge: "150" }).fields.minEdge.error, /100¢/);
  assert.match(bad({ maxPair: "1.5" }).fields.maxPair.error, /whole/);
  assert.ok(bad({ maxPair: "0" }).fields.maxPair.error);
  assert.ok(bad({ maxSpend: "-5" }).fields.maxSpend.error);
  assert.ok(bad({ maxSpend: "1e3" }).fields.maxSpend.error, "no scientific notation");
  // an error elsewhere does not invent one here
  assert.equal(bad({ minEdge: "abc" }).fields.maxPair.error, null);
});

test("a cap below what is deployed is allowed — it is the panic button — but says what it does", () => {
  const d = m.limitsDraft(CUR, { ...same, maxSpend: "50" }, 960000);
  assert.equal(d.canApply, true);
  assert.match(d.warnings[0], /below the \$96\.00 already deployed/);
  assert.match(d.warnings[0], /Nothing is unwound/);
});

test("the universe box edits the BASE list only, and reports the diff", () => {
  const base = ["KXA", "KXB", "KXC"];
  const d = m.listDraft(base, "kxa\nKXC, kxd  KXD\n", { venue: "kalshi", maxItems: 200 });
  assert.deepEqual(d.items, ["KXA", "KXC", "KXD"], "normalised, de-duplicated, order kept");
  assert.deepEqual(d.added, ["KXD"]);
  assert.deepEqual(d.removed, ["KXB"]);
  assert.equal(d.dupes, 1);
  assert.equal(d.canApply, true);
  const untouched = m.listDraft(base, "KXA\nKXB\nKXC", { venue: "kalshi" });
  assert.equal(untouched.dirty, false);
  assert.equal(untouched.canApply, false);
});

test("an empty Kalshi list is refused; an empty Polymarket list is a real choice", () => {
  assert.ok(m.listDraft(["KXA"], "", { venue: "kalshi" }).error);
  const pm = m.listDraft(["a-b"], "", { venue: "polymarket_us", allowEmpty: true });
  assert.equal(pm.error, null);
  assert.equal(pm.canApply, true);
  assert.deepEqual(pm.removed, ["a-b"]);
  assert.ok(m.listDraft([], Array.from({ length: 101 }, (_, i) => "s" + i).join("\n"),
    { venue: "polymarket_us", maxItems: 100 }).error);
});

test("polymarket slugs are lower-cased, kalshi tickers upper-cased", () => {
  assert.deepEqual(m.listDraft([], "Fed-Oct-2026", { venue: "polymarket_us" }).items, ["fed-oct-2026"]);
  assert.deepEqual(m.listDraft([], "kxbtcy-27jan0100-t20000.00", { venue: "kalshi" }).items,
    ["KXBTCY-27JAN0100-T20000.00"]);
});

test("the watch estimate is base targets plus pairs, times the poll interval", () => {
  const pairs = { confirmed: 69, poll: { interval_s: 2.222 } };
  const pm = { base: new Array(8).fill("x") };
  const e = m.watchEstimate(10, pairs, pm);
  assert.equal(e.targets, 18);
  assert.ok(Math.abs(e.cycleS - 40.0) < 0.05);
  assert.equal(m.watchEstimate(500, pairs, pm).pairs, 69, "cannot watch more than are confirmed");
  assert.equal(m.watchEstimate(10, null, pm), null);
  assert.deepEqual(m.parseWatchN("12"), { n: 12, error: null });
  assert.ok(m.parseWatchN("1.5").error);
  assert.ok(m.parseWatchN("-1").error);
  assert.ok(m.parseWatchN("201").error);
});

test("receipts and confirms land in the section that asked", () => {
  assert.equal(m.sectionOf("paper.limits"), "paper");
  assert.equal(m.sectionOf("recording.stop"), "recorder");
  assert.equal(m.sectionOf("pairs.top"), "watch");
  assert.equal(m.sectionOf("universe.kalshi"), "kalshi");
  assert.equal(m.sectionOf("universe.polymarket"), "polymarket");
  assert.equal(m.sectionOf("jobs.propose"), "jobs");
});

test("only set-replacing actions are previewed first", () => {
  for (const a of ["pairs.top", "pairs.track", "universe.kalshi", "universe.polymarket"]) assert.ok(m.PREVIEW_FIRST.has(a));
  for (const a of ["paper.suspend", "paper.limits", "recording.stop", "jobs.doctor"]) assert.ok(!m.PREVIEW_FIRST.has(a));
});

test("the jobs table shows the newest run of each job, with progress", () => {
  const jobs = [
    { name: "doctor", job_id: "a", started_ts_ns: 1, status: "ok" },
    { name: "doctor", job_id: "b", started_ts_ns: 5, status: "running", phase: "venues", step: 2, total: 4, message: "kalshi" },
    { name: "propose", job_id: "c", started_ts_ns: 3, status: "error" },
  ];
  const latest = m.latestJobs(jobs);
  assert.equal(latest.doctor.job_id, "b");
  assert.equal(latest.propose.job_id, "c");
  assert.deepEqual(m.jobProgress(latest.doctor), { frac: 0.5, where: "venues · 2/4 · kalshi" });
  assert.equal(m.jobProgress({ phase: "", total: 0 }).frac, null, "no total: indeterminate, not 0%");
});
