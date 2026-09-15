# Kalshi and Polymarket US cross-venue arb

Detects and trades price gaps between equivalent binary markets on Kalshi and Polymarket US. Three-day build: day 1 is data (adapters, books, recorder and pair matcher), day 2 is the engine (fees, models, sizing, risk and paper execution) and day 3 is the control plane plus live trading at tiny size.

## Hard rules

- Day 1 is read-only. No code that places, amends or cancels orders exists until a later prompt asks for it.
- Only Polymarket US (docs.polymarket.us) is in scope. The international Polymarket (polymarket.com and its CLOB and Gamma APIs) is off limits.
- Never log, print or commit secrets. Keys load from file paths set in `.env`. `.env`, `secrets/` and key files are gitignored.
- Never guess an endpoint, field name, auth scheme or message format. Read the venue docs, then record what you verified (with the doc URL) in `docs/venue-notes.md`. When the docs and a prompt disagree, the docs win and the conflict gets noted.
- If you're blocked on credentials or an ambiguous doc, stop and ask. Never stub a venue silently.
- Postgres, Prometheus and Grafana bind to 127.0.0.1 only. On the VM they're reached over an SSH tunnel.

## Prices, quantities and time

- Prices are integer ticks of $0.0001 (`Ticks = int`): 1¢ is 100 and $0.555 is 5550. No floats for prices or fees in book state or storage. Floats are fine for analytics.
- Quantities are integer contracts unless a venue's docs prove otherwise.
- Everything is stored in UTC. Every inbound message carries `recv_ts_ns` (time.time_ns), `recv_mono_ns` (time.monotonic_ns), `run_id` and a per-run `ingest_seq`. ET is for display only, because many rules texts are written in ET.

## Book model

- One normalized book per market: YES bids and YES asks as ladders, best first. NO ladders are derived by complement (NO price = 10000 - YES price, in ticks).
- Kalshi publishes bids only, for YES and for NO. A NO bid at x is a YES ask at 10000 - x.
- Polymarket US has one instrument per market (YES). Buying NO is selling YES.
- A book is valid only while all of these hold:
  - it has a snapshot and no sequence gap since
  - it isn't crossed
  - every level has a positive quantity and a price inside (0, 10000)
  - it updated within the staleness limit
- Anything else marks the book invalid and triggers a resync.

## Reliability

- Every WebSocket gets reconnect with exponential backoff and jitter, a heartbeat, stall detection, resubscribe after reconnect and a fresh snapshot after any gap.
- Every long-running task is supervised and restarts with backoff. One venue failing never takes down the other.
- Every raw inbound message (WebSocket or REST) is enqueued for the recorder before it's parsed. Parse errors are counted and logged, never fatal.
- Every new failure mode gets a Prometheus metric. Metric names live in `src/arb/metrics.py`.
- Venue-specific code lives only under `src/arb/venues/<venue>/`. Everything else goes through the shared interfaces (`EventSource`, `MarketDataAdapter` and `Book`).

## Code conventions

- Python 3.12 with asyncio and uvloop. Type hints everywhere, pyright in standard mode, ruff for lint and format, uv for dependencies.
- Pydantic v2 for config and message models. SQLAlchemy 2.0 async and Alembic for Postgres.
- Tests use pytest, pytest-asyncio and hypothesis. Parser tests run against real captured payloads in `tests/fixtures/<venue>/`, never invented ones.
- Every CLI command works both locally (`uv run arb ...`) and on the VM (`docker compose exec app arb ...`).
- After each milestone: run the checks, update `PROGRESS.md` and commit with a short conventional commit message.

## Commands

- `uv run pytest`, `uv run ruff check .` and `uv run pyright`
- `uv run alembic upgrade head` migrates the database (URL from `.env` via `AppConfig`)
- `docker compose up -d` starts Postgres with pgvector, Prometheus, Grafana and the app
- `uv run arb doctor` checks env, keys, clock skew, database, venue reachability and disk
- `uv run arb record [--tickers T1,T2 | --top N] [--poly-top N | --poly-slugs ...] [--duration S]` streams raw Kalshi WS + Polymarket US REST-polled market data into Postgres
- `uv run arb ui [--tickers ... | --top N] [--poly-top N | --poly-slugs ...] [--pairs-top N] [--port 8080] [--no-record]` serves the terminal UI at http://127.0.0.1:8080 (records while it runs unless --no-record); a multi-page app routed client-side over the History API on one long-lived WebSocket — `/` MONITOR, `/arb`, `/pairs`, `/paper`, `/system`, `/help` and `/market/<market_id>` (DES, not in the nav); `CTRL+1`–`CTRL+6` jump and `CTRL+[`/`CTRL+]` cycle on macOS, `ALT+…` elsewhere (`ALT` is a live alias everywhere); history back/forward is the browser's own chord, unbound in the app; a bare letter at `ARB>` always types — page keys fire only with a list focused; `ESC` clears the command line or returns to MONITOR; `ARB>` commands: `MON`/`MONITOR`, `ARB`, `PAIRS`, `PAPER`, `SYS`/`SYSTEM`, `HELP`/`?`, `BACK`, `DES`, `<TICKER>`, `<TICKER> DES`
- Grafana at http://127.0.0.1:3000 (admin/admin) has the provisioned "ARB — Data Plane" dashboard
- `uv run arb pairs propose [--min-score 0.75]` fetches both venues' open universes and proposes pairs into Postgres; `arb pairs list [--status ...]`, `arb pairs confirm|reject ID`; review in the terminal with the `PAIRS` command
- `uv run arb ui --paper [--min-net-ticks 50 --max-cts-per-pair 100 --max-notional 1000]` simulates fills on measured edges (ledger on the `PAPER` page and `GET /api/paper`)
- `uv run arb replay [RUN_ID] [--pairs-top N] [--paper ...] [--persist]` replays a recorded run through the identical pipeline (default: latest run)
- Keep this list current as commands are added.

## Where decisions live

- `docs/venue-notes.md`: verified API facts with doc links
- `docs/decisions.md`: design choices and why
- `PROGRESS.md`: current milestone, what works, what's next and open questions
