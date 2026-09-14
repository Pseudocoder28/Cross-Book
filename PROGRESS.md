# Progress

## Current milestone

**M14 — fees, edge math, ARB screen (Day-2 core, measurement only).**

## What works

- `src/arb/fees.py`: exact integer fee models — Kalshi (0.07·mult·C·P·(1−P), 6-dp round-up then tick alignment, maker 0/0.25/0.5 by `fee_type`) and Polymarket US (Θ·C·p·(1−p), banker's cents, maker rebate) — verified against both venues' documented examples. `src/arb/edge.py`: depth-aware two-direction edge walk in tick·Qty integers.
- `src/arb/arbmon.py`: quotes every tracked confirmed pair off the live books; `arb ui --pairs-top N` resolves fee parameters at startup (Kalshi `GET /series/{ticker}` with event overrides; Polymarket `GET /v1/markets?slug=…`), subscribes both legs, broadcasts an `arb` snapshot on every relevant book change, `GET /api/arb`.
- Terminal `ARB` page: ranked table (net/ct, size, gross, fees, direction, both BBOs, book state with QUIET for quiet-but-live books), detail with legs, fee model, reverse direction; Enter jumps to the Kalshi leg's DES.
- Live: 28 Bitcoin year-end ladder pairs confirmed as a verified test set (identical CF BRTI resolution); the monitor showed real +0.2 to +1.0¢/contract net edges on thin Polymarket books, and surfaced that the `KXBTCY` series carries `fee_multiplier 0`.
- 155 tests; ruff, pyright, `node --check` clean.

### From M13

- `src/arb/pairs/`: `text.py` (venue-vocabulary normalization, name similarity with containment), `matcher.py` (blocked, IDF-weighted title similarity + outcome overlap → one-to-one outcome pairing; explainable features), `store.py` (chunked upserts that never overwrite a human decision; list/decide/decide-many), `run.py` (`arb pairs propose` fetches both universes — Kalshi `/events` cursor pages, Polymarket `/v1/events` at `limit=500` — records them, proposes, persists). Alembic `0002` adds the `pairs` table.
- Universe fetchers: `kalshi.discovery.fetch_universe` / `event_refs`, `polymarket_us.discovery.fetch_active_markets` (429-retry) / `event_refs`.
- Review in the terminal: `PAIRS` command → list sorted by score with both legs, right-hand detail with both rules texts and match features; ↑↓ select, Y/N decide, Shift+Y/Shift+N decide the whole event pairing, U undecide, TAB cycles proposed/confirmed/rejected/all. API: `GET /api/pairs`, `POST /api/pairs/{id}/decide`, `POST /api/pairs/decide` (batch).
- Evaluated on the live universes (6,000 Kalshi events × 3,563 Polymarket events, 88k markets): thousands of high-confidence proposals — identical-title events (NFL divisions, EPL/Serie A, Bitcoin/Musk/gas-price ladders, Supreme Court) at 1.0, MVP/Cy Young/ROTY at ~0.96, state governor/senate races at 0.95. Real data fixes along the way: Polymarket `minimumTradeQty` can be `0.01` (Decimal now), asyncpg's 32,767-parameter cap (chunked upserts).
- 138 tests; ruff, pyright, `node --check` clean.

### From M12

- `src/arb/venues/polymarket_us/{discovery,source,adapter,detail}.py`: category-ranked discovery over `/v1/events` (recorded), a rate-budgeted round-robin book poller (`PolymarketUSRestSource`, an `EventSource`), an adapter emitting unsequenced snapshots + book stats, and a DES payload (tick size, fee coefficient, min qty). `level_deltas` in `books.py` diffs consecutive snapshots into tape events. Per-venue staleness in `BookManager`.
- `arb ui`/`arb record` take `--poly-top N` / `--poly-slugs`; both venues are recorded (`rest:events`, `rest:book`). Status reports `polled`; the terminal shows K/P badges, an amber `POLY US POLLED` pill, and live poll stats in the Polymarket panel; DES works for both venues.
- **Measured the real Polymarket limiter**: a 5-token bucket refilling ~1 token/2 s on the book endpoint (docs claim 20 req/s). Default poll rate 0.45 req/s; verified zero 429s at that rate.
- Infra (M16, committed separately): Grafana "ARB — Data Plane" dashboard (31 panels) provisioned and loaded; compose app container now runs the recording terminal with `restart: unless-stopped`.
- 134 tests; ruff, pyright, `node --check` clean.

### From M11

- Enter / `DES` / double-click opens a Bloomberg-style description page for the selected market: event title and candidate, ticker anatomy (series → event → market), full resolution rules, settlement sources, status/category/type/mutually-exclusive/early-close, venue quote with implied probability, live book summary (best levels, depth totals, age), lifetime + 24h volume, open interest, open/close/expected-expiration in UTC and ET. Arrows page between markets; Esc closes.
- Backend: `GET /api/markets/{market_id}` served from discovery-seeded metadata with a 30 s TTL live refresh via the verified `GET /markets/{ticker}` and `GET /events/{event_ticker}` (both recorded before parsing); 404 for unknown markets. New parsers + `build_market_detail` tested against real captured fixtures.
- Verified in real Chrome over CDP: open/page/close/command flows; live refresh observed (`LIVE · 1s AGO`). 125 tests; ruff, pyright, `node --check` clean.

### From M10

- Selecting anything in the terminal copies it to the clipboard and shows a bottom-right `COPIED · N CHARS · N ROWS` toast (red `COPY BLOCKED` if the browser refuses). Multi-row selections copy as TSV (tab-separated cells, one row per line) so ladder/tape/system selections paste into a spreadsheet with columns intact; a selection inside a single cell copies the exact highlighted substring. Live re-rendering pauses while the pointer is down so updates can't wipe a selection mid-drag, then flushes on release.
- Verified in real Chrome over the DevTools protocol: multi-row ladder → `"1.90\t98.10\t8,838.22\n1.80\t98.20\t1,550"`, system rows → `"MSG TOTAL\t507\nRATE 1S\t0.0/s"`, partial cell → exact substring, plain click copies nothing, UI resumes after the drag, toast auto-hides.

### From M9

- `uv run arb ui` at http://127.0.0.1:8080 — black/amber terminal: live market monitor (real volumes + event titles from discovery), depth ladder with complement NO prices, mid/spread seam and flash-on-change, tape, latency sparkline (last/median/p95), system panel (recorder, parse errors, seq gaps, DB rows by run), Polymarket US down-screen driven by live REST reachability, keyboard navigation, dual UTC/ET clocks.
- Backend: `BookManager` (`src/arb/books.py`) + `KalshiMarketDataAdapter` (per-`sid` seq tracking; gap → metric + `ResyncRequired` + forced WS reconnect for fresh snapshots per the reliability rules); FastAPI server (`src/arb/ui/server.py`) with `/api/status`, `/metrics`, and a WS push protocol (integer ticks / 0.0001-contract units on the wire); recorder-first ingest identical to `arb record`; per-client bounded send queues so a slow browser can never stall the feed.
- Frontend: three static files, vanilla JS/CSS, no build step, no external requests; design synthesized from a judged three-way panel; staleness shown as calm "QUIET", red INVALID reserved for structural book failures.
- Verified live end-to-end (2026-09-14): REST + WS contract probed, headless-Chrome renders confirmed live books, tape deltas, latency ~18 ms median, recorder rows growing in Postgres during viewing.
- 119 tests; ruff, pyright, `node --check` clean.

### From M8

- `src/arb/venues/kalshi/discovery.py`: liquidity-ranked discovery via `/events?with_nested_markets=true` (documented params only); every REST response goes to the recorder before parsing.
- `src/arb/venues/kalshi/source.py`: `KalshiWSSource` — shared `ReconnectingWebSocket` + signed handshake (headers recomputed per attempt) + resubscribe with fresh cmd id on every (re)connect.
- `src/arb/record.py` + `uv run arb record`: sources → recorder → Postgres, supervised tasks, Prometheus metrics server (`metrics_host:metrics_port`; compose sets `METRICS_HOST=0.0.0.0` for in-network scraping), graceful drain on shutdown (`Recorder.drain` via queue join).
- **Verified live end-to-end (2026-09-14)**: 25 s run recorded 2 discovery pages + 10 WS frames into `raw_messages` with contiguous `ingest_seq` under one `run_id`; payload bytes byte-exact in Postgres.
- 99 tests; ruff and pyright clean.

### From M7

- Compose stack running and verified (2026-09-14): Postgres+pgvector healthy, Prometheus ready, Grafana healthy with provisioned datasource, app container built — every port bound to 127.0.0.1 only.
- Migration `0001` applied to real Postgres; verified a live write/read roundtrip through `insert_raw_messages` (rows cleaned up afterwards).
- `uv run arb doctor` exits 0 locally (all ok except the expected Polymarket US keys warn) and **inside the container** via `docker compose exec app uv run arb doctor` (DB over the compose network, keys via mounted `secrets/`, env via `env_file`).
- Dockerfile sets `UV_NO_SYNC=1` so in-container `uv run` uses the baked `--no-dev` environment instead of re-syncing at runtime.
- Note: Prometheus's `arb` scrape target (`app:9000`) stays down until `arb record` serves `/metrics` — expected.

### From M6

- `src/arb/venues/kalshi/auth.py`: RSA-PSS request/handshake signing (doc-verified scheme), tested with throwaway keys; real key confirmed working against the production WS.
- `src/arb/venues/kalshi/ws.py`: subscribe command + `orderbook_snapshot`/`orderbook_delta` parser (signed `delta_fp`, NO-side folded by complement); non-book frames yield no events.
- Live WS capture committed (`tests/fixtures/kalshi/ws_orderbook_capture.jsonl`: 1 ack, 5 snapshots, 15 deltas from five liquid markets) via `scripts/capture_kalshi_ws.py`; replaying the whole capture through `Book`s stays valid and uncrossed.
- **Key protocol finding (recorded in venue-notes): WS `seq` is per-subscription, not per-market** — Kalshi books will run unsequenced with subscription-level gap detection (`get_snapshot` for recovery).
- 96 tests; ruff and pyright clean.

### From M5

- `uv run arb doctor` (`src/arb/doctor.py`): env/.env presence, key provisioning (paths only, never contents), venue reachability + clock skew via HTTP Date (verified live: both venues 200, skew ≈ +0.2 s), database + migration state, free disk. Exits non-zero only on failures. Verified end-to-end against production endpoints; database check correctly FAILs while the compose stack is down (Docker wasn't running on this machine).
- CLI runs under uvloop; `arb` with no command prints help.

### From M4

- Quantities generalized to fixed-point `Qty` (0.0001-contract units) after live Kalshi books showed fractional counts (`"15.17"`).
- `src/arb/venues/kalshi/rest.py`: `parse_markets_response` (cursor pagination) and `parse_orderbook_response` — NO bids folded into YES asks by complement at the edge.
- `src/arb/venues/polymarket_us/rest.py`: `parse_markets_response` (Decimal-exact tick size / fee coefficient) and `parse_book_response`.
- Real fixtures captured live 2026-09-13 into `tests/fixtures/{kalshi,polymarket_us}/` (markets pages + liquid order books); parser tests pin exact normalized values and apply snapshots into `Book` cleanly.
- Empirical findings recorded in venue-notes: Kalshi REST market data is public in practice (docs conflict noted), `/markets` listing is flooded with zero-volume multivariate shards (discover via `/events`), Polymarket market objects carry undocumented fields.

### From M3

- `src/arb/config.py`: `AppConfig` (pydantic-settings, `.env`) with doc-verified endpoint defaults; secrets only as file paths.
- `src/arb/storage/`: SQLAlchemy 2.0 async `raw_messages` model + Alembic (async env, URL from `AppConfig`); migration `0001` renders correct Postgres DDL (BIGSERIAL, BYTEA, timestamptz, unique `(run_id, ingest_seq)`).
- `src/arb/recorder.py`: bounded-queue recorder — non-blocking `enqueue` (drop+count on overflow), batching writer, in-place retry with backoff, supervised.
- Infra: `docker-compose.yml` (Postgres+pgvector, Prometheus, Grafana, app — all on 127.0.0.1), `Dockerfile` (uv, layer-cached), Prometheus scrape config, Grafana datasource provisioning.
- Recorder metrics: enqueued/dropped/written/write-failures counters + queue-depth gauge.
- 71 tests; ruff and pyright clean.

### From M2

- `src/arb/run.py`: `RunContext` — `run_id` (sortable UTC stamp + suffix) and the per-run cross-source `ingest_seq`.
- `src/arb/supervise.py`: `Backoff` (exponential, jittered, resettable) and `supervise()` — restarts long-running tasks with backoff, never swallows cancellation.
- `src/arb/ws.py`: `ReconnectingWebSocket` (an `EventSource`) — per-attempt connector (fresh signed headers every attempt), resubscribe callback on every connect, stall detection via recv timeout, protocol ping/pong heartbeat in the default `websockets` connector, `RawMessage` stamping.
- New metrics: `arb_ws_connects_total`, `arb_ws_connect_failures_total`, `arb_ws_disconnects_total{reason}`, `arb_supervisor_restarts_total`.
- 64 tests; ruff and pyright clean.

### From M1

- `src/arb/types.py`: `Ticks` ($0.0001 units), exact dollar-string ↔ ticks conversion, price bounds, complement, `BookSide`, `RawMessage` envelope (`recv_ts_ns`, `recv_mono_ns`, `run_id`, `ingest_seq`).
- `src/arb/book.py`: normalized `Book` — YES bid/ask ladders best-first, complement-derived NO views, snapshot + SET/DELTA level updates, sequence-gap/crossed/bad-level/staleness validity rules, sticky structural invalidation with `needs_resync`.
- `src/arb/interfaces.py`: `EventSource` and `MarketDataAdapter` protocols, `BookEvent`, `ParseError`.
- `src/arb/metrics.py`: first counters (`arb_book_invalidations_total`, `arb_parse_errors_total`).
- 53 tests (unit + hypothesis) covering tick math and all book validity transitions; ruff and pyright clean.

### From M0

- Repository structure and Python packaging (`pyproject.toml`, src layout, `arb` entry point).
- Tooling config: ruff, pyright (standard mode), pytest (+ pytest-asyncio, hypothesis).
- Secret hygiene: `.gitignore` excludes `.env`, `secrets/` and key files; `.env.example` holds paths and non-secret config only.
- Doc scaffolding: `docs/venue-notes.md`, `docs/decisions.md`, this file.
- Package skeleton: `src/arb/` with `metrics.py` placeholder, `venues/{kalshi,polymarket_us}/`, and a minimal `cli.py` (`arb` prints "not implemented yet").
- Test scaffolding: `tests/` with `fixtures/{kalshi,polymarket_us}/`.
- Kalshi API facts verified against docs.kalshi.com and recorded in `docs/venue-notes.md` (hosts, RSA-PSS auth, WS channels, orderbook snapshot/delta format, fixed-point dollar-string prices, rate limits, discovery endpoints).
- Polymarket US API facts verified against docs.polymarket.us and recorded in `docs/venue-notes.md` (gateway REST is public; markets WS needs Ed25519 API-key auth on handshake; full-book WS messages with no seq numbers; whole contracts; limit/offset pagination; fee formula Θ·C·p·(1−p)).

## What's next

Day 1 (data, read-only):

- Book manager consuming recorded/live events: per-`sid` seq tracking with `get_snapshot` gap recovery, book invalidation metrics, staleness policy.
- Polymarket US REST-polling source into `arb record` (public gateway, 20 req/s budget); WS adapter blocked on their credentials (KYC + polymarket.us/developer).
- Pair matcher for equivalent markets.
- Run the app container as the recorder (swap `sleep infinity` for `arb record`) once multi-venue recording lands.

## Open questions

- Credentials not yet provisioned, and **both venues require authenticated WebSockets even for public market data**. Kalshi: Key ID + RSA PEM (demo or production, user's choice). Polymarket US: Key ID + Ed25519 secret from polymarket.us/developer (app signup + KYC; no sandbox). Until then, Polymarket books can be polled over unauthenticated gateway REST at 20 req/s/IP; Kalshi has no unauthenticated fallback.
- Kalshi: WS gap-recovery procedure unspecified in docs (we chose resubscribe + fresh snapshot); exact WS field names to confirm against asyncapi.yaml; whether `GET /markets`/`GET /events` need auth; market categorization source; fractional contract counts vs integer-quantity assumption.
- Polymarket US: WS wire format is contradictory in the docs (snake_case + numeric enums vs camelCase + string enums) — settle from captured payloads; no seq numbers on the markets WS, so validity rests on staleness + `transactTime` + periodic REST reconciliation; REST book depth and heartbeat cadence undocumented; rules-text field unclear (`description` vs `rulesDisclaimer`).
