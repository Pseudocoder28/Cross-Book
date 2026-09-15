/* pages/help.js — the reference card: keys, commands, screens, vocabulary.

   The terminal has a lot of hidden capability and, until this page, no way to
   discover any of it. A help page that lies is worse than none, so as much of
   it as possible is DERIVED rather than typed out:

     - the page list, their paths and their nav numbers come from the router
       (navPages()), so a page added or reordered later is documented for free;
     - the per-page key list is read out of each page's own `.des-foot` footer
       strip in the DOM, so it cannot drift from what the page advertises;
     - the Alt+N range is computed from how many nav pages actually exist.

   The rest (globals, commands, glossary) is written against the source it
   describes — core/keys.js, core/cmd.js — and the project docs (data-model.md,
   engine.md, pairs.md, ui.md). Nothing here is invented.

   The page is built once, lazily, on first mount: navPages() is only complete
   after main.js has registered every module. */

import { $, el, isReducedMotion } from "../core/dom.js";
import { navPages } from "../core/router.js";
import * as cmd from "../core/cmd.js";

const LINE_SCROLL = 48;         // arrow key scroll step, three text lines
const PAGE_SCROLL = 0.9;        // PgUp/PgDn move most of a viewport


// ---------- what each screen is, and what its columns mean ----------
// Keyed by page id. Titles/paths/nav numbers are NOT stored here — they come
// from the router. `cols` entries are [column label, meaning].

