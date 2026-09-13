# Decisions

Design choices and why. Newest first.

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
