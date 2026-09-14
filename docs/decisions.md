# Decisions

Design choices and why. Newest first.

## M9 — terminal UI (`arb ui`)

- **One process: visualize + record.** `arb ui` runs the same
  recorder-first ingest as `arb record` (every raw frame enqueued before
  parsing) and additionally parses through the Kalshi adapter into
  `BookManager`, broadcasting to browser clients. `--no-record` exists for
  DB-less viewing.
- **Frontend is three static files, zero build step, zero external
  requests.** Vanilla JS/CSS served by FastAPI; system mono font stack; no
  CDN. A Python repo should not grow a node toolchain for one page.
- **Wire format keeps integers.** Prices cross the WebSocket as ticks and
  quantities as 0.0001-contract units; the browser formats (ticks/100 =
  cents). No float drift server-side.
- **A slow browser must never stall ingest.** Broadcast is non-blocking:
  per-client bounded queues (1024) drained by per-connection sender tasks;
  an overflowing client is dropped (metric
  `arb_ui_ws_clients_dropped_total`).
- **Fresh snapshot after any gap, via forced reconnect.**
  `ReconnectingWebSocket.force_reconnect()` closes the live connection; the
  loop reconnects and resubscribes, and Kalshi answers every subscribe with
  full snapshots. The cheaper `update_subscription`/`get_snapshot` path
  stays an open item.
- **UI presents staleness as QUIET, not INVALID.** The Book's 5 s staleness
  rule is a trading-validity gate; a prediction market that simply hasn't
  ticked is normal. The depth banner shows a dim "QUIET · LAST UPDATE Ns
  AGO"; red INVALID is reserved for structural reasons (seq gap, crossed,
  bad level).
- **Design chosen by a judged panel** (Bloomberg purist vs modern desk vs
  density maximalist): black/amber terminal chrome, tabular-nums data,
  flash-on-change, depth bars, function-key strip, and an intentional
  Polymarket US down-screen driven by live REST reachability.

## M15 — replay and paper trading

- **Replay is the live pipeline fed from Postgres.** `arb replay` streams
  a run's `raw_messages` in `ingest_seq` order through the *same* adapters
  and `BookManager`, using the recorded `recv_mono_ns` as the clock, so
  books, invalidations and edges are reconstructed deterministically. There
  is no second code path to drift from the live one.
- **Paper fills are honest about their one optimism.** A fill is assumed
  at the displayed liquidity the edge walk consumed, instantly, on both
  legs — everything else (fees, sizes, limits) is the real model. "P&L" is
  the net edge locked in at settlement (both legs pay exactly $1.00
  together), i.e. expected value under pair equivalence, not a mark.
- **Risk limits are the only thing standing between an edge and a fill:**
  minimum net per contract, maximum contracts per pair, maximum total cost.
  Deliberately simple and explicit; the live trader on Day 3 inherits them.
- **Same trader, live or replayed.** `arb ui --paper` runs it on the live
  monitor (trades persisted per run); `arb replay --paper` runs it over
  history (persisted only with `--persist`, tagged `replay:<run_id>`).
- **Still no orders.** Paper trading talks to no venue.

## M14 — fees, edge math, ARB screen

- **Fees in exact integer arithmetic, rounded the venue's way.** Kalshi:
  0.07 × multiplier × C × P × (1 − P), trade fee rounded up to $0.000001,
  then up to the direct-member tick; maker multipliers by `fee_type`
  (0 / 0.25 / 0.5). Polymarket US: Θ × C × p × (1 − p) with the market's
  `feeCoefficient` (maker −0.0125), banker's rounding to the cent. Both
  reproduce the venues' documented examples in tests. Per-series Kalshi
  parameters are fetched from the documented `GET /series` endpoint (event
  overrides win), Polymarket's from the market object.
- **Edge is measured depth-aware, in both directions.** Buy YES on A + NO
  on B costs `yes_ask_A + (10000 − yes_bid_B)`; the walk merges both ladders
  in cost order and stops at the first fill whose gross no longer covers
  its own fees. Sizes are what the books actually offer, not a hope.
- **The ARB screen quotes only confirmed pairs, and labels quiet books
  honestly.** Tracked pairs' legs are subscribed on both venues at startup;
  every book change re-quotes affected pairs and broadcasts a full ranked
  snapshot. A Kalshi book that merely hasn't ticked is QUIET, not invalid —
  the feed is live and every change arrives.
- **Measurement only.** Nothing here places, amends or cancels an order;
  Day 1's read-only rule still holds until a later prompt lifts it.

