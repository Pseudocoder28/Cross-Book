"""Application configuration.

Loaded from the environment and ``.env`` (pydantic-settings). Secrets are
never stored in config — only *paths* to key files (under ``secrets/``,
gitignored). Endpoint defaults below are the doc-verified values recorded in
``docs/venue-notes.md``; anything unverified stays unset.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

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
    # REST polling budget until WS credentials exist. The docs say 20 req/s
    # per IP; the book endpoint actually behaves like a 5-token bucket that
    # refills one token per ~2 s (measured, venue-notes). 0.45/s leaves room
    # for the once-a-minute reachability probe.
    polymarket_us_poll_rate: float = 0.45
    polymarket_us_poll_top: int = 8

    # --- Postgres (127.0.0.1 only; SSH tunnel on the VM) ---
    database_url: str = "postgresql+asyncpg://arb:arb@127.0.0.1:5432/arb"

    # --- Clock (SNTP server doctor measures the local clock against; the
    # venues' HTTP Date header only has 1 s resolution) ---
    ntp_server: str = "pool.ntp.org"

    # --- Books ---
    book_staleness_limit_ms: int = 5_000

    # --- Recorder ---
    recorder_queue_max: int = 100_000
    recorder_batch_max: int = 500

    # --- Metrics endpoint (compose overrides host to 0.0.0.0 inside the
    # network so Prometheus can scrape; host ports stay 127.0.0.1-only) ---
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9_000

    # --- Terminal UI (compose overrides host to 0.0.0.0 inside the network;
    # the host port mapping stays 127.0.0.1-only) ---
    ui_host: str = "127.0.0.1"
    ui_port: int = 8_080

    # --- Terminal UI security (enforced in src/arb/ui/security.py) ---
    # Binding anywhere but loopback exposes controls that start a trading
    # process to anything that can route here, so it is an explicit opt-in.
    # Compose needs 0.0.0.0 *inside* its network (the published port stays
    # 127.0.0.1-only), so the hatch has to be settable from the environment —
    # ARB_ALLOW_REMOTE_BIND is the documented name, UI_ALLOW_REMOTE_BIND is
    # accepted too so it matches the UI_* prefix of the settings beside it.
    ui_allow_remote_bind: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ARB_ALLOW_REMOTE_BIND",
            "UI_ALLOW_REMOTE_BIND",
            "ui_allow_remote_bind",
        ),
    )
    # Read-only serves every view and refuses every control, so a tunnelled
    # port can be shown to someone without handing them the controls.
    ui_read_only: bool = False
    # Comma-separated allowlists layered on top of "loopback is always fine"
    # (empty by default). Strings rather than lists because pydantic-settings
    # decodes complex env values as JSON; arb.ui.security.parse_csv splits them.
    ui_allowed_hosts: str = ""
    ui_allowed_origins: str = ""
