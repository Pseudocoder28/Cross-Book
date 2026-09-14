# Terminal UI

Code: backend [`src/arb/ui/server.py`](../src/arb/ui/server.py), frontend
[`src/arb/ui/static/`](../src/arb/ui/static/) (`index.html`, `app.js`,
`style.css` — three static files, vanilla JS/CSS, zero build step, zero
external network requests). Served by `arb ui` at `http://127.0.0.1:8080`
by default.

## Design intent

A black/amber Bloomberg-terminal-style desk for the whole system: live
market monitor, a depth ladder per market, a tape of changes, a latency
panel, pair review, the ARB screen, and paper-trading ledger — all from one
page, keyboard-driven, no mouse required (though selections and clicks
work). The aesthetic was chosen by a judged three-way design panel (a
Bloomberg purist, a modern-desk take, and a density-maximalist take); the
black/amber/tabular-nums look won. A Python project shouldn't grow a
node/npm toolchain for one page, so the frontend stays three plain files
served directly by FastAPI's `StaticFiles`.

## `arb ui` does everything `arb record` does, plus rendering

`run_ui()` mirrors `record.run_record()`'s wiring — recorder-first ingest
(`record_raw()` enqueues before any parsing, same hard rule everywhere
else), every long-running loop under `supervise()` in its own task, one
venue's failure never stopping another. On top of that it:

- Parses every frame through the venue adapters into a shared
  `BookManager`.
- Optionally loads confirmed pairs into an `ArbMonitor`
  (`--pairs-top`, `pairs/tracked.py` — see [`pairs.md`](pairs.md)) and
  optionally a `PaperTrader` on top of that (`--paper`).
- Pushes JSON frames to every connected browser over a WebSocket.
- Serves `GET /metrics` itself — `arb ui` does **not** start the separate
  Prometheus HTTP server that `arb record` does; there's only one HTTP
  server, one port.

`--no-record` disables the Postgres write path entirely (useful for a
DB-less demo) without touching anything else — books, the ARB screen and
paper trading (over live quotes) still work.

## `UIState`: how the routes stay testable

`create_app(state: UIState) -> FastAPI` builds every route and the
WebSocket endpoint against a small `Protocol` (`UIState`), not against the
concrete `ServerState` class directly. Tests construct a stub implementing
that protocol and exercise the full HTTP/WS surface with **no network, no
database, no live venue connection** — `tests/test_ui_server.py` does
exactly this. `ServerState` is the real, stateful implementation `run_ui()`
wires up in production: it tracks per-market book payload caches, DES
metadata (seeded from discovery, refreshed on demand with a TTL), pair
review, the arb monitor, the paper trader, per-client outbound queues, and
the rolling counters behind the stats frame.

## HTTP surface

| Route | Purpose |
| --- | --- |
| `GET /` | serves `index.html` |
| `GET /api/status` | run id, uptime, per-venue connection state, recording flag, database status (raw-message totals, per-run counts) |
| `GET /api/markets/{market_id}` | DES payload for one market (404 if unknown) |
| `GET /api/pairs?status=...` | pair review listing (see [`pairs.md`](pairs.md)) |
| `POST /api/pairs/{id}/decide` | confirm/reject one pair, body `{"status": "confirmed"\|"rejected"\|"proposed"}` |
| `POST /api/pairs/decide` | batch decide, body `{"ids": [...], "status": "..."}` |
| `GET /api/arb` | current `ArbMonitor.snapshot()` — every tracked pair's best quote |
| `GET /api/paper` | paper-trading ledger: limits, totals, positions, recent trades |
| `GET /metrics` | Prometheus exposition |
| `WS /ws` | the live push feed, below |

## WebSocket wire protocol

Server → client, JSON text frames, tagged by `"t"`:

- **`hello`** — sent once, immediately on connect: `run_id` and the full
  market list (id, ticker, title, 24h volume, venue), sorted by volume
  descending. Followed, in the same burst before any other broadcast can
  interleave, by a `book` frame for every market that already has book
  state, and (if an `ArbMonitor` is active) one `arb` frame — this ordering
  guarantee is deliberate: `queue.put_nowait()` is called for all of these
  with no `await` in between, so a freshly connected client's queue always
  has `hello` → all current `book`s → `arb` ahead of anything a concurrent
  broadcast could add, and never sees a `delta` for a market it hasn't been
  told exists yet.
