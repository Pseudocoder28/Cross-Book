"""Capture real Kalshi WS orderbook frames into tests/fixtures/kalshi/.

Usage: uv run python scripts/capture_kalshi_ws.py

Read-only: subscribes to public orderbook data over the authenticated
WebSocket. Requires KALSHI_API_KEY_ID and the private key file (see
.env.example). Never prints or logs key material.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from websockets.asyncio.client import connect

from arb.config import AppConfig
from arb.run import RunContext
from arb.venues.kalshi.auth import load_private_key, ws_auth_headers
from arb.venues.kalshi.discovery import fetch_liquid_tickers
from arb.venues.kalshi.ws import subscribe_orderbook_cmd

OUT = Path("tests/fixtures/kalshi/ws_orderbook_capture.jsonl")
MAX_SECONDS = 90.0
MIN_DELTAS = 15
TOP_N = 5


async def capture() -> tuple[list[bytes], dict[str, int], set[str]]:
    config = AppConfig()
    if not config.kalshi_api_key_id:
        raise SystemExit("KALSHI_API_KEY_ID not set")
    private_key = load_private_key(config.kalshi_private_key_path)

    targets = await fetch_liquid_tickers(config, RunContext(), top_n=TOP_N)
    if not targets:
        raise SystemExit("no liquid markets found to subscribe to")
    print(f"subscribing to {len(targets)} markets: {targets}")

    headers = ws_auth_headers(key_id=config.kalshi_api_key_id, private_key=private_key)
    counts: dict[str, int] = {}
    snapshot_tickers: set[str] = set()
    captured: list[bytes] = []
    deltas = 0
    deadline = time.monotonic() + MAX_SECONDS

    async with connect(config.kalshi_ws_url, additional_headers=headers) as ws:
        await ws.send(subscribe_orderbook_cmd(1, targets))
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
            except TimeoutError:
                break
            data = frame.encode() if isinstance(frame, str) else bytes(frame)
            captured.append(data)
            doc = json.loads(data)
            frame_type = str(doc.get("type", "?"))
            counts[frame_type] = counts.get(frame_type, 0) + 1
            if frame_type == "orderbook_snapshot":
                snapshot_tickers.add(doc["msg"]["market_ticker"])
            elif frame_type == "orderbook_delta":
                deltas += 1
            if len(snapshot_tickers) >= len(targets) and deltas >= MIN_DELTAS:
                break

    return captured, counts, snapshot_tickers


def main() -> None:
    captured, counts, snapshot_tickers = asyncio.run(capture())
    OUT.write_bytes(b"\n".join(captured) + b"\n")
    print(f"captured {sum(counts.values())} frames: {counts}")
    print(f"snapshots for: {sorted(snapshot_tickers)}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