## M13 — pair matcher with human review

- **The matcher proposes; a human confirms.** Equivalence lives in the
  resolution rules, which no lexical score can judge (Kalshi's presidential
  market resolves on who is *inaugurated*; another venue's may resolve on
  who *wins*). Proposals carry both rules texts and the scoring features so
  the reviewer sees exactly what the matcher saw; only confirmed pairs feed
  the arb engine.
- **Two-stage, explainable, deterministic scoring.** Events pair on
  IDF-weighted title similarity plus *outcome overlap* (how many outcomes
  have a name twin on the other side) — the overlap is what separates a
  pennant race from a chess league sharing the word "champion", and the IDF
  weighting is what stops "American League" outweighing "Silver Slugger".
  Markets then pair one-to-one on outcome-name similarity (party suffixes,
  accents, "Jr." stripped; containment counts so "Dodgers" ⊂ "Los Angeles
  Dodgers"). Dates are a weak feature because Kalshi ``close_time`` trails
  the real event by up to a year. No LLM: reproducible and free.
- **Blocking makes it cheap.** An inverted index on informative title tokens
  keeps 6,000 × 3,500 events to ~0.5 s. Universes are fetched with
  documented params only and recorded before parsing like all REST.
- **Batch review is a first-class action.** Shift+Y / Shift+N decide a whole
  event pairing (a 30-team pennant is one judgement, not thirty). Decisions
  are never overwritten by re-proposal: upserts refresh score/detail only.

## M12 — Polymarket US REST poller (both venues live)

- **Poll the public gateway now; swap in the WebSocket later behind the
  same interface.** `PolymarketUSRestSource` is an `EventSource` like the
  Kalshi WS source, so `arb record`, `arb ui`, the book manager and the
  recorder are venue-blind. When credentials arrive only the source changes.
- **Rate budget from measurement, not the docs.** The book endpoint is a
  5-token bucket refilling ~1/2 s (venue-notes), so the default is
  0.45 req/s with a 10 s stop on any 429. A 429 must never cascade: the
  poller pauses, Kalshi is untouched (separate supervised task).
- **A polled venue gets a poll-cycle staleness budget.** `BookManager`
  supports per-venue staleness; Polymarket books are allowed three cycles
  before STALE, otherwise every book would flag stale between polls.
- **Snapshot diffs feed the tape.** `level_deltas` turns consecutive polled
  snapshots into the same DELTA events a streaming venue emits, so the tape
  and any downstream consumer see one event shape.
- **Targets are chosen by category, not volume.** Live listings carry no
  volume fields; non-sports markets (where Kalshi overlap lives) are
  selected first, then sports. Explicit `--poly-slugs` overrides.
- **The UI says POLLED, not LIVE.** Amber state, poll stats and "last book
  Ns ago" in the panel — a polled book must never masquerade as streaming.

## M11 — DES (market description) page

- **Bloomberg's `DES` is the drill-down.** Enter on a selected market (or
  `DES` / `<TICKER> DES` on the command line, or a double-click) replaces
  the workspace with a description page; Esc returns; arrows page through
  markets without leaving it. Familiar to anyone who has used a terminal,
  and it keeps the workspace layout untouched.
