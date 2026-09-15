/* core/state.js — the one shared state object and the single rAF render batch.

   Nothing in this file touches the network. Rendering NEVER happens straight
   out of a WebSocket handler: handlers mutate `state` and call schedule(),
   the rAF batch calls each dirty renderer at most once per frame. A slow
   render can therefore never stall ingest. */

export const state = {
  runId: null,
  markets: [],               // hello order, volume desc
  byId: new Map(),           // market_id -> market
  books: new Map(),          // market_id -> {bids, asks, valid, reason, age_ms, ts_ms, recvAt}
  selectedId: null,
  stats: null,               // latest stats message
  statsBuf: [],              // last STATS_KEEP stats messages
  latPoints: [],             // [{v: number|null}] 1s median buckets, newest last
  latDomainMax: 100,
  latLastFit: 0,
  status: null,              // latest /api/status payload
  statusAt: 0,               // Date.now() of last successful poll
  conn: "connecting",        // connecting | live | reconnecting | down
  dirtyBooks: new Set(),     // market ids whose book changed since the last batch
  tape: { queue: [], total: 0 },  // ws.js fills, pages/monitor.js drains
  des: { open: false, marketId: null, detail: null, error: null, ctl: null },
  pairs: { open: false, rows: [], idx: 0, filter: "proposed", loading: false, msg: "" },
  arb: { open: false, quotes: [], idx: 0, selectedPair: null },
  paper: { open: false, data: null, idx: 0, selectedId: null, error: null, timer: 0, ctl: null },
};

// ---------- render scheduling: one rAF batch ----------

const dirty = new Set();
const renderers = [];        // [{key, fn}] in registration order
let rafPending = false;
let selecting = false;       // pointer is down: DOM writes are held off

/** Bind a render function to a dirty key. The router does this for each page's
    own `render` under the page id; call it yourself only for EXTRA keys. */
export function registerRenderer(key, fn) {
  const found = renderers.find((r) => r.key === key);
  if (found) found.fn = fn;
  else renderers.push({ key, fn });
}

/** Mark keys dirty and request one rAF batch. schedule() with no keys just
    flushes whatever is already dirty. */
export function schedule(...keys) {
  for (const k of keys) dirty.add(k);
  if (!rafPending) {
    rafPending = true;
    requestAnimationFrame(frame);
  }
}

function frame() {
  rafPending = false;
  // A live re-render replaces the text nodes a drag is anchored in, which
  // destroys the selection mid-gesture. Hold the paint (never the data):
  // dirty flags accumulate and flush the moment the pointer is released.
  if (selecting) return;
  // A renderer may mark another key dirty (the monitor dirties the depth
  // ladder when the selected book moved). Re-walk in registration order
  // until it settles, bounded so a self-scheduling renderer cannot spin.
  for (let pass = 0; pass < 4; pass++) {
    let ran = false;
    for (const r of renderers) {
      if (!dirty.has(r.key)) continue;
      dirty.delete(r.key);
      ran = true;
      r.fn();
    }
    if (!ran) break;
  }
  // Keys nobody registered a renderer for (a page module that is not loaded
  // yet) can never be drawn; drop them rather than leak them forever.
  for (const k of Array.from(dirty)) {
    if (!renderers.some((r) => r.key === k)) dirty.delete(k);
  }
  if (dirty.size) schedule();   // a renderer kept re-dirtying: finish next frame
}

/** Pointer-down pause: while true the rAF batch holds every DOM write so a
    select-to-copy drag cannot have its anchor nodes replaced under it. */
export function setSelecting(v) {
  selecting = !!v;
}

export function isSelecting() {
  return selecting;
}

// ---------- selection: one amber thread through every page ----------

/** Make `id` the selected market. Returns false for an unknown id. */
export function select(id) {
  if (!state.byId.has(id)) return false;
  state.selectedId = id;
  document.body.classList.add("has-sel");
  schedule("monitor", "depth", "des", "market");
  return true;
}

/** Move the selection d rows through the full market list (hello order).
    The monitor page overrides this with its own filtered/sorted order. */
export function selectRelative(d) {
  if (!state.markets.length) return false;
  let i = state.markets.findIndex((m) => m.market_id === state.selectedId);
  i = i < 0 ? 0 : Math.max(0, Math.min(state.markets.length - 1, i + d));
  return select(state.markets[i].market_id);
}
