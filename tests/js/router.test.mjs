/* tests/js/router.test.mjs — core/router.js path handling and mounting.

   `normalize()` and `match()` are private, so every assertion here goes
   through the public door — navigate() — which is also the only door
   production code uses. What is pinned: a path with a query, a hash, a
   trailing slash or no leading slash all reach the same page; an unknown path
   lands on MONITOR and rewrites the URL; and /market/:id hands the page a
   DECODED id, because a market_id can contain characters that must be
   percent-encoded to survive a URL.

   The router is registered ONCE for the whole file: register() is
   deliberately idempotent-by-id and there is no unregister, so a per-test
   fixture would be a lie about how main.js uses it. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl, mkEl, locationStub, historyStub, documentStub, runFrame } from "./shim.mjs";

const router = await import(jsUrl("core/router.js"));
const { schedule } = await import(jsUrl("core/state.js"));

// ---------- fixtures ----------

const log = [];

function page(id, path, opts = {}) {
  const p = {
    id,
    path,
    root: id + "-page",
    title: id.toUpperCase(),
    nav: opts.nav !== false,
    mounts: 0,
    unmounts: 0,
    renders: 0,
    lastParams: null,
    mount(params) {
      p.mounts += 1;
      p.lastParams = params;
      log.push("mount:" + id);
    },
    unmount() {
      p.unmounts += 1;
      log.push("unmount:" + id);
    },
    render() {
      p.renders += 1;
      log.push("render:" + id);
    },
  };
  mkEl("section", { id: p.root });
  return p;
}

const monitor = page("monitor", "/");
const arb = page("arb", "/arb");
const pairs = page("pairs", "/pairs");
const market = page("market", "/market/:id", { nav: false });

test("currentPage() is null before anything is mounted", () => {
  assert.equal(router.currentPage(), null);
});

test("register: nav order is registration order; nav:false is hidden from it", () => {
  for (const p of [monitor, arb, pairs, market]) router.register(p);
  assert.deepEqual(
    router.navPages().map((p) => p.id),
    ["monitor", "arb", "pairs"],
  );
  // A second register() of the same id is a no-op, not a duplicate nav tab.
  router.register({ ...arb, path: "/somewhere-else" });
  assert.deepEqual(
    router.navPages().map((p) => p.id),
    ["monitor", "arb", "pairs"],
  );
});

test("navigate mounts the page, titles the document and shows only its root", () => {
  router.navigate("/arb");
  assert.equal(router.currentPage().id, "arb");
  assert.equal(arb.mounts, 1);
  assert.equal(documentStub.title, "ARB · ARB");
  assert.equal(documentStub.getElementById("arb-page").hidden, false);
  assert.equal(documentStub.getElementById("pairs-page").hidden, true);
  assert.equal(locationStub.pathname, "/arb");
});

test("leaving a page unmounts it and hides its root, in that order", () => {
  router.navigate("/arb");
  log.length = 0;
  router.navigate("/pairs");
  assert.deepEqual(log, ["unmount:arb", "mount:pairs"]);
  assert.equal(documentStub.getElementById("arb-page").hidden, true);
  assert.equal(documentStub.getElementById("pairs-page").hidden, false);
});

test("normalize: trailing slash, query, hash and a missing leading slash", () => {
  for (const p of ["/pairs", "/pairs/", "/pairs?q=KX#top", "pairs", "/pairs/?q=1"]) {
    router.navigate("/arb"); // move away so each case is a real transition
    router.navigate(p);
    assert.equal(router.currentPage().id, "pairs", "path " + JSON.stringify(p));
    assert.equal(locationStub.pathname, "/pairs", "url for " + JSON.stringify(p));
  }
});

test('normalize keeps "/" as "/" rather than emptying it', () => {
  router.navigate("/arb");
  router.navigate("/");
  assert.equal(router.currentPage().id, "monitor");
  assert.equal(locationStub.pathname, "/");
});

test("an unknown path lands on MONITOR and rewrites the URL to /", () => {
  router.navigate("/arb");
  router.navigate("/no-such-page");
  assert.equal(router.currentPage().id, "monitor");
  assert.equal(locationStub.pathname, "/");
  const last = historyStub.entries[historyStub.entries.length - 1];
  assert.equal(last.kind, "replace"); // the bad URL is replaced, not stacked
});

test("an unknown path does not recurse when / itself is unmatched-shaped", () => {
  // "/" resolves, so the fallback terminates after exactly one hop.
  router.navigate("/nope/deeper/still");
  assert.equal(router.currentPage().id, "monitor");
});

test("/market/:id hands the page the param", () => {
  router.navigate("/market/KXPRES-26");
  assert.equal(router.currentPage().id, "market");
  assert.deepEqual(market.lastParams, { id: "KXPRES-26" });
});

test("/market/:id decodes a percent-encoded id", () => {
  router.navigate("/market/" + encodeURIComponent("KX A/B-26"));
  assert.equal(locationStub.pathname, "/market/KX%20A%2FB-26");
  assert.deepEqual(market.lastParams, { id: "KX A/B-26" });
});

test("a malformed percent-escape falls back to the raw segment, not a throw", () => {
  assert.throws(() => decodeURIComponent("%E0%A4%A")); // the input really is malformed
  router.navigate("/market/%E0%A4%A");
  assert.equal(router.currentPage().id, "market");
  assert.deepEqual(market.lastParams, { id: "%E0%A4%A" });
});

test("re-navigating to the same page and params does not remount", () => {
  router.navigate("/market/AAA");
  const at = market.mounts;
  const un = market.unmounts;
  router.navigate("/market/AAA");
  assert.equal(market.mounts, at, "same id: no remount");
  router.navigate("/market/BBB");
  assert.equal(market.mounts, at + 1, "changed id: remount");
  assert.deepEqual(market.lastParams, { id: "BBB" });
  // A param change is a full unmount/remount of the SAME page — it is not an
  // in-place param update. pages/market.js re-navigates with {replace:true}
  // on every DES arrow, so that is one unmount+mount per arrow key, and
  // core/router.js's restoreFocus() guard exists precisely because of it.
  assert.equal(market.unmounts, un + 1);
  assert.equal(documentStub.getElementById("market-page").hidden, false);
});

test("the route separator is real: /market/a/b is not a market id", () => {
  router.navigate("/market/a/b");
  assert.equal(router.currentPage().id, "monitor");
});

test("a path that merely starts with a route is not that route", () => {
  router.navigate("/pairseses");
  assert.equal(router.currentPage().id, "monitor");
});

test("the rAF renderer the router registers paints only the MOUNTED page", () => {
  router.navigate("/arb");
  arb.renders = 0;
  pairs.renders = 0;
  schedule("arb", "pairs"); // a background page's data arrived too
  runFrame();
  assert.equal(arb.renders, 1);
  assert.equal(pairs.renders, 0, "a hidden page must not paint");
});

test("navigate dispatches arb:navigate so the keys strip can follow", () => {
  const seen = [];
  documentStub.addEventListener("arb:navigate", (e) => seen.push(e.detail.id));
  router.navigate("/pairs");
  router.navigate("/arb");
  assert.deepEqual(seen, ["pairs", "arb"]);
});

test("navigate({replace:true}) replaces instead of pushing", () => {
  router.navigate("/pairs");
  historyStub.entries.length = 0;
  router.navigate("/arb", { replace: true });
  assert.deepEqual(
    historyStub.entries.map((e) => e.kind),
    ["replace"],
  );
});
