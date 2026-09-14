"""Kalshi market discovery over documented REST params only.

Discovery goes through ``GET /events?with_nested_markets=true`` (documented;
excludes multivariate events by design) because the raw ``/markets`` listing
is flooded with zero-volume multivariate shard markets — see venue-notes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from arb.config import AppConfig
from arb.run import RunContext
from arb.types import RawMessage

MAX_PAGES = 10
ENOUGH_CANDIDATES = 200


async def fetch_liquid_tickers(
    config: AppConfig,
    run: RunContext,
    *,
    top_n: int,
    sink: Callable[[RawMessage], object] | None = None,
) -> list[str]:
    """Return the ``top_n`` open market tickers by 24h volume.

    Every REST response is offered to ``sink`` (the recorder) as a
    RawMessage before it's parsed, per the recording rule.
    """
    volumes: dict[str, float] = {}
    cursor = ""
    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(MAX_PAGES):
            params: dict[str, str | int] = {
                "limit": 200,
                "status": "open",
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            response = await client.get(f"{config.kalshi_api_base}/events", params=params)
            if sink is not None:
                sink(
                    RawMessage(
                        venue="kalshi",
                        stream="rest:events",
                        payload=response.content,
                        recv_ts_ns=time.time_ns(),
                        recv_mono_ns=time.monotonic_ns(),
                        run_id=run.run_id,
                        ingest_seq=run.next_ingest_seq(),
                    )
                )
            response.raise_for_status()
            doc = json.loads(response.content)
            for event in doc["events"]:
                for market in event.get("markets") or []:
                    volume = float(market.get("volume_24h_fp") or 0)
                    if volume > 0:
                        volumes[market["ticker"]] = volume
            cursor = doc.get("cursor") or ""
            if not cursor or len(volumes) >= ENOUGH_CANDIDATES:
                break
    ranked = sorted(volumes.items(), key=lambda item: item[1], reverse=True)
    return [ticker for ticker, _ in ranked[:top_n]]
