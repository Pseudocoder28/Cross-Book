/* core/ws.js — one WebSocket for the whole session.

   Navigation NEVER touches this socket: routing is client side precisely so
   the tape, the latency window and every book survive a page change. The
   core handles `hello book delta stats` itself (they feed state.books,
   state.markets, the tape queue and the latency window); pages register for
   the frames they care about with onMessage(). */

import { state, schedule } from "./state.js";

const STATS_KEEP = 300;         // stats buffer
const TAPE_QUEUE_CAP = 24;

const handlers = new Map();     // frame type ("*" = all) -> [fn]

let ws = null;
let wsAttempts = 0;
let wsTimer = 0;
let latBucket = [];             // delta latencies inside the current 1s bucket

/** Register a handler for one frame type: hello book delta stats arb paper.
    "*" gets every frame. Handlers fire whether or not the page is mounted —
    a background page must keep its snapshot current so arriving on it is
    instant. Never render from here; mutate state and call schedule(). */
export function onMessage(type, fn) {
  const list = handlers.get(type);
  if (list) list.push(fn);
  else handlers.set(type, [fn]);
}

/** "connecting" | "open" | "closed" */
export function wsState() {
  if (state.conn === "live") return "open";
  if (state.conn === "connecting") return "connecting";
  return "closed";
}

// ---------- core frame handling ----------

function onHello(m) {
  state.runId = typeof m.run_id === "string" ? m.run_id : null;
  const mk = Array.isArray(m.markets) ? m.markets.slice() : [];
  mk.sort((a, b) => (b.volume_24h || 0) - (a.volume_24h || 0)); // server sends desc; keep the invariant
  state.markets = mk;
  state.byId = new Map(mk.map((x) => [x.market_id, x]));
  if (mk.length) {
    const keep = state.selectedId && state.byId.has(state.selectedId);
    if (!keep) state.selectedId = mk[0].market_id;
    document.body.classList.add("has-sel");
  } else {
    state.selectedId = null;
    document.body.classList.remove("has-sel");
  }
  for (const id of state.books.keys()) {
    if (state.byId.has(id)) state.dirtyBooks.add(id);
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
  state.dirtyBooks.add(m.market_id);
  schedule("monitor");
  // the depth ladder and the market page's live-book block track the selection
  if (m.market_id === state.selectedId) schedule("depth", "des", "market");
}

function onDelta(m) {
  if (typeof m.market_id !== "string") return;
  state.tape.total += 1;
  state.tape.queue.push(m);
  const q = state.tape.queue;
  if (q.length > TAPE_QUEUE_CAP) q.splice(0, q.length - TAPE_QUEUE_CAP);
  // stats messages drain this bucket ~1/s; cap it so a stats stall can't grow it unboundedly
  if (typeof m.latency_ms === "number" && latBucket.length < 4096) latBucket.push(m.latency_ms);
}

function pushLatencyPoint() {
  let v = null; // a delta-free second is a gap, not a repeat of the last sample
  if (latBucket.length) {
    latBucket.sort((a, b) => a - b);
    v = latBucket[Math.floor(latBucket.length / 2)]; // 1s median bucket
  }
  latBucket = [];
  state.latPoints.push({ v });
  if (state.latPoints.length > STATS_KEEP) state.latPoints.shift();
}

function onStats(m) {
  state.stats = m;
  state.statsBuf.push(m);
  if (state.statsBuf.length > STATS_KEEP) state.statsBuf.shift();
  pushLatencyPoint();
  schedule("latnums", "system", "status", "spark", "poly");
}

function dispatch(m) {
  const list = handlers.get(m.t);
  if (list) for (const fn of list) fn(m);
  const all = handlers.get("*");
  if (all) for (const fn of all) fn(m);
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
  dispatch(m);
}

function setConn(s) {
  state.conn = s;
  schedule("status");
}

// ---------- WebSocket with jittered exponential backoff (0.5s..15s) ----------

/** Open the session socket. Called once, by main.js. */
export function connect() {
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