- **`book`** — full YES-book state for one market: bid/ask ladders (`[price,
  qty]` pairs), validity (`valid`, `reason`), book age in ms, timestamp.
  Coalesced server-side to at most one push per market per
  `BOOK_FLUSH_INTERVAL_S` (0.1s, i.e. ≤10/s per market) via a dirty-set +
  flush-loop pattern (`ServerState.mark_dirty()` / `take_dirty()`) — an
  ingest loop marks a market dirty on every applied event, but the actual
  `book` frame is only rendered and broadcast on the next flush tick, so a
  bursty market doesn't spam ten times its allotted rate.
- **`delta`** — one tape entry per applied level change: market, side,
  price, signed qty delta, latency (ms, when known), timestamp. For
  Kalshi this is a direct 1:1 mapping of parsed WS deltas; for Polymarket
  US (poll-based, full snapshots only) it's the output of `level_deltas()`
  diffing consecutive polls — see
  [`data-model.md`](data-model.md#bookmanager-the-fan-out-layer) — so the
  tape reads the same shape for both venues.
- **`stats`** — broadcast every `STATS_INTERVAL_S` (1s): total message
  count and 1-second rate, latency percentiles (last/median/p95 over the
  last `LATENCY_WINDOW` = 512 samples), keepalive RTT and a derived clock-skew
  estimate (below), parse-error and seq-gap counters, connected client
  count, recorder enqueued/dropped counts (when recording), Polymarket US
  poll stats (polls, rate-limited count, errors, targets, configured rate,
  age of the last successful poll), uptime.
- **`arb`** — `{"t": "arb", "quotes": [...]}`, the full `ArbMonitor.snapshot()`.
  Re-broadcast whenever a flush tick's dirty set intersects the monitor's
  tracked market ids.
- **`paper`** — `{"t": "paper", "trades": [...]}`, sent only when new paper
  trades were just taken this tick (not a full ledger — the ledger is
  fetched via `GET /api/paper` or reconstructed from accumulated `paper`
  frames by the frontend).

Inbound client messages are ignored by contract — this is a push-only feed;
the WS endpoint's receive loop exists purely to detect disconnects.

### Wire format keeps integers

Prices cross the wire as integer ticks ($0.0001) and quantities as integer
`Qty` units (0.0001 contracts) — never floats, never formatted strings. The
browser divides by 100 (ticks → cents) or 10,000 (`Qty` → contracts) purely
for display; no float ever touches server-side book state or gets
serialized as a price. This mirrors the same-discipline rule that governs
storage and the fee/edge engine (see [`data-model.md`](data-model.md)).

### A slow browser must never stall ingest

`ServerState.broadcast()` is called from the ingest path (the Kalshi/Polymarket
consume loops, the flush loop) and must never `await` a client socket — if
it did, one slow browser tab could back-pressure the entire market-data
pipeline. Instead, every connected client has its own bounded
`asyncio.Queue` (`SEND_QUEUE_MAX = 1024`), drained by a dedicated
per-connection sender task that does the actual `await ws.send_text(...)`.
`broadcast()` only ever does `queue.put_nowait()`; a client whose queue is
full is dropped and closed (code `1013`, "try again later") and counted
(`arb_ui_ws_clients_dropped_total`) rather than allowed to back up the feed
for everyone else.

### Skew-aware latency

One-way latency (exchange `ts_ms` on a delta → local receive time) needs
synchronized clocks to mean anything; the WebSocket transport's keepalive
RTT does not — only the local clock is involved in a round trip.
`clock_skew_ms = median_one_way − rtt/2` estimates how far the local clock
is offset from the venue's. The frontend flips the latency panel to a
`CLOCK SKEW · TRUST RTT/2` banner whenever the one-way median goes negative
or `|skew| > 25ms` — a real observed case: median −24.5ms, RTT 24ms → skew
≈ −36ms, meaning the local clock was running behind the venue's (fixed with
an `sntp` clock sync, per `PROGRESS.md`).

## Screens

The terminal is one page with a command line (`ARB> `, type anywhere to
command) and several full-screen overlays:

