/* pages/depth.js — the DEPTH panel at the centre of MONITOR.

   A probability scope over an exact ladder:

     strip   state words (aria-live) · numbers (not live)
     hero    best bid · mid as YES-implied probability · best ask
     scope   one canvas: the 0-100¢ rail with every level as a barcode and a
             bracket for the zoomed window, a fan down into the plot, the
             cumulative-depth terrain, and a size-at-price strip under it
     ladder  the full book as DOM, rank by rank, bid ║ ask, decimal-aligned

   THE MOTION RULES, because this is where a pretty book starts lying:
     * Nothing tweens between two sizes or two prices. Bars, terrain and the
       touch snap to the true state on the frame it arrives. Motion is an
       ANNOTATION of a change: added size glows inside its bar and fades,
       removed size leaves a neutral dashed ghost outside the bar and fades,
       a new touch price ignites a column of light where it now is.
     * The only thing that moves continuously is the camera (the zoomed
       window and the y scale), and every mark, gridline and label is
       re-projected through the same interpolated scale in every frame, so
       each frame is a true chart under the axis printed on it.
     * Removed size is never called a trade: the feed cannot tell a trade
       from a cancel, so the ghost says "REMOVED (TRADE OR CANCEL)".
     * Polymarket US is polled. It gets a shutter sweep when a snapshot lands
       and a held-stripe texture once the snapshot is older than a poll cycle
       — never the word LIVE, never motion between polls.
     * The animation loop runs only while something is decaying and idles to
       zero rAF otherwise. Reduced motion never starts it: every effect has a
       static form that holds for a second.

   Rendering goes through registerRenderer("depth") like everything else; the
   loop draws the canvas only and never calls schedule() (state.js re-walks
   dirty keys, so a renderer that schedules itself spins). */

import { $, el, flash, isReducedMotion } from "../core/dom.js";
import { state, schedule } from "../core/state.js";
import { onMessage } from "../core/ws.js";
import { fmtCents, fmtMid, fmtQty, fmtAge, fmtAgo } from "../core/format.js";
import * as M from "./depth-model.js";

const STALE_MS = 5000;             // Kalshi: no update for this long reads QUIET
const PM_OVERDUE_CYCLES = 3;       // Polymarket: OVERDUE past 3 poll cycles
const PM_FALLBACK_CYCLE_S = 30;
const SR_THROTTLE_MS = 5000;
const ARIA_LABEL_MS = 5000;
const FX_SLOTS = 64;
const IGNITE_GAP_MS = 600;
const CAMERA_MS = 260;
const SHUTTER_MS = 320;
const RM_HOLD_MS = 1000;           // reduced motion: a static mark holds this long
const FONT = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';

let mounted = false;

// ---------------------------------------------------------------------------
// per-market memory: scales and windows survive switching away and back
// ---------------------------------------------------------------------------

const mem = new Map();
function memFor(id) {
  let m = mem.get(id);
  if (!m) {
    m = { g: null, win: null, narrowSince: null, ydom: null, ydomSince: null,
      size: null, sizeSince: null, rail: null, railSince: null,
      lastValidMid: null, lastValidAt: 0,
      mv: { bid: null, ask: null }, ign: { bid: -1e9, ask: -1e9 } };
    mem.set(id, m);
  }
  return m;
}

// book-frame arrival times per market (FRAMES/S), bounded
const arrivals = new Map();
onMessage("book", (m) => {
  if (!m || typeof m.market_id !== "string") return;
  let a = arrivals.get(m.market_id);
  if (!a) { a = { ts: new Float64Array(64), i: 0 }; arrivals.set(m.market_id, a); }
  a.ts[a.i] = performance.now();
  a.i = (a.i + 1) & 63;
});
function framesPerSec(id, now) {
  const a = arrivals.get(id);
  if (!a) return 0;
  let n = 0;
  for (const t of a.ts) if (t && now - t <= 5000) n += 1;
  return n / 5;
}

let pmCycle = null;
onMessage("control", (m) => {
  const c = m && m.control && m.control.universe && m.control.universe.polymarket_us
    && m.control.universe.polymarket_us.cycle_s;
  if (typeof c === "number" && c > 0) pmCycle = c;
});

let helloSince = false;            // a re-sync: never diff across it
onMessage("hello", () => {
  helloSince = true;
  for (const id of mem.keys()) if (!state.byId.has(id)) mem.delete(id);
  for (const id of arrivals.keys()) if (!state.byId.has(id)) arrivals.delete(id);
});

// ---------------------------------------------------------------------------
// elements
// ---------------------------------------------------------------------------

const E = {};
function grab() {
  for (const id of ["depth", "depth-title", "depth-stat", "depth-banner", "depth-stats",
    "dp-bb", "dp-ba", "dp-bbmv", "dp-bamv", "dp-bbsub", "dp-basub", "dp-bbimb", "dp-baimb",
    "dp-bblbl", "dp-balbl", "dp-midlbl", "dp-mid", "dp-pct", "dp-midsub", "dp-imbrow", "dp-imb",
    "dp-imbl", "dp-imbr", "dp-scope", "dp-cv", "dp-ov", "dp-cap", "dp-tag", "dp-empty",
    "dp-tip", "dp-scale-b", "dp-scale-a", "dp-reset", "ladder", "dp-sr"]) {
    E[id] = $(id);
  }
  E.imbB = E["dp-imb"].querySelector(".b");
  E.imbA = E["dp-imb"].querySelector(".a");
  E.hero = E["dp-bb"].closest(".dp-hero");
}

