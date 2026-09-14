/* ARB TERMINAL — vanilla ES2020, no frameworks, no network beyond /ws and /api/status.
   Implements the UI contract: hello / book / delta / stats over WS, /api/status poll. */
(() => {
  "use strict";

  // ---------- constants ----------
  const STALE_MS = 5000;          // project staleness default
  const MAX_TAPE_ROWS = 200;
  const TAPE_DRAIN_MS = 250;
  const TAPE_PER_DRAIN = 3;       // <= 12 rendered rows/s
  const TAPE_QUEUE_CAP = 24;
  const MAX_LADDER = 12;          // levels per side
  const STATS_KEEP = 300;         // stats buffer
  const SPARK_WINDOW = 120;       // seconds shown
  const SPARK_W = 340;
  const SPARK_H = 48;
  const FLASH_MIN_GAP_MS = 84;    // coalesce >12 flashes/s per cell
  const STATUS_POLL_MS = 10000;
  const P95_HOT_MS = 250;
  const CANVAS_FONT = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';

  const $ = (id) => document.getElementById(id);
  const nf = new Intl.NumberFormat("en-US");
  const utcFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const etFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/New_York", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

  const rmQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  let reducedMotion = rmQuery.matches;
  if (typeof rmQuery.addEventListener === "function") {
    rmQuery.addEventListener("change", (e) => { reducedMotion = e.matches; });
  }

  // ---------- state ----------
  const state = {
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
    conn: "connecting",
    des: { open: false, marketId: null, detail: null, error: null, ctl: null },
    pairs: { open: false, rows: [], idx: 0, filter: "proposed", loading: false, msg: "" },
    arb: { open: false, quotes: [], idx: 0, selectedPair: null },
    paper: { open: false, data: null, idx: 0, selectedId: null, error: null, timer: 0, ctl: null },
  };

  const dirtyBooks = new Set();
  const dirty = { monitor: false, depth: false, latnums: false, system: false, poly: false, status: false, spark: false, des: false, arb: false, paper: false };
  let rafPending = false;

  let latBucket = [];          // delta latencies inside the current 1s bucket
  let tapeQueue = [];
  let tapeTotal = 0;
  let cmdBuf = "";
  let cmdMsgTimer = 0;
  let selecting = false;       // pointer is down: DOM writes are held off
  let copyToastTimer = 0;
  let copyFadeTimer = 0;
  let sparkHover = null;       // hover x in CSS px, or null

  // ---------- formatting ----------
  function fmtCents(ticks) {
    return ticks == null ? "—" : (ticks / 100).toFixed(2);
  }
  function fmtMid(ticks) {
    if (ticks == null) return "—";
    return Number.isInteger(ticks) ? (ticks / 100).toFixed(2) : (ticks / 100).toFixed(3);
  }
  function fmtQty(e4) {
    if (e4 == null) return "—";
    const neg = e4 < 0;
    const a = Math.abs(e4);
    const whole = Math.floor(a / 10000);
    const frac = a % 10000;
    let s = nf.format(whole);
    if (frac) s += "." + String(frac).padStart(4, "0").replace(/0+$/, "");
    return (neg ? "-" : "") + s;
  }
  function fmtSignedQty(e4) {
    if (e4 == null) return "—";
    return (e4 >= 0 ? "+" : "") + fmtQty(e4);
  }
  function fmtMs(v) {
    if (v == null || !isFinite(v)) return "—";
    return (v < 10 ? v.toFixed(1) : String(Math.round(v))) + "ms";
  }
  function fmtAge(ms) {
    return ms < 1000 ? Math.round(ms) + "ms" : (ms / 1000).toFixed(1) + "s";
  }
  function fmtAgo(ms) {
    return Math.max(0, Math.round(ms / 1000)) + "s";
  }
  function fmtRate(r) {
    if (r == null || !isFinite(r)) return "—";
    return (r >= 100 ? String(Math.round(r)) : r >= 10 ? r.toFixed(0) : r.toFixed(1)) + "/s";
  }
  function fmtUptime(s) {
    if (s == null || !isFinite(s)) return "—";
    s = Math.floor(s);
    const d = Math.floor(s / 86400);
    const h = String(Math.floor((s % 86400) / 3600)).padStart(2, "0");
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    return (d ? d + "d " : "") + h + ":" + m + ":" + ss;
  }
  function fmtTapeTime(tsMs) {
    const d = new Date(tsMs);
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    const ss = String(d.getUTCSeconds()).padStart(2, "0");
    return mm + ":" + ss + "." + Math.floor(d.getUTCMilliseconds() / 100);
  }
  function percentile(arr, p) {
    if (!arr.length) return null;
    const a = arr.slice().sort((x, y) => x - y);
    return a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))];
  }

  // ---------- DOM helpers ----------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  const rmTimers = new WeakMap();

  // Background-only flash: 120ms in (0.2,0,0,1), 240ms linear decay, 14% alpha.
  // Reduced motion: persistent glyph + 700 weight for 1s, no animation.
  function flash(cell, kind) {
    const now = performance.now();
    if (cell._lastFlash != null && now - cell._lastFlash < FLASH_MIN_GAP_MS) return;
    cell._lastFlash = now;
    const up = kind === "up" || kind === "bid";
    const down = kind === "down" || kind === "ask";
    if (reducedMotion) {
      if (!up && !down) return;
      cell.classList.remove("rm-up", "rm-down");
      cell.classList.add(up ? "rm-up" : "rm-down");
      clearTimeout(rmTimers.get(cell));
      rmTimers.set(cell, setTimeout(() => cell.classList.remove("rm-up", "rm-down"), 1000));
      return;
    }
    if (typeof cell.animate !== "function") return;
    const c = up ? "rgba(47,224,160,0.14)" : down ? "rgba(255,79,94,0.14)" : "rgba(230,230,230,0.10)";
    cell.animate(
      [
        { backgroundColor: "rgba(0,0,0,0)", easing: "cubic-bezier(0.2,0,0,1)" },
        { backgroundColor: c, offset: 1 / 3, easing: "linear" },
        { backgroundColor: "rgba(0,0,0,0)" },
      ],
      { duration: 360 },
    );
  }

  function setVal(elm, text, warn) {
    if (elm.textContent !== text) {
      elm.textContent = text;
      if (elm._has) flash(elm, "neutral");
      elm._has = true;
    }
    if (warn !== undefined) elm.classList.toggle("warn", !!warn);
  }

  // ---------- render scheduling: one rAF batch ----------
  function schedule(...keys) {
    for (const k of keys) dirty[k] = true;
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
    if (dirty.monitor) {
      dirty.monitor = false;
      if (dirtyBooks.size) {
        const ids = Array.from(dirtyBooks);
        dirtyBooks.clear();
        renderMonitorRows(ids);
        if (state.selectedId && ids.includes(state.selectedId)) {
          dirty.depth = true;
          if (state.des.open) dirty.des = true; // live-book block on the DES page
        }
      }
    }
    if (dirty.depth) { dirty.depth = false; renderDepth(); }
    if (dirty.latnums) { dirty.latnums = false; renderLatNums(); }
    if (dirty.system) { dirty.system = false; renderSystem(); }
    if (dirty.poly) { dirty.poly = false; renderPoly(); }
    if (dirty.status) { dirty.status = false; renderStatusBar(); }
    if (dirty.spark) { dirty.spark = false; drawSpark(); }
    if (dirty.des) { dirty.des = false; renderDes(); }
    if (dirty.arb) { dirty.arb = false; renderArb(); }
    if (dirty.paper) { dirty.paper = false; renderPaper(); }
  }

  // ---------- monitor ----------
  const monRefs = new Map(); // market_id -> {row, ticker, bid, ask, mid, spr, prev}

  function buildMonitor() {
    const c = $("mon-rows");
    c.textContent = "";
    monRefs.clear();
    if (!state.markets.length) {
      c.append(el("div", "mon-row quiet-line", "AWAITING MARKETS"));
    }
    state.markets.forEach((m, i) => {
      const row = el("div", "mon-row" + (m.venue === "polymarket_us" ? " v-pm" : ""));
      row.id = "mon-" + i;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", "false");
      row.dataset.id = m.market_id;
      row.title = (m.title ? m.title + " · " : "") + m.ticker
        + (m.volume_24h != null ? " · 24h vol " + nf.format(Math.round(m.volume_24h)) : "");
      const idx = el("span", "mon-idx", i < 9 ? String(i + 1) : "");
      const ticker = el("span", "mon-ticker", m.ticker || m.market_id);
      const bid = el("span", "num", "—");
      const ask = el("span", "num", "—");
      const mid = el("span", "num", "—");
      const spr = el("span", "num", "—");
      row.append(idx, ticker, bid, ask, mid, spr);
      row.addEventListener("click", () => select(m.market_id));
      row.addEventListener("dblclick", () => { select(m.market_id); openDes(m.market_id); });
      c.append(row);
      monRefs.set(m.market_id, { row, ticker, bid, ask, mid, spr, prev: {} });
    });
    $("monitor-stat").textContent = state.markets.length + " MKTS";
  }

  function dirOf(oldV, newV) {
    if (oldV == null || newV == null || oldV === newV) return "neutral";
    return newV > oldV ? "up" : "down";
  }

  function updCell(refs, key, cell, val, text, kind) {
    if (refs.prev[key] === val && cell.textContent === text) return;
    const had = refs.prev[key] != null;
    refs.prev[key] = val;
    if (cell.textContent !== text) {
      cell.textContent = text;
      cell.title = val != null ? val + " ticks" : "";
      if (had) flash(cell, kind);
    }
  }

  function renderMonitorRows(ids) {
    for (const id of ids) {
      const r = monRefs.get(id);
      const b = state.books.get(id);
      if (!r || !b) continue;
      const bb = b.bids.length ? b.bids[0][0] : null;
      const ba = b.asks.length ? b.asks[0][0] : null;
      const mid = bb != null && ba != null ? (bb + ba) / 2 : null;
      const spr = bb != null && ba != null ? ba - bb : null;
      updCell(r, "bid", r.bid, bb, fmtCents(bb), dirOf(r.prev.bid, bb));
      updCell(r, "ask", r.ask, ba, fmtCents(ba), dirOf(r.prev.ask, ba));
      updCell(r, "mid", r.mid, mid, fmtMid(mid), "neutral");
      updCell(r, "spr", r.spr, spr, fmtCents(spr), "neutral");
    }
  }

  // ---------- selection: one amber thread ----------
  function select(id) {
    if (!state.byId.has(id)) return;
    state.selectedId = id;
    document.body.classList.add("has-sel");
    for (const [mid, r] of monRefs) {
      const on = mid === id;
      r.row.classList.toggle("sel", on);
      r.row.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        $("mon-rows").setAttribute("aria-activedescendant", r.row.id);
        r.row.scrollIntoView({ block: "nearest" });
      }
    }
    for (const row of Array.from($("tape-rows").children)) {
      row.classList.toggle("sel", row.dataset.id === id);
    }
    schedule("depth");
    if (state.des.open && state.des.marketId !== id) openDes(id); // arrows page through DES
  }

  function moveSel(d) {
    if (!state.markets.length) return;
    let i = state.markets.findIndex((m) => m.market_id === state.selectedId);
    i = i < 0 ? 0 : Math.max(0, Math.min(state.markets.length - 1, i + d));
    select(state.markets[i].market_id);
  }

  function quickSelect(i) {
    const m = state.markets[i];
    if (m) select(m.market_id);
  }

  // ---------- depth ladder ----------
  const ladderRefs = { asks: [], bids: [] };
  const ladderEl = $("ladder");

  function ladderRow(side) {
    const row = el("div", "ladder-row " + side);
    const bq = el("div", "l-qty bid-q");
    const aq = el("div", "l-qty ask-q");
    const yes = el("span", "l-yes");
    const no = el("span", "l-no");
    const q = side === "bid" ? bq : aq;
    const track = el("div", "track");
    const fill = el("div", "fill");
    const qtxt = el("span", "qtxt");
    track.style.visibility = "hidden";
    fill.style.visibility = "hidden";
    q.append(track, fill, qtxt);
    row.append(bq, yes, no, aq);
    return { row, qcell: q, track, fill, qtxt, yes, no, prevP: undefined, prevQ: undefined };
  }

  function buildLadder() {
    const ac = $("ask-rows");
    const bc = $("bid-rows");
    for (let i = 0; i < MAX_LADDER; i++) {
      const r = ladderRow("ask");
      if (i === MAX_LADDER - 1) r.row.classList.add("best"); // best ask sits just above mid
      ladderRefs.asks.push(r);
      ac.append(r.row);
    }
    for (let i = 0; i < MAX_LADDER; i++) {
      const r = ladderRow("bid");
      if (i === 0) r.row.classList.add("best"); // best bid just below mid
      ladderRefs.bids.push(r);
      bc.append(r.row);
    }
  }

  function clearLadderRow(r) {
    if (r.yes.textContent !== "") { r.yes.textContent = ""; r.yes.removeAttribute("title"); }
    if (r.no.textContent !== "") { r.no.textContent = ""; r.no.removeAttribute("title"); }
    if (r.qtxt.textContent !== "") r.qtxt.textContent = "";
    r.track.style.visibility = "hidden";
    r.fill.style.visibility = "hidden";
    r.prevP = undefined;
    r.prevQ = undefined;
  }

  function setLadderRow(r, lvl, side, maxQ) {
    if (!lvl || lvl[0] == null) {
      clearLadderRow(r);
      return;
    }
    const p = lvl[0];
    const q = lvl[1];
    r.track.style.visibility = "";
    r.fill.style.visibility = "";
    const yesTxt = fmtCents(p);
    if (r.yes.textContent !== yesTxt) {
      r.yes.textContent = yesTxt;
      r.yes.title = p + " ticks";
    }
    const noTxt = fmtCents(10000 - p);
    if (r.no.textContent !== noTxt) {
      r.no.textContent = noTxt;
      r.no.title = (10000 - p) + " ticks";
    }
    const changed = r.prevP === p && r.prevQ !== undefined && r.prevQ !== q;
    const qTxt = fmtQty(q);
    if (r.qtxt.textContent !== qTxt) r.qtxt.textContent = qTxt;
    r.fill.style.width = maxQ > 0 ? Math.min(100, (q / maxQ) * 100).toFixed(1) + "%" : "0%";
    if (changed) flash(r.qcell, side);
    r.prevP = p;
    r.prevQ = q;
  }

  function renderDepth() {
    const id = state.selectedId;
    const mkt = id ? state.byId.get(id) : null;
    $("depth-title").textContent = "DEPTH — " + (mkt ? mkt.ticker : "—");
    const banner = $("depth-banner");
    const stat = $("depth-stat");
    const book = id ? state.books.get(id) : null;

    if (!book) {
      stat.textContent = "—";
      stat.classList.remove("warn");
      banner.textContent = id ? "AWAITING BOOK" : "NO SELECTION";
      banner.classList.add("quiet");
      banner.classList.remove("pulse");
      ladderEl.classList.add("dim");
      for (const r of ladderRefs.asks) clearLadderRow(r);
      for (const r of ladderRefs.bids) clearLadderRow(r);
      $("mid-val").textContent = "—";
      $("spr-val").textContent = "—";
      return;
    }

    const age = book.age_ms + (performance.now() - book.recvAt);
    const quiet = age > STALE_MS;
    // "stale" from the server just means no update inside the trading-engine
    // staleness window — a quiet prediction market, not a broken book. Only
    // structural reasons (seq gap, crossed, bad level) are alarming.
    const structural = book.valid === false && book.reason && book.reason !== "stale";
    stat.textContent = "AGE " + fmtAge(age);
    stat.classList.toggle("warn", Boolean(structural));

    if (structural) {
      banner.classList.remove("quiet");
      banner.textContent = "INVALID · " + String(book.reason).toUpperCase();
    } else if (quiet || book.reason === "stale") {
      banner.classList.add("quiet");
      banner.textContent = "QUIET · LAST UPDATE " + (age / 1000).toFixed(0) + "s AGO";
    } else {
      banner.classList.remove("quiet");
      banner.textContent = "";
    }
    banner.classList.toggle("pulse", Boolean(structural) && !reducedMotion);
    ladderEl.classList.toggle("dim", Boolean(structural));

    const asks = book.asks.slice(0, MAX_LADDER);
    const bids = book.bids.slice(0, MAX_LADDER);
    let maxQ = 0;
    for (const l of asks) if (l && l[1] > maxQ) maxQ = l[1];
    for (const l of bids) if (l && l[1] > maxQ) maxQ = l[1];
    for (let k = 0; k < MAX_LADDER; k++) {
      // ask level k renders k rows above the mid seam (container bottom row = best ask)
      setLadderRow(ladderRefs.asks[MAX_LADDER - 1 - k], asks[k] || null, "ask", maxQ);
      setLadderRow(ladderRefs.bids[k], bids[k] || null, "bid", maxQ);
    }

    const bb = bids.length ? bids[0][0] : null;
    const ba = asks.length ? asks[0][0] : null;
    const midEl = $("mid-val");
    const sprEl = $("spr-val");
    const midTxt = bb != null && ba != null ? fmtMid((bb + ba) / 2) : "—";
    const sprTxt = bb != null && ba != null ? fmtCents(ba - bb) : "—";
    if (midEl.textContent !== midTxt) midEl.textContent = midTxt;
    if (sprEl.textContent !== sprTxt) sprEl.textContent = sprTxt;
  }

  // ---------- tape ----------
  function buildTapeRow(d) {
    const side = d.side === "bid" ? "b" : "a";
    const row = el("div", "tape-row side-" + side + (reducedMotion ? "" : " new-" + side));
    row.dataset.id = d.market_id || "";
    if (d.market_id === state.selectedId) row.classList.add("sel");
    const mkt = d.market_id ? state.byId.get(d.market_id) : null;
    const px = el("span", "t-px", fmtCents(d.price));
    if (d.price != null) px.title = d.price + " ticks";
    row.append(
      el("span", "t-time", d.ts_ms != null ? fmtTapeTime(d.ts_ms) : "—"),
      el("span", "t-side", side === "b" ? "B" : "A"),
      px,
      el("span", "t-dq", fmtSignedQty(d.qty_delta)),
      el("span", "t-lat", fmtMs(d.latency_ms)),
      el("span", "t-tk", mkt ? mkt.ticker : (d.market_id || "")),
    );
    return row;
  }

  function drainTape() {
    if (selecting) return;  // prepending rows would shift a drag in progress
    if (!tapeQueue.length) return;
    const take = tapeQueue.splice(0, TAPE_PER_DRAIN);
    const c = $("tape-rows");
    for (const d of take) c.prepend(buildTapeRow(d)); // oldest of the batch first; newest ends on top
    while (c.childElementCount > MAX_TAPE_ROWS) c.lastElementChild.remove();
    $("tape-stat").textContent = nf.format(tapeTotal) + " MSGS";
  }

  // ---------- latency sparkline ----------
  const spark = $("spark");
  let sctx = null;

  function setupCanvas() {
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    spark.width = Math.round(SPARK_W * dpr);
    spark.height = Math.round(SPARK_H * dpr);
    spark.style.width = SPARK_W + "px";
    spark.style.height = SPARK_H + "px";
    sctx = spark.getContext("2d");
    if (sctx) sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function pushLatencyPoint(statsMsg) {
    let v = null;
    if (latBucket.length) {
      latBucket.sort((a, b) => a - b);
      v = latBucket[Math.floor(latBucket.length / 2)]; // 1s median bucket
    } else if (statsMsg.latency_ms && statsMsg.latency_ms.last != null) {
      v = statsMsg.latency_ms.last;
    }
    latBucket = [];
    state.latPoints.push({ v });
    if (state.latPoints.length > STATS_KEEP) state.latPoints.shift();
  }

  function drawSpark() {
    if (!sctx) return;
    const W = SPARK_W;
    const H = SPARK_H;
    const plotW = W - 48; // right gutter for direct labels
    const tip = $("spark-tip");
    sctx.clearRect(0, 0, W, H);

    const pts = state.latPoints.slice(-SPARK_WINDOW);
    const n = pts.length;
    const vals = [];
    for (const p of pts) if (p.v != null) vals.push(p.v);

    if (!vals.length) {
      sctx.fillStyle = "#6b7280";
      sctx.font = "10px " + CANVAS_FONT;
      sctx.textAlign = "center";
      sctx.fillText("AWAITING DATA", W / 2, H / 2 + 3);
      sctx.textAlign = "left";
      tip.hidden = true;
      return;
    }

    const lm = (state.stats && state.stats.latency_ms) || {};
    const nowMs = Date.now();
    if (!state.latLastFit || nowMs - state.latLastFit > 30000) { // y-domain refit every 30s
      const p95 = lm.p95 != null ? lm.p95 : percentile(vals, 0.95);
      const target = p95 != null && p95 > 0 ? p95 : Math.max.apply(null, vals);
      state.latDomainMax = Math.max(10, 1.5 * target); // 0 -> 1.5 x p95
      state.latLastFit = nowMs;
    }
    const dmax = state.latDomainMax;
    const step = plotW / (SPARK_WINDOW - 1);
    const xAt = (i) => plotW - (n - 1 - i) * step;
    const yAt = (v) => Math.max(2, H - 2 - (v / dmax) * (H - 4));

    // segments split on null buckets
    const segs = [];
    let cur = null;
    const clamped = [];
    for (let i = 0; i < n; i++) {
      const v = pts[i].v;
      if (v == null) { cur = null; continue; }
      if (!cur) { cur = []; segs.push(cur); }
      const x = xAt(i);
      cur.push([x, yAt(Math.min(v, dmax))]);
      if (v > dmax) clamped.push(x);
    }

    // area fill: same-hue 6% fading to 0
    const grad = sctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, "rgba(86,156,214,0.06)");
    grad.addColorStop(1, "rgba(86,156,214,0)");
    sctx.fillStyle = grad;
    for (const seg of segs) {
      if (seg.length < 2) continue;
      sctx.beginPath();
      sctx.moveTo(seg[0][0], seg[0][1]);
      for (let j = 1; j < seg.length; j++) sctx.lineTo(seg[j][0], seg[j][1]);
      sctx.lineTo(seg[seg.length - 1][0], H);
      sctx.lineTo(seg[0][0], H);
      sctx.closePath();
      sctx.fill();
    }

    // median / p95: 1px dotted rules, right-labeled; p95 goes amber when hot
    const hot = lm.p95 != null && lm.p95 > P95_HOT_MS;
    const labels = [];
    const drawRule = (v, color) => {
      const yy = Math.round(yAt(Math.min(v, dmax))) + 0.5;
      sctx.save();
      sctx.strokeStyle = color;
      sctx.lineWidth = 1;
      sctx.setLineDash([1, 2]);
      sctx.beginPath();
      sctx.moveTo(0, yy);
      sctx.lineTo(plotW, yy);
      sctx.stroke();
      sctx.restore();
      return yy;
    };
    if (lm.median != null) {
      const y = drawRule(lm.median, "#6b7280");
      labels.push({ text: "MED " + Math.round(lm.median), y, color: "#8a9099" });
    }
    if (lm.p95 != null) {
      const y = drawRule(lm.p95, hot ? "#ffb02e" : "#6b7280");
      labels.push({ text: "P95 " + Math.round(lm.p95), y, color: hot ? "#ffb02e" : "#8a9099" });
    }
    // The current value joins the gutter label set so the three direct
    // labels (last / MED / P95) can never overprint each other.
    let last = null;
    for (let i = n - 1; i >= 0; i--) {
      if (pts[i].v != null) {
        last = { y: yAt(Math.min(pts[i].v, dmax)), v: pts[i].v };
        break;
      }
    }
    if (last) {
      labels.push({ text: Math.round(last.v) + "ms", y: last.y, color: "#e6e6e6", size: 11 });
    }
    for (const lb of labels) lb.y = Math.max(8, Math.min(H - 2, lb.y + 3));
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) {
      if (labels[i].y - labels[i - 1].y < 11) labels[i].y = labels[i - 1].y + 11;
    }
    for (let i = labels.length - 1; i >= 0; i--) {
      const maxY = H - 2 - (labels.length - 1 - i) * 11;
      if (labels[i].y > maxY) labels[i].y = maxY;
    }
    for (const lb of labels) {
      sctx.fillStyle = lb.color;
      sctx.font = (lb.size || 10) + "px " + CANVAS_FONT;
      sctx.fillText(lb.text, plotW + 4, lb.y);
    }

    // line: 1.5px blue, no tweening
    sctx.strokeStyle = "#569cd6";
    sctx.lineWidth = 1.5;
    for (const seg of segs) {
      if (seg.length === 1) {
        sctx.fillStyle = "#569cd6";
        sctx.fillRect(seg[0][0] - 1, seg[0][1] - 1, 2, 2);
        continue;
      }
      sctx.beginPath();
      sctx.moveTo(seg[0][0], seg[0][1]);
      for (let j = 1; j < seg.length; j++) sctx.lineTo(seg[j][0], seg[j][1]);
      sctx.stroke();
    }

    // spikes clamp to top edge with a 3px amber tick
    sctx.fillStyle = "#ffb02e";
    for (const x of clamped) sctx.fillRect(x - 1, 0, 2, 3);

    // hover: 1px crosshair + tooltip
    if (sparkHover != null && sparkHover <= plotW + 4) {
      let i = Math.round(n - 1 - (plotW - sparkHover) / step);
      i = Math.max(0, Math.min(n - 1, i));
      const hx = Math.round(xAt(i)) + 0.5;
      if (hx >= 0) {
        sctx.strokeStyle = "#8a9099";
        sctx.lineWidth = 1;
        sctx.beginPath();
        sctx.moveTo(hx, 0);
        sctx.lineTo(hx, H);
        sctx.stroke();
        const v = pts[i].v;
        tip.textContent = "t−" + (n - 1 - i) + "s · " + (v != null ? Math.round(v) + "ms" : "—");
        tip.hidden = false;
        tip.style.left = (8 + Math.max(0, Math.min(W - 96, hx + 10))) + "px";
        tip.style.top = "4px";
      } else {
        tip.hidden = true;
      }
    } else {
      tip.hidden = true;
    }
  }

  function renderLatNums() {
    const s = state.stats;
    if (!s) return;
    const lm = s.latency_ms || {};
    setVal($("lat-last"), fmtMs(lm.last));
    setVal($("lat-med"), fmtMs(lm.median));
    setVal($("lat-p95"), fmtMs(lm.p95), lm.p95 != null && lm.p95 > P95_HOT_MS);
    setVal($("lat-n"), lm.n != null ? nf.format(lm.n) : "—");
    $("lat-stat").textContent = fmtMs(lm.last);
  }

  // ---------- system ----------
  function renderSystem() {
    const s = state.stats;
    if (s) {
      setVal($("sys-msg-total"), s.msg_total != null ? nf.format(s.msg_total) : "—");
      setVal($("sys-rate"), fmtRate(s.msg_rate_1s));
      setVal($("sys-parse"), s.parse_errors != null ? nf.format(s.parse_errors) : "—", (s.parse_errors || 0) > 0);
      setVal($("sys-gaps"), s.seq_gaps != null ? nf.format(s.seq_gaps) : "—", (s.seq_gaps || 0) > 0);
      setVal($("sys-clients"), s.ws_clients != null ? String(s.ws_clients) : "—");
      setVal($("sys-uptime"), fmtUptime(s.uptime_s));
      const rec = s.recorder;
      const recState = $("sys-rec-state");
      setVal(recState, rec ? "ON" : "OFF");
      recState.classList.toggle("on", !!rec);
      setVal($("sys-rec-enq"), rec ? nf.format(rec.enqueued) : "—");
      setVal($("sys-rec-drop"), rec ? nf.format(rec.dropped) : "—", rec ? rec.dropped > 0 : false);
      $("sys-stat").textContent = fmtRate(s.msg_rate_1s);
    }
    const db = state.status && state.status.database;
    if (db) {
      setVal($("sys-db-conn"), db.connected ? "UP" : "DOWN", !db.connected);
      setVal($("sys-db-rows"), db.raw_messages_total != null ? nf.format(db.raw_messages_total) : "—");
      const runs = Array.isArray(db.runs) ? db.runs.slice(0, 10) : [];
      const c = $("sys-runs");
      c.textContent = "";
      for (const r of runs) {
        const row = el("div", "kv");
        const k = el("span", "k run-id", r.run_id);
        k.title = r.run_id;
        row.append(k, el("span", "v num", nf.format(r.count)));
        c.append(row);
      }
      if (!runs.length) c.append(el("div", "kv quiet-line", "NO RUNS"));
    }
  }

  // ---------- polymarket down-screen ----------
  function renderPoly() {
    const v = state.status && state.status.venues && state.status.venues.polymarket_us;
    const dot = $("poly-dot");
    const line = $("poly-line");
    if (!v) {
      $("poly-stat").textContent = "—";
      dot.className = "mini-dot";
      line.textContent = "REST /markets · — · checked —";
      return;
    }
    $("poly-stat").textContent = String(v.state || "down").toUpperCase();
    const polled = v.state === "polled" || v.state === "connecting";
    $("poly-main").textContent = polled ? "REST POLLING" : "DATA UNAVAILABLE";
    if (v.detail) {
      $("poly-sub").textContent = v.detail.toUpperCase();
      $("poly-sub").title = v.detail;
    }
    const ps = state.stats && state.stats.polymarket_us;
    if (polled && ps) {
      const age = ps.last_poll_age_ms;
      const fresh = age != null && age < 30000;
      dot.className = "mini-dot " + (fresh ? "dot-up" : "dot-down");
      line.textContent =
        "polls " + nf.format(ps.polls) + " · 429s " + nf.format(ps.rate_limited)
        + " · errors " + nf.format(ps.errors)
        + " · last book " + (age == null ? "—" : fmtAge(age) + " ago");
      return;
    }
    const ok = !!v.rest_reachable;
    dot.className = "mini-dot " + (ok ? "dot-up" : "dot-down");
    const ago = state.statusAt ? fmtAgo(Date.now() - state.statusAt) + " ago" : "—";
    line.textContent = "REST /markets · " + (ok ? "OK" : "UNREACHABLE") + " · checked " + ago;
  }

  // ---------- status bar ----------
  function setVenue(elm, vv) {
    const st = vv ? String(vv.state || "down") : null;
    elm.textContent = st ? st.toUpperCase() : "—";
    elm.title = vv && vv.detail ? vv.detail : "";
    elm.className = "val vstate " + (st === "live" ? "st-live" : st === "polled" ? "st-polled" : st === "connecting" ? "st-warn" : st ? "st-down" : "st-dim");
  }

  function renderStatusBar() {
    const runId = state.runId || (state.status && state.status.run_id) || null;
    const runEl = $("run-id");
    runEl.textContent = runId ? (runId.length > 14 ? runId.slice(0, 12) + "…" : runId) : "—";
    runEl.title = runId || "";
    const v = state.status && state.status.venues;
    setVenue($("v-kalshi"), v && v.kalshi);
    setVenue($("v-poly"), v && v.polymarket_us);
    const recEl = $("rec-state");
    if (state.status == null) {
      recEl.textContent = "—";
      recEl.className = "val st-dim";
    } else if (state.status.recording) {
      recEl.textContent = "● ON";
      recEl.className = "val st-live";
    } else {
      recEl.textContent = "OFF";
      recEl.className = "val st-dim";
    }
  }

  function setConn(s) {
    state.conn = s;
    $("conn").className = "conn conn-" + s;
    $("conn-text").textContent =
      s === "live" ? "LIVE" : s === "reconnecting" ? "RECONNECTING" : s === "connecting" ? "CONNECTING" : "DOWN";
  }

  function renderClocks() {
    const d = new Date();
    $("clock-utc").textContent = d.toISOString().slice(0, 10) + " " + utcFmt.format(d) + " UTC";
    $("clock-et").textContent = etFmt.format(d) + " ET";
  }

  // ---------- command line ----------
  function renderCmd() {
    $("cmd-text").textContent = cmdBuf;
  }

  function cmdMsg(text, cls) {
    const e = $("cmd-msg");
    e.textContent = text;
    e.className = "cmd-msg " + cls;
    clearTimeout(cmdMsgTimer);
    cmdMsgTimer = setTimeout(() => { e.textContent = ""; }, 2500);
  }

  function execCmd() {
    const raw = cmdBuf.trim();
    cmdBuf = "";
    renderCmd();
    if (!raw) return;
    let q = raw.replace(/\s*<\s*GO\s*>\s*$/i, "").replace(/\s+GO$/i, "").trim().toUpperCase();
    if (!q) return;
    // "DES" opens the description of the selected market; "<TICKER> DES"
    // selects first. Bloomberg muscle memory, kept deliberately.
    let wantDes = false;
    if (q === "PAIRS") {
      openPairs();
      cmdMsg("PAIRS", "ok");
      return;
    }
    if (q === "ARB") {
      openArb();
      cmdMsg("ARB", "ok");
      return;
    }
    if (q === "PAPER") {
      openPaper();
      cmdMsg("PAPER", "ok");
      return;
    }
    if (q === "DES") {
      if (!state.selectedId) { cmdMsg("NO MARKET SELECTED", "err"); return; }
      openDes(state.selectedId);
      cmdMsg((state.byId.get(state.selectedId) || {}).ticker + " DES", "ok");
      return;
    }
    if (/\s+DES$/.test(q)) { wantDes = true; q = q.replace(/\s+DES$/, "").trim(); }
    const tickers = state.markets;
    const hit =
      tickers.find((m) => (m.ticker || "").toUpperCase() === q) ||
      tickers.find((m) => (m.ticker || "").toUpperCase().startsWith(q)) ||
      tickers.find((m) => (m.ticker || "").toUpperCase().includes(q));
    if (hit) {
      select(hit.market_id);
      if (wantDes) openDes(hit.market_id);
      cmdMsg(hit.ticker + (wantDes ? " DES" : " <GO>"), "ok");
    } else {
      cmdMsg("NO MATCH · " + q, "err");
    }
  }

  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    const k = e.key;
    if (state.pairs.open && onPairsKey(e)) {
      e.preventDefault();
      return;
    }
    if (state.arb.open && onArbKey(e)) {
      e.preventDefault();
      return;
    }
    if (state.paper.open && onPaperKey(e)) {
      e.preventDefault();
      return;
    }
    if (k === "ArrowUp" || k === "ArrowDown") {
      e.preventDefault();
      moveSel(k === "ArrowUp" ? -1 : 1);
      return;
    }
    if (k === "Enter") {
      e.preventDefault();
      if (cmdBuf.trim() === "") {
        if (state.selectedId) openDes(state.selectedId); // Enter on a market = DES
      } else {
        execCmd();
      }
      return;
    }
    if (k === "Escape") {
      if (state.des.open) { closeDes(); return; }
      cmdBuf = "";
      renderCmd();
      return;
    }
    if (k === "Backspace") {
      e.preventDefault();
      cmdBuf = cmdBuf.slice(0, -1);
      renderCmd();
      return;
    }
    if (k.length === 1) {
      if (cmdBuf === "" && /^[1-9]$/.test(k)) {
        e.preventDefault();
        quickSelect(Number(k) - 1);
        return;
      }
      if (/^[\x20-\x7e]$/.test(k)) {
        e.preventDefault();
        if (cmdBuf.length < 48) cmdBuf += k.toUpperCase();
        renderCmd();
      }
    }
  }

  // ---------- DES: market description page ----------
  const etWhenFmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });

  function fmtWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    const utc = d.toISOString().slice(0, 16).replace("T", " ") + "Z";
    return utc + " · " + etWhenFmt.format(d).replace(",", "") + " ET";
  }

  // Ticks are $0.0001, so cents and implied probability share a number:
  // 50 ticks = 0.50¢ = 0.50%.
  function fmtCentsPct(ticks) {
    if (ticks == null) return "—";
    return fmtCents(ticks) + "¢ · " + (ticks / 100).toFixed(2) + "%";
  }

  function fmtCount(v) {
    return v == null || !isFinite(v) ? "—" : nf.format(Math.round(v));
  }

  function openDes(id) {
    if (!id || !state.byId.has(id)) return;
    const des = state.des;
    if (des.ctl) des.ctl.abort();
    des.open = true;
    des.marketId = id;
    des.detail = null;
    des.error = null;
    $("des").hidden = false;
    document.body.classList.add("des-open");
    schedule("des");
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    des.ctl = ctl;
    fetch("/api/markets/" + encodeURIComponent(id), { cache: "no-store", signal: ctl ? ctl.signal : undefined })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
      .then((detail) => {
        if (des.marketId !== id) return; // paged away while loading
        des.detail = detail;
        schedule("des");
      })
      .catch((err) => {
        if (des.marketId !== id || (err && err.name === "AbortError")) return;
        des.error = "NO DESCRIPTION AVAILABLE · " + String(err.message || err).toUpperCase();
        schedule("des");
      });
  }

  function closeDes() {
    const des = state.des;
    if (des.ctl) des.ctl.abort();
    des.open = false;
    des.ctl = null;
    $("des").hidden = true;
    document.body.classList.remove("des-open");
  }

  function renderDes() {
    const des = state.des;
    if (!des.open) return;
    const m = state.byId.get(des.marketId) || {};
    const d = des.detail;
    setText("des-ticker", m.ticker || des.marketId);
    setText("des-stat", des.error ? "ERROR" : d ? (d.source === "live" ? "LIVE" : "DISCOVERY") + " · " + fmtAgo(Date.now() - d.fetched_at_ms) + " AGO" : "LOADING");
    const errEl = $("des-error");
    errEl.hidden = !des.error;
    errEl.textContent = des.error || "";

    setText("des-event", d ? d.event_title || m.title || "—" : m.title || "—");
    setText("des-sub", d ? [d.yes_sub_title, d.event_sub_title].filter(Boolean).join(" · ") || "—" : "—");
    setText("des-series", d ? d.series_ticker || "—" : "—");
    setText("des-eventtk", d ? d.event_ticker || "—" : "—");
    setText("des-mkt", m.ticker || "—");
    setText("des-rules", d ? d.rules_primary || "—" : "—");
    const r2 = d && d.rules_secondary;
    $("des-rules2-wrap").hidden = !r2;
    setText("des-rules2", r2 || "");
    const src = $("des-sources");
    src.textContent = "";
    if (d && d.settlement_sources && d.settlement_sources.length) {
      for (const s of d.settlement_sources) {
        const line = el("div", "src-line");
        line.append(el("span", "src-name", s.name || "—"));
        if (s.url) line.append(el("span", "src-url", "  " + s.url));
        src.append(line);
      }
    } else {
      src.textContent = d ? "NONE LISTED" : "—";
    }

    const statusEl = $("des-status");
    statusEl.textContent = d ? (d.status || "—").toUpperCase() + (d.result ? " · " + d.result.toUpperCase() : "") : "—";
    statusEl.className = "v " + (d && d.status === "active" ? "st-live" : "st-off");
    setText("des-cat", d ? d.category || "—" : "—");
    setText("des-type", d ? (d.market_type || "—").toUpperCase() : "—");
    setText("des-mx", d ? (d.mutually_exclusive == null ? "—" : d.mutually_exclusive ? "YES" : "NO") : "—");
    setText("des-early", d ? (d.can_close_early == null ? "—" : d.can_close_early ? "ALLOWED" : "NO") : "—");
    // Venue-specific extras (Polymarket US): shown only when present.
    const extras = [
      ["des-tick-row", "des-tick", d && d.tick_size_ticks != null ? fmtCents(d.tick_size_ticks) + "¢" : null],
      ["des-fee-row", "des-fee", d && d.fee_coefficient != null ? String(d.fee_coefficient) : null],
      ["des-minqty-row", "des-minqty", d && d.min_trade_qty != null ? nf.format(d.min_trade_qty) : null],
    ];
    for (const [rowId, valId, text] of extras) {
      $(rowId).hidden = text == null;
      if (text != null) setText(valId, text);
    }

    if (d) {
      const vb = d.yes_bid_ticks, va = d.yes_ask_ticks;
      const vmid = vb != null && va != null ? (vb + va) / 2 : null;
      setText("des-implied", fmtCentsPct(vmid == null ? null : Math.round(vmid)));
      setText("des-vq", vb == null || va == null ? "—" : fmtCents(vb) + " / " + fmtCents(va));
      setText("des-last", fmtCentsPct(d.last_price_ticks));
      setText("des-vol", fmtCount(d.volume));
      setText("des-vol24", fmtCount(d.volume_24h));
      setText("des-oi", fmtCount(d.open_interest));
      setText("des-open", fmtWhen(d.open_time));
      setText("des-close", fmtWhen(d.close_time));
      setText("des-exp", fmtWhen(d.expected_expiration_time));
    } else {
      for (const k of ["implied", "vq", "last", "vol", "vol24", "oi", "open", "close", "exp"]) setText("des-" + k, "—");
    }

    const book = state.books.get(des.marketId);
    if (book) {
      const bb = book.bids[0], ba = book.asks[0];
      const sum = (lv) => lv.reduce((a, l) => a + (l[1] || 0), 0);
      setText("des-bb", bb ? fmtCents(bb[0]) + "¢ × " + fmtQty(bb[1]) : "—");
      setText("des-ba", ba ? fmtCents(ba[0]) + "¢ × " + fmtQty(ba[1]) : "—");
      setText("des-ms", bb && ba ? fmtMid((bb[0] + ba[0]) / 2) + " / " + fmtCents(ba[0] - bb[0]) : "—");
      setText("des-bd", fmtQty(sum(book.bids)));
      setText("des-ad", fmtQty(sum(book.asks)));
      setText("des-lv", book.bids.length + " / " + book.asks.length);
      setText("des-age", fmtAge(book.age_ms + (performance.now() - book.recvAt)));
    } else {
      for (const k of ["bb", "ba", "ms", "bd", "ad", "lv", "age"]) setText("des-" + k, "—");
    }
  }

  function setText(id, text) {
    const e = $(id);
    if (e && e.textContent !== text) e.textContent = text;
  }

  // ---------- PAIRS: cross-venue pair review ----------
  const PAIR_FILTERS = ["proposed", "confirmed", "rejected", "all"];

  function openPairs() {
    const p = state.pairs;
    p.open = true;
    if (state.des.open) closeDes();
    if (state.arb.open) closeArb();
    if (state.paper.open) closePaper();
    $("pairs").hidden = false;
    document.body.classList.add("des-open");
    loadPairs();
  }

  function closePairs() {
    state.pairs.open = false;
    $("pairs").hidden = true;
    document.body.classList.remove("des-open");
  }

  async function loadPairs() {
    const p = state.pairs;
    p.loading = true;
    renderPairs();
    try {
      const q = p.filter === "all" ? "" : "?status=" + encodeURIComponent(p.filter);
      const r = await fetch("/api/pairs" + q, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      p.rows = (await r.json()).pairs || [];
      p.idx = Math.min(p.idx, Math.max(0, p.rows.length - 1));
      p.msg = "";
    } catch (err) {
      p.rows = [];
      p.msg = "LOAD FAILED · " + String(err.message || err).toUpperCase();
    }
    p.loading = false;
    renderPairs();
  }

  async function decidePair(status) {
    const p = state.pairs;
    const row = p.rows[p.idx];
    if (!row) return;
    try {
      const r = await fetch("/api/pairs/" + row.id + "/decide", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ status }),
        cache: "no-store",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const updated = await r.json();
      p.rows[p.idx] = { ...row, ...updated };
      p.msg = "#" + row.id + " " + status.toUpperCase();
      // Under a status filter the decided row no longer belongs; drop it and
      // keep the cursor on the next candidate — review flows top to bottom.
      if (p.filter !== "all" && updated.status !== p.filter) {
        p.rows.splice(p.idx, 1);
        p.idx = Math.min(p.idx, Math.max(0, p.rows.length - 1));
      }
    } catch (err) {
      p.msg = "DECIDE FAILED · " + String(err.message || err).toUpperCase();
    }
    renderPairs();
  }

  async function decideEventGroup(status) {
    // Shift+Y / Shift+N: every candidate in the selected row's event pairing
    // (same Kalshi event ↔ same Polymarket event) gets the same decision —
    // a 30-team pennant race is one judgement, not thirty.
    const p = state.pairs;
    const row = p.rows[p.idx];
    if (!row) return;
    const key = (r) => (r.kalshi.event_title || "") + "" + (r.polymarket_us.event_title || "");
    const group = p.rows.filter((r) => key(r) === key(row));
    try {
      const r = await fetch("/api/pairs/decide", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ids: group.map((g) => g.id), status }),
        cache: "no-store",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const res = await r.json();
      p.msg = res.updated + " " + status.toUpperCase() + " · " + (row.kalshi.event_title || "").slice(0, 40).toUpperCase();
      await loadPairs();
    } catch (err) {
      p.msg = "DECIDE FAILED · " + String(err.message || err).toUpperCase();
      renderPairs();
    }
  }

  function movePair(d) {
    const p = state.pairs;
    if (!p.rows.length) return;
    p.idx = Math.max(0, Math.min(p.rows.length - 1, p.idx + d));
    renderPairs();
  }

  function cyclePairFilter() {
    const p = state.pairs;
    p.filter = PAIR_FILTERS[(PAIR_FILTERS.indexOf(p.filter) + 1) % PAIR_FILTERS.length];
    p.idx = 0;
    loadPairs();
  }

  function renderPairs() {
    const p = state.pairs;
    if (!p.open) return;
    setText("pairs-filter", p.filter.toUpperCase());
    setText("pairs-stat", p.loading ? "LOADING" : (p.msg || (nf.format(p.rows.length) + " PAIRS")));
    const c = $("pair-rows");
    c.textContent = "";
    if (!p.rows.length && !p.loading) {
      c.append(el("div", "pair-row quiet-line", p.filter === "proposed" ? "NO PROPOSALS — RUN: arb pairs propose" : "NONE"));
    }
    p.rows.forEach((row, i) => {
      const r = el("div", "pair-row" + (i === p.idx ? " sel" : ""));
      r.setAttribute("role", "option");
      r.setAttribute("aria-selected", i === p.idx ? "true" : "false");
      const k = el("span", "pr-leg"), q = el("span", "pr-leg");
      k.append(el("span", "pr-id", row.kalshi.ticker || ""), el("span", "pr-out", row.kalshi.outcome || ""));
      q.append(el("span", "pr-id", row.polymarket_us.ticker || ""), el("span", "pr-out", row.polymarket_us.outcome || ""));
      r.append(
        el("span", "pr-score num", Number(row.score).toFixed(2)),
        k, q,
        el("span", "pr-status st-" + row.status, String(row.status).toUpperCase()),
      );
      r.addEventListener("click", () => { p.idx = i; renderPairs(); });
      c.append(r);
    });
    const row = p.rows[p.idx];
    $("pair-empty").hidden = !!row;
    $("pair-detail").hidden = !row;
    if (!row) return;
    const kk = row.kalshi || {}, pp = row.polymarket_us || {}, f = row.features || {};
    setText("pd-k-event", kk.event_title || "—");
    setText("pd-k-outcome", kk.outcome || "—");
    setText("pd-k-ticker", kk.ticker || "—");
    setText("pd-k-close", fmtWhen(kk.close_time));
    setText("pd-k-rules", kk.rules || "—");
    setText("pd-p-event", pp.event_title || "—");
    setText("pd-p-outcome", pp.outcome || "—");
    setText("pd-p-ticker", pp.ticker || "—");
    setText("pd-p-close", fmtWhen(pp.close_time));
    setText("pd-p-rules", pp.rules || "—");
    setText("pd-score", Number(row.score).toFixed(3));
    setText("pd-title", f.title_similarity != null ? Number(f.title_similarity).toFixed(2) : "—");
    setText("pd-outcome", f.outcome_similarity != null ? Number(f.outcome_similarity).toFixed(2) : "—");
    setText("pd-overlap", f.outcome_overlap != null ? Number(f.outcome_overlap).toFixed(2) : "—");
    setText("pd-days", f.days_apart != null ? String(f.days_apart) : "—");
    const st = $("pd-status");
    st.textContent = String(row.status).toUpperCase();
    st.className = "v " + (row.status === "confirmed" ? "st-live" : row.status === "rejected" ? "st-off" : "");
    const selRow = c.children[p.idx];
    if (selRow && selRow.scrollIntoView) selRow.scrollIntoView({ block: "nearest" });
  }

  function onPairsKey(e) {
    const k = e.key;
    if (k === "Escape") { closePairs(); return true; }
    if (k === "ArrowUp" || k === "ArrowDown") { movePair(k === "ArrowUp" ? -1 : 1); return true; }
    if (k === "Tab") { cyclePairFilter(); return true; }
    const up = k.length === 1 ? k.toUpperCase() : k;
    if (up === "Y") { (e.shiftKey ? decideEventGroup : decidePair)("confirmed"); return true; }
    if (up === "N") { (e.shiftKey ? decideEventGroup : decidePair)("rejected"); return true; }
    if (up === "U") { decidePair("proposed"); return true; }
    if (up === "R") { loadPairs(); return true; }
    return false;
  }

  // ---------- ARB: cross-venue edge monitor ----------
  function fmtSignedCents(ticks) {
    if (ticks == null) return "—";
    const c = ticks / 100;
    return (c > 0 ? "+" : "") + c.toFixed(2) + "¢";
  }

  function fmtDollarsFromTicks(ticks) {
    if (ticks == null) return "—";
    return (ticks < 0 ? "-$" : "$") + (Math.abs(ticks) / 10000).toFixed(2);
  }

  function dirLabel(direction) {
    return direction === "yes_a_no_b" ? "BUY YES K · BUY NO P" : "BUY YES P · BUY NO K";
  }

  function bboText(leg) {
    if (!leg || !leg.has_book) return "—";
    const b = leg.best_bid ? fmtCents(leg.best_bid[0]) : "—";
    const a = leg.best_ask ? fmtCents(leg.best_ask[0]) : "—";
    return b + "/" + a;
  }

  function openArb() {
    state.arb.open = true;
    if (state.des.open) closeDes();
    if (state.pairs.open) closePairs();
    if (state.paper.open) closePaper();
    $("arbpage").hidden = false;
    document.body.classList.add("des-open");
    renderArb();
  }

  function closeArb() {
    state.arb.open = false;
    $("arbpage").hidden = true;
    document.body.classList.remove("des-open");
  }

  function onArb(m) {
    state.arb.quotes = Array.isArray(m.quotes) ? m.quotes : [];
    if (state.arb.open) schedule("arb");
  }

  function moveArb(d) {
    const a = state.arb;
    if (!a.quotes.length) return;
    a.idx = Math.max(0, Math.min(a.quotes.length - 1, a.idx + d));
    a.selectedPair = a.quotes[a.idx].pair_id;
    renderArb();
  }

  function renderArb() {
    const a = state.arb;
    if (!a.open) return;
    // Keep the cursor on the same pair as the ranking reshuffles under it.
    if (a.selectedPair != null) {
      const i = a.quotes.findIndex((q) => q.pair_id === a.selectedPair);
      if (i >= 0) a.idx = i;
    }
    a.idx = Math.min(a.idx, Math.max(0, a.quotes.length - 1));
    const best = a.quotes.length ? a.quotes[0].best.net_per_contract_ticks : null;
    setText("arb-stat", a.quotes.length
      ? nf.format(a.quotes.length) + " PAIRS · BEST " + fmtSignedCents(best) + "/CT"
      : "0 PAIRS");
    const c = $("arb-rows");
    c.textContent = "";
    a.quotes.forEach((q, i) => {
      const b = q.best;
      const has = b.qty > 0;
      const row = el("div", "arb-row" + (i === a.idx ? " sel" : "") + (has ? " pos" : " flat"));
      row.setAttribute("role", "option");
      // A quiet (stale-only) book on a live feed is still a quotable book;
      // only structural problems or a missing book are flagged.
      const legState = (leg) => (!leg.has_book ? "NONE" : leg.valid ? "OK" : leg.reason === "stale" ? "QUIET" : (leg.reason || "?").toUpperCase().slice(0, 7));
      const ks = legState(q.kalshi), ps = legState(q.polymarket_us);
      const booksOk = (ks === "OK" || ks === "QUIET") && (ps === "OK" || ps === "QUIET");
      const books = el("span", "ar-books " + (booksOk ? "ok" : "bad"),
        ks === "OK" && ps === "OK" ? "OK" : "K:" + ks + " P:" + ps);
      row.append(
        el("span", "ar-net num", has ? fmtSignedCents(b.net_per_contract_ticks) : "—"),
        el("span", "ar-size num", has ? nf.format(Math.round(b.contracts)) : "—"),
        el("span", "num", has ? fmtSignedCents(b.gross_per_contract_ticks) : "—"),
        el("span", "num", has ? fmtSignedCents(-b.fee_per_contract_ticks) : "—"),
        el("span", "ar-label", q.label || (q.kalshi.ticker + " / " + q.polymarket_us.ticker)),
        el("span", "ar-dir", has ? dirLabel(b.direction) : "NO EDGE"),
        el("span", "num", bboText(q.kalshi)),
        el("span", "num", bboText(q.polymarket_us)),
        books,
      );
      row.addEventListener("click", () => { a.idx = i; a.selectedPair = q.pair_id; renderArb(); });
      c.append(row);
    });
    const q = a.quotes[a.idx];
    $("arb-empty").hidden = !!q;
    $("arb-detail").hidden = !q;
    if (!q) return;
    const b = q.best, o = q.other;
    setText("ad-label", q.label || "—");
    setText("ad-dir", b.qty > 0 ? dirLabel(b.direction) : "NO EDGE AT CURRENT BOOKS");
    setText("ad-net", b.qty > 0 ? fmtSignedCents(b.net_per_contract_ticks) : "—");
    setText("ad-gross", b.qty > 0 ? fmtSignedCents(b.gross_per_contract_ticks) : "—");
    setText("ad-fees", b.qty > 0 ? fmtSignedCents(-b.fee_per_contract_ticks) : "—");
    setText("ad-size", b.qty > 0 ? fmtQty(b.qty) + " CTS" : "—");
    setText("ad-total", b.qty > 0 ? fmtDollarsFromTicks(b.net_ticks) : "—");
    const legs = $("ad-legs");
    legs.textContent = "";
    for (const leg of b.legs) {
      const box = el("div", "ad-leg");
      const mk = (k, v) => { const kv = el("div", "kv"); kv.append(el("span", "k", k), el("span", "v num", v)); return kv; };
      box.append(
        mk(leg.venue.toUpperCase().replace("_US", " US"), leg.side.replace("_", " ").toUpperCase()),
        mk("WORST PRICE", b.qty > 0 ? fmtCents(leg.worst_price) + "¢" : "—"),
        mk("FEE", b.qty > 0 ? fmtDollarsFromTicks(leg.fee_ticks) : "—"),
      );
      legs.append(box);
    }
    const fi = q.fee_info || {};
    setText("ad-kfee", (fi.kalshi_fee_type || "—") + " × " + (fi.kalshi_fee_multiplier || "—"));
    setText("ad-pfee", "THETA " + (fi.polymarket_fee_coefficient || "—"));
    const bookLine = (leg) => (leg.has_book ? (leg.valid ? "VALID" : leg.reason === "stale" ? "QUIET (NO RECENT UPDATE)" : "INVALID · " + String(leg.reason || "").toUpperCase()) + " · " + bboText(leg) : "NO BOOK");
    setText("ad-kbook", bookLine(q.kalshi));
    setText("ad-pbook", bookLine(q.polymarket_us));
    setText("ad-onet", o.qty > 0 ? fmtSignedCents(o.net_per_contract_ticks) : "NONE");
    setText("ad-osize", o.qty > 0 ? fmtQty(o.qty) + " CTS" : "—");
    const selRow = c.children[a.idx];
    if (selRow && selRow.scrollIntoView) selRow.scrollIntoView({ block: "nearest" });
  }

  function onArbKey(e) {
    const k = e.key;
    if (k === "Escape") { closeArb(); return true; }
    if (k === "ArrowUp" || k === "ArrowDown") { moveArb(k === "ArrowUp" ? -1 : 1); return true; }
    if (k === "Enter") {
      const q = state.arb.quotes[state.arb.idx];
      if (q && state.byId.has(q.kalshi.market_id)) { closeArb(); select(q.kalshi.market_id); openDes(q.kalshi.market_id); }
      return true;
    }
    return false;
  }

  // ---------- PAPER: simulated-fill ledger ----------
  const PAPER_POLL_MS = 3000;

  function fmtClockUtc(tsMs) {
    if (tsMs == null || !isFinite(tsMs)) return "—";
    const d = new Date(tsMs);
    if (isNaN(d.getTime())) return "—";
    return utcFmt.format(d);
  }

  // Ticks per contract from a ledger total: net_ticks is a dollar total in
  // $0.0001 ticks, qty is in 0.0001-contract units, so the ratio needs the
  // 10000 back to land on per-contract ticks. Guarded for qty 0.
  function perContractTicks(netTicks, qty) {
    if (netTicks == null || !qty || qty <= 0) return null;
    return (netTicks * 10000) / qty;
  }

  function openPaper() {
    const p = state.paper;
    if (state.des.open) closeDes();
    if (state.pairs.open) closePairs();
    if (state.arb.open) closeArb();
    p.open = true;
    $("paperpage").hidden = false;
    document.body.classList.add("des-open");
    renderPaper();
    loadPaper();
    clearInterval(p.timer);
    p.timer = setInterval(loadPaper, PAPER_POLL_MS);
  }

  function closePaper() {
    const p = state.paper;
    p.open = false;
    clearInterval(p.timer);
    p.timer = 0;
    if (p.ctl) { p.ctl.abort(); p.ctl = null; }
    $("paperpage").hidden = true;
    document.body.classList.remove("des-open");
  }

  async function loadPaper() {
    const p = state.paper;
    if (!p.open) return;
    if (p.ctl) p.ctl.abort();
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    p.ctl = ctl;
    try {
      const r = await fetch("/api/paper", { cache: "no-store", signal: ctl ? ctl.signal : undefined });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (!p.open || p.ctl !== ctl) return;
      p.data = data && typeof data === "object" ? data : {};
      p.error = null;
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (!p.open) return;
      p.error = "LOAD FAILED · " + String(err && err.message || err).toUpperCase();
    }
    if (p.ctl === ctl) p.ctl = null;
    schedule("paper");
  }

  function movePaper(d) {
    const p = state.paper;
    const trades = (p.data && Array.isArray(p.data.trades)) ? p.data.trades : [];
    if (!trades.length) return;
    p.idx = Math.max(0, Math.min(trades.length - 1, p.idx + d));
    p.selectedId = trades[p.idx].id != null ? trades[p.idx].id : null;
    renderPaper();
  }

  function renderPaper() {
    const p = state.paper;
    if (!p.open) return;
    const d = p.data || {};
    const enabled = d.enabled === true;
    const totals = d.totals || {};
    const limits = d.limits || {};
    const positions = Array.isArray(d.positions) ? d.positions : [];
    const trades = Array.isArray(d.trades) ? d.trades : [];

    // Keep the cursor on the same trade as the tape grows under it (newest first).
    if (p.selectedId != null) {
      const i = trades.findIndex((t) => t.id === p.selectedId);
      if (i >= 0) p.idx = i;
    }
    p.idx = Math.min(p.idx, Math.max(0, trades.length - 1));

    const stat = p.error
      ? p.error
      : !p.data
        ? "LOADING"
        : (enabled ? "ENABLED" : "DISABLED") + " · " + nf.format(trades.length) + " TRADES · NET " + fmtDollarsFromTicks(totals.net_ticks != null ? totals.net_ticks : 0);
    setText("paper-stat", stat);
    $("paper-stat").classList.toggle("warn", !!p.error);

    $("paper-banner").hidden = !p.data || enabled;

    const st = $("pt-state");
    st.textContent = !p.data ? "—" : enabled ? "ENABLED" : "DISABLED";
    st.className = "v " + (!p.data ? "" : enabled ? "pos" : "off");
    setText("pt-trades", totals.trades != null ? nf.format(totals.trades) : "—");
    setText("pt-qty", totals.qty != null ? fmtQty(totals.qty) : "—");
    setText("pt-cost", fmtDollarsFromTicks(totals.cost_ticks));
    setText("pt-fees", fmtDollarsFromTicks(totals.fee_ticks));
    const netEl = $("pt-net");
    netEl.textContent = fmtDollarsFromTicks(totals.net_ticks);
    netEl.className = "v num" + (totals.net_ticks > 0 ? " pos" : "");

    const pc = $("pos-rows");
    pc.textContent = "";
    if (!positions.length) {
      pc.append(el("div", "pos-row quiet-line", p.data ? "NO POSITIONS" : "—"));
    }
    for (const pos of positions) {
      const row = el("div", "pos-row" + (pos.net_ticks > 0 ? " pos" : ""));
      const label = el("span", "ps-label", pos.label || (pos.pair_id != null ? "PAIR #" + pos.pair_id : "—"));
      label.title = pos.label || "";
      const perCt = perContractTicks(pos.net_ticks, pos.qty);
      const net = el("span", "ps-net num", fmtDollarsFromTicks(pos.net_ticks));
      if (perCt != null) net.title = fmtSignedCents(perCt) + "/CT";
      row.append(
        label,
        el("span", "num", fmtQty(pos.qty)),
        net,
        el("span", "num", pos.trades != null ? nf.format(pos.trades) : "—"),
      );
      pc.append(row);
    }

    const tc = $("ptr-rows");
    tc.textContent = "";
    if (!trades.length) {
      tc.append(el("div", "ptr-row quiet-line", p.data ? (enabled ? "NO TRADES YET — WAITING FOR AN EDGE ABOVE MIN NET" : "NO TRADES") : "—"));
    }
    trades.forEach((t, i) => {
      const row = el("div", "ptr-row" + (i === p.idx ? " sel" : "") + (t.net_ticks > 0 ? " pos" : ""));
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", i === p.idx ? "true" : "false");
      const label = el("span", "pt-label", t.label || (t.pair_id != null ? "PAIR #" + t.pair_id : "—"));
      label.title = t.label || "";
      row.append(
        el("span", "pt-time", fmtClockUtc(t.ts_ms)),
        label,
        el("span", "pt-dir", t.direction ? dirLabel(t.direction) : "—"),
        el("span", "num", fmtQty(t.qty)),
        el("span", "num", fmtDollarsFromTicks(t.cost_ticks)),
        el("span", "num", fmtDollarsFromTicks(t.fee_ticks)),
        el("span", "pt-net num", fmtDollarsFromTicks(t.net_ticks)),
      );
      row.addEventListener("click", () => { p.idx = i; p.selectedId = t.id != null ? t.id : null; renderPaper(); });
      tc.append(row);
    });

    const t = trades[p.idx];
    $("paper-empty").hidden = !!t;
    $("paper-detail").hidden = !t;
    if (t) {
      setText("pp-label", t.label || (t.pair_id != null ? "PAIR #" + t.pair_id : "—"));
      setText("pp-dir", t.direction ? dirLabel(t.direction) : "—");
      setText("pp-id", t.id != null ? "#" + t.id : "—");
      setText("pp-time", t.ts_ms != null && isFinite(t.ts_ms) ? fmtWhen(new Date(t.ts_ms).toISOString()) : "—");
      setText("pp-qty", t.qty != null ? fmtQty(t.qty) + " CTS" : "—");
      setText("pp-cost", fmtDollarsFromTicks(t.cost_ticks));
      setText("pp-fees", fmtDollarsFromTicks(t.fee_ticks));
      const ppNet = $("pp-net");
      ppNet.textContent = fmtDollarsFromTicks(t.net_ticks);
      ppNet.className = "v num" + (t.net_ticks > 0 ? " pos" : "");
      const perCt = perContractTicks(t.net_ticks, t.qty);
      setText("pp-netct", perCt != null ? fmtSignedCents(perCt) : "—");
      const legs = $("pp-legs");
      legs.textContent = "";
      const legList = Array.isArray(t.legs) ? t.legs : [];
      if (!legList.length) legs.append(el("div", "quiet-line", "NO LEG DETAIL"));
      for (const leg of legList) {
        const box = el("div", "ad-leg");
        const mk = (k, v) => { const kv = el("div", "kv"); kv.append(el("span", "k", k), el("span", "v num", v)); return kv; };
        const venue = String(leg.venue || "—").toUpperCase().replace("_US", " US");
        const side = String(leg.side || "—").replace("_", " ").toUpperCase();
        box.append(
          mk(venue, side),
          mk("WORST PRICE", leg.worst_price != null ? fmtCents(leg.worst_price) + "¢" : "—"),
          mk("QTY", leg.qty != null ? fmtQty(leg.qty) : "—"),
          mk("FEE", fmtDollarsFromTicks(leg.fee_ticks)),
        );
        legs.append(box);
      }
    }

    setText("pl-minnet", limits.min_net_ticks != null ? fmtSignedCents(limits.min_net_ticks) : "—");
    setText("pl-maxqty", limits.max_qty_per_pair != null ? fmtQty(limits.max_qty_per_pair) + " CTS" : "—");
    setText("pl-maxnot", fmtDollarsFromTicks(limits.max_notional_ticks));

    const selRow = tc.children[p.idx];
    if (t && selRow && selRow.scrollIntoView) selRow.scrollIntoView({ block: "nearest" });
  }

  function onPaperKey(e) {
    const k = e.key;
    if (k === "Escape") { closePaper(); return true; }
    if (k === "ArrowUp" || k === "ArrowDown") { movePaper(k === "ArrowUp" ? -1 : 1); return true; }
    const up = k.length === 1 ? k.toUpperCase() : k;
    if (up === "R") { loadPaper(); return true; }
    return false;
  }

  // ---------- select-to-copy ----------
  // Anything you select on the terminal lands on the clipboard. Tabular
  // regions are rebuilt cell-by-cell as TSV, because the DOM's own
  // serialization of a flex grid loses the column boundaries — a ladder
  // selection should paste into a spreadsheet with its columns intact.
  const ROW_SEL = ".ladder-row, .mon-row, .tape-row, .mid-row, .kv";

  function cellsOf(row) {
    return row.children.length ? Array.from(row.children) : [row];
  }

  function cellText(node) {
    return node.textContent.replace(/\s+/g, " ").trim();
  }

  function selectionText(sel) {
    const plain = sel.toString();
    let range;
    try {
      range = sel.getRangeAt(0);
    } catch (err) {
      return plain;
    }
    const rows = [];
    for (const row of document.querySelectorAll(ROW_SEL)) {
      if (range.intersectsNode(row)) rows.push(row);
    }
    if (!rows.length) return plain;

    if (rows.length === 1) {
      const hit = cellsOf(rows[0]).filter((c) => range.intersectsNode(c));
      // One cell touched: hand back exactly what was highlighted, so half a
      // number stays half a number. Several: re-join them with tabs.
      if (hit.length <= 1) return plain;
      return hit.map(cellText).filter(Boolean).join("\t");
    }
    const lines = [];
    for (const row of rows) {
      const line = cellsOf(row).map(cellText).filter(Boolean).join("\t");
      if (line) lines.push(line);
    }
    return lines.length ? lines.join("\n") : plain;
  }

  function showCopyToast(text, ok) {
    const box = $("copy-toast");
    const chars = text.length;
    const lines = text.split("\n").length;
    $("ct-label").textContent = ok ? "COPIED" : "COPY BLOCKED";
    $("ct-count").textContent = ok
      ? nf.format(chars) + (chars === 1 ? " CHAR" : " CHARS") + (lines > 1 ? " · " + lines + " ROWS" : "")
      : "CLIPBOARD DENIED";
    $("ct-preview").textContent = text.replace(/\s+/g, " ").trim().slice(0, 120);
    box.classList.toggle("err", !ok);
    box.classList.remove("fading");
    box.hidden = false;
    clearTimeout(copyToastTimer);
    clearTimeout(copyFadeTimer);
    copyToastTimer = setTimeout(() => {
      if (reducedMotion) {
        box.hidden = true;
        return;
      }
      box.classList.add("fading");
      copyFadeTimer = setTimeout(() => {
        box.hidden = true;
        box.classList.remove("fading");
      }, 160);
    }, 1500);
  }

  async function copySelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
    const text = selectionText(sel).replace(/\u00a0/g, " ");  // nbsp -> plain space
    if (!text.trim()) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        showCopyToast(text, true);
        return;
      }
    } catch (err) {
      /* permission or insecure context — fall back below */
    }
    try {
      // Legacy path copies the live selection as-is (no TSV rebuild).
      if (document.execCommand && document.execCommand("copy")) {
        showCopyToast(sel.toString(), true);
        return;
      }
    } catch (err) {
      /* fall through to the honest failure toast */
    }
    showCopyToast(text, false);
  }

  function endSelecting() {
    if (!selecting) return;
    selecting = false;
    schedule();  // flush whatever the drag held back
  }

  // ---------- WS message handlers ----------
  function onHello(m) {
    state.runId = typeof m.run_id === "string" ? m.run_id : null;
    const mk = Array.isArray(m.markets) ? m.markets.slice() : [];
    mk.sort((a, b) => (b.volume_24h || 0) - (a.volume_24h || 0)); // server sends desc; keep the invariant
    state.markets = mk;
    state.byId = new Map(mk.map((x) => [x.market_id, x]));
    buildMonitor();
    if (mk.length) {
      const keep = state.selectedId && state.byId.has(state.selectedId);
      select(keep ? state.selectedId : mk[0].market_id);
    } else {
      state.selectedId = null;
      document.body.classList.remove("has-sel");
      $("mon-rows").removeAttribute("aria-activedescendant");
    }
    for (const id of state.books.keys()) {
      if (state.byId.has(id)) dirtyBooks.add(id);
      else state.books.delete(id); // prune books for markets dropped by the re-sync
    }
    schedule("monitor", "depth", "status");
  }

  function onBook(m) {
    if (typeof m.market_id !== "string") return;
    state.books.set(m.market_id, {
      bids: Array.isArray(m.bids) ? m.bids : [],
      asks: Array.isArray(m.asks) ? m.asks : [],
      valid: m.valid !== false,
      reason: m.reason != null ? m.reason : null,
      age_ms: typeof m.age_ms === "number" ? m.age_ms : 0,
      ts_ms: m.ts_ms,
      recvAt: performance.now(),
    });
    dirtyBooks.add(m.market_id);
    schedule("monitor");
  }

  function onDelta(m) {
    if (typeof m.market_id !== "string") return;
    tapeTotal += 1;
    tapeQueue.push(m);
    if (tapeQueue.length > TAPE_QUEUE_CAP) tapeQueue.splice(0, tapeQueue.length - TAPE_QUEUE_CAP);
    // stats messages drain this bucket ~1/s; cap it so a stats stall can't grow it unboundedly
    if (typeof m.latency_ms === "number" && latBucket.length < 4096) latBucket.push(m.latency_ms);
  }

  function onStats(m) {
    state.stats = m;
    state.statsBuf.push(m);
    if (state.statsBuf.length > STATS_KEEP) state.statsBuf.shift();
    pushLatencyPoint(m);
    schedule("latnums", "system", "status", "spark", "poly");
  }

  function handleMsg(m) {
    if (!m || typeof m !== "object") return;
    switch (m.t) {
      case "hello": onHello(m); break;
        case "arb": onArb(m); break;
      case "book": onBook(m); break;
      case "delta": onDelta(m); break;
      case "stats": onStats(m); break;
      default: break; // unknown types tolerated silently
    }
  }

  // ---------- WebSocket with jittered exponential backoff (0.5s..15s) ----------
  let ws = null;
  let wsAttempts = 0;
  let wsTimer = 0;

  function connect() {
    clearTimeout(wsTimer);
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    let sock;
    try {
      sock = new WebSocket(proto + location.host + "/ws");
    } catch (err) {
      scheduleReconnect();
      return;
    }
    ws = sock;
    sock.onopen = () => {
      if (sock !== ws) return;
      wsAttempts = 0;
      setConn("live");
    };
    sock.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (err) { return; }
      handleMsg(m);
    };
    sock.onerror = () => { /* close follows */ };
    sock.onclose = () => {
      if (sock !== ws) return;
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    wsAttempts += 1;
    setConn(wsAttempts >= 3 ? "down" : "reconnecting");
    const base = Math.min(15000, 500 * Math.pow(2, wsAttempts - 1));
    const delay = Math.max(500, base / 2 + Math.random() * (base / 2)); // jittered, floored at 0.5s, capped 15s
    clearTimeout(wsTimer);
    wsTimer = setTimeout(connect, delay);
  }

  // ---------- /api/status poll every 10s (tolerate failures) ----------
  async function pollStatus() {
    try {
      const ctl = typeof AbortController === "function" ? new AbortController() : null;
      const to = ctl ? setTimeout(() => ctl.abort(), 5000) : 0;
      const r = await fetch("/api/status", { cache: "no-store", signal: ctl ? ctl.signal : undefined });
      if (to) clearTimeout(to);
      if (!r.ok) return;
      state.status = await r.json();
      state.statusAt = Date.now();
      schedule("status", "system", "poly");
    } catch (err) {
      /* keep last known data */
    }
  }

  // ---------- init ----------
  buildLadder();
  setupCanvas();
  renderClocks();
  renderCmd();
  schedule("depth", "status", "poly", "system", "spark");

  document.addEventListener("keydown", onKey);

  // Select-to-copy. mouseup carries the transient user activation the async
  // clipboard API requires, so the write is done there rather than on
  // selectionchange (which fires mid-drag and without activation).
  document.addEventListener("mousedown", (e) => {
    if (e.button === 0) selecting = true;
  });
  window.addEventListener("mouseup", (e) => {
    if (e.button !== 0) return;
    endSelecting();
    copySelection();
  });
  // Button released outside the window: the next move over the page reports
  // no buttons held, which is when we un-pause rendering.
  document.addEventListener("mousemove", (e) => {
    if (selecting && e.buttons === 0) endSelecting();
  });
  window.addEventListener("blur", endSelecting);
  // Select-all is a keyboard gesture, and it also carries activation.
  document.addEventListener("keyup", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "a" || e.key === "A")) copySelection();
  });
  spark.addEventListener("mousemove", (e) => {
    const r = spark.getBoundingClientRect();
    sparkHover = e.clientX - r.left;
    schedule("spark");
  });
  spark.addEventListener("mouseleave", () => {
    sparkHover = null;
    $("spark-tip").hidden = true;
    schedule("spark");
  });
  window.addEventListener("resize", () => {
    setupCanvas();
    schedule("spark");
  });

  connect();
  pollStatus();
  setInterval(pollStatus, STATUS_POLL_MS);
  setInterval(drainTape, TAPE_DRAIN_MS);
  setInterval(() => {
    renderClocks();
    if (state.selectedId) schedule("depth"); // client-side book aging -> STALE banner
    if (state.status) schedule("poly");      // "checked Ns ago" ticks
    if (state.des.open) schedule("des");     // book age / fetched-ago tick
  }, 1000);
})();
