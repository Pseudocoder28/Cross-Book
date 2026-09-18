/* pages/depth-model.js — the pure half of the DEPTH panel.

   No DOM, no state, no time source: every function takes what it needs and
   returns a value, so the arithmetic that decides what the book LOOKS like can
   be tested in node (tests/js/depth-model.test.mjs) without a browser.

   Units, because a factor of 100 here would misdraw every book on screen:
   prices are integer ticks of $0.0001 (100 ticks = 1¢, range 0-10000, and a
   price IS a probability: 5400 ticks = 54%); quantities are integer units of
   1e-4 contracts ("e4"). Cumulative sums stay integer e4 — exact far past any
   real book (2^53 e4 is ~9e11 contracts). */

export const QTY_UNIT = 10000;       // e4 units per contract
export const TICKS_MAX = 10000;      // $1.00, the top of a binary market
export const IMB_BAND = 500;         // ±5¢ around the mid for the imbalance bar

// ---------------------------------------------------------------------------
// the book, as the panel reads it
// ---------------------------------------------------------------------------

function levels(raw) {
  const out = [];
  let cum = 0;
  if (!Array.isArray(raw)) return out;
  for (const l of raw) {
    if (!l || l[0] == null || l[1] == null) continue;
    const p = l[0];
    const q = l[1];
    cum += q;
    out.push({ p, q, cum });
  }
  return out;
}

/** The book with cumulative depth, the touch, and what can honestly be said
    about the middle. `mid` and `spr` are null whenever there is no two-sided,
    uncrossed book: a mid of a one-sided or crossed book is a number the market
    is not quoting. */
export function buildModel(book) {
  const bids = levels(book && book.bids);   // best (highest) first
  const asks = levels(book && book.asks);   // best (lowest) first
  const bb = bids.length ? bids[0].p : null;
  const ba = asks.length ? asks[0].p : null;
  const crossed = bb != null && ba != null && bb >= ba;
  const two = bb != null && ba != null;
  const mid = two && !crossed ? (bb + ba) / 2 : null;
  const spr = two && !crossed ? ba - bb : null;
  const oneSided = bids.length && !asks.length ? "bids" : asks.length && !bids.length ? "asks" : null;
  const totB = bids.length ? bids[bids.length - 1].cum : 0;
  const totA = asks.length ? asks[asks.length - 1].cum : 0;
  // Imbalance within ±5¢ of the mid, or of the lone touch on a one-sided book.
  const centre = mid != null ? mid : bb != null && ba == null ? bb : ba != null && bb == null ? ba : null;
  let imbB = 0;
  let imbA = 0;
  if (centre != null) {
    for (const l of bids) { if (l.p >= centre - IMB_BAND) imbB += l.q; else break; }
    for (const l of asks) { if (l.p <= centre + IMB_BAND) imbA += l.q; else break; }
  }
  return {
    bids, asks, bb, ba, mid, spr, crossed, oneSided,
    empty: !bids.length && !asks.length,
    totB, totA, imbB, imbA,
  };
}

/** Depth resting at or better than `x` on each side. Cumulative depth is
    monotone, so the deepest point inside a window is always at its edge. */
export function cumBidAt(bids, x) {
  let lo = 0;
  let hi = bids.length;               // bids descending: first index with p < x
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (bids[m].p >= x) lo = m + 1; else hi = m;
  }
  return lo ? bids[lo - 1].cum : 0;
}

export function cumAskAt(asks, x) {
  let lo = 0;
  let hi = asks.length;               // asks ascending: first index with p > x
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (asks[m].p <= x) lo = m + 1; else hi = m;
  }
  return lo ? asks[lo - 1].cum : 0;
}

// ---------------------------------------------------------------------------
// price grid
// ---------------------------------------------------------------------------

function gcd(a, b) {
  a = Math.abs(a);
  b = Math.abs(b);
  while (b) [a, b] = [b, a % b];
  return a;
}

/** The book's price grid in ticks: 100 (1¢), 10 (0.1¢) or 1. Sticky and only
    ever gets finer — a 1¢ book that once quoted 54.5¢ is a 0.1¢ book, and
    flipping back would redraw the gridlines every time that level came and
    went. It sets gridline density and the "N TICKS" spread label; it never
    changes how a price is formatted. */
export function detectGrid(model, prev) {
  let g = 0;
  for (const l of model.bids) g = gcd(g, l.p);
  for (const l of model.asks) g = gcd(g, l.p);
  const found = !g ? 100 : g % 100 === 0 ? 100 : g % 10 === 0 ? 10 : 1;
  return prev ? Math.min(prev, found) : found;
}

// ---------------------------------------------------------------------------
// what changed between two frames
// ---------------------------------------------------------------------------

/** Every price whose resting size differs, per side. Absent counts as zero,
    so a new level is oldQ 0 and a vanished one is newQ 0. The feed does not
    say WHY size left a level — trade or cancel look identical — and nothing
    downstream may pretend otherwise. */