function setTxt(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

/** Replace children only when the rendered text differs — for the few
    strings that carry a coloured word (POLLED, the ● of LIVE). */
function setRich(node, parts) {
  const key = parts.map((p) => (typeof p === "string" ? p : p[0] + "\u0001" + p[1])).join("\u0002");
  if (node._rich === key) return;
  node._rich = key;
  node.replaceChildren(...parts.map((p) => (typeof p === "string" ? p : el("span", p[0], p[1]))));
}

// ---------------------------------------------------------------------------
// palette (read from the CSS tokens, so the canvas and the DOM cannot drift)
// ---------------------------------------------------------------------------

const PAL = {};
function readPalette() {
  const cs = getComputedStyle(document.documentElement);
  for (const k of ["bg", "panel", "hair", "raised", "ink", "ink2", "dim", "bid", "ask", "amber",
    "bid-fill", "ask-fill", "bid-glow", "ask-glow", "bid-hot", "ask-hot", "ghost", "grid",
    "held", "amber-hatch"]) {
    PAL[k] = cs.getPropertyValue("--" + k).trim();
  }
}
const RGB = { bid: "47,224,160", ask: "255,79,94", ink: "230,230,230", amber: "255,176,46", dim: "107,114,128" };
const rgba = (k, a) => "rgba(" + RGB[k] + "," + a + ")";

// ---------------------------------------------------------------------------
// the ladder: a pool of rank-indexed rows that only ever grows
// ---------------------------------------------------------------------------

const rows = [];

function qtyCell(side, cls) {
  const c = el("span", cls + " c-" + side);
  c.setAttribute("role", "cell");
  const q = el("span", "q");
  const qi = el("span", "qi");
  const qf = el("span", "qf");
  q.append(qi, qf);
  c.append(q);
  return { c, qi, qf };
}

function makeHalf(side) {
  const cum = qtyCell(side, "cum");
  const qty = qtyCell(side, "qty");
  const bar = el("span", "lb c-" + side);
  bar.setAttribute("aria-hidden", "true");
  const lf = el("i", "lf");
  const ld = el("i", "ld");
  const lcap = el("i", "lcap");
  bar.append(lf, ld, lcap);
  const no = el("span", "no c-" + side);
  const yes = el("span", "yes c-" + side);
  no.setAttribute("role", "cell");
  yes.setAttribute("role", "cell");
  return { cum, qty, bar, lf, ld, lcap, no, yes,
    p: null, q: null, tf: "", clamp: false, ghostEnd: 0, ghostAt: -1e9, anim: null, rmT: 0 };
}

function makeRow(i) {
  const row = el("div", "ladder-row");
  row.setAttribute("role", "row");
  row.dataset.rank = String(i);
  const b = makeHalf("b");
  const a = makeHalf("a");
  const seam = el("span", "dp-seam");
  seam.setAttribute("aria-hidden", "true");
  // Cell order is the copy order (main.js rebuilds a selection as TSV from
  // the children): bid cum, qty, bar, no, yes ║ ask yes, no, bar, qty, cum.
  row.append(b.cum.c, b.qty.c, b.bar, b.no, b.yes, seam, a.yes, a.no, a.bar, a.qty.c, a.cum.c);
  if (i === 0) row.classList.add("r0");
  row.addEventListener("pointermove", (e) => onLadderHover(i, e));
  row.addEventListener("pointerleave", clearLens);
  return { row, b, a, endB: null, endA: null, edge: "" };
}

function ensureRows(n) {
  while (rows.length < n) {
    const r = makeRow(rows.length);
    rows.push(r);
    E.ladder.append(r.row);
  }
}

let trackW = 150;                  // a bar cell's width, for the 2px floor

function setQty(parts, e4) {
  const p = M.fmtQtyParts(e4);
  setTxt(parts.qi, p.i);
  setTxt(parts.qf, p.f);
}

function clearHalf(h) {
  if (h.p == null && h.yes.textContent === "") return;
  for (const n of [h.yes, h.no, h.cum.qi, h.cum.qf, h.qty.qi, h.qty.qf]) setTxt(n, "");
  h.yes.removeAttribute("title");
  h.no.removeAttribute("title");
  if (h.tf !== "scaleX(0)") { h.lf.style.transform = "scaleX(0)"; h.tf = "scaleX(0)"; }
  if (h.clamp) { h.bar.classList.remove("clamp"); h.clamp = false; }
  if (h.anim) { h.anim.cancel(); h.anim = null; }
  h.ld.style.opacity = "0";
  h.p = null;
  h.q = null;
}

/** One side of one ladder row. `mark` is true when this frame may annotate a
    change (no discontinuity since the last one). */
function setHalf(h, side, lvl, S, mark, now) {
  if (!lvl) { clearHalf(h); return; }
  const { p, q, cum } = lvl;
  setTxt(h.yes, fmtCents(p));
  setTxt(h.no, fmtCents(M.TICKS_MAX - p));
  if (h.p !== p) {
    h.yes.title = p + " ticks · NO " + (M.TICKS_MAX - p) + " ticks";
    h.no.title = "NO " + fmtCents(M.TICKS_MAX - p) + "¢ = 100 − YES";
  }
  setQty(h.qty, q);
  setQty(h.cum, cum);
  const frac = S > 0 ? q / S : 0;
  const clamp = frac > 1;
  const s = clamp ? 1 : Math.max(frac, q > 0 ? 2 / Math.max(1, trackW) : 0);
  const tf = "scaleX(" + s.toFixed(4) + ")";
  if (tf !== h.tf) { h.lf.style.transform = tf; h.tf = tf; }
  if (clamp !== h.clamp) { h.bar.classList.toggle("clamp", clamp); h.clamp = clamp; }
  if (mark && h.p === p && h.q != null && h.q !== q) markHalf(h, side, h.q, q, S, now);
  h.p = p;
  h.q = q;
}

/** Annotate a size change on a bar: a glowing segment for size added
    (inside the bar), a neutral dashed ghost for size removed (outside it). */
function markHalf(h, side, oldQ, newQ, S, now) {
  const pct = (v) => Math.max(0, Math.min(100, (v / S) * 100));
  const fresh = newQ > oldQ;
  let from;
  let to;
  if (fresh) {
    from = pct(oldQ);
    to = pct(newQ);
  } else {
    // Consecutive removals merge into one ghost instead of stacking.
    const end = now - h.ghostAt < 520 ? Math.max(oldQ, h.ghostEnd) : oldQ;
    h.ghostEnd = end;
    h.ghostAt = now;
    from = pct(newQ);
    to = pct(end);
  }
  const w = to - from;
  const ld = h.ld;
  ld.className = "ld " + (fresh ? "fr" : "gh");
  const anchor = side === "b" ? "right" : "left";
  const other = side === "b" ? "left" : "right";
  ld.style[anchor] = from.toFixed(2) + "%";
  ld.style[other] = "auto";
  ld.style.width = Math.max(w, w > 0 ? 0.8 : 0).toFixed(2) + "%";
  ld.title = fresh ? "SIZE ADDED" : "REMOVED (TRADE OR CANCEL)";
  if (h.anim) { h.anim.cancel(); h.anim = null; }
  clearTimeout(h.rmT);
  if (isReducedMotion()) {
    ld.style.opacity = "1";
    h.rmT = setTimeout(() => { ld.style.opacity = "0"; }, RM_HOLD_MS);
    flash(h.qty.c, fresh ? "up" : "down");
    return;
  }
  ld.style.opacity = "0";
  if (typeof ld.animate === "function") {
    h.anim = ld.animate([{ opacity: 1 }, { opacity: 0 }],
      { duration: fresh ? 420 : 520, easing: "cubic-bezier(0.2,0,0,1)", fill: "forwards" });
  }
}

const endOfBook = (n) => "╌╌ END OF BOOK · " + n + (n === 1 ? " LVL" : " LVLS") + " ╌╌";

function renderLadder(md, S, win, mark, now, dim) {
  const nb = md ? md.bids.length : 0;
  const na = md ? md.asks.length : 0;
  const n = Math.max(nb, na) + 1;
  ensureRows(n);
  E.ladder.classList.toggle("dim", dim);
  E.ladder.setAttribute("aria-rowcount", String(Math.max(nb, na)));
  // chart edge: the last in-window row on a side, when the side goes on past it
  let edgeB = -1;
  let edgeA = -1;
  if (md && win) {
    for (let i = 0; i < nb; i++) if (md.bids[i].p >= win.lo) edgeB = i; else break;
    for (let i = 0; i < na; i++) if (md.asks[i].p <= win.hi) edgeA = i; else break;
    if (edgeB === nb - 1) edgeB = -1;
    if (edgeA === na - 1) edgeA = -1;
  }
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const show = md != null && i < n;
    if (r.row.hidden === show) r.row.hidden = !show;
    if (!show) continue;
    setHalf(r.b, "b", i < nb ? md.bids[i] : null, S, mark, now);
    setHalf(r.a, "a", i < na ? md.asks[i] : null, S, mark, now);
    const endB = i === nb ? (nb ? endOfBook(nb) : (i === 0 ? "NO BIDS" : null)) : null;
    const endA = i === na ? (na ? endOfBook(na) : (i === 0 ? "NO ASKS" : null)) : null;
    if (endB !== r.endB) { if (endB) r.row.dataset.endB = endB; else delete r.row.dataset.endB; r.endB = endB; }
    if (endA !== r.endA) { if (endA) r.row.dataset.endA = endA; else delete r.row.dataset.endA; r.endA = endA; }
    const edge = (i === edgeB ? "b" : "") + (i === edgeA ? "a" : "");
    if (edge !== r.edge) {
      r.row.classList.toggle("edge-b", i === edgeB);
      r.row.classList.toggle("edge-a", i === edgeA);
      r.row.title = edge ? "CHART EDGE: the scope shows rows down to here" : "";
      r.edge = edge;
    }
  }
}

// ---------------------------------------------------------------------------
// the scope canvas
// ---------------------------------------------------------------------------

let cv = null;
let ctx = null;
let ov = null;
let octx = null;
let dpr = 1;
let geo = null;
let geomDirty = true;
let pats = null;