const SCREENS = {
  monitor: {
    lead: "The default workspace: every market this run is watching, the depth ladder for "
      + "the selected one, a tape of level changes and the latency panel. Click or arrow to "
      + "select a row; the ladder, the tape highlight and DES all follow the selection.",
    groups: [
      {
        name: "MARKET LIST",
        cols: [
          ["#", "row number; rows 1-9 carry a 1-9 quick-select shortcut, shown in amber"],
          ["VEN", "K = Kalshi (streamed), PM = Polymarket US (REST-polled)"],
          ["TICKER", "the venue's own market identifier; hover for title and 24h volume"],
          ["BID", "best YES bid, in dollars per contract"],
          ["ASK", "best YES ask"],
          ["MID", "(bid + ask) / 2, shown to three decimals when it lands on half a cent"],
          ["SPR", "ask − bid, the spread you would cross to take"],
        ],
        note: "The filter box matches ticker, title or market id; ALL/K/PM filters by venue; "
          + "a column header sorts, and clicking it again reverses. Filter and sort are "
          + "remembered across reloads. Markets with no book yet always sort last.",
      },
      {
        name: "DEPTH LADDER",
        cols: [
          ["BID QTY", "resting size at that bid, with a bar scaled to the largest level shown"],
          ["YES", "the level's YES price"],
          ["NO", "the same level as a NO price: 10000 − YES, in ticks"],
          ["ASK QTY", "resting size at that ask"],
        ],
        note: "Asks run down to the MID · SPR seam, bids run below it, best prices closest to "
          + "the seam. A banner reads QUIET when the book has simply gone quiet, or "
          + "INVALID · <REASON> when the local state is structurally wrong.",
      },
      {
        name: "TAPE",
        cols: [
          ["time", "mm:ss.d, UTC, from the venue's own timestamp"],
          ["B / A", "which side of the book changed"],
          ["price", "the level's price"],
          ["Δqty", "signed change in resting size at that level"],
          ["lat", "one-way latency for that message, when the venue timestamped it"],
          ["ticker", "the market the change belongs to"],
        ],
        note: "Newest on top, rendered at most a dozen rows a second so a burst cannot "
          + "outrun the eye. The row for the selected market is highlighted.",
      },
      {
        name: "LATENCY",
        cols: [
          ["chart", "one-second medians of one-way latency over the last 120s, with dotted "
            + "MED and P95 rules; a gap means no message arrived that second"],
          ["LAST / MED / P95 / N", "the current sample, median, 95th percentile and sample "
            + "count over the last 512 deltas"],
          ["RTT", "WebSocket keepalive round trip — no venue clock involved"],
          ["SKEW", "estimated local clock offset: median one-way − RTT/2"],
        ],
        note: "An amber tick at the top or bottom edge is a sample outside the chart's "
          + "domain, including a negative one. The panel flips to CLOCK SKEW · TRUST RTT/2 "
          + "when the median goes negative or |skew| exceeds 25ms.",
      },
    ],
  },
  arb: {
    lead: "Every confirmed pair being tracked this run, ranked by net edge per contract. "
      + "Both directions are measured; the row shows the better one. Taker fees on both legs "
      + "are already subtracted. Measurement only — nothing here places an order.",
    groups: [
      {
        name: "COLUMNS",
        cols: [
          ["NET/CT", "gross minus both legs' fees, per contract — the number the list is sorted by"],
          ["SIZE", "contracts the two ladders actually offer at a profitable marginal price"],
          ["GROSS", "edge per contract before fees"],
          ["FEES", "both legs' taker fees per contract"],
          ["PAIR", "the confirmed pair's label"],
          ["DIRECTION", "which venue's YES is bought and which venue's NO"],
          ["K BID/ASK", "the Kalshi leg's best bid and ask"],
          ["P BID/ASK", "the Polymarket US leg's best bid and ask"],
          ["BOOKS", "each leg's book state: valid, quiet, or the structural reason it is not"],
        ],
        note: "Selecting a row shows both legs, the fee model in play and the reverse "
          + "direction's numbers. The screen is empty until pairs are confirmed and "
          + "`arb ui` is restarted with --pairs-top > 0.",
      },
    ],
  },
  pairs: {
    lead: "The review queue: candidate Kalshi ↔ Polymarket US market pairings proposed by the "
      + "matcher, for a human to confirm or reject. A proposed pair is inert no matter how "
      + "high it scores; only confirmed pairs ever reach the fee and edge engine.",
    groups: [
      {
        name: "COLUMNS",
        cols: [
          ["SCORE", "0.6 × event score + 0.4 × outcome similarity, 0 to 1"],
          ["KALSHI", "the Kalshi leg: event title, outcome and ticker"],
          ["POLYMARKET US", "the Polymarket US leg: event title, outcome and slug"],
          ["STATUS", "proposed, confirmed or rejected"],
        ],
        note: "The right pane carries both legs' full resolution rules, which is what you are "
          + "actually judging, plus the features behind the score: title similarity, outcome "
          + "similarity, outcome overlap and how many days apart the two close.",
      },
    ],
  },
  paper: {
    lead: "The simulated ledger. When `arb ui --paper` is running, every measured edge that "
      + "clears the risk limits is 'taken' at the liquidity the edge walk already consumed. "
      + "No venue is contacted and no order exists; the ledger is a record of what the "
      + "measured edges would have been worth.",
    groups: [
      {
        name: "TOTALS",
        cols: [
          ["STATE", "whether paper trading is enabled for this run"],
          ["TRADES / CONTRACTS", "simulated fills taken, and size across all of them"],
          ["COST", "combined cost of both legs"],
          ["FEES", "combined taker fees"],
          ["EXPECTED NET", "net edge locked in at settlement, summed"],
        ],
      },
      {
        name: "POSITIONS AND TRADES",
        cols: [
          ["PAIR", "the confirmed pair the fill belongs to"],
          ["QTY / CONTRACTS", "size, in contracts"],
          ["DIRECTION", "which leg was YES and which was NO"],
          ["NET", "that trade's expected net at settlement"],
        ],
        note: "RISK LIMITS shows the caps in force: minimum net per contract, maximum "
          + "contracts per pair and maximum total notional (--min-net-ticks, "
          + "--max-cts-per-pair, --max-notional).",
      },
    ],
  },
  system: {
    lead: "The plumbing, one card per subsystem. This is where you look when a number "
      + "elsewhere stops moving. Read-only diagnostics, live off the one-second stats frame "
      + "and /api/status.",
    groups: [
      {
        name: "CARDS",
        cols: [
          ["ENGINE", "inbound messages this run and the last second's rate, plus parse errors "
            + "and sequence gaps — both should stay at zero, and turn amber when they do not"],
          ["RECORDER", "whether this run is writing raw messages to Postgres, and how many "
            + "were enqueued versus dropped"],
          ["DATABASE", "connection state, total recorded rows and the row count per run id — "
            + "those ids are what `arb replay` takes"],
          ["CLOCK & LATENCY", "one-way latency percentiles, keepalive RTT and the clock-skew "
            + "estimate derived from them"],
          ["KALSHI · WEBSOCKET", "the streamed venue's transport state"],
          ["POLYMARKET US", "the polled venue: poll count, 429 rate-limit responses, errors "
            + "and the age of the last successful book"],
          ["OBSERVABILITY", "where the same numbers live outside the terminal — the Prometheus "
            + "metric names and the provisioned Grafana dashboard"],
        ],
      },
    ],
  },
  help: {
    lead: "This page. Built from the live router and from each page's own footer strip, so it "
      + "describes the terminal you are actually running.",
    groups: [],
  },
  market: {
    lead: "The description page for one market — Enter on a selection, `<TICKER> DES`, or "
      + "double-click a row. It is the page that answers 'what exactly does this market pay "
      + "out on', which is the question a cross-venue pair lives or dies by.",
    groups: [
      {
        name: "WHAT IT SHOWS",
        cols: [
          ["ticker anatomy", "series → event → market, the three levels of a venue identifier"],
          ["resolution rules", "the venue's own rules text, in full, plus secondary rules and "
            + "settlement sources"],
          ["implied yes", "the quote read as a probability — with $0.0001 ticks, 50 ticks is "
            + "$0.0050, 0.50¢ and 0.50% implied all at once"],
          ["live book", "best levels, depth per side, level count and book age"],
          ["activity", "volume, 24h volume and open interest"],
          ["timeline", "open, close and expected expiration, in UTC and ET — rules texts are "
            + "usually written in ET"],
        ],
      },
    ],
  },
};

