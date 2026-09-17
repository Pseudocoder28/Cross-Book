/* tests/js/state.test.mjs — core/state.js: the single rAF render batch.

   The batch is the throttle that keeps a slow render from stalling ingest:
   WebSocket handlers mutate `state` and call schedule(), and the frame calls
   each dirty renderer at most once. Four properties are load-bearing and all
   four are pinned here:

     * coalescing        N schedules of one key == 1 render
     * the re-walk       a renderer that dirties an EARLIER-registered key
                         still paints this frame (monitor -> depth)
     * the bound         a renderer that re-dirties itself cannot spin forever
     * the drop          a key with no renderer is discarded, not leaked

   plus the pointer-down hold, which is what keeps a select-to-copy drag from
   having its anchor text nodes replaced mid-gesture.

   requestAnimationFrame is a queue in the shim: runFrame() runs exactly one
   frame, so "does this land in THIS frame or the NEXT one" is observable. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl, runFrame, pendingFrames, documentStub } from "./shim.mjs";

const { state, schedule, registerRenderer, setSelecting, isSelecting, select, selectRelative } =
  await import(jsUrl("core/state.js"));

/** A renderer that counts its calls. Keys are unique per test: state.js has
    no unregister, exactly as production has none. */
function counter(key, body) {
  const c = { key, calls: 0 };
  registerRenderer(key, () => {
    c.calls += 1;
    if (body) body(c);
  });
  return c;
}

test("many schedules of one key coalesce into one render", () => {
  const a = counter("t-coalesce");
  schedule("t-coalesce");
  schedule("t-coalesce");
  schedule("t-coalesce");
  assert.equal(a.calls, 0, "rendering never happens inside the handler");
  assert.equal(pendingFrames(), 1, "one rAF is requested, not three");
  runFrame();
  assert.equal(a.calls, 1);
});

test("a clean key does not render again on the next frame", () => {
  const a = counter("t-clean");
  schedule("t-clean");
  runFrame();
  assert.equal(a.calls, 1);
  schedule("t-other-unrelated-key");
  runFrame();
  assert.equal(a.calls, 1);
});

test("schedule() with no keys flushes whatever is already dirty", () => {
  const a = counter("t-flush");
  schedule("t-flush");
  runFrame();
  a.calls = 0;
  // The pointer-release path in main.js calls schedule() bare.
  schedule("t-flush");
  schedule();
  runFrame();
  assert.equal(a.calls, 1);
});

test("a renderer that dirties an EARLIER-registered key still paints this frame", () => {
  // Registration order matters: "depth" is registered BEFORE the renderer
  // that dirties it, so a single pass would miss it and the ladder would lag
  // one frame behind the book it is drawn from.
  const depth = counter("t-depth");
  const mon = counter("t-monitor", () => schedule("t-depth"));
  schedule("t-monitor");
  runFrame();
  assert.equal(mon.calls, 1);
  assert.equal(depth.calls, 1, "the re-walk must catch the newly dirtied key");
});

test("registerRenderer replaces the fn for a key instead of stacking two", () => {
  let first = 0;
  let second = 0;
  registerRenderer("t-replace", () => {
    first += 1;
  });
  registerRenderer("t-replace", () => {
    second += 1;
  });
  schedule("t-replace");
  runFrame();
  assert.equal(first, 0, "the old renderer is gone, not merely shadowed");
  assert.equal(second, 1);
});

test("a self-dirtying renderer is bounded, and finishes on a later frame", () => {
  let arm = true;
  const c = counter("t-spin", () => {
    if (arm) schedule("t-spin");
  });
  schedule("t-spin");
  runFrame();
  assert.equal(c.calls, 4, "the pass loop is bounded at 4, not unbounded");
  assert.equal(pendingFrames(), 1, "still dirty: another frame is queued");
  arm = false;
  runFrame();
  assert.equal(c.calls, 5);
  runFrame();
  assert.equal(c.calls, 5, "settled");
});

test("a key nobody registered a renderer for is DROPPED, not leaked", () => {
  schedule("t-ghost"); // e.g. a page module that has not loaded yet
  runFrame();
  // If it were merely left dirty, registering a renderer later and flushing
  // would paint it out of nowhere — and `if (dirty.size) schedule()` would
  // have requested a frame forever.
  assert.equal(pendingFrames(), 0, "a stuck dirty key would re-arm rAF every frame");
  const ghost = counter("t-ghost");
  schedule("t-unrelated");
  runFrame();
  assert.equal(ghost.calls, 0, "the dropped key must not resurrect");
});

test("the pointer-down hold defers DOM writes and loses nothing", () => {
  const a = counter("t-hold");
  setSelecting(true);
  assert.equal(isSelecting(), true);
  schedule("t-hold");
  runFrame();
  assert.equal(a.calls, 0, "a drag must not have its text nodes replaced");
  schedule("t-hold-2nd-key"); // data keeps arriving during the drag
  runFrame();
  assert.equal(a.calls, 0);
  // main.js's endSelecting(): release, then a bare schedule() to flush.
  setSelecting(false);
  schedule();
  runFrame();
  assert.equal(a.calls, 1, "the held flag flushes exactly once on release");
});

test("setSelecting coerces to a boolean", () => {
  setSelecting(0);
  assert.equal(isSelecting(), false);
  setSelecting("yes");
  assert.equal(isSelecting(), true);
  setSelecting(false);
});

// ---------- selection ----------

function seedMarkets(ids) {
  state.markets = ids.map((id) => ({ market_id: id, ticker: id }));
  state.byId = new Map(state.markets.map((m) => [m.market_id, m]));
  state.selectedId = null;
  documentStub.body.classList.remove("has-sel");
}

test("select: unknown id is refused, known id marks the body and schedules", () => {
  seedMarkets(["A", "B", "C"]);
  const des = counter("des");
  assert.equal(select("NOPE"), false);
  assert.equal(state.selectedId, null);
  assert.equal(documentStub.body.classList.contains("has-sel"), false);
  assert.equal(select("B"), true);
  assert.equal(state.selectedId, "B");
  assert.equal(documentStub.body.classList.contains("has-sel"), true);
  runFrame();
  assert.equal(des.calls, 1, "select() dirties the pages that follow the selection");
});

test("selectRelative walks the market list and clamps at both ends", () => {
  seedMarkets(["A", "B", "C"]);
  select("A");
  assert.equal(selectRelative(1), true);
  assert.equal(state.selectedId, "B");
  assert.equal(selectRelative(1), true);
  assert.equal(state.selectedId, "C");
  assert.equal(selectRelative(1), true);
  assert.equal(state.selectedId, "C", "clamped at the end, not wrapped");
  assert.equal(selectRelative(-5), true);
  assert.equal(state.selectedId, "A", "clamped at the start");
});

test("selectRelative with nothing selected starts at the top of the list", () => {
  seedMarkets(["A", "B", "C"]);
  assert.equal(state.selectedId, null);
  assert.equal(selectRelative(1), true);
  assert.equal(state.selectedId, "A");
});

test("selectRelative on an empty universe is false, not a crash", () => {
  seedMarkets([]);
  assert.equal(selectRelative(1), false);
  assert.equal(selectRelative(-1), false);
});
