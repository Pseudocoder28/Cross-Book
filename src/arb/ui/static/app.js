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
  };

  const dirtyBooks = new Set();
  const dirty = { monitor: false, depth: false, latnums: false, system: false, poly: false, status: false, spark: false };
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
        if (state.selectedId && ids.includes(state.selectedId)) dirty.depth = true;
      }
    }
    if (dirty.depth) { dirty.depth = false; renderDepth(); }
    if (dirty.latnums) { dirty.latnums = false; renderLatNums(); }
    if (dirty.system) { dirty.system = false; renderSystem(); }
    if (dirty.poly) { dirty.poly = false; renderPoly(); }
    if (dirty.status) { dirty.status = false; renderStatusBar(); }
    if (dirty.spark) { dirty.spark = false; drawSpark(); }
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
      const row = el("div", "mon-row");
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
    if (v.detail) {
      $("poly-sub").textContent = v.detail.toUpperCase();
      $("poly-sub").title = v.detail;
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
    elm.className = "val vstate " + (st === "live" ? "st-live" : st === "connecting" ? "st-warn" : st ? "st-down" : "st-dim");
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
    const q = raw.replace(/\s*<\s*GO\s*>\s*$/i, "").replace(/\s+GO$/i, "").trim().toUpperCase();
    if (!q) return;
    const tickers = state.markets;
    const hit =
      tickers.find((m) => (m.ticker || "").toUpperCase() === q) ||
      tickers.find((m) => (m.ticker || "").toUpperCase().startsWith(q)) ||
      tickers.find((m) => (m.ticker || "").toUpperCase().includes(q));
    if (hit) {
      select(hit.market_id);
      cmdMsg(hit.ticker + " <GO>", "ok");
    } else {
      cmdMsg("NO MATCH · " + q, "err");
    }
  }

  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    const k = e.key;
    if (k === "ArrowUp" || k === "ArrowDown") {
      e.preventDefault();
      moveSel(k === "ArrowUp" ? -1 : 1);
      return;
    }
    if (k === "Enter") {
      e.preventDefault();
      execCmd();
      return;
    }
    if (k === "Escape") {
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
    schedule("latnums", "system", "status", "spark");
  }

  function handleMsg(m) {
    if (!m || typeof m !== "object") return;
    switch (m.t) {
      case "hello": onHello(m); break;
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
  }, 1000);
})();