// The shell around every page, described once.
const FRAME = [
  ["STATUS BAR", "connection state, the run id (copy it whole — `arb replay` needs all of it), "
    + "each venue's state, whether this run is recording, and the UTC and ET clocks"],
  ["NAV", "one tab per page; the small digit is its Alt shortcut"],
  ["ARB>", "the command line. Any printable key you press anywhere lands here"],
  ["KEYS STRIP", "the always-visible reminder at the bottom of the frame"],
  ["SELECT TO COPY", "drag across any rows and they are copied as tab-separated values, one "
    + "row per line; a selection inside one cell copies exactly what was highlighted"],
];

// ---------- commands (core/cmd.js) ----------

const COMMANDS = [
  ["MON / MONITOR", "go to the market monitor", "MON"],
  ["ARB", "go to the cross-venue edge screen", "ARB"],
  ["PAIRS", "go to the pair review queue", "PAIRS"],
  ["PAPER", "go to the simulated ledger", "PAPER"],
  ["SYS / SYSTEM", "go to the system screen", "SYS"],
  ["HELP / ?", "come back here", "?"],
  ["BACK", "browser history back, one step", "BACK"],
  ["DES", "description page for the current selection; says NO MARKET SELECTED if there is none", "DES"],
  ["<TICKER>", "select a market: exact ticker first, then prefix, then substring", "KXPRES"],
  ["<TICKER> DES", "select it and open its description page", "KXPRES DES"],
  ["<GO>", "a trailing <GO> or GO is stripped before the command runs", "KXPRES <GO>"],
];

// ---------- global keys (core/keys.js) ----------

function globalKeys() {
  const n = navPages().length;
  return [
    ["any character", "types into the ARB> line, uppercased (48 characters max)"],
    ["⏎", "empty command line: open DES for the selection. Otherwise: run the command"],
    ["⌫", "delete the last character of the command"],
    ["ESC", "clear a half-typed command; if there is none, leave the page for MONITOR"],
    ["↑ ↓", "move the market selection (a page may use these for its own list)"],
    ["1 - 9", "quick-select that row — monitor page only, and only with an empty command line"],
    ["ALT+1 - ALT+" + n, "jump straight to the nth page in the nav"],
    ["ALT+[  ALT+]", "previous / next page, wrapping"],
    ["ALT+← ALT+→", "browser history back / forward"],
    ["⌘A / CTRL+A", "select the whole screen and copy it"],
  ];
}

// Keys this page adds. Kept in one place so the footer strip below cannot
// disagree with the table above it.
const HELP_KEYS = [
  ["↑ ↓", "scroll this page"],
  ["PGUP PGDN", "scroll by a screen"],
  ["HOME END", "jump to the top / bottom"],
  ["/", "focus the filter box (with an empty command line)"],
];
const HELP_FOOT = "↑↓ SCROLL · PGUP/PGDN · HOME/END · / FILTER · ESC CLOSE";

// ---------- glossary (docs/data-model.md, engine.md, pairs.md, ui.md) ----------

