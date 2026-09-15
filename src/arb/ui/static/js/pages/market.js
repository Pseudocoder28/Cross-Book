/* pages/market.js — DES: the market description page, one route per market.

   Ported from the pre-multipage static/app.js, see git history (openDes / closeDes / renderDes). The markup lives
   in index.html as <section id="des">; this module binds to it and adds the
   page chrome the overlay never needed: a way back, where you are in the list,
   and which venue you are actually looking at.

   Two things are easy to get wrong here and both are load-bearing:
   - the dirty key is "des", not "market". ws.js and state.select() schedule
     "des", so the renderer is registered under that name too or a book tick
     never repaints this page.
   - the market id is opaque and may contain "/", so it is read off
     location.pathname by prefix, never by splitting on "/". */

import { $, el, setText } from "../core/dom.js";
import { state, schedule, registerRenderer, select, selectRelative } from "../core/state.js";
import { navigate } from "../core/router.js";
import * as cmd from "../core/cmd.js";
import {
  nf, fmtCents, fmtMid, fmtQty, fmtAge, fmtAgo, fmtCentsPct, fmtCount, fmtWhen,
} from "../core/format.js";

const PREFIX = "/market/";
const TICK_MS = 1000;           // ages ("BOOK AGE", "... AGO") must keep moving

let mounted = false;
let tickTimer = 0;
let lastSrcKey = null;          // settlement sources currently in the DOM

// ---------- page chrome added to the ported markup ----------

const desRoot = $("des");
const headEl = desRoot ? desRoot.querySelector(".des-head") : null;
const titleEl = desRoot ? desRoot.querySelector(".des-title") : null;
const leftEl = desRoot ? desRoot.querySelector(".des-left") : null;
const anatomyEl = desRoot ? desRoot.querySelector(".des-anatomy") : null;
const rightEl = desRoot ? desRoot.querySelector(".des-right") : null;

// "◀ MONITOR": a real anchor with data-page, so the router's delegated click
// handler routes it and middle-click / copy-link still do the right thing.
const backEl = el("a", "des-back", "◀ MONITOR");
backEl.href = "/";
backEl.dataset.page = "monitor";
backEl.title = "BACK TO THE MARKET MONITOR";
if (headEl && titleEl) headEl.insertBefore(backEl, titleEl);

// The venue badge: this page serves Kalshi AND Polymarket US and the ported
// layout never said which.
const venueEl = el("span", "des-venue v-unknown", "—");
if (titleEl) titleEl.append(venueEl);

const copyEl = el("button", "des-copy", "COPY");
copyEl.type = "button";
copyEl.title = "COPY THIS TICKER TO THE CLIPBOARD";
if (titleEl) titleEl.append(copyEl);

// Arrows page through markets, so say where in the list you are.
const posEl = el("span", "des-pos", "—");
if (headEl && $("des-stat")) headEl.insertBefore(posEl, $("des-stat"));

// The opaque id is what the URL carries and what `arb` CLI commands take, so
// it gets its own .kv row — .kv is what select-to-copy picks up.
const idValEl = el("span", "v");
const idKvEl = el("span", "kv");
idKvEl.append(el("span", "k", "ID"), idValEl);
if (anatomyEl) anatomyEl.append(idKvEl);

// Venue again, in the metadata column, where it is copyable with the rest.
const venueKvEl = el("div", "kv");
const venueValEl = el("span", "v");
venueKvEl.append(el("span", "k", "VENUE"), venueValEl);
if (rightEl && rightEl.firstElementChild) {
  rightEl.insertBefore(venueKvEl, rightEl.firstElementChild.nextSibling);
}

// A deep link to a market that has rolled off the discovery list is expected;
// it gets a named state, not a page of em dashes.
const unknownIdEl = el("div", "des-unknown-id", "");
const unknownEl = el("div", "des-unknown");
unknownEl.id = "des-unknown";
unknownEl.hidden = true;
unknownEl.append(
  el("div", "des-unknown-title", "UNKNOWN MARKET"),
  unknownIdEl,
  el("div", "des-unknown-hint",
    "THIS ID IS NOT IN THE RUNNING UNIVERSE. IT MAY HAVE CLOSED, ROLLED OFF THE "
    + "DISCOVERY LIST, OR BELONG TO AN EARLIER RUN. PRESS ESC OR CLICK ◀ MONITOR."),
);
if (leftEl) leftEl.insertBefore(unknownEl, leftEl.firstChild);

