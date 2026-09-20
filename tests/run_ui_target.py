"""The real ``arb.ui.server.run_ui`` as a child process. Not a test module.

``tests/shutdown_target.py`` proves ``serve_then_cleanup``; this proves the
thing an operator actually stops. It is ``arb.cli._run_ui`` with a config that
cannot reach anything: every venue address is a closed local port, the
database is an in-memory sqlite, the signing key is a throwaway the test
generated, and the HTTP port is ephemeral. No venue, credential or Postgres is
involved, and none is reachable.

    python run_ui_target.py <throwaway-key.pem>

Logging goes to stdout, so the test can wait for uvicorn's ``Uvicorn running
on`` line and read ``run_ui``'s own ``shutdown complete`` line afterwards.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

import uvloop

from arb.config import AppConfig
from arb.ui.server import run_ui

# The discard port: nothing listens on it, so a connect is refused at once.
NOWHERE = "127.0.0.1:9"
# See tests/shutdown_target.py: an orphaned child ends itself.
ORPHAN_LIMIT_S = 300


def closed_port_config(key_path: Path, *, key_id: str | None) -> AppConfig:
    """An ``AppConfig`` that reads no ``.env`` and points nowhere."""
    return AppConfig(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url="sqlite+aiosqlite:///:memory:",
        kalshi_api_base=f"http://{NOWHERE}",
        kalshi_ws_url=f"ws://{NOWHERE}",
        kalshi_api_key_id=key_id,
        kalshi_private_key_path=key_path,
        polymarket_us_gateway_base=f"http://{NOWHERE}",
        polymarket_us_api_base=f"http://{NOWHERE}",
        polymarket_us_ws_url=f"ws://{NOWHERE}",
    )


def main(argv: list[str]) -> int:
    (key_path,) = argv
    # A foreground `arb ui`, however pytest itself was launched.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.alarm(ORPHAN_LIMIT_S)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(name)s: %(message)s")
    config = closed_port_config(Path(key_path), key_id="not-a-real-key-id")
    try:
        uvloop.run(
            run_ui(
                config,
                tickers=["T"],
                top_n=0,
                record=True,
                host="127.0.0.1",
                port=0,
                poly_top=0,
                pairs_top=0,
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