const GLOSSARY = [
  ["TICK", "$0.0001. One cent is 100 ticks and $0.555 is 5550. Prices cross the wire as "
    + "integer ticks and are divided by 100 only to be displayed as cents — no price in this "
    + "system is ever a float."],
  ["QTY UNIT", "0.0001 contracts. Sizes cross the wire as integers too, and are divided by "
    + "10,000 for display."],
  ["IMPLIED PROBABILITY", "a YES price read as a probability: $0.42 is a 42% implied chance. "
    + "At tick resolution the two share a representation, which is why DES shows them together."],
  ["YES / NO COMPLEMENT", "NO price = 10000 − YES price, in ticks. It is the one formula the "
    + "whole system uses to derive one side of a market from the other, so buying NO is "
    + "selling YES. Kalshi publishes bids only, for YES and for NO: a NO bid at x is a YES ask "
    + "at 10000 − x. Polymarket US quotes one instrument, YES."],
  ["BID / ASK", "bid: the best price someone is resting to buy YES at. Ask: the lowest price "
    + "someone is resting to sell YES at."],
  ["MID / SPREAD", "mid is (bid + ask) / 2, the midpoint between the two. Spread is ask − bid, "
    + "what a taker crosses."],
  ["VALID (BOOK)", "a book counts as valid only while all of: it has a snapshot with no "
    + "sequence gap since; it is not crossed; every level has positive quantity and a price "
    + "strictly inside (0, 10000); and it updated inside the staleness limit. Anything else "
    + "marks it invalid and triggers a resync."],
  ["QUIET", "a display state, not an error: the book has had no update inside the staleness "
    + "window (5s by default). A prediction market that simply has not traded in a while is "
    + "normal, especially a thin one, so it is dim rather than red. The engine calls this "
    + "reason STALE."],
  ["STALE", "the staleness reason itself. It is computed at query time, never stored, and "
    + "clears itself the moment a contiguous update lands — a quiet-but-gapless book is "
    + "untradeable right now, not wrong."],
  ["INVALID", "a structural fault: NO_SNAPSHOT, SEQ_GAP, CROSSED or BAD_LEVEL. These are "
    + "sticky — every further incremental update is ignored until a fresh snapshot arrives. "
    + "This is what the red banner is reserved for."],
  ["SEQ GAP", "a missing sequence number in a venue's stream. Something was not delivered, so "
    + "the local book can no longer be trusted and has to be rebuilt from a new snapshot."],
  ["CROSSED", "best bid ≥ best ask. A locked book (bid == ask) counts too: a real matching "
    + "engine would have executed them, so seeing both rest means the local state is wrong."],
  ["GROSS EDGE", "the edge before fees. Buying YES on one venue and NO on the other pays "
    + "exactly $1.00 at settlement, so the gross per contract is yes_bid_B − yes_ask_A."],
  ["NET EDGE", "gross minus both legs' fees, per contract. It is the only number worth "
    + "ranking on: a positive gross the fees eat is not a trade."],
  ["SIZE (EDGE)", "not a flat assumption. Both ladders are walked together in increasing-cost "
    + "order and the walk stops at the first increment whose own marginal gross no longer "
    + "covers its own fees, so the size shown is what the books actually offer and what is "
    + "actually worth taking."],
  ["TAKER / MAKER", "a taker crosses the spread and pays the taker fee; a maker rests and is "
    + "charged less or paid. Kalshi's maker fee is a fraction of its taker coefficient (0, ¼ "
    + "or ½ by fee type); Polymarket US's maker coefficient is negative, a rebate. Every "
    + "number on the ARB and PAPER screens assumes taker on both legs — the conservative read."],
  ["FEE FORMULA", "both venues use the same shape, Θ × contracts × P × (1 − P), with different "
    + "coefficients and rounding. Kalshi taker Θ = 0.07 × the series fee multiplier, rounded up "
    + "twice (to $0.000001, then to the tick); Polymarket US taker Θ is the market's own fee "
    + "coefficient, typically 0.06, rounded half-even to the cent."],
  ["ONE-WAY LATENCY", "the venue's timestamp on a message to the moment it was received here. "
    + "It is only meaningful if the two clocks agree, which is why it can print negative."],
  ["RTT", "the WebSocket keepalive round trip. Only the local clock is involved in a round "
    + "trip, so RTT is trustworthy no matter how far the clocks have drifted."],
  ["CLOCK SKEW", "median one-way latency − RTT/2: an estimate of how far the local clock sits "
    + "from the venue's. Past 25ms, or on a negative median, the latency panel says TRUST "
    + "RTT/2. The measurement is never de-biased — a silently corrected number would be a "
    + "worse lie than an honest negative one."],
  ["PAIR", "one Kalshi market and one Polymarket US market judged to be the same real-world "
    + "outcome. The matcher proposes; a human confirms. Equivalence lives in the resolution "
    + "rules and no lexical score can judge it: one venue may resolve on who is inaugurated "
    + "and the other on who wins the election, and every title token still matches. Only a "
    + "confirmed pair feeds the engine."],
  ["EXPECTED NET", "how paper trading reports P&L: the net edge locked in at settlement, "
    + "because both legs of a correct pair pay $1.00 together by construction. It is an "
    + "expected value under pair equivalence, not a mark-to-market price — if the pair is "
    + "wrong, the number means nothing."],
  ["LIVE vs POLLED", "Kalshi is a WebSocket stream and shows LIVE. Polymarket US is REST-"
    + "polled and shows amber POLLED, so a polled book never visually claims to be something "
    + "it is not."],
  ["RUN ID", "the identifier every recorded message is tagged with. It is shown in full in the "
    + "status bar because a run id copied short is one `arb replay` cannot find."],
];

