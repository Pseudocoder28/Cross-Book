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

Three CLI commands, and only three. `arb ui` starts the server the controls
live in; `arb doctor` is what you run when that server will not start (a button
inside a dead process diagnoses nothing); `arb replay` is the worker the UI's
`jobs.replay` spawns as a subprocess. Everything else is a control on
`/control` — recording, paper trading, the market universe, tracked pairs and
pair proposal/backfill are runtime state, not argv.

- `uv run pytest`, `uv run ruff check .` and `uv run pyright`; `node --test "tests/js/**/*.test.mjs"` runs the frontend unit suite (node's built-in runner — no package.json, no npm). `uv run pytest -m "not browser"` skips the headless-Chrome acceptance tests for a fast loop; they skip themselves where there is no Chrome.
- `uv run alembic upgrade head` migrates the database (URL from `.env` via `AppConfig`). Run it before using the UI's controls: every action writes an audit row to `control_actions` (migration `0004`), and a G3 action refuses to arm if that write fails.
- `docker compose up -d` starts Postgres with pgvector, Prometheus, Grafana and the app
- `uv run arb doctor` checks env, keys, clock skew, database, venue reachability and disk (also a `/control` job, `jobs.doctor`)
- `uv run arb ui [--tickers T1,T2 | --top N] [--poly-top N | --poly-slugs ...] [--pairs-top N] [--host H] [--port 8080] [--no-record] [--read-only] [--paper --min-net-ticks 50 --max-cts-per-pair 100 --max-notional 1000]` serves the terminal UI at http://127.0.0.1:8080. Every flag is a *starting* value the UI changes from there; `docker-compose.yml` runs `arb ui --top 20 --host 0.0.0.0`, so the flag names are a live dependency. A non-loopback `--host` is refused unless `ARB_ALLOW_REMOTE_BIND=1`; `--read-only` (or `UI_READ_ONLY=1`) serves every view and refuses every control.
- The UI is a multi-page app routed client-side over the History API on one long-lived WebSocket — `/` MONITOR, `/arb`, `/pairs`, `/paper`, `/system`, `/control`, `/help` and `/market/<market_id>` (DES, not in the nav); `CTRL+1`–`CTRL+7` jump and `CTRL+[`/`CTRL+]` cycle on macOS, `ALT+…` elsewhere (`ALT` is a live alias everywhere); history back/forward is the browser's own chord, unbound in the app; a bare letter at `ARB>` always types — page keys fire only with a list focused; `ESC` clears the command line or returns to MONITOR; `ARB>` commands: `MON`/`MONITOR`, `ARB`, `PAIRS`, `PAPER`, `SYS`/`SYSTEM`, `HELP`/`?`, `BACK`, `DES`, `<TICKER>`, `<TICKER> DES`
- `/control` is the operating surface: recorder on/off, paper suspend/resume and live risk limits, the Kalshi and Polymarket US universes, tracked-pairs top N, and the jobs (doctor, propose, backfill, replay, cancel). Every action goes through one executor (`POST /api/control/{action}`), which owns the read-only refusal, the server-side arm-then-confirm and the audit row. G3 actions arm first and show the sentence that gets recorded. The same route answers `{"preview": true}` with the effect sentence and does nothing — the page uses it before any action that replaces a set (watch set, universes).
- `uv run arb replay [RUN_ID] [--pairs-top N] [--paper ...] [--persist]` replays a recorded run through the identical pipeline (default: latest run). It is also a machine interface: `ControlPlane._apply_replay` builds this argv, so flag names, defaults and the optional positional are a contract pinned by `tests/test_cli.py`.
- Grafana at http://127.0.0.1:3000 (admin/admin) has the provisioned "ARB — Data Plane" dashboard; Prometheus scrapes `arb ui`'s own `GET /metrics` and nothing else
- `uv run python scripts/preview_ui.py [--port 8765] [--failures]` serves the real UI against simulated books (Kalshi streamed, Polymarket US polled; `--failures` cycles a seq gap and a crossed book) with no venues, keys or database — for designing and judging the MONITOR depth panel's motion. `uv run python scripts/snap.py OUT [--selector #depth] [--frames N] [--market ID] [--until JS] [--reduced]` screenshots or filmstrips it in headless Chrome. Dev tools, not `arb` commands.
- Keep this list current as commands and controls are added.

## Where decisions live

- `docs/venue-notes.md`: verified API facts with doc links
- `docs/decisions.md`: design choices and why
- `PROGRESS.md`: current milestone, what works, what's next and open questions