export function diffLevels(prev, next) {
  const out = [];
  if (!prev || !next) return out;
  for (const side of ["bid", "ask"]) {
    const a = new Map();
    for (const l of side === "bid" ? prev.bids : prev.asks) a.set(l.p, l.q);
    const b = new Map();
    for (const l of side === "bid" ? next.bids : next.asks) b.set(l.p, l.q);
    for (const [p, oldQ] of a) {
      const newQ = b.has(p) ? b.get(p) : 0;
      if (newQ !== oldQ) out.push({ side, p, oldQ, newQ });
    }
    for (const [p, newQ] of b) {
      if (!a.has(p) && newQ !== 0) out.push({ side, p, oldQ: 0, newQ });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// scales
// ---------------------------------------------------------------------------

const NICE = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8];

/** Smallest "nice" contract count ≥ v, in e4. Never below one contract, so an
    empty or dust-only book still gets a real axis. */
export function niceCeil(v) {
  const c = Math.max(1, v / QTY_UNIT);
  let k = Math.pow(10, Math.floor(Math.log10(c)));
  for (;;) {
    for (const n of NICE) {
      const cand = n * k;
      if (cand >= c - 1e-9) return Math.round(cand * QTY_UNIT);
    }
    k *= 10;
  }
}

/** One step of a grow-now / shrink-late scale.

    Grows the instant the data needs it — a bar drawn past the end of its
    scale would lie. Shrinks only after the smaller scale has been enough on
    every render for `holdMs`, so a maximum that changes every tick never
    rescales every tick. Returns the new {val, since}. */
export function hystStep(cur, want, grow, shrinkOk, since, now, holdMs) {
  if (cur == null || grow) return { val: want, since: null };
  if (!shrinkOk) return { val: cur, since: null };
  if (since == null) return { val: cur, since: now };
  if (now - since >= holdMs) return { val: want, since: null };
  return { val: cur, since };
}

/** Y domain for the cumulative terrain (shared by both sides). */
export function stepYDomain(mem, need, now) {
  const want = niceCeil(need * 1.08);
  const cur = mem.ydom;
  const r = hystStep(cur, want, cur == null || need > 0.96 * cur, want <= cur / 2, mem.ydomSince, now, 4000);
  mem.ydom = r.val;
  mem.ydomSince = r.since;
  return mem.ydom;
}

/** The input the per-level size scale is fitted to: the 85th percentile
    (nearest rank, rounded down) of the first 20 levels of each side.

    Not the maximum, and not "the max capped at 2.5x the second": a real book
    often carries two or three walls, and fitting the scale to any of them
    flattens every ordinary level to a hairline — measured on a live-shaped
    book, one 71,694-contract wall put the scale at 60K and left 40 of 43 bars
    as 1px stubs. Fitted to the 85th percentile, the ordinary levels read and
    the walls CLAMP, and a clamped bar is drawn with a white-hot cap, so a wall
    is louder than before, not hidden. The exact size is printed beside every
    bar either way. Fixed to the top of the book, never to what is scrolled
    into view, so scrolling cannot rescale. */
export function sizeScaleInput(model) {
  const qs = [];
  for (const arr of [model.bids, model.asks]) {
    for (let i = 0; i < arr.length && i < 20; i++) qs.push(arr[i].q);
  }
  if (!qs.length) return 0;
  qs.sort((a, b) => a - b);
  return qs[Math.floor(0.85 * (qs.length - 1))];
}

export function stepSizeScale(mem, model, now) {
  const t = sizeScaleInput(model);
  const want = niceCeil(t / 0.92);
  const cur = mem.size;
  const r = hystStep(cur, want, cur == null || want > cur, t < 0.45 * cur, mem.sizeSince, now, 8000);
  mem.size = r.val;
  mem.sizeSince = r.since;
  return mem.size;
}

/** Rail barcode scale: the biggest single level in the whole book. */
export function stepRailScale(mem, model, now) {
  let m = 0;
  for (const l of model.bids) if (l.q > m) m = l.q;
  for (const l of model.asks) if (l.q > m) m = l.q;
  const want = niceCeil(m);
  const cur = mem.rail;
  const r = hystStep(cur, want, cur == null || want > cur, want <= cur / 2, mem.railSince, now, 4000);
  mem.rail = r.val;
  mem.railSince = r.since;
  return mem.rail;
}

/** Axis tick step for a domain: the first of dom/4, dom/5, dom/3 that lands
    on a 1-2-2.5-5 step, in e4. */
export function yTickStep(dom) {
  for (const d of [4, 5, 3]) {
    const s = dom / d;
    const c = s / QTY_UNIT;
    const k = Math.pow(10, Math.floor(Math.log10(c)));
    const m = Math.round((c / k) * 1000) / 1000;
    if ([1, 2, 2.5, 5].includes(m)) return s;
  }
  return dom / 4;
}

// ---------------------------------------------------------------------------
// the x window
// ---------------------------------------------------------------------------

export const SPANS = [100, 200, 400, 1000, 2000, 3000, 5000, 10000];

/** Levels that should be visible: out to the Kth level each side, one grid
    step beyond it. Null for an empty book. */
function needRange(model, g, k, span) {
  const { bids, asks } = model;
  if (!bids.length && !asks.length) return null;
  let lo;
  let hi;
  if (bids.length) lo = bids[Math.min(k, bids.length) - 1].p - g;
  if (asks.length) hi = asks[Math.min(k, asks.length) - 1].p + g;
  if (lo == null) lo = asks[0].p - span / 4;
  if (hi == null) hi = bids[0].p + span / 4;
  return [Math.max(0, lo), Math.min(TICKS_MAX, hi)];
}

function centreOf(model) {
  if (model.bb != null && model.ba != null) return (model.bb + model.ba) / 2;
  return model.bb != null ? model.bb : model.ba;
}

/** The window of width `span` centred (on a span/10 grid) on the book. */
export function windowAt(model, span) {
  const c = centreOf(model);
  if (c == null || span >= TICKS_MAX) return { lo: 0, hi: TICKS_MAX, span: TICKS_MAX };
  const step = span / 10;
  const centre = Math.round(c / step) * step;
  let lo = centre - span / 2;
  lo = Math.max(0, Math.min(TICKS_MAX - span, lo));
  return { lo, hi: lo + span, span };
}

/** The narrowest candidate window that holds the Kth level on each side. */
export function idealWindow(model, g, plotW) {
  const k = Math.max(6, Math.min(16, Math.round(plotW / 80)));
  for (const span of SPANS) {
    if (span < 10 * g) continue;
    const need = needRange(model, g, k, span);
    if (!need) return { lo: 0, hi: TICKS_MAX, span: TICKS_MAX };
    const w = windowAt(model, span);
    if (w.lo <= need[0] && w.hi >= need[1]) return w;
  }
  return { lo: 0, hi: TICKS_MAX, span: TICKS_MAX };
}

function holds(w, model) {
  const inside = (p) => p == null || (p >= w.lo && p <= w.hi);
  return inside(model.bb) && inside(model.ba);
}

/** One render's step of the auto-fit window, with hysteresis:
    wider commits now; narrower only after 3s of being enough every render;
    at the same span the camera moves only when the book leaves the middle
    half of the window. Mutates and returns `mem.win`. */
export function stepWindow(mem, model, g, plotW, now) {
  const ideal = idealWindow(model, g, plotW);
  const cur = mem.win;
  if (!cur) {
    mem.win = ideal;
    mem.narrowSince = null;
    return mem.win;
  }
  if (ideal.span > cur.span) {
    mem.win = ideal;
    mem.narrowSince = null;
    return mem.win;
  }
  if (ideal.span < cur.span) {
    if (mem.narrowSince == null) mem.narrowSince = now;
    if (now - mem.narrowSince >= 3000) {
      mem.win = ideal;
      mem.narrowSince = null;
      return mem.win;
    }
  } else {
    mem.narrowSince = null;
  }
  // Same span (or a narrower one still pending): recentre only when needed.
  const c = centreOf(model);
  const q = (cur.hi - cur.lo) / 4;
  const drifted = c != null && (c < cur.lo + q || c > cur.hi - q) && cur.span < TICKS_MAX;
  if (drifted || !holds(cur, model)) {
    const w = windowAt(model, cur.span);
    if (w.lo !== cur.lo) mem.win = w;
  }
  return mem.win;
}

// ---------------------------------------------------------------------------
// sweep: what taking liquidity down to a level would cost
// ---------------------------------------------------------------------------

/** Walk one side from the touch through level `i` inclusive. `avg` is the
    size-weighted average price in ticks; ex-fees, and labelled so. */
export function sweep(side, i) {
  if (!side.length || i < 0) return null;
  const n = Math.min(i, side.length - 1);
  let notional = 0;
  for (let k = 0; k <= n; k++) notional += side[k].p * side[k].q;
  const qty = side[n].cum;
  return { qty, avg: qty ? notional / qty : null, worst: side[n].p, levels: n + 1 };
}

// ---------------------------------------------------------------------------
// formatting that only this panel needs
// ---------------------------------------------------------------------------

const nf = new Intl.NumberFormat("en-US");

/** fmtQty split into its integer and fractional parts, so a column of
    quantities can be decimal-aligned with the fraction set smaller. The two
    parts concatenate to exactly fmtQty's string. */
export function fmtQtyParts(e4) {
  if (e4 == null) return { i: "—", f: "" };
  const neg = e4 < 0;
  const a = Math.abs(e4);
  const whole = Math.floor(a / QTY_UNIT);
  const frac = a % QTY_UNIT;
  return {
    i: (neg ? "-" : "") + nf.format(whole),
    f: frac ? "." + String(frac).padStart(4, "0").replace(/0+$/, "") : "",
  };
}

/** Axis label: 150, 2.5K, 10K, 1.2M contracts. */
export function fmtContractsShort(e4) {
  const c = e4 / QTY_UNIT;
  const trim = (x) => String(Math.round(x * 10) / 10).replace(/\.0$/, "");
  if (c >= 1e6) return trim(c / 1e6) + "M";
  if (c >= 1e3) return trim(c / 1e3) + "K";
  return c >= 10 ? String(Math.round(c)) : trim(c);
}