// ---------- safety ----------

const SAFETY = [
  ["NO ORDERS", "this system places, amends and cancels nothing. There is no order-entry code "
    + "path in it at all. Both venue adapters are read-only: market data in, nothing out."],
  ["PAPER IS A SIMULATION", "paper trading contacts no venue. It assumes an instant fill, on "
    + "both legs at once, at the liquidity the edge walk already saw — that is its one "
    + "deliberate optimism. Fees, sizes and limits are the real models."],
  ["MEASURED, NOT PROMISED", "an edge on the ARB screen is what two books showed at one "
    + "instant. It is not an executable quote, and it says nothing about whether both legs "
    + "would still be there a round trip later."],
  ["A PAIR IS A HUMAN DECISION", "every number downstream of a pair inherits that pair's "
    + "correctness. Confirm on the resolution rules, not on the score."],
  ["RECORDING", "`arb ui` records raw messages to Postgres while it runs unless it was started "
    + "with --no-record. The REC field in the status bar is the truth of it."],
];

// ---------- build ----------

const SECTIONS = [
  { id: "start", title: "START HERE" },
  { id: "screens", title: "READING THE SCREENS" },
  { id: "keys", title: "KEYBOARD" },
  { id: "cmds", title: "COMMANDS" },
  { id: "glossary", title: "GLOSSARY" },
  { id: "safety", title: "SAFETY" },
];

let built = false;
let mounted = false;
let mainEl = null;
let qEl = null;
let statEl = null;
let pageKeysEl = null;
const items = [];               // {node, sec, text} — every filterable line
const secNodes = new Map();     // section id -> {wrap, body}
const jumpBtns = new Map();     // section id -> button

/** One term/definition line. `.kv` so select-to-copy sees it as a data row. */
function row(k, v, cls) {
  const r = el("div", "kv help-row" + (cls ? " " + cls : ""));
  r.append(el("span", "k", k), el("span", "v", v));
  return r;
}

function add(secId, node, text) {
  (dynTarget || secNodes.get(secId).body).append(node);
  items.push({ node, sec: secId, text: text.toLowerCase(), dyn: Boolean(dynTarget) });
}

// While set, add() writes into this container and marks its items as
// rebuildable — the per-page key rows are re-derived on every mount.
let dynTarget = null;

function addLead(secId, text) {
  const p = el("p", "help-lead", text);
  add(secId, p, text);
}

function addSub(secId, text) {
  // A subheading is structure, not data: it is not filterable on its own and
  // it carries no row class, so select-to-copy ignores it.
  const h = el("div", "help-sub", text);
  (dynTarget || secNodes.get(secId).body).append(h);
  return h;
}

function addRow(secId, k, v, cls) {
  add(secId, row(k, v, cls), k + " " + v);
}

// ---------- per-page keys, read from each page's own footer ----------

const KEYISH = /^(?:[↑↓←→⏎⌫]+|ESC|TAB|ENTER|SPACE|HOME|END|PGUP|PGDN|DEL|SHIFT\+\S+|ALT\+\S+|CTRL\+\S+|CMD\+\S+|[A-Z0-9]|[0-9]-[0-9])$/;

/** One footer token. "PGUP/PGDN" is one token made of two keys, so a token
    counts as a key when every slash-separated part of it does. */
function isKeyToken(tok) {
  if (tok === "/") return true;                       // the key, or a joiner
  return tok.split("/").every((p) => p !== "" && KEYISH.test(p));
}

/** Split a footer item like "SHIFT+Y / SHIFT+N WHOLE EVENT" into the key part
    and what it does. A line with no leading key tokens ("MEASUREMENT ONLY —
    NO ORDERS") comes back as a note, which is rendered dim. */