function setupCanvas() {
  geomDirty = false;
  readPalette();
  dpr = Math.max(1, window.devicePixelRatio || 1);
  const r = E["dp-scope"].getBoundingClientRect();
  const W = Math.max(1, Math.round(r.width));
  const H = Math.max(1, Math.round(r.height));
  for (const c of [cv, ov]) {
    c.width = Math.round(W * dpr);
    c.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const small = W < 820;
  const G = small ? 30 : 36;
  const x0 = 8 + G;
  const pw = Math.max(40, W - 16 - 2 * G);
  const compact = H < 280;
  let y = 3;
  const needleY = y; y += 10;
  const railY = y; const railH = compact ? 8 : 14; y += railH;
  const railLblY = y; if (!compact) y += 11;
  const fanTop = railY + railH;
  y += compact ? 2 : 8;
  const noY = y; y += 12;
  const plotY = y;
  const yesY = H - 16;
  const histBase = yesY - 3;
  const histMax = 30;
  const plotB = histBase - histMax - 6;
  geo = { W, H, small, G, x0, pw, compact, needleY, railY, railH, railLblY, fanTop, noY,
    plotY, plotB, plotH: Math.max(20, plotB - plotY), histBase, histMax, yesY };
  pats = makePatterns();
  const cap = E["dp-cap"];
  cap.style.left = x0 + 4 + "px";
  cap.style.top = plotY + 3 + "px";
  const tag = E["dp-tag"];
  tag.style.right = W - (x0 + pw) + 4 + "px";
  tag.style.top = plotY + 3 + "px";
  const bar = rows.length ? rows[0].b.bar : null;
  if (bar && bar.clientWidth) trackW = bar.clientWidth;
}

function makePatterns() {
  const mk = (size, draw) => {
    const c = document.createElement("canvas");
    c.width = Math.round(size * dpr);
    c.height = Math.round(size * dpr);
    const x = c.getContext("2d");
    x.scale(dpr, dpr);
    draw(x, size);
    const p = ctx.createPattern(c, "repeat");
    if (p && typeof p.setTransform === "function" && typeof DOMMatrix === "function") {
      p.setTransform(new DOMMatrix().scale(1 / dpr));
    }
    return p;
  };
  const diag = (color, width) => (x, s) => {
    x.strokeStyle = color;
    x.lineWidth = width;
    x.beginPath();
    x.moveTo(-1, s + 1); x.lineTo(s + 1, -1);
    x.moveTo(-1, 1); x.lineTo(1, -1);
    x.moveTo(s - 1, s + 1); x.lineTo(s + 1, s - 1);
    x.stroke();
  };
  return {
    askHatch: mk(6, diag(rgba("ask", 0.10), 1)),
    held: mk(4, diag(PAL.held || "rgba(10,10,10,0.55)", 1.5)),
    amber: mk(6, diag(rgba("amber", 0.10), 1)),
    crossed: mk(5, diag(PAL["amber-hatch"] || rgba("amber", 0.18), 1.5)),
  };
}

// cubic-bezier(0.4,0,0.2,1): both ends of a camera move matter
function easeIO(t) {
  if (t <= 0) return 0;
  if (t >= 1) return 1;
  const x1 = 0.4; const x2 = 0.2;
  let u = t;
  for (let i = 0; i < 6; i++) {
    const x = 3 * (1 - u) * (1 - u) * u * x1 + 3 * (1 - u) * u * u * x2 + u * u * u - t;
    const dx = 3 * (1 - u) * (1 - u) * x1 + 6 * (1 - u) * u * (x2 - x1) + 3 * u * u * (1 - x2);
    if (Math.abs(dx) < 1e-6) break;
    u -= x / dx;
  }
  return 3 * (1 - u) * u * u * 1 + u * u * u;   // y1 = 0, y2 = 1
}

// the camera: the window and y scale are the only things that ever ease
const cam = { from: null, to: null, t0: 0 };
function camNow(now) {
  if (!cam.to) return null;
  if (!cam.from) return cam.to;
  const t = isReducedMotion() ? 1 : easeIO(Math.min(1, (now - cam.t0) / CAMERA_MS));
  if (t >= 1) return cam.to;
  const f = cam.from;
  const g = cam.to;
  return { lo: f.lo + (g.lo - f.lo) * t, hi: f.hi + (g.hi - f.hi) * t, y: f.y + (g.y - f.y) * t };
}
function camTarget(target, now, snap) {
  const c = cam.to;
  if (c && c.lo === target.lo && c.hi === target.hi && c.y === target.y) return;
  if (snap || !c || isReducedMotion()) {
    cam.from = null;
    cam.to = target;
    return;
  }
  cam.from = camNow(now);
  cam.to = target;
  cam.t0 = now;
  kick(CAMERA_MS);
}

// effects: a fixed number of slots, each an annotation that decays
let fx = [];
function pushFx(e) {
  const key = e.k + e.side + e.p;
  fx = fx.filter((x) => x.k + x.side + x.p !== key);
  if (fx.length >= FX_SLOTS) fx.shift();
  fx.push(e);
}
function fxAlpha(e, now, tau) {
  const t = now - e.t0;
  if (t < 0) return 0;
  if (isReducedMotion()) return t < RM_HOLD_MS ? 1 : -1;
  if (t > e.dur) return -1;
  return Math.exp(-t / tau);
}

// the loop: runs only while something decays
let raf = 0;
let animUntil = 0;
function kick(ms) {
  if (isReducedMotion() || !mounted) return;
  animUntil = Math.max(animUntil, performance.now() + ms);
  if (!raf) raf = requestAnimationFrame(loop);
}
// Fades are exponential decays over ~450ms and read the same at 30fps as at
// 60, so while only fades run the loop draws every other frame; the two things
// that genuinely travel (the camera and the Polymarket shutter) get every
// frame. Measured on a 9-updates/s book: the loop's raster cost halves.
let lastLoopDraw = 0;
function loop(now) {
  raf = 0;
  if (!mounted) return;
  if (now >= animUntil) {
    drawScope(now);                 // the last frame: expired marks leave
    return;
  }
  const travelling = (cam.from && now - cam.t0 < CAMERA_MS) || now - view.shutterAt < SHUTTER_MS;
  if (travelling || now - lastLoopDraw >= 30) {
    drawScope(now);
    lastLoopDraw = now;
  }
  raf = requestAnimationFrame(loop);
}

// what the scope currently shows
const view = {
  md: null, venue: "kalshi", S: null, rail: null, g: 100, glow: 0, held: false,
  structural: false, crossed: false, reason: null, empty: true, awaiting: false,
  midHist: [], shutterAt: -1e9, heldFor: 0,
};

const X = (p, c) => geo.x0 + ((p - c.lo) / (c.hi - c.lo)) * geo.pw;
const RX = (p) => geo.x0 + (p / M.TICKS_MAX) * geo.pw;
const Y = (q, c) => geo.plotB - (q / c.y) * geo.plotH;

const LABEL_STEPS = [10, 20, 50, 100, 200, 500, 1000, 2000, 2500, 5000];
function labelStep(c, minPx) {
  const ppt = geo.pw / (c.hi - c.lo);
  for (const s of LABEL_STEPS) if (s * ppt >= minPx) return s;
  return 5000;
}
const axisTxt = (p, step) => (step >= 100 ? String(p / 100) : (p / 100).toFixed(1));

function drawScope(now) {
  if (!ctx || !geo) return;
  const g = geo;
  ctx.clearRect(0, 0, g.W, g.H);
  ctx.font = "10px " + FONT;
  ctx.textBaseline = "alphabetic";
  const md = view.md;
  const c = camNow(now) || { lo: 0, hi: M.TICKS_MAX, y: M.niceCeil(0) };
  drawRail(c, md, now);
  if (!g.compact) drawFan(c);
  drawGrid(c);
  if (md && !md.empty) {
    drawWash(c, md);
    if (view.held) drawHeld();
    drawTerrain(c, md, "bid", now);
    drawTerrain(c, md, "ask", now);
    drawRug(c, md);
    drawMid(c, md);
    drawIgnition(c, now);
    drawHist(c, md, now);
    drawEdges(c, md);
    if (view.crossed) drawCrossed(c, md);
    drawShutter(now);
    if (view.structural) drawInvalid();
  }
  drawAxes(c, md);
  // Drop expired effects so the list cannot grow while nothing is watching.
  fx = fx.filter((e) => fxAlpha(e, now, 1) !== -1);
}

function drawRail(c, md, now) {
  const g = geo;
  const x1 = g.x0 + g.pw;
  const base = g.railY + g.railH;
  // ruler
  ctx.fillStyle = PAL.hair;
  ctx.fillRect(g.x0, base, g.pw, 1);
  for (let p = 0; p <= M.TICKS_MAX; p += 500) {
    const x = Math.round(RX(p) * dpr) / dpr;
    ctx.fillStyle = p % 1000 === 0 ? PAL.dim : PAL.hair;
    ctx.fillRect(x, base, 1, p % 1000 === 0 ? 3 : 2);
  }
  // bracket: the window the plot is showing
  const bx0 = RX(c.lo);
  const bx1 = RX(c.hi);
  ctx.fillStyle = rgba("ink", 0.05);
  ctx.fillRect(bx0, g.railY - 1, bx1 - bx0, g.railH + 2);
  ctx.strokeStyle = rgba("ink", 0.38);
  ctx.lineWidth = 1;
  ctx.beginPath();
  const t = g.railY - 1.5;
  const b = base + 1.5;
  ctx.moveTo(bx0 + 3, t); ctx.lineTo(bx0, t); ctx.lineTo(bx0, b); ctx.lineTo(bx0 + 3, b);
  ctx.moveTo(bx1 - 3, t); ctx.lineTo(bx1, t); ctx.lineTo(bx1, b); ctx.lineTo(bx1 - 3, b);
  ctx.stroke();
  // barcode: every level in the book, height √size
  if (md && view.rail) {
    const hmax = g.railH - 1;
    for (const side of ["bid", "ask"]) {
      const L = side === "bid" ? md.bids : md.asks;
      ctx.fillStyle = view.structural ? rgba("dim", 0.8) : rgba(side, 0.85);
      for (const l of L) {
        const h = 1 + (hmax - 1) * Math.sqrt(Math.min(l.q, view.rail) / view.rail);
        ctx.fillRect(Math.round(RX(l.p) * dpr) / dpr, base - h, 1, h);
      }
    }
  }
  // 60s range of the mid, then the needle and its afterimage
  const midOk = md && md.mid != null && !view.structural;
  if (midOk && view.midHist.length) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of view.midHist) { if (s.lo < lo) lo = s.lo; if (s.hi > hi) hi = s.hi; }
    ctx.fillStyle = rgba("ink", 0.35);
    ctx.fillRect(RX(lo) - 1, g.needleY + 8, Math.max(2, RX(hi) - RX(lo) + 2), 1);
  }
  for (const e of fx) {
    if (e.k !== "ndl") continue;
    const a = fxAlpha(e, now, 160);
    if (a <= 0) continue;
    needle(RX(e.p), rgba("ink", 0.45 * a));
  }
  if (midOk) {
    needle(RX(md.mid), PAL.ink);
    const hist = view.midHist;
    if (hist.length) {
      let lo = Infinity;
      let hi = -Infinity;
      for (const s of hist) { if (s.lo < lo) lo = s.lo; if (s.hi > hi) hi = s.hi; }
      const txt = lo === hi ? "60s " + fmtMid(lo) + " FLAT" : "60s " + fmtMid(lo) + "–" + fmtMid(hi);
      ctx.font = "9px " + FONT;
      const w = ctx.measureText(txt).width;
      const nx = RX(md.mid);
      const tx = nx + 8 + w < x1 ? nx + 8 : nx - 8 - w;
      ctx.fillStyle = PAL.ink2;
      ctx.fillText(txt, tx, g.needleY + 7);
      ctx.font = "10px " + FONT;
    }
  } else if (md && md.oneSided) {
    const p = md.bb != null ? md.bb : md.ba;
    needle(RX(p), md.bb != null ? PAL.bid : PAL.ask);
  }
  // labels
  if (!g.compact) {
    ctx.fillStyle = PAL.ink2;
    ctx.font = "9px " + FONT;
    const step = g.small ? 2500 : 1000;
    for (let p = 0; p <= M.TICKS_MAX; p += step) {
      const s = String(p / 100);
      const w = ctx.measureText(s).width;
      const x = Math.min(x1 - w, Math.max(g.x0, RX(p) - w / 2));
      ctx.fillText(s, x, g.railLblY + 9);
    }
    ctx.font = "10px " + FONT;
  }
}

function needle(x, color) {
  const y = geo.needleY + 8;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(x - 3.5, y - 5);
  ctx.lineTo(x + 3.5, y - 5);
  ctx.lineTo(x, y);
  ctx.closePath();
  ctx.fill();
  ctx.fillRect(Math.round(x * dpr) / dpr - 0.5, y, 1, geo.railH + 2);
}

function drawFan(c) {
  const g = geo;
  const top = g.fanTop + 1;
  const bx0 = RX(c.lo);
  const bx1 = RX(c.hi);
  const x1 = g.x0 + g.pw;
  ctx.fillStyle = rgba("ink", 0.028);
  ctx.beginPath();
  ctx.moveTo(bx0, top); ctx.lineTo(bx1, top); ctx.lineTo(x1, g.plotY); ctx.lineTo(g.x0, g.plotY);
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = rgba("ink", 0.12);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(bx0, top); ctx.lineTo(g.x0, g.plotY);
  ctx.moveTo(bx1, top); ctx.lineTo(x1, g.plotY);
  ctx.stroke();
}