// The footer is a promise about the keys; keep it true. Two deliberate lines
// rather than one that wraps by accident in the 380px column.
setText("des-foot", "↑↓ NEXT MARKET · ESC CLOSE · COPY = TICKER");
const footNoteEl = el("div", "des-foot-note", "DRAG ANY ROW TO COPY IT");
if ($("des-foot")) $("des-foot").after(footNoteEl);

// ---------- helpers ----------

/** The id from the URL. The server routes /market/{id:path}, so an id holding
    a "/" arrives as several segments — take it by prefix, never by split. */
function currentId(params) {
  const p = location.pathname;
  if (p.startsWith(PREFIX)) {
    const raw = p.slice(PREFIX.length);
    try { return decodeURIComponent(raw); } catch (err) { return raw; }
  }
  return (params && params.id) || "";
}

/** Venue from the detail, then the hello row, then the id prefix — the last
    one is what a cold deep link has before the hello frame lands. */
function venueOf(d, m, id) {
  let v = (d && d.venue) || m.venue || "";
  if (!v && id) {
    const c = id.indexOf(":");          // ids are "<venue>:<native identifier>"
    v = c > 0 ? id.slice(0, c) : "";
  }
  if (v === "kalshi") return { text: "KALSHI", cls: "v-kalshi" };
  if (v === "polymarket_us") return { text: "POLYMARKET US", cls: "v-poly" };
  return { text: "—", cls: "v-unknown" };
}

/** Position in the SAME array selectRelative walks, so the count matches the
    arrows exactly. */
function posText(id) {
  const n = state.markets.length;
  if (!n) return "—";
  const i = state.markets.findIndex((x) => x.market_id === id);
  if (i < 0) return "NOT IN UNIVERSE";
  return "MARKET " + (i + 1) + " OF " + n;
}

async function copyTicker() {
  const des = state.des;
  const m = state.byId.get(des.marketId) || {};
  const text = m.ticker || des.marketId || "";
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      cmd.message("COPIED " + text, "ok");
      return;
    }
  } catch (err) {
    /* denied or insecure context — fall through to the honest message */
  }
  cmd.message("CLIPBOARD DENIED", "err");
}

copyEl.addEventListener("click", (e) => {
  copyTicker();
  if (e.detail > 0) copyEl.blur();   // pointer click: hand the keyboard back to ARB>
});

// ---------- DES: market description page (ported from app.js) ----------

function openDes(id) {
  if (!id) return;
  const des = state.des;
  if (des.ctl) des.ctl.abort();
  des.open = true;
  des.marketId = id;
  des.detail = null;
  des.error = null;
  des.unknown = false;
  schedule("des");
  const ctl = typeof AbortController === "function" ? new AbortController() : null;
  des.ctl = ctl;
  fetch("/api/markets/" + encodeURIComponent(id), { cache: "no-store", signal: ctl ? ctl.signal : undefined })
    .then((r) => {
      if (r.status === 404) {
        const err = new Error("HTTP 404");
        err.notFound = true;
        return Promise.reject(err);
      }
      return r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status));
    })
    .then((detail) => {
      if (des.marketId !== id) return; // paged away while loading
      des.detail = detail;
      schedule("des");
    })
    .catch((err) => {
      if (des.marketId !== id || (err && err.name === "AbortError")) return;
      if (err && err.notFound) des.unknown = true;
      else des.error = "NO DESCRIPTION AVAILABLE · " + String(err.message || err).toUpperCase();
      schedule("des");
    });
}

function closeDes() {
  const des = state.des;
  if (des.ctl) des.ctl.abort();
  des.open = false;
  des.ctl = null;
}