function splitFootItem(txt) {
  const parts = txt.split(/\s+/);
  let i = 0;
  while (i < parts.length && isKeyToken(parts[i])) i++;
  while (i > 1 && parts[i - 1] === "/") i--;          // never END on a joiner...
  if (i === 0) return { key: "", desc: txt };         // ...but "/ SEARCH" is a key
  return { key: parts.slice(0, i).join(" "), desc: parts.slice(i).join(" ") };
}

/** The keys a page advertises, taken from its own `.des-foot` strip so this
    table cannot drift from the page. Returns [] when the page has no footer. */
function footKeys(rootId) {
  const root = document.getElementById(rootId);
  const foot = root ? root.querySelector(".des-foot") : null;
  if (!foot) return [];
  return foot.textContent.split("·").map((s) => s.trim()).filter(Boolean).map(splitFootItem);
}

/** Every page in nav order, plus the DES route, which is not in the nav. */
function allPages() {
  const list = navPages().map((p) => ({ id: p.id, title: p.title, path: p.path, root: p.root, nav: true }));
  if (!list.some((p) => p.id === "market")) {
    list.push({ id: "market", title: "DES", path: "/market/<id>", root: "des", nav: false });
  }
  return list;
}

function buildStart() {
  addLead("start",
    "A cross-venue desk for one job: find the same bet priced differently on Kalshi and on "
    + "Polymarket US, measure what the gap is worth after fees, and — on the PAPER screen — "
    + "simulate taking it. It measures and simulates. It does not trade.");
  addLead("start",
    "Everything is keyboard-first. Type anywhere and it goes to the ARB> line; press Enter to "
    + "run it. ALT and a nav number jumps between pages, Esc comes back to MONITOR, and every "
    + "page is a real URL you can bookmark or reload.");
  addSub("start", "THE FRAME");
  for (const [k, v] of FRAME) addRow("start", k, v);
  addSub("start", "PAGES");
  for (const p of allPages()) {
    const nth = navPages().findIndex((n) => n.id === p.id);
    const where = p.path + (nth >= 0 ? "  ·  ALT+" + (nth + 1) : "  ·  not in the nav");
    addRow("start", p.title, where, "help-pathrow");
  }
}

function buildScreens() {
  addLead("screens",
    "One entry per screen: what it is for, then what each column means. Prices everywhere are "
    + "dollars per contract; an em dash means the value is not there yet.");
  for (const p of allPages()) {
    const doc = SCREENS[p.id];
    const head = addSub("screens", p.title + "  " + p.path);
    head.classList.add("help-screen-h");
    if (!doc) {
      addRow("screens", p.title, "no description available for this page yet", "quiet-line");
      continue;
    }
    addLead("screens", doc.lead);
    for (const g of doc.groups) {
      const gh = addSub("screens", g.name);
      gh.classList.add("help-group-h");
      for (const [k, v] of g.cols) addRow("screens", k, v);
      if (g.note) addLead("screens", g.note);
    }
  }
}

function buildKeys() {
  addLead("keys",
    "Keys are ignored while a text box has focus — the monitor's filter and this page's "
    + "filter own every key while you are in them. A page gets first refusal on a key; "
    + "anything it does not claim falls through to the global bindings below.");
  addSub("keys", "GLOBAL");
  for (const [k, v] of globalKeys()) addRow("keys", k, v);
  addSub("keys", "MONITOR");
  addRow("keys", "↑ ↓", "move through the filtered, sorted list you can actually see");
  addRow("keys", "1 - 9", "select that row, with an empty command line");
  addRow("keys", "click", "select · double-click opens DES");
  addRow("keys", "column header", "sort by that column; click it again to reverse");
  addSub("keys", "HELP");
  for (const [k, v] of HELP_KEYS) addRow("keys", k, v);
  // Everything below is read out of the pages themselves, on every mount.
  pageKeysEl = el("div", "help-dyn");
  secNodes.get("keys").body.append(pageKeysEl);
  refreshPageKeys();
  addLead("keys",
    "Per-page keys are read out of each page's own footer strip every time this page is "
    + "opened, so they cannot drift from what the page advertises.");
}

/** Re-derive the per-page key rows from the live footers. Pages build (and in
    two cases rewrite) their footer strips themselves, so this is done on each
    mount rather than once. */
function refreshPageKeys() {
  if (!pageKeysEl) return;
  for (let i = items.length - 1; i >= 0; i--) if (items[i].dyn) items.splice(i, 1);
  pageKeysEl.textContent = "";
  dynTarget = pageKeysEl;
  try {
    for (const p of allPages()) {
      if (p.id === "monitor" || p.id === "help") continue;
      const fk = footKeys(p.root);
      if (!fk.length) continue;
      addSub("keys", p.title);
      // Verbatim, caps and all: this is the page's own wording, not a paraphrase.
      for (const f of fk) {
        if (f.key) addRow("keys", f.key, f.desc);
        else addRow("keys", "", f.desc, "quiet-line");
      }
    }
  } finally {
    dynTarget = null;
  }
}

