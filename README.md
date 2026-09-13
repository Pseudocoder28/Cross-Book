# arb — Kalshi × Polymarket US cross-venue arbitrage

Detects and (later) trades price gaps between equivalent binary markets on
Kalshi and Polymarket US.

This is the initial repository scaffold. The build plan, hard rules and
conventions live in [`CLAUDE.md`](CLAUDE.md). Read it before adding code.

- Current status and next steps: [`PROGRESS.md`](PROGRESS.md)
- Verified venue API facts: [`docs/venue-notes.md`](docs/venue-notes.md)
- Design decisions and rationale: [`docs/decisions.md`](docs/decisions.md)

## Getting started

```sh
cp .env.example .env      # fill in; .env is gitignored
uv sync                   # install dependencies (dev group included)
uv run pytest
uv run ruff check .
uv run pyright
```

## Layout

```
src/arb/            shared package (interfaces, metrics, config, cli)
src/arb/venues/     venue-specific code only (kalshi/, polymarket_us/)
tests/              pytest suite
tests/fixtures/     real captured payloads for parser tests
docs/               venue notes and design decisions
```
