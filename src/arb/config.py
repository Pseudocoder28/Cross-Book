"""Application configuration.

Loaded from the environment and ``.env`` (pydantic-settings). Secrets are
never stored in config — only *paths* to key files (under ``secrets/``,
gitignored). Endpoint defaults below are the doc-verified values recorded in
``docs/venue-notes.md``; anything unverified stays unset.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    run_id: str | None = None

    # --- Kalshi (docs/venue-notes.md: hosts verified 2026-09-13) ---
    kalshi_api_base: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: Path = Path("secrets/kalshi_private_key.pem")

    # --- Polymarket US (docs/venue-notes.md: hosts verified 2026-09-13) ---
    polymarket_us_gateway_base: str = "https://gateway.polymarket.us"
    polymarket_us_api_base: str = "https://api.polymarket.us"
    polymarket_us_ws_url: str = "wss://api.polymarket.us/v1/ws/markets"
    polymarket_us_access_key: str | None = None
    polymarket_us_secret_key_path: Path = Path("secrets/polymarket_us_secret")

    # --- Postgres (127.0.0.1 only; SSH tunnel on the VM) ---
    database_url: str = "postgresql+asyncpg://arb:arb@127.0.0.1:5432/arb"

    # --- Books ---
    book_staleness_limit_ms: int = 5_000

    # --- Recorder ---
    recorder_queue_max: int = 100_000
    recorder_batch_max: int = 500

    # --- Metrics endpoint (compose overrides host to 0.0.0.0 inside the
    # network so Prometheus can scrape; host ports stay 127.0.0.1-only) ---
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9_000