function drawGrid(c) {
  const g = geo;
  const span = c.hi - c.lo;
  const ppt = g.pw / span;
  // vertical: minor at the finest of 1¢/5¢/10¢ that is ≥ 6px apart
  let minor = 0;
  for (const s of [view.g < 100 ? view.g : 100, 100, 500, 1000]) {
    if (s * ppt >= 6) { minor = s; break; }
  }
  const major = labelStep(c, 56);
  const start = Math.ceil(c.lo / (minor || major)) * (minor || major);
  for (let p = start; p <= c.hi; p += minor || major) {
    const x = Math.round(X(p, c) * dpr) / dpr;
    const isMajor = p % major === 0;
    ctx.fillStyle = isMajor ? PAL.hair : PAL.grid;
    ctx.fillRect(x, g.plotY, 1, g.plotB - g.plotY);
  }
  // 50¢: even odds
  if (c.lo <= 5000 && c.hi >= 5000) {
    const x = Math.round(X(5000, c) * dpr) / dpr + 0.5;
    ctx.save();
    ctx.strokeStyle = rgba("ink", 0.22);
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(x, g.plotY); ctx.lineTo(x, g.plotB);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = PAL.dim;
    ctx.font = "9px " + FONT;
    // top of the line, unless the caption is there; then halfway down it
    const cap = E["dp-cap"];
    const capRight = g.x0 + 4 + (cap.offsetWidth || 0) + 8;
    const md = view.md;
    const nearMid = md && md.mid != null && !view.structural && Math.abs(X(md.mid, c) - x) < 44;
    const ey = (x + 3 < capRight && x > g.x0 - 40) || nearMid ? g.plotY + (g.plotB - g.plotY) / 2 : g.plotY + 11;
    ctx.fillText("EVEN", x + 3, ey);
    ctx.font = "10px " + FONT;
  }
  // horizontal: cumulative contracts
  const ystep = M.yTickStep(cam.to ? cam.to.y : c.y);
  ctx.fillStyle = PAL.hair;
  for (let q = ystep; q <= c.y + 1; q += ystep) {
    const y = Math.round(Y(q, c) * dpr) / dpr;
    if (y < g.plotY) break;
    ctx.fillRect(g.x0, y, g.pw, 1);
  }
  ctx.fillStyle = PAL.hair;
  ctx.fillRect(g.x0, g.plotB, g.pw, 1);
}

function drawWash(c, md) {
  if (md.bb == null || md.ba == null || md.crossed) return;
  const g = geo;
  const a = X(md.bb, c);
  const b = X(md.ba, c);
  ctx.fillStyle = rgba("ink", 0.035);
  ctx.fillRect(a, g.plotY, b - a, g.plotB - g.plotY);
}

/** The stepped cumulative silhouette of one side, touch outward. Returns the
    x of its last drawn level (the END of the book when it is in view). */
function terrainPath(c, L, side) {
  const g = geo;
  const path = new Path2D();
  const out = new Path2D();
  if (!L.length) return null;
  const x0 = X(L[0].p, c);
  path.moveTo(x0, g.plotB);
  path.lineTo(x0, Y(L[0].cum, c));
  out.moveTo(x0, g.plotB);
  out.lineTo(x0, Y(L[0].cum, c));
  let lastX = x0;
  const pastEdge = (x) => (side === "bid" ? x < g.x0 - 4 : x > g.x0 + g.pw + 4);
  for (let i = 1; i < L.length; i++) {
    const x = X(L[i].p, c);
    const yPrev = Y(L[i - 1].cum, c);
    path.lineTo(x, yPrev);
    out.lineTo(x, yPrev);
    path.lineTo(x, Y(L[i].cum, c));
    out.lineTo(x, Y(L[i].cum, c));
    lastX = x;
    if (pastEdge(x)) break;
  }
  path.lineTo(lastX, g.plotB);
  out.lineTo(lastX, g.plotB);
  path.closePath();
  return { path, out, lastX };
}

function drawTerrain(c, md, side, now) {
  const g = geo;
  const L = side === "bid" ? md.bids : md.asks;
  if (!L.length) return;
  const t = terrainPath(c, L, side);
  if (!t) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(g.x0, g.plotY - 2, g.pw, g.plotB - g.plotY + 3);
  ctx.clip();
  const dim = view.structural;
  const grad = ctx.createLinearGradient(0, g.plotY, 0, g.plotB);
  if (dim) {
    grad.addColorStop(0, rgba("dim", 0.12));
    grad.addColorStop(1, rgba("dim", 0.03));
  } else {
    grad.addColorStop(0, rgba(side, 0.24));
    grad.addColorStop(0.55, rgba(side, 0.10));
    grad.addColorStop(1, rgba(side, 0.03));
  }
  ctx.fillStyle = grad;
  ctx.fill(t.path);
  if (side === "ask" && pats.askHatch && !dim) {
    ctx.fillStyle = pats.askHatch;
    ctx.fill(t.path);
  }
  // outline, with a glow that goes matte in 1 Hz steps as the book ages
  ctx.lineJoin = "miter";
  if (dim) {
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = PAL.dim;
    ctx.lineWidth = 1.25;
    ctx.stroke(t.out);
    ctx.setLineDash([]);
  } else {
    const s = view.glow;
    if (s > 0) {
      ctx.strokeStyle = rgba(side, 0.10 * s);
      ctx.lineWidth = 5;
      ctx.stroke(t.out);
      ctx.strokeStyle = rgba(side, 0.28 * s);
      ctx.lineWidth = 2.75;
      ctx.stroke(t.out);
    }
    ctx.strokeStyle = side === "bid" ? PAL.bid : PAL.ask;
    ctx.lineWidth = 1.5;
    ctx.stroke(t.out);
    // risers that just changed flare, then settle back to the outline
    for (const e of fx) {
      if (e.k !== "rs" || e.side !== side) continue;
      const a = fxAlpha(e, now, 140);
      if (a <= 0) continue;
      const i = L.findIndex((l) => l.p === e.p);
      if (i < 0) continue;
      const x = X(L[i].p, c);
      const yTop = Y(L[i].cum, c);
      const yBot = i === 0 ? g.plotB : Y(L[i - 1].cum, c);
      ctx.strokeStyle = rgba(e.up ? side : "ink", (e.up ? 0.95 : 0.6) * a);
      ctx.lineWidth = 1.5 + 2.5 * a;
      ctx.beginPath();
      ctx.moveTo(x, yBot); ctx.lineTo(x, yTop);
      ctx.stroke();
    }
    // level corners
    const ppt = g.pw / (c.hi - c.lo);
    if (ppt * view.g >= 5) {
      ctx.fillStyle = side === "bid" ? PAL.bid : PAL.ask;
      for (const l of L) {
        const x = X(l.p, c);
        if (x < g.x0 - 3 || x > g.x0 + g.pw + 3) continue;
        ctx.fillRect(x - 1.25, Y(l.cum, c) - 1.25, 2.5, 2.5);
      }
    }
  }
  ctx.restore();
  // END of book, when it is in view
  const inView = t.lastX >= g.x0 && t.lastX <= g.x0 + g.pw;
  if (inView) {
    const x = Math.round(t.lastX * dpr) / dpr + 0.5;
    ctx.save();
    ctx.strokeStyle = rgba("ink", 0.35);
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(x, g.plotB); ctx.lineTo(x, g.plotB - 14);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = PAL.dim;
    ctx.font = "9px " + FONT;
    const w = ctx.measureText("END").width;
    ctx.fillText("END", side === "bid" ? x - w - 3 : x + 3, g.plotB - 5);
    ctx.font = "10px " + FONT;
  }
}

function drawRug(c, md) {
  const g = geo;
  for (const side of ["bid", "ask"]) {
    const L = side === "bid" ? md.bids : md.asks;
    ctx.fillStyle = view.structural ? rgba("dim", 0.7) : rgba(side, 0.6);
    for (let i = 0; i < L.length; i++) {
      const x = X(L[i].p, c);
      if (x < g.x0 || x > g.x0 + g.pw) continue;
      const xr = Math.round(x * dpr) / dpr;
      if (i === 0) {
        ctx.fillStyle = view.structural ? PAL.dim : side === "bid" ? PAL.bid : PAL.ask;
        ctx.fillRect(xr - 1, g.plotB - 6, 2, 7);
        ctx.fillStyle = view.structural ? rgba("dim", 0.7) : rgba(side, 0.6);
      } else {
        ctx.fillRect(xr, g.plotB - 3, 1, 4);
      }
    }
  }
}

function drawMid(c, md) {
  if (md.mid == null || view.structural) return;
  const g = geo;
  const x = Math.round(X(md.mid, c) * dpr) / dpr + 0.5;
  ctx.save();
  ctx.strokeStyle = rgba("ink", 0.42);
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(x, g.plotY + 14); ctx.lineTo(x, g.plotB);
  ctx.stroke();
  ctx.restore();
  ctx.fillStyle = PAL.ink2;
  ctx.font = "9px " + FONT;
  const w = ctx.measureText("MID").width;
  const capRight = g.x0 + 4 + (E["dp-cap"].offsetWidth || 0) + 6;
  const tag = E["dp-tag"];
  const tagLeft = tag.textContent ? g.x0 + g.pw - 4 - tag.offsetWidth - 6 : Infinity;
  const clash = x - w / 2 < capRight || x + w / 2 > tagLeft;
  ctx.fillText("MID", x - w / 2, clash ? g.plotY + 25 : g.plotY + 11);
  ctx.font = "10px " + FONT;
}

function drawIgnition(c, now) {
  const g = geo;
  for (const e of fx) {
    if (e.k !== "ign") continue;
    const a = fxAlpha(e, now, 170);
    if (a <= 0) continue;
    const x = X(e.p, c);
    if (x < g.x0 - 6 || x > g.x0 + g.pw + 6) continue;
    const gr = ctx.createLinearGradient(x - 7, 0, x + 7, 0);
    gr.addColorStop(0, rgba(e.side, 0));
    gr.addColorStop(0.5, rgba(e.side, 0.55 * a));
    gr.addColorStop(1, rgba(e.side, 0));
    ctx.fillStyle = gr;
    ctx.fillRect(x - 7, g.plotY, 14, g.plotB - g.plotY);
    const vg = ctx.createLinearGradient(0, g.plotY, 0, g.plotB);
    vg.addColorStop(0, rgba(e.side, 0));
    vg.addColorStop(1, rgba(e.side, 0.9 * a));
    ctx.fillStyle = vg;
    ctx.fillRect(Math.round(x * dpr) / dpr - 0.5, g.plotY, 1.5, g.plotB - g.plotY);
  }
}