function buildCmds() {
  addLead("cmds",
    "The ARB> line takes one command at a time, uppercased as you type. Enter runs it, "
    + "Backspace edits it, Esc throws it away. Commands navigate — each one lands on a real "
    + "URL.");
  for (const [k, v, eg] of COMMANDS) {
    const r = el("div", "kv help-row");
    const val = el("span", "v");
    val.append(document.createTextNode(v), el("span", "help-eg", "ARB> " + eg));
    r.append(el("span", "k", k), val);
    add("cmds", r, k + " " + v + " " + eg);
  }
  addLead("cmds",
    "A ticker match is tried three ways in order: exact, then prefix, then substring. No "
    + "match leaves the screen alone and says NO MATCH.");
}

function buildGlossary() {
  addLead("glossary",
    "The vocabulary the screens assume. Definitions follow the project docs — data-model.md, "
    + "engine.md, pairs.md and ui.md — not a general finance glossary.");
  for (const [k, v] of GLOSSARY) addRow("glossary", k, v);
}

function buildSafety() {
  addLead("safety",
    "What this terminal does, stated plainly, so nothing on it can be mistaken for something "
    + "it is not.");
  for (const [k, v] of SAFETY) addRow("safety", k, v, "help-safety");
}

function build() {
  if (built) return;
  built = true;
  const root = $("help-page");
  root.classList.add("des", "help");

  const head = el("div", "des-head");
  const title = el("span", "des-title");
  title.append(el("span", "des-tag", "HELP"), el("span", null, "KEYS · COMMANDS · SCREENS · VOCABULARY"));
  statEl = el("span", "head-stat", "—");
  statEl.id = "help-stat";
  head.append(title, statEl);

  // rail: filter + jump list + this page's own key strip
  const rail = el("div", "help-rail");
  qEl = el("input", "help-q");
  qEl.id = "help-q";
  qEl.type = "text";
  qEl.placeholder = "FILTER  ( / )";
  qEl.autocomplete = "off";
  qEl.spellcheck = false;
  qEl.setAttribute("aria-label", "Filter help text");
  const jump = el("nav", "help-jump");
  jump.setAttribute("aria-label", "Help sections");
  rail.append(qEl, jump);
  const foot = el("div", "des-foot help-foot");
  for (const part of HELP_FOOT.split("·")) foot.append(el("span", null, part.trim()));
  rail.append(foot);

  mainEl = el("div", "help-main");
  mainEl.id = "help-main";
  mainEl.tabIndex = 0;
  mainEl.setAttribute("aria-label", "Help");

  for (const s of SECTIONS) {
    const wrap = el("section", "help-sec");
    wrap.id = "help-s-" + s.id;
    const h = el("h2", "des-section help-h", s.title);
    h.id = "help-h-" + s.id;
    wrap.setAttribute("aria-labelledby", h.id);
    const body = el("div", "help-secbody");
    wrap.append(h, body);
    mainEl.append(wrap);
    secNodes.set(s.id, { wrap, body });

    const b = el("button", "help-jbtn", s.title);
    b.type = "button";
    b.addEventListener("click", (e) => {
      goTo(s.id);
      if (e.detail > 0) b.blur();       // pointer click: hand typing back to ARB>
    });
    jump.append(b);
    jumpBtns.set(s.id, b);
  }

  const empty = el("div", "help-empty quiet-line", "NO MATCH");
  empty.id = "help-empty";
  empty.hidden = true;
  mainEl.append(empty);

  const body = el("div", "des-body help-body");
  body.append(rail, mainEl);
  root.append(head, body);

  buildStart();
  buildScreens();
  buildKeys();
  buildCmds();
  buildGlossary();
  buildSafety();

  qEl.addEventListener("input", applyFilter);
  qEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();              // clear the box, do not leave the page
      if (qEl.value) { qEl.value = ""; applyFilter(); } else qEl.blur();
    } else if (e.key === "Enter") {
      e.preventDefault();
      qEl.blur();                       // hand the keyboard back to the ARB> line
    }
  });
  mainEl.addEventListener("scroll", onScroll, { passive: true });
  applyFilter();
}

// ---------- filter ----------

