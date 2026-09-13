# Progress

## Current milestone

**M1 — shared core (types, Book, interfaces).**

## What works

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

## What's next

Day 1 (data, read-only):

- Verify Polymarket US API facts against docs.polymarket.us and record them in `docs/venue-notes.md` (in progress).
- WebSocket client with reconnect/backoff/jitter, heartbeat, stall detection, resubscribe and gap-triggered resnapshot; supervised tasks.
- Raw-message recorder (enqueue before parse) and Postgres storage (SQLAlchemy 2.0 async + Alembic).
- Venue adapters under `src/arb/venues/`, parser tests against real captured fixtures.
- Pair matcher for equivalent markets.
- `uv run arb doctor`.
- `docker compose` for Postgres (pgvector), Prometheus, Grafana, app.

## Open questions

- Credentials not yet provisioned. Kalshi requires an authenticated WebSocket even for public market data, so a Kalshi API key (Key ID + RSA PEM) blocks all Kalshi streaming — demo or production, user's choice. Polymarket US requirements unknown until its docs are verified.
- Kalshi: WS gap-recovery procedure unspecified in docs (we chose resubscribe + fresh snapshot); exact WS field names to confirm against asyncapi.yaml; whether `GET /markets`/`GET /events` need auth; market categorization source; fractional contract counts vs integer-quantity assumption.
- Polymarket US: everything — auth, endpoints, WS format — still to be read from docs.polymarket.us.