function drawHist(c, md, now) {
  const g = geo;
  const S = view.S;
  if (!S) return;
  const base = g.histBase;
  const hmax = g.histMax;
  const ppt = g.pw / (c.hi - c.lo);
  const w = Math.max(1, Math.min(6, ppt * view.g * 0.55));
  ctx.fillStyle = PAL.hair;
  ctx.fillRect(g.x0, base + 1, g.pw, 1);
  const hOf = (q) => (q <= 0 ? 0 : Math.max(2, Math.min(1, q / S) * hmax));
  for (const side of ["bid", "ask"]) {
    const L = side === "bid" ? md.bids : md.asks;
    for (const l of L) {
      const x = X(l.p, c);
      if (x < g.x0 - w || x > g.x0 + g.pw + w) continue;
      const h = hOf(l.q);
      ctx.fillStyle = view.structural ? rgba("dim", 0.6) : rgba(side, 0.62);
      ctx.fillRect(x - w / 2, base + 1 - h, w, h);
      if (l.q > S && !view.structural) {
        ctx.fillStyle = side === "bid" ? PAL["bid-hot"] : PAL["ask-hot"];
        ctx.fillRect(x - w / 2, base + 1 - h, w, 2);
      }
    }
  }
  // size changes at a price: fresh size glows, removed size ghosts
  for (const e of fx) {
    if (e.k !== "hf" && e.k !== "hg") continue;
    const fresh = e.k === "hf";
    const a = fxAlpha(e, now, fresh ? 140 : 170);
    if (a <= 0) continue;
    const x = X(e.p, c);
    if (x < g.x0 - w || x > g.x0 + g.pw + w) continue;
    const h0 = hOf(e.oldQ);
    const h1 = hOf(e.newQ);
    if (fresh) {
      ctx.fillStyle = rgba(e.side, 0.95 * a);
      ctx.fillRect(x - w / 2 - 0.5, base + 1 - h1, w + 1, Math.max(1.5, h1 - h0));
      ctx.fillStyle = rgba(e.side, 0.25 * a);
      ctx.fillRect(x - w / 2 - 2, base + 1 - h1 - 2, w + 4, h1 - h0 + 2);
    } else {
      ctx.save();
      ctx.strokeStyle = rgba("ink", 0.6 * a);
      ctx.setLineDash([2, 2]);
      ctx.lineWidth = 1;
      ctx.strokeRect(x - w / 2 + 0.5, base + 1 - h0 + 0.5, Math.max(1, w - 1), Math.max(1, h0 - h1 - 1));
      ctx.restore();
    }
  }
}

function edgeLabel(txt, x, y, alignRight) {
  const w = ctx.measureText(txt).width;
  const left = alignRight ? x - w : x;
  ctx.fillStyle = "rgba(0,0,0,0.78)";
  ctx.fillRect(left - 4, y - 9, w + 8, 12);
  ctx.fillStyle = PAL.ink2;
  ctx.fillText(txt, left, y);
}

function drawEdges(c, md) {
  const g = geo;
  ctx.font = "9px " + FONT;
  const sum = (arr) => arr.reduce((a, l) => a + l.q, 0);
  const lv = (n) => (n === 1 ? " LVL" : " LVLS");
  const below = md.bids.filter((l) => l.p < c.lo);
  const above = md.asks.filter((l) => l.p > c.hi);
  const left = below.length
    ? "\u25C2 " + below.length + lv(below.length) + " \u00b7 " + fmtQty(sum(below)) + " CTS BELOW " + fmtCents(Math.round(c.lo))
    : "";
  const right = above.length
    ? "ABOVE " + fmtCents(Math.round(c.hi)) + " \u00b7 " + fmtQty(sum(above)) + " CTS \u00b7 " + above.length + lv(above.length) + " \u25B8"
    : "";
  const y = g.plotB - 16;
  if (left) edgeLabel(left, g.x0 + 4, y, false);
  if (right) {
    // on a narrow panel the two can meet in the middle: the right one steps up
    const lw = left ? ctx.measureText(left).width + 16 : 0;
    const rw = ctx.measureText(right).width + 8;
    const meet = left && g.x0 + 4 + lw > g.x0 + g.pw - 4 - rw;
    edgeLabel(right, g.x0 + g.pw - 4, meet ? y - 14 : y, true);
  }
  ctx.font = "10px " + FONT;
}

function drawCrossed(c, md) {
  if (md.bb == null || md.ba == null || md.bb < md.ba) return;
  const g = geo;
  const a = X(md.ba, c);
  const b = X(md.bb, c);
  ctx.fillStyle = pats.crossed;
  ctx.fillRect(a, g.plotY, Math.max(2, b - a), g.plotB - g.plotY);
  ctx.strokeStyle = PAL.amber;
  ctx.lineWidth = 1;
  ctx.strokeRect(a + 0.5, g.plotY + 0.5, Math.max(1, b - a - 1), g.plotB - g.plotY - 1);
  ctx.fillStyle = PAL.amber;
  ctx.font = "700 10px " + FONT;
  const txt = "CROSSED " + fmtCents(md.bb - md.ba) + "¢";
  ctx.fillText(txt, Math.min(g.x0 + g.pw - ctx.measureText(txt).width, b + 4), g.plotY + 26);
  ctx.font = "10px " + FONT;
}

function drawHeld() {
  const g = geo;
  if (!pats.held) return;
  ctx.fillStyle = pats.held;
  ctx.fillRect(g.x0, g.plotY, g.pw, g.histBase + 2 - g.plotY);
}

function drawShutter(now) {
  const t = now - view.shutterAt;
  if (t < 0 || t > SHUTTER_MS || isReducedMotion()) return;
  const g = geo;
  const x = g.x0 + (t / SHUTTER_MS) * g.pw;
  const wake = ctx.createLinearGradient(x - 28, 0, x, 0);
  wake.addColorStop(0, rgba("ink", 0));
  wake.addColorStop(1, rgba("ink", 0.09));
  ctx.fillStyle = wake;
  ctx.fillRect(x - 28, g.plotY, 28, g.histBase + 2 - g.plotY);
  ctx.fillStyle = rgba("ink", 0.75);
  ctx.fillRect(Math.round(x * dpr) / dpr, g.plotY, 1, g.histBase + 2 - g.plotY);
}

function drawInvalid() {
  const g = geo;
  if (pats.amber) {
    ctx.fillStyle = pats.amber;
    ctx.fillRect(g.x0, g.plotY, g.pw, g.plotB - g.plotY);
  }
  ctx.fillStyle = PAL.amber;
  ctx.font = "700 11px " + FONT;
  const txt = "INVALID — " + String(view.reason || "").toUpperCase().replace(/_/g, " ");
  const w = ctx.measureText(txt).width;
  ctx.fillText(txt, g.x0 + (g.pw - w) / 2, g.plotY + (g.plotB - g.plotY) / 2);
  ctx.font = "10px " + FONT;
}

function drawAxes(c, md) {
  const g = geo;
  const step = labelStep(c, g.small ? 44 : 56);
  const first = Math.ceil(c.lo / step) * step;
  // touch chips first: they win every collision
  const chips = [];
  ctx.font = "700 10px " + FONT;
  if (md && !md.empty && !view.structural) {
    const midX = md.mid != null ? X(md.mid, c) : null;
    const chip = (p, side) => {
      const txt = fmtCents(p);
      const w = ctx.measureText(txt).width + 8;
      const x = X(p, c);
      let left = x - w / 2;
      if (midX != null) left = side === "bid" ? Math.min(left, midX - 1 - w) : Math.max(left, midX + 1);
      left = Math.max(g.x0 - g.G + 2, Math.min(g.x0 + g.pw + g.G - 2 - w, left));
      chips.push({ left, w, txt, side });
    };
    if (md.bb != null) chip(md.bb, "bid");
    if (md.ba != null) chip(md.ba, "ask");
  }
  ctx.font = "10px " + FONT;
  // YES along the bottom, NO (= 100 − YES) along the top
  ctx.fillStyle = PAL.ink2;
  for (let p = first; p <= c.hi + 0.001; p += step) {
    const x = X(p, c);
    const yes = axisTxt(p, step);
    const w = ctx.measureText(yes).width;
    const lx = x - w / 2;
    const hit = chips.some((k) => lx < k.left + k.w + 3 && lx + w > k.left - 3);
    if (!hit && lx >= g.x0 - g.G && lx + w <= g.x0 + g.pw + g.G) ctx.fillText(yes, lx, g.yesY + 11);
    const no = axisTxt(M.TICKS_MAX - p, step);
    const wn = ctx.measureText(no).width;
    ctx.fillStyle = PAL.dim;
    if (x - wn / 2 >= g.x0 - 4 && x + wn / 2 <= g.x0 + g.pw + 4) ctx.fillText(no, x - wn / 2, g.noY + 9);
    ctx.fillStyle = PAL.ink2;
  }
  ctx.font = "700 9px " + FONT;
  ctx.fillStyle = PAL.dim;
  ctx.fillText("NO¢", 8, g.noY + 9);
  ctx.fillText("YES¢", 8, g.yesY + 11);
  // chips
  ctx.font = "700 10px " + FONT;
  for (const k of chips) {
    ctx.fillStyle = k.side === "bid" ? PAL.bid : PAL.ask;
    ctx.fillRect(k.left, g.yesY + 1, k.w, 13);
    ctx.fillStyle = "#000";
    ctx.fillText(k.txt, k.left + 4, g.yesY + 11);
  }
  // y labels in both gutters
  ctx.font = "9px " + FONT;
  ctx.fillStyle = PAL.ink2;
  const ystep = M.yTickStep(cam.to ? cam.to.y : c.y);
  for (let q = ystep; q <= c.y + 1; q += ystep) {
    const y = Y(q, c);
    if (y < g.plotY + 4) break;
    const s = M.fmtContractsShort(q);
    const w = ctx.measureText(s).width;
    ctx.fillText(s, g.x0 - 5 - w, y + 3);
    ctx.fillText(s, g.x0 + g.pw + 5, y + 3);
  }
  ctx.fillText("0", g.x0 - 5 - ctx.measureText("0").width, g.plotB + 3);
  // size-strip scale, left gutter
  if (view.S) {
    ctx.fillStyle = PAL.dim;
    ctx.fillText("≤" + M.fmtContractsShort(view.S), 8, g.histBase - g.histMax + 8);
    ctx.fillText("SIZE", 8, g.histBase - g.histMax + 18);
  }
  ctx.font = "10px " + FONT;
}

