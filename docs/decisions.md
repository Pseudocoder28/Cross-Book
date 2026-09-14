# Decisions

Design choices and why. Newest first.

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