- **Metadata is seeded from discovery and refreshed on demand.** The
  `/events?with_nested_markets=true` pages already carry full Market and
  EventData objects, so DES works instantly with no extra calls; opening a
  page refreshes via the documented `GET /markets/{ticker}` (and
  `GET /events/{event_ticker}` when the event isn't cached) behind a 30 s
  TTL, and falls back to the seed if the venue call fails. Every refresh
  response goes through the recorder before parsing, like all REST.
- **Implied probability is shown next to cents.** With $0.0001 ticks the
  two share a number (50 ticks = 0.50¢ = 0.50%), which is exactly the kind
  of thing a reader shouldn't have to work out for a long-shot market.

## M10 — select-to-copy in the terminal UI

- **Copy fires on `mouseup`, not `selectionchange`.** The async clipboard API
  needs transient user activation; `selectionchange` fires mid-drag without
  it and would be rejected. `mouseup` (and `keyup` for Cmd/Ctrl+A) carries
  activation. Fallback is `document.execCommand("copy")`, which copies the
  live selection as-is; if both fail the toast says COPY BLOCKED rather than
  lying about success.
- **Tabular selections are rebuilt as TSV.** The ladder, monitor, tape and
  system rows are flex grids, so the DOM's own serialization loses the
  column boundaries. Multi-row selections copy as tab-separated cells, one
  row per line, so a book selection pastes into a spreadsheet intact.
  Selecting inside a single cell returns the exact highlighted substring —
  half a number stays half a number.
- **Rendering pauses for the duration of a drag.** A live re-render replaces
  the text nodes a selection is anchored in, which destroys it mid-gesture.
  `frame()` returns early while the pointer is down and `drainTape()` stops
  prepending rows; dirty flags accumulate and flush on release, so data is
  only ever delayed, never dropped. Stale-pause is impossible: a `mousemove`
  reporting no buttons held, or a window blur, ends the pause.

## M6 — Kalshi signing, WS parser, live capture

- **Kalshi WS books get unsequenced events.** The live capture proved `seq`
  is subscription-scoped, not market-scoped, so `Book`'s `seq == last + 1`
  rule would false-positive on any multi-market subscription. The upcoming
  Kalshi WS source tracks seq per `sid`; on a gap it requests fresh
  snapshots (`update_subscription` / `get_snapshot`, documented) and
  invalidates the affected books. Books rely on staleness + explicit resync
  for this venue, same as Polymarket US.
- **Delta mapping**: `side: "yes"` → YES-bid ladder at the quoted price;
  `side: "no"` → YES-ask ladder at the complement. Signed `delta_fp` parses
  through `qty_delta_from_contracts` (resting quantities stay unsigned).
- **`scripts/capture_kalshi_ws.py` is committed** so WS fixtures can be
  re-captured reproducibly (documented params only; discovery via
  `/events?with_nested_markets=true` because `/markets` is flooded with
  zero-volume multivariate shards). It never prints key material.
- **Signing lives in `venues/kalshi/auth.py`** (RSA-PSS SHA-256, salt =
  digest length, per docs) and is tested with throwaway generated keys —
  real keys never enter tests or logs.

## M4 — REST adapters and real fixtures

- **Quantities are integer units of 0.0001 contracts (`Qty`), not integer
  contracts.** CLAUDE.md assumed integer contracts "unless a venue's docs
  prove otherwise" — the live Kalshi book proved otherwise (counts like
  `"15.17"`; see venue-notes). Same fixed-point discipline as prices: exact
  parse or loud failure, no floats.
- **`market_id` is venue-qualified: `kalshi:<ticker>` /
  `polymarket_us:<slug>`.** Native identifiers stay untouched for API calls;
  the qualified form is globally unique across the book map and storage.
- **Polymarket JSON numbers are parsed as `Decimal`** (`parse_float=Decimal`)
  so `orderPriceMinTickSize` and `feeCoefficient` stay exact for the fee
  engine. Kalshi encodes everything as strings already.
- **Fixtures are live captures, committed.** Both venues' market-data REST
  answered unauthenticated (Kalshi's docs claim auth on the orderbook — the
  conflict is recorded in venue-notes; the client will sign anyway once
  credentials exist). WS parsers wait for captured WS payloads — they need
  credentials on both venues.
- **Parsers normalize at the edge**: Kalshi NO bids become YES asks by
  complement inside the adapter, so a `BookSnapshot` is venue-agnostic the
  moment it leaves venue code.

## M3 — config, storage, recorder, compose stack

- **Recorder never blocks the ingest path.** `enqueue` is non-blocking; on a
  full queue the message is dropped and counted
  (`arb_recorder_dropped_total`) — losing one message beats stalling a venue
  feed and losing the connection. The queue (default 100k) absorbs Postgres
  hiccups.
- **Failed sink writes retry in place with backoff.** Batches are never
  reordered or silently discarded; the writer runs under `supervise()`.
- **`raw_messages` stores payload bytes verbatim** (BYTEA), with BIGINT
  nanosecond timestamps (no floats, per project rules) and a unique
  `(run_id, ingest_seq)` so the archive is totally ordered per run and
  duplicate writes fail loudly. Index on `(venue, recv_ts_ns)` for replay
  queries.
- **Alembic reads the database URL from `AppConfig`** (environment / `.env`),
  never from `alembic.ini` — no connection strings in git.
- **Models stay dialect-portable; migrations are Postgres-only.** Fast tests
  run the real models against in-memory SQLite (aiosqlite, dev-only dep);
  the one concession is a `with_variant(Integer, "sqlite")` on the PK since
  SQLite only autoincrements INTEGER keys.