// ---------------------------------------------------------------------------
// the hover sweep lens (pointer only; the keyboard model is untouched)
// ---------------------------------------------------------------------------

let lens = null;                   // {side, i}

function lensFromPrice(md, price) {
  if (!md || md.empty) return null;
  const pivot = md.mid != null ? md.mid : md.bb != null ? md.bb : md.ba;
  const side = md.bids.length && (price <= pivot || !md.asks.length) ? "bid" : "ask";
  const L = side === "bid" ? md.bids : md.asks;
  if (!L.length) return null;
  let i = 0;
  if (side === "bid") { while (i + 1 < L.length && L[i + 1].p >= price) i++; }
  else { while (i + 1 < L.length && L[i + 1].p <= price) i++; }
  return { side, i };
}

function sweepText(md, ln) {
  const L = ln.side === "bid" ? md.bids : md.asks;
  const s = M.sweep(L, ln.i);
  if (!s) return "";
  const worst = fmtCents(s.worst);
  const other = fmtCents(M.TICKS_MAX - s.worst);
  const head = ln.side === "bid"
    ? "SELL YES DOWN TO " + worst + " (NO ASK " + other + ")"
    : "BUY YES UP TO " + worst + " (NO BID " + other + ")";
  return head + " · " + s.levels + (s.levels === 1 ? " LVL" : " LVLS") + " · "
    + fmtQty(s.qty) + " CTS · AVG " + (s.avg / 100).toFixed(3) + "¢ · EX-FEES";
}

function drawLens() {
  if (!octx || !geo) return;
  const g = geo;
  octx.clearRect(0, 0, g.W, g.H);
  const md = view.md;
  const tip = E["dp-tip"];
  for (const r of rows) if (r.row.classList.contains("hl")) r.row.classList.remove("hl");
  if (!lens || !md || md.empty) { tip.hidden = true; return; }
  const L = lens.side === "bid" ? md.bids : md.asks;
  if (lens.i >= L.length) { tip.hidden = true; return; }
  const c = camNow(performance.now());
  const lv = L[lens.i];
  const x = X(lv.p, c);
  const y = Y(lv.cum, c);
  // the swept terrain, touch to here, lit
  const part = terrainPath(c, L.slice(0, lens.i + 1), lens.side);
  if (part) {
    octx.save();
    octx.beginPath();
    octx.rect(g.x0, g.plotY, g.pw, g.plotB - g.plotY + 1);
    octx.clip();
    octx.fillStyle = rgba(lens.side, 0.28);
    octx.fill(part.path);
    octx.restore();
  }
  octx.strokeStyle = rgba("ink", 0.55);
  octx.lineWidth = 1;
  octx.setLineDash([2, 2]);
  octx.beginPath();
  const xr = Math.round(x * dpr) / dpr + 0.5;
  const yr = Math.round(y * dpr) / dpr + 0.5;
  octx.moveTo(xr, g.plotY); octx.lineTo(xr, g.histBase + 1);
  octx.moveTo(g.x0, yr); octx.lineTo(g.x0 + g.pw, yr);
  octx.stroke();
  octx.setLineDash([]);
  octx.fillStyle = lens.side === "bid" ? PAL.bid : PAL.ask;
  octx.fillRect(x - 3, y - 3, 6, 6);
  octx.fillStyle = "#000";
  octx.fillRect(x - 1, y - 1, 2, 2);
  const row = rows[lens.i];
  if (row) row.row.classList.add("hl");
  tip.textContent = sweepText(md, lens);
  tip.hidden = false;
  const tw = tip.offsetWidth;
  const left = Math.max(4, Math.min(g.W - tw - 4, x + (lens.side === "bid" ? -tw - 10 : 10)));
  tip.style.left = left + "px";
  tip.style.top = Math.max(g.plotY + 18, Math.min(g.plotB - 18, y - 22)) + "px";
}

function onScopeHover(e) {
  const md = view.md;
  if (!md || !geo || !cam.to) return;
  const r = E["dp-ov"].getBoundingClientRect();
  const x = e.clientX - r.left;
  const c = camNow(performance.now());
  const price = c.lo + ((x - geo.x0) / geo.pw) * (c.hi - c.lo);
  lens = lensFromPrice(md, price);
  drawLens();
}

function onLadderHover(i, e) {
  const md = view.md;
  if (!md) return;
  const row = rows[i].row;
  const r = row.getBoundingClientRect();
  const side = e.clientX < r.left + r.width / 2 ? "bid" : "ask";
  const L = side === "bid" ? md.bids : md.asks;
  if (i >= L.length) { clearLens(); return; }
  if (lens && lens.side === side && lens.i === i) return;
  lens = { side, i };
  row.title = sweepText(md, lens);
  drawLens();
}

function clearLens() {
  lens = null;
  drawLens();
}

// ---------------------------------------------------------------------------
// the hero
// ---------------------------------------------------------------------------

function heroVal(node, text) {
  if (node.textContent === text) return;
  const had = node.textContent !== "" && node.textContent !== "—";
  node.textContent = text;
  if (had && !isReducedMotion()) flash(node, "neutral");
}

function moveChip(node, mv, now) {
  if (!mv) { setTxt(node, ""); return; }
  const secs = Math.max(0, Math.floor((now - mv.at) / 1000));
  setRich(node, [["mvg", mv.up ? "▲ " : "▼ "],
    (mv.up ? "+" : "−") + fmtCents(mv.amt) + " · " + fmtAgo(secs * 1000)]);
}

function renderHero(md, m, id, now, st) {
  const dash = "—";
  const untrusted = st.structural;
  E.hero.classList.toggle("untrusted", untrusted);
  setTxt(E["dp-bblbl"], "BEST BID · YES" + (untrusted ? " · UNTRUSTED" : ""));
  setTxt(E["dp-balbl"], "BEST ASK · YES" + (untrusted ? " · UNTRUSTED" : ""));
  if (!md || md.empty) {
    for (const k of ["dp-bb", "dp-ba", "dp-mid"]) setTxt(E[k], dash);
    for (const k of ["dp-bbsub", "dp-basub", "dp-bbimb", "dp-baimb", "dp-midsub", "dp-bbmv", "dp-bamv", "dp-imbl", "dp-imbr"]) setTxt(E[k], "");
    E["dp-imbrow"].classList.add("off");
    E["dp-mid"].className = "dp-mid dim";
    setTxt(E["dp-midsub"], md && md.empty ? "EMPTY BOOK · 0 LEVELS" : "");
    return;
  }
  heroVal(E["dp-bb"], md.bb != null ? fmtCents(md.bb) : dash);
  heroVal(E["dp-ba"], md.ba != null ? fmtCents(md.ba) : dash);
  const bq = md.bids.length ? md.bids[0].q : null;
  const aq = md.asks.length ? md.asks[0].q : null;
  setRich(E["dp-bbsub"], md.bb != null
    ? [["qv", fmtQty(bq)], " CTS", ["dp-hno", " · NO ASK " + fmtCents(M.TICKS_MAX - md.bb)]]
    : ["NO BIDS"]);
  setRich(E["dp-basub"], md.ba != null
    ? [["dp-hno", "NO BID " + fmtCents(M.TICKS_MAX - md.ba) + " · "], ["qv", fmtQty(aq)], " CTS"]
    : ["NO OFFERS"]);
  setTxt(E["dp-bbimb"], md.bids.length && md.mid != null ? "±5¢ Σ " + fmtQty(md.imbB) : "");
  setTxt(E["dp-baimb"], md.asks.length && md.mid != null ? "±5¢ Σ " + fmtQty(md.imbA) : "");
  moveChip(E["dp-bbmv"], m ? m.mv.bid : null, now);
  moveChip(E["dp-bamv"], m ? m.mv.ask : null, now);

  const mid = E["dp-mid"];
  const imbRow = E["dp-imbrow"];
  if (untrusted || md.crossed) {
    mid.className = "dp-mid bad";
    heroVal(mid, md.crossed ? "CROSSED" : "INVALID");
    setTxt(E["dp-pct"], "");
    if (md.crossed) {
      setTxt(E["dp-midsub"], "BID " + fmtCents(md.bb) + " ≥ ASK " + fmtCents(md.ba) + " · BY " + fmtCents(md.bb - md.ba) + "¢");
    } else {
      setTxt(E["dp-midsub"], m && m.lastValidMid != null
        ? "LAST VALID MID " + fmtMid(m.lastValidMid) + "% · " + fmtAge(now - m.lastValidAt) + " AGO"
        : "NO VALID MID SEEN YET");
    }
    imbRow.classList.add("off");
    setTxt(E["dp-imbl"], "");
    setTxt(E["dp-imbr"], "");
    return;
  }
  if (md.mid == null) {
    mid.className = "dp-mid dim";
    heroVal(mid, dash);
    setTxt(E["dp-pct"], "");
    setTxt(E["dp-midsub"], "ONE-SIDED · " + (md.oneSided === "bids" ? "BIDS ONLY" : "ASKS ONLY"));
    imbRow.classList.remove("off");
    E.imbB.style.width = md.oneSided === "bids" ? "100%" : "0%";
    E.imbA.style.width = md.oneSided === "asks" ? "100%" : "0%";
    setTxt(E["dp-imbl"], md.oneSided === "bids" ? "100" : "0");
    setTxt(E["dp-imbr"], md.oneSided === "asks" ? "100" : "NO ASKS");
    return;
  }
  const wide = md.spr > 500;
  mid.className = "dp-mid" + (wide ? " wide" : "");
  heroVal(mid, fmtMid(md.mid));
  setTxt(E["dp-pct"], "%");
  const ticks = md.spr / view.g;
  const tickTxt = Number.isInteger(ticks) ? ticks + (ticks === 1 ? " TICK" : " TICKS") : "";
  setTxt(E["dp-midsub"], wide
    ? "WIDE · " + fmtCents(md.bb) + "–" + fmtCents(md.ba) + " · SPR " + fmtCents(md.spr) + "¢"
    : "SPR " + fmtCents(md.spr) + "¢" + (tickTxt ? " · " + tickTxt : "") + " · NO " + fmtMid(M.TICKS_MAX - md.mid) + "%");
  const tot = md.imbB + md.imbA;
  imbRow.classList.toggle("off", tot === 0);
  if (tot > 0) {
    const pb = Math.round((md.imbB / tot) * 100);
    E.imbB.style.width = pb + "%";
    E.imbA.style.width = 100 - pb + "%";
    setTxt(E["dp-imbl"], pb + "");
    setTxt(E["dp-imbr"], 100 - pb + "");
    E["dp-imb"].title = "SIZE WITHIN ±5¢ OF MID — BIDS " + fmtQty(md.imbB) + " : ASKS " + fmtQty(md.imbA);
  }
}

