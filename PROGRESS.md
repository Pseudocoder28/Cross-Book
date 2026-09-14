# Progress

## Current milestone

**M6 — Kalshi auth, WS parser, live WS capture.**

## What works

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

- Kalshi WS `EventSource` (connector with signed headers + resubscribe + per-`sid` seq tracking + `get_snapshot` gap recovery) wired into `arb record` → recorder → Postgres. End-to-end DB verification needs Docker running (`docker compose up -d`, then `uv run alembic upgrade head`).
- Polymarket US WS adapter — blocked on their credentials (KYC + polymarket.us/developer); REST polling works meanwhile.
- Pair matcher for equivalent markets (Kalshi discovery via `/events?with_nested_markets=true`).

## Open questions

- Credentials not yet provisioned, and **both venues require authenticated WebSockets even for public market data**. Kalshi: Key ID + RSA PEM (demo or production, user's choice). Polymarket US: Key ID + Ed25519 secret from polymarket.us/developer (app signup + KYC; no sandbox). Until then, Polymarket books can be polled over unauthenticated gateway REST at 20 req/s/IP; Kalshi has no unauthenticated fallback.
- Kalshi: WS gap-recovery procedure unspecified in docs (we chose resubscribe + fresh snapshot); exact WS field names to confirm against asyncapi.yaml; whether `GET /markets`/`GET /events` need auth; market categorization source; fractional contract counts vs integer-quantity assumption.
- Polymarket US: WS wire format is contradictory in the docs (snake_case + numeric enums vs camelCase + string enums) — settle from captured payloads; no seq numbers on the markets WS, so validity rests on staleness + `transactTime` + periodic REST reconciliation; REST book depth and heartbeat cadence undocumented; rules-text field unclear (`description` vs `rulesDisclaimer`).