function applyFilter() {
  const q = (qEl.value || "").trim().toLowerCase();
  let shown = 0;
  const live = new Map();
  for (const it of items) {
    const on = !q || it.text.includes(q);
    if (it.node.hidden === on) it.node.hidden = !on;
    if (on) {
      shown += 1;
      live.set(it.sec, true);
    }
  }
  for (const s of SECTIONS) {
    const on = !q || live.has(s.id);
    const n = secNodes.get(s.id);
    if (n.wrap.hidden === on) n.wrap.hidden = !on;
    jumpBtns.get(s.id).classList.toggle("off", !on);
  }
  syncSubs();
  $("help-empty").hidden = shown > 0;
  statEl.textContent = q
    ? shown + " OF " + items.length + " LINES"
    : items.length + " LINES · " + SECTIONS.length + " SECTIONS";
  onScroll();
}

// ---------- jump list ----------

/** A subheading survives the filter only while something under it did — a
    heading left standing over someone else's rows is worse than no heading.
    A heading covers everything up to the next heading at its own level or
    higher: "MONITOR /" covers its groups, "MARKET LIST" covers only its rows. */
function syncSubs() {
  const lvl = (e) => (e.classList.contains("help-group-h") ? 2 : 1);
  for (const n of secNodes.values()) {
    for (const sub of n.body.querySelectorAll(".help-sub")) {
      let any = false;
      for (let e = sub.nextElementSibling; e; e = e.nextElementSibling) {
        if (e.classList.contains("help-sub")) {
          if (lvl(e) <= lvl(sub)) break;
          continue;
        }
        if (e.classList.contains("help-dyn")) break;   // the per-page blocks carry their own headings
        if (e.classList.contains("help-row") || e.classList.contains("help-lead")) {
          if (!e.hidden) { any = true; break; }
        }
      }
      if (sub.hidden === any) sub.hidden = !any;
    }
  }
}

function goTo(id) {
  const n = secNodes.get(id);
  if (!n || n.wrap.hidden) return;
  mainEl.scrollTo({ top: Math.max(0, n.wrap.offsetTop - 6), behavior: isReducedMotion() ? "auto" : "smooth" });
  setActive(id);
}

let activeSec = null;

function setActive(id) {
  if (id === activeSec) return;
  activeSec = id;
  for (const [sid, b] of jumpBtns) {
    const on = sid === id;
    b.classList.toggle("on", on);
    if (on) b.setAttribute("aria-current", "true");
    else b.removeAttribute("aria-current");
  }
}

let spyPending = false;

function onScroll() {
  if (spyPending) return;
  spyPending = true;
  requestAnimationFrame(() => {
    spyPending = false;
    if (!mounted) return;
    const y = mainEl.scrollTop + 8;
    // At the bottom of the scroll the last section can never reach the top
    // edge, so mark it read rather than leaving the highlight one section back.
    const atEnd = mainEl.scrollTop + mainEl.clientHeight >= mainEl.scrollHeight - 2;
    let cur = null;
    for (const s of SECTIONS) {
      const n = secNodes.get(s.id);
      if (n.wrap.hidden) continue;
      if (cur === null) cur = s.id;     // scrolled above the first visible section
      if (atEnd || n.wrap.offsetTop <= y) cur = s.id;
    }
    if (cur) setActive(cur);
  });
}

// ---------- page module ----------

export default {
  id: "help",
  path: "/help",
  title: "HELP",
  nav: true,
  root: "help-page",

  mount() {
    mounted = true;
    build();
    refreshPageKeys();       // another page may have rewritten its footer since
    applyFilter();
    onScroll();
  },

  unmount() {
    mounted = false;
  },

  render() {
    build();
  },

  onKey(e) {
    if (!mainEl) return false;
    const k = e.key;
    if (k === "ArrowDown" || k === "ArrowUp") {
      mainEl.scrollTop += k === "ArrowDown" ? LINE_SCROLL : -LINE_SCROLL;
      return true;
    }
    if (k === "PageDown" || k === "PageUp") {
      const step = mainEl.clientHeight * PAGE_SCROLL;
      mainEl.scrollTop += k === "PageDown" ? step : -step;
      return true;
    }
    if (k === "Home") { mainEl.scrollTop = 0; return true; }
    if (k === "End") { mainEl.scrollTop = mainEl.scrollHeight; return true; }
    // "/" is a printable character, so it would otherwise land in the command
    // buffer. Claim it only when that buffer is empty.
    if (k === "/" && cmd.buffer() === "") {
      qEl.focus();
      qEl.select();
      return true;
    }
    return false;
  },
};