// ---------------------------------------------------------------------------
// strip, head, captions, and the screen-reader mirror
// ---------------------------------------------------------------------------

let pollLanded = null;             // {text, until}

function statusOf(book, id, now) {
  const mkt = id ? state.byId.get(id) : null;
  const venue = mkt && mkt.venue === "polymarket_us" ? "polymarket_us" : "kalshi";
  const age = book ? book.age_ms + (now - book.recvAt) : 0;
  const structural = Boolean(book && book.valid === false && book.reason && book.reason !== "stale");
  const cycle = pmCycle || PM_FALLBACK_CYCLE_S;
  return { mkt, venue, age, structural, cycle, pm: venue === "polymarket_us" };
}

function renderStrip(book, md, st, id, now) {
  const banner = E["depth-banner"];
  const stats = E["depth-stats"];
  const statEl = E["depth-stat"];
  let words;
  let cls;
  const stat = [];
  if (!id) {
    words = ["NO SELECTION"]; cls = "quiet";
  } else if (!book) {
    words = [st.pm ? "AWAITING FIRST SNAPSHOT · POLYMARKET US (POLLED · CYCLE "
      + (pmCycle ? pmCycle.toFixed(1) + "s" : "—") + ")" : "AWAITING BOOK · KALSHI WS"];
    cls = "quiet";
  } else if (state.conn !== "live") {
    words = ["WS RECONNECTING · DATA FROZEN"]; cls = "";
  } else if (st.structural) {
    words = ["INVALID · " + String(book.reason).toUpperCase().replace(/_/g, " ") + " · BOOK UNTRUSTED UNTIL RESYNC"];
    cls = "pulse";
  } else if (st.pm) {
    if (pollLanded && now < pollLanded.until) { words = [pollLanded.text]; cls = "live"; }
    else if (st.age > PM_OVERDUE_CYCLES * st.cycle * 1000) { words = ["OVERDUE"]; cls = ""; }
    else { words = [["dp-polled", "POLLED"]]; cls = "live"; }
  } else if (st.age > STALE_MS || book.reason === "stale") {
    words = ["QUIET"]; cls = "quiet";
  } else {
    words = [["dp-dot", "●"], " LIVE"]; cls = "live";
  }
  setRich(banner, words);
  banner.className = "depth-banner" + (cls ? " " + cls : "");
  if (cls === "pulse" && isReducedMotion()) banner.classList.remove("pulse");

  if (book && md) {
    const parts = [];
    if (st.pm) {
      const over = st.age > PM_OVERDUE_CYCLES * st.cycle * 1000;
      parts.push(over ? "NO SNAPSHOT FOR " + fmtAgo(st.age) : "SNAPSHOT " + fmtAgo(st.age) + " AGO");
      const fill = Math.min(1, st.age / (st.cycle * 1000));
      stats._bar = stats._bar || makePollBar();
      stats._bar.i.style.width = (fill * 100).toFixed(1) + "%";
      stats._bar.el.classList.toggle("over", over);
      const tail = "CYCLE " + (pmCycle ? pmCycle.toFixed(1) + "s" : "—") + " · BIDS "
        + md.bids.length + " · ASKS " + md.asks.length;
      if (stats._mode !== "pm") {
        stats._mode = "pm";
        stats._rich = null;
        stats.replaceChildren(el("span", "", parts[0]), stats._bar.el, el("span", "", tail));
      } else {
        setTxt(stats.firstChild, parts[0]);
        setTxt(stats.lastChild, tail);
      }
    } else {
      const quiet = st.age > STALE_MS;
      const txt = (quiet ? "LAST UPDATE " + fmtAgo(st.age) + " AGO · " : "")
        + "BIDS " + md.bids.length + " LVLS " + fmtQty(md.totB) + " · ASKS "
        + md.asks.length + " LVLS " + fmtQty(md.totA) + " · "
        + framesPerSec(id, now).toFixed(1) + " FRAMES/S";
      if (stats._mode !== "k") { stats._mode = "k"; stats.replaceChildren(); stats._rich = null; }
      setTxt(stats, txt);
    }
    stat.push(st.pm ? "POLYMARKET US · " : "KALSHI · WS · ");
    if (st.pm) stat.push(["dp-polled", "POLLED"], " · ");
    stat.push("AGE " + fmtAge(st.age));
  } else {
    if (stats._mode !== "none") { stats._mode = "none"; stats.replaceChildren(); stats._rich = null; }
    stat.push("—");
  }
  setRich(statEl, stat);
  statEl.classList.toggle("warn", st.structural);
}

function makePollBar() {
  const bar = el("span", "dp-pollbar");
  const i = el("i");
  bar.append(i);
  bar.title = "SNAPSHOT AGE AGAINST ONE POLL CYCLE";
  return { el: bar, i };
}

let ariaAt = 0;
let ariaKey = "";
let ariaState = "";
/** The scope's accessible description. A change of STATE (no book → a book,
    valid → invalid) is announced at once; only a change of window within the
    same state is held to one rebuild per 5s, because the label is read on
    focus and a label that churns with every camera move reads as noise. The
    throttle must never hold a placeholder over a real book: that would tell a
    screen-reader user there is no book while one is on screen. */
function renderAria(md, win, now, st) {
  let cls;
  let key;
  let label;
  if (!md) { cls = "none"; key = cls; label = "Cumulative depth: no book"; }
  else if (md.empty) { cls = "empty"; key = cls; label = "Cumulative depth: empty book"; }
  else {
    cls = st.structural ? "invalid" : "ok";
    key = cls + ":" + (win ? win.lo + "-" + win.hi : "");
    const lo = win ? fmtCents(win.lo) : "0";
    const hi = win ? fmtCents(win.hi) : "100";
    label = "Cumulative depth, window " + lo + " to " + hi + " cents. Bids " + fmtQty(md.totB)
      + " contracts over " + md.bids.length + " levels; asks " + fmtQty(md.totA) + " contracts over "
      + md.asks.length + " levels." + (st.structural ? " Book invalid." : "");
  }
  if (key === ariaKey) return;
  if (cls === ariaState && now - ariaAt < ARIA_LABEL_MS) return;
  ariaKey = key;
  ariaState = cls;
  ariaAt = now;
  E["dp-scope"].setAttribute("aria-label", label);
}

let srPending = "";
let srLast = "";
let srTimer = 0;
let srAt = 0;
function speak(text) {
  if (text === srLast) return;
  srPending = text;
  const now = performance.now();
  const wait = Math.max(0, SR_THROTTLE_MS - (now - srAt));
  if (srTimer) return;
  srTimer = setTimeout(() => {
    srTimer = 0;
    srAt = performance.now();
    srLast = srPending;
    E["dp-sr"].textContent = srPending;
  }, wait);
}

// ---------------------------------------------------------------------------
// the renderer
// ---------------------------------------------------------------------------

let lastId = null;
let lastBook = null;
let lastModel = null;
let lastStructural = false;
let forceDraw = false;
let resetUntil = 0;

function selectionCut(id) {
  fx = [];
  lens = null;
  view.midHist = [];
  view.shutterAt = -1e9;
  pollLanded = null;
  cam.from = null;
  cam.to = null;
  if (octx && geo) octx.clearRect(0, 0, geo.W, geo.H);
  E["dp-tip"].hidden = true;
  if (!isReducedMotion() && cv && typeof cv.animate === "function" && id) {
    cv.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 120, easing: "cubic-bezier(0.2,0,0,1)" });
  }
}

function recordMid(md, now) {
  if (md.mid == null) return;
  const sec = Math.floor(now / 1000);
  const h = view.midHist;
  const last = h[h.length - 1];
  if (last && last.sec === sec) {
    if (md.mid < last.lo) last.lo = md.mid;
    if (md.mid > last.hi) last.hi = md.mid;
  } else {
    h.push({ sec, lo: md.mid, hi: md.mid });
  }
  while (h.length && h[0].sec <= sec - 60) h.shift();
}