function renderDes() {
  const des = state.des;
  if (!des.open) return;
  const m = state.byId.get(des.marketId) || {};
  const d = des.detail;
  setText("des-ticker", m.ticker || des.marketId);
  setText("des-stat", des.unknown ? "UNKNOWN" : des.error ? "ERROR" : d ? (d.source === "live" ? "LIVE" : "DISCOVERY") + " · " + fmtAgo(Date.now() - d.fetched_at_ms) + " AGO" : "LOADING");
  const errEl = $("des-error");
  errEl.hidden = !des.error;
  const errText = des.error || "";
  if (errEl.textContent !== errText) errEl.textContent = errText;

  // ADDED: page chrome — where you are, where you can go, what venue this is.
  if (desRoot) desRoot.classList.toggle("unknown", Boolean(des.unknown));
  unknownEl.hidden = !des.unknown;
  if (des.unknown && unknownIdEl.textContent !== des.marketId) unknownIdEl.textContent = des.marketId || "";
  const pos = des.unknown ? "NOT IN UNIVERSE" : posText(des.marketId);
  if (posEl.textContent !== pos) posEl.textContent = pos;
  const ven = venueOf(d, m, des.marketId);
  if (venueEl.textContent !== ven.text) venueEl.textContent = ven.text;
  venueEl.className = "des-venue " + ven.cls;
  if (venueValEl.textContent !== ven.text) venueValEl.textContent = ven.text;
  if (idValEl.textContent !== (des.marketId || "—")) idValEl.textContent = des.marketId || "—";
  copyEl.disabled = !(m.ticker || des.marketId);

  setText("des-event", d ? d.event_title || m.title || "—" : m.title || "—");
  setText("des-sub", d ? [d.yes_sub_title, d.event_sub_title].filter(Boolean).join(" · ") || "—" : "—");
  setText("des-series", d ? d.series_ticker || "—" : "—");
  setText("des-eventtk", d ? d.event_ticker || "—" : "—");
  setText("des-mkt", m.ticker || "—");
  setText("des-rules", d ? d.rules_primary || "—" : "—");
  const r2 = d && d.rules_secondary;
  $("des-rules2-wrap").hidden = !r2;
  setText("des-rules2", r2 || "");
  // Settlement sources are static per market, but this render also runs on the
  // 1s age tick and on every book frame. Rebuilding the list would replace the
  // text nodes a double-click selection is anchored in, so a URL picked out for
  // copying would come back empty a second later. Rebuild only on real change.
  const src = $("des-sources");
  const srcKey = d
    ? (d.settlement_sources || [])
      .map((s) => (s.name || "—") + "\u0000" + (s.url || "")).join("\u0001")
    : "\u0002none";
  if (srcKey !== lastSrcKey) {
    lastSrcKey = srcKey;
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
  }

  const statusEl = $("des-status");
  const statusText = d ? (d.status || "—").toUpperCase() + (d.result ? " · " + d.result.toUpperCase() : "") : "—";
  if (statusEl.textContent !== statusText) statusEl.textContent = statusText;
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

// ws.js and state.select() schedule "des", never "market". Without this the
// page never repaints on a book tick.
registerRenderer("des", () => { if (mounted) renderDes(); });

function tick() {
  if (state.des.open) schedule("des"); // client-side ageing of BOOK AGE / ... AGO
}

export default {
  id: "market",
  path: "/market/:id",
  title: "DES",
  nav: false,
  root: "des",

  mount(params) {
    mounted = true;
    const id = currentId(params);
    // app.js guarded on state.byId.has(id) before opening; a deep link can
    // arrive before the hello frame, so the server is the authority now and
    // a 404 is what tells us the id is unknown.
    select(id);
    openDes(id);
    if (!tickTimer) tickTimer = setInterval(tick, TICK_MS);
  },

  unmount() {
    mounted = false;
    clearInterval(tickTimer);
    tickTimer = 0;
    closeDes();
  },

  render() {
    renderDes();
  },

  onKey(e) {
    const k = e.key;
    // app.js's select() ended with "if (state.des.open && ...) openDes(id)":
    // arrows page through markets while this page is open. replace: true so
    // arrowing through twenty markets does not bury the back button.
    if (k === "ArrowUp" || k === "ArrowDown") {
      if (selectRelative(k === "ArrowUp" ? -1 : 1) && state.selectedId) {
        navigate(PREFIX + encodeURIComponent(state.selectedId), { replace: true });
      }
      return true;   // always claimed: the global binding would step a second time
    }
    return false;    // Escape belongs to core/keys.js
  },

  onMessage(msg) {
    // A cold deep link mounts before hello: adopt the selection once the
    // universe arrives so the arrows and the monitor highlight agree.
    if (!mounted || !msg || msg.t !== "hello") return;
    const id = state.des.marketId;
    if (id && state.byId.has(id) && state.selectedId !== id) select(id);
    schedule("des");
  },
};