- **Monitor / ladder / tape** — the default view: live market list with
  real volumes and titles, a depth ladder per selected market (with
  complement-derived NO prices, mid/spread seam, flash-on-change), a running
  tape of deltas, a latency sparkline, and a system panel (recorder
  counters, parse errors, seq gaps, DB row counts by run).
- **`DES`** (Enter on a selected market, `<TICKER> DES`, or double-click) —
  a Bloomberg-style description page: event title/candidate, ticker
  anatomy (series → event → market), full resolution rules and secondary
  rules, settlement sources, status/category/type/mutually-exclusive/early-close,
  implied probability next to the quote (deliberately shown together — with
  $0.0001 ticks the two numbers share a representation: 50 ticks = $0.0050
  = 0.50¢ = 0.50% implied), live book summary (best levels, depth, age),
  volumes and open interest, open/close/expected-expiration in both UTC and
  ET (rules texts are usually written in ET, per `CLAUDE.md`). Arrow keys
  page between markets without leaving the screen; `Esc` closes. Backed by
  `GET /api/markets/{id}`, seeded from discovery metadata and refreshed live
  on open with a 30s TTL (`DETAIL_TTL_MS`), falling back to the seed if a
  live refresh fails.
- **`PAIRS`** — the pair-review workflow described in
  [`pairs.md`](pairs.md#human-review-in-the-terminal-ui).
- **`ARB`** — the ranked cross-venue edge screen: every tracked confirmed
  pair, sorted by best net edge per contract, with size, gross, fees,
  direction, both venues' BBOs, and book state (a dim `QUIET` for a book
  that's simply gone quiet, distinct from red `INVALID` reserved for
  structural failures — see the "QUIET vs INVALID" note below). Selecting a
  row shows both legs' detail, the fee model in play, and the reverse
  direction's numbers; `Enter` jumps to the Kalshi leg's `DES` page.
- **`PAPER`** (when `--paper` is set) — the paper-trading ledger: running
  totals, per-pair positions, a trade tape, and the active `PaperLimits`.

### "QUIET" vs "INVALID"

The `Book`'s 5-second staleness rule (see
[`data-model.md`](data-model.md#book-validity-and-the-update-state-machine))
is a *trading-validity* gate, not a display judgment — a prediction market
that simply hasn't traded in a while is normal, especially a low-volume
one, and shouldn't look broken. The UI shows this state as a dim "QUIET ·
LAST UPDATE Ns AGO" rather than red; red `INVALID` is reserved for the
structural reasons (`SEQ_GAP`, `CROSSED`, `BAD_LEVEL`, `NO_SNAPSHOT`) that
actually mean the local book state is wrong.

### Polymarket US shows "POLLED", not "LIVE"

Because Polymarket US is currently REST-polled rather than streamed (see
[`venues/polymarket-us.md`](venues/polymarket-us.md)), its panel uses an
amber "POLLED" state, distinct from Kalshi's green "LIVE" — plus live poll
stats and "last book Ns ago" — so a polled book never visually claims to be
something it isn't. `ServerState.polymarket_status()` reports `"connecting"`
before the first successful poll, `"polled"` while polls are succeeding
within `VENUE_DOWN_AFTER_S` (30s), and `"down"` past that.

## Select-to-copy

Selecting anything in the terminal — a ladder region, tape rows, system
panel rows — copies it to the clipboard and shows a `COPIED · N CHARS · N
ROWS` toast (or a red `COPY BLOCKED` if the browser refuses). Multi-row
selections are rebuilt as tab-separated values (one row per line) rather
than relying on the DOM's own text serialization, because the UI's rows are
flex grids whose visual column boundaries the browser's default copy
wouldn't preserve — a ladder selection pastes into a spreadsheet with
columns intact. A selection inside a single cell copies exactly the
highlighted substring. Copy fires on `mouseup` (and `keyup` for Cmd/Ctrl+A),
not on `selectionchange`, because the async Clipboard API requires
transient user activation that a `selectionchange` event firing mid-drag
doesn't carry; `document.execCommand("copy")` is the fallback. Live
re-rendering pauses for the duration of a pointer-down drag so a re-render
can't destroy the DOM nodes a selection is anchored in — updates accumulate
as dirty flags and flush on release, so data is delayed during a drag, never
dropped.