export function renderDepth() {
  if (!mounted) return;
  const now = performance.now();
  if (geomDirty) setupCanvas();
  const id = state.selectedId;
  const book = id ? state.books.get(id) : null;
  const mkt = id ? state.byId.get(id) : null;
  const selChanged = id !== lastId;
  if (selChanged) selectionCut(id);

  setTxt(E["depth-title"], "DEPTH — " + (mkt ? mkt.ticker : "—"));
  const st = statusOf(book, id, now);

  // The 1s tick with nothing new: ages, glow and stripe steps only.
  if (!selChanged && book === lastBook && !forceDraw) {
    const md = lastModel;
    renderStrip(book, md, st, id, now);
    if (md && id) renderHero(md, memFor(id), id, now, st);
    const glow = glowStep(st);
    const held = heldNow(st);
    if (glow !== view.glow || held !== view.held) {
      view.glow = glow;
      view.held = held;
      drawScope(now);
    }
    setHeldTag(st, book);
    E["dp-reset"].hidden = now > resetUntil;
    return;
  }
  forceDraw = false;

  const md = book ? M.buildModel(book) : null;
  const m = id ? memFor(id) : null;
  const disc = selChanged || !lastModel || helloSince || st.structural || lastStructural || !md;
  if (lastStructural && !st.structural && md && !selChanged) resetUntil = now + 5000;
  helloSince = false;
  const changes = disc ? [] : M.diffLevels(lastModel, md);

  let win = null;
  if (md && m) {
    m.g = M.detectGrid(md, m.g);
    view.g = m.g;
    win = M.stepWindow(m, md, m.g, geo ? geo.pw : 800, now);
    const need = Math.max(M.cumBidAt(md.bids, win.lo), M.cumAskAt(md.asks, win.hi));
    const prevS = m.size;
    M.stepYDomain(m, need, now);
    M.stepSizeScale(m, md, now);
    M.stepRailScale(m, md, now);
    const scaleTxt = "SIZE ≤" + M.fmtContractsShort(m.size);
    setTxt(E["dp-scale-b"], scaleTxt);
    setTxt(E["dp-scale-a"], scaleTxt);
    if (prevS != null && m.size !== prevS) {
      for (const k of ["dp-scale-b", "dp-scale-a"]) flash(E[k], m.size > prevS ? "up" : "down");
    }
    if (md.mid != null && !st.structural && !md.crossed) { m.lastValidMid = md.mid; m.lastValidAt = now; }
    // touch moves: the hero chips, and ignition where the touch now is
    if (!disc && lastModel) {
      for (const side of ["bid", "ask"]) {
        const was = side === "bid" ? lastModel.bb : lastModel.ba;
        const is = side === "bid" ? md.bb : md.ba;
        if (was != null && is != null && was !== is) {
          m.mv[side] = { up: is > was, amt: Math.abs(is - was), at: now };
          if (now - m.ign[side] >= IGNITE_GAP_MS) {
            m.ign[side] = now;
            pushFx({ k: "ign", side, p: is, t0: now, dur: 520 });
          }
        }
      }
      if (lastModel.mid != null && md.mid != null && lastModel.mid !== md.mid) {
        pushFx({ k: "ndl", side: "", p: lastModel.mid, t0: now, dur: 400 });
      }
    }
    if (!st.structural) recordMid(md, now);
  }

  view.md = md;
  view.venue = st.venue;
  view.S = m ? m.size : null;
  view.rail = m ? m.rail : null;
  view.structural = st.structural;
  view.crossed = Boolean(md && (md.crossed || (book && book.reason === "crossed")));
  view.reason = book ? book.reason : null;
  view.glow = glowStep(st);
  view.held = heldNow(st);

  // annotations for every changed price
  let maxDur = 0;
  if (changes.length) {
    const pm = st.pm;
    const c = cam.to;
    let nNew = 0;
    let nGone = 0;
    for (const ch of changes) {
      if (ch.oldQ === 0) nNew += 1;
      if (ch.newQ === 0) nGone += 1;
      const fresh = ch.newQ > ch.oldQ;
      // Polymarket: each column lights as the shutter passes it
      let t0 = now;
      if (pm && geo && c) {
        const x = X(ch.p, c);
        t0 = now + SHUTTER_MS * Math.max(0, Math.min(1, (x - geo.x0) / geo.pw));
      }
      pushFx({ k: fresh ? "hf" : "hg", side: ch.side, p: ch.p, oldQ: ch.oldQ, newQ: ch.newQ, t0, dur: fresh ? 420 : 520 });
      if (ch.newQ > 0 && ch.oldQ > 0) pushFx({ k: "rs", side: ch.side, p: ch.p, up: fresh, t0, dur: 420 });
      maxDur = Math.max(maxDur, t0 - now + 520);
    }
    if (pm) {
      view.shutterAt = now;
      maxDur = Math.max(maxDur, SHUTTER_MS);
      const prices = new Set(changes.map((x) => x.side + x.p)).size;
      const extra = [];
      if (nNew) extra.push("+" + nNew + " NEW");
      if (nGone) extra.push("−" + nGone + " GONE");
      pollLanded = {
        text: "POLL LANDED · " + prices + " LEVEL" + (prices === 1 ? "" : "S") + " CHANGED"
          + (extra.length ? " (" + extra.join(" · ") + ")" : ""),
        until: now + 3000,
      };
    }
  } else if (st.pm && !disc && book !== lastBook) {
    view.shutterAt = now;
    maxDur = SHUTTER_MS;
    pollLanded = { text: "POLL LANDED · NO LEVELS CHANGED", until: now + 3000 };
  }

  // camera
  if (win && m) camTarget({ lo: win.lo, hi: win.hi, y: m.ydom }, now, selChanged || !lastModel);
  else camTarget({ lo: 0, hi: M.TICKS_MAX, y: M.niceCeil(0) }, now, true);

  // DOM
  renderStrip(book, md, st, id, now);
  renderHero(md, m, id, now, st);
  renderLadder(md, m ? m.size : 1, win, !disc, now, st.structural);
  renderCaption(md, m, win, st, book);
  renderAria(md, win, now, st);
  setHeldTag(st, book);
  E["dp-reset"].hidden = now > resetUntil;
  const empty = !id ? "NO SELECTION · ↑↓ IN THE MONITOR LIST"
    : !book ? "AWAITING BOOK" : md && md.empty ? "EMPTY BOOK · 0 LEVELS · VALID" : "";
  setTxt(E["dp-empty"], empty);
  if (md && md.bb != null && md.ba != null && md.mid != null && !st.structural) {
    speak("Best bid " + fmtCents(md.bb) + ", best ask " + fmtCents(md.ba) + ", spread "
      + fmtCents(md.spr) + ", mid " + fmtMid(md.mid) + " percent.");
  }

  // canvas: the new book is on screen this frame, then decay runs if needed
  drawScope(now);
  if (lens) drawLens();
  if (maxDur) {
    if (isReducedMotion()) {
      setTimeout(() => { forceDraw = true; schedule("depth"); }, RM_HOLD_MS + 20);
    } else {
      kick(maxDur);
    }
  }

  lastId = id;
  lastBook = book;
  lastModel = md;
  lastStructural = st.structural;
}

function glowStep(st) {
  if (st.pm || st.structural) return 0;
  return Math.round(0.6 * Math.max(0, 1 - st.age / STALE_MS) * 10) / 10;
}

function heldNow(st) {
  return st.pm && st.age > st.cycle * 1000;
}

function setHeldTag(st, book) {
  const tag = E["dp-tag"];
  if (!st.pm || !book) { setRich(tag, []); return; }
  const when = typeof book.ts_ms === "number"
    ? new Date(book.ts_ms).toISOString().slice(11, 19) + "Z" : "—";
  const parts = ["SNAPSHOT " + when];
  if (heldNow(st)) parts.push(" · ", ["held", "HELD, NOT OBSERVED " + fmtAgo(st.age)]);
  setRich(tag, parts);
}

function renderCaption(md, m, win, st, book) {
  let txt = "";
  if (md && win && m && !md.empty) {
    txt = "CUM DEPTH · " + fmtCents(win.lo) + "–" + fmtCents(win.hi) + "¢ · Y 0–"
      + M.fmtContractsShort(m.ydom) + " CTS";
    if (book && book.valid === false && !st.structural) txt += " · QUIET";
  }
  setTxt(E["dp-cap"], txt);
}

// ---------------------------------------------------------------------------
// lifecycle
// ---------------------------------------------------------------------------

let ro = null;
let dprQuery = null;

function watchDpr() {
  if (dprQuery) dprQuery.removeEventListener("change", onDpr);
  dprQuery = window.matchMedia("(resolution: " + (window.devicePixelRatio || 1) + "dppx)");
  dprQuery.addEventListener("change", onDpr);
}
function onDpr() {
  geomDirty = true;
  watchDpr();
  schedule("depth");
}

export function initDepth() {
  grab();
  cv = E["dp-cv"];
  ov = E["dp-ov"];
  ctx = cv.getContext("2d");
  octx = ov.getContext("2d");
  ensureRows(1);
  if (typeof ResizeObserver === "function") {
    ro = new ResizeObserver(() => { geomDirty = true; schedule("depth"); });
    ro.observe(E["dp-scope"]);
    ro.observe(E.ladder);
  }
  window.addEventListener("resize", () => { geomDirty = true; });
  if (window.matchMedia) watchDpr();
  ov.addEventListener("pointermove", onScopeHover);
  ov.addEventListener("pointerleave", clearLens);
  // seed the poll cycle if the control frame already arrived
  const c = state.control && state.control.universe && state.control.universe.polymarket_us;
  if (c && typeof c.cycle_s === "number") pmCycle = c.cycle_s;
}

export function mountDepth() {
  mounted = true;
  geomDirty = true;
  lastBook = null;                  // redraw everything on arrival
}

export function unmountDepth() {
  mounted = false;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  animUntil = 0;
}

/** For tests and the 1s tick: how many rAF frames are pending. */
export function depthAnimating() {
  return raf !== 0;
}