- **Compose images are pinned** (pgvector/pgvector:pg16, prometheus v2.53,
  grafana 11.1) and every host port binds to 127.0.0.1. The app service's
  command is a placeholder until `arb record` exists; inside the network its
  `DATABASE_URL` points at the `postgres` service.

## M2 — reliability layer (run identity, backoff, supervision, WS client)

- **The connector owns URL and auth; the WS client owns reliability.** Both
  venues sign a timestamp into the WebSocket handshake, so headers must be
  recomputed on every attempt — hence a `connector()` factory called per
  connect, with `headers_factory` in the default websockets-based connector.
  Venue code composes a connector + `on_connected` (resubscribe) callback;
  the shared client does reconnect, backoff+jitter, stall detection and
  envelope stamping.
- **Stall detection = no inbound frame within `stall_timeout_s`.**
  Protocol-level ping/pong (websockets' built-in) is the heartbeat;
  the recv timeout catches half-open connections where pings survive but
  data stops. The timeout must sit above the venue's heartbeat cadence
  (Polymarket US cadence is undocumented — measure, then configure).
- **Backoff resets only after a connection proves healthy**
  (`healthy_after_s` uptime), not on mere connect success — a flapping
  endpoint that accepts connections and immediately drops them still gets
  slowed down. Jitter multiplies the delay by a random factor in
  `[1 - jitter_frac, 1]`.
- **`supervise()` never swallows cancellation.** Everything else is logged,
  counted (`arb_supervisor_restarts_total`) and restarted; a clean return of
  a long-running task is treated as a failure and restarted too.
- **`ingest_seq` is allocated per run across all sources** (one
  `RunContext`), so the recorder's stream is totally ordered even when both
  venues are live; `run_id` is a sortable UTC timestamp + random suffix.

## M1 — shared core (types, Book, interfaces)

- **`Book` is a pure data structure.** No I/O, no clocks (callers pass
  monotonic timestamps from the message envelope), no metrics. Keeps it
  exhaustively testable; the manager layer owns metrics and resync policy.
- **Ladders are dicts keyed by price with sorted tuple views.** Prediction
  market books are small (< a few hundred levels, prices bounded 1..9999), so
  O(1) level updates plus O(n log n) reads are simpler than a sorted
  container and fast enough. Revisit only if profiling says so.
- **A locked book (best bid == best ask) counts as crossed.** On both venues a
  bid and ask at the same price would have matched in the engine, so a locked
  state means our book is wrong — invalidate and resync.
- **Structural invalidation is sticky; staleness is not.** NO_SNAPSHOT,
  SEQ_GAP, CROSSED and BAD_LEVEL require a fresh snapshot (`needs_resync`) and
  updates are dropped until one arrives. STALE is computed at query time and
  clears when a contiguous update lands, because a quiet-but-gapless book is
  merely untradeable, not wrong.
- **Sequence policy.** Contiguity (`seq == last_seq + 1`) is enforced when
  both sides have sequence numbers. A book with no sequence adopts the first
  one it sees (Kalshi snapshots may not carry `seq`); unsequenced updates are
  accepted without advancing it (for venues without sequencing). Docs don't
  specify Kalshi gap recovery, so resubscribe + fresh snapshot is our policy.
- **`RawMessage` is a frozen slotted dataclass, not a Pydantic model.** It's
  the hot-path envelope wrapping unparsed bytes for the recorder — there is
  nothing to validate yet. Pydantic v2 stays the rule for config and parsed
  venue message models.
- **Normalized events are YES-side only.** `BookSide` is bid/ask in YES terms;
  adapters fold venue NO-side data in by complement so shared code never
  branches on instrument side.

## M0 — repository scaffold

- **Prices as integer ticks (`Ticks = int`), 1 tick = $0.0001.** Avoids float
  error in book state, fees and storage; analytics may use floats. (Per CLAUDE.md.)
- **src layout, package name `arb`.** Keeps the importable package isolated from
  repo-root config and tests; `arb` is the CLI entry point.
- **Venue module names `kalshi` and `polymarket_us`.** Python-safe identifiers
  for the `src/arb/venues/<venue>/` and `tests/fixtures/<venue>/` dirs.
- **`asyncpg` as the async Postgres driver** for SQLAlchemy 2.0 async
  (`postgresql+asyncpg://`).
- **`.env` holds paths to key files, not secrets themselves.** Secrets live in
  `secrets/` (gitignored); env vars point at them.
- **Deferred to later prompts:** venue endpoints/auth (must be read from docs
  first), docker-compose infra, and all order placement code (Day 1 is read-only).
