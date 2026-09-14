"""Kalshi market discovery over documented REST params only.

Discovery goes through ``GET /events?with_nested_markets=true`` (documented;
excludes multivariate events by design) because the raw ``/markets`` listing
is flooded with zero-volume multivariate shard markets — see venue-notes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from arb.config import AppConfig
from arb.run import RunContext
from arb.types import RawMessage

MAX_PAGES = 10
ENOUGH_CANDIDATES = 200


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    ticker: str
    volume_24h: float  # analytics/display only — float is fine here
    title: str  # "<event title> — <yes_sub_title>", best effort


async def fetch_liquid_markets(
    config: AppConfig,
    run: RunContext,
    *,
    top_n: int,
    sink: Callable[[RawMessage], object] | None = None,
) -> list[DiscoveredMarket]:
    """Return the ``top_n`` open markets by 24h volume (desc).

    Every REST response is offered to ``sink`` (the recorder) as a
    RawMessage before it's parsed, per the recording rule.
    """
    found: dict[str, DiscoveredMarket] = {}
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
                event_title = str(event.get("title") or "")
                for market in event.get("markets") or []:
                    volume = float(market.get("volume_24h_fp") or 0)
                    if volume <= 0:
                        continue
                    sub = str(market.get("yes_sub_title") or "")
                    title = f"{event_title} — {sub}" if sub else event_title
                    found[market["ticker"]] = DiscoveredMarket(
                        ticker=market["ticker"], volume_24h=volume, title=title
                    )
            cursor = doc.get("cursor") or ""
            if not cursor or len(found) >= ENOUGH_CANDIDATES:
                break
    ranked = sorted(found.values(), key=lambda m: m.volume_24h, reverse=True)
    return ranked[:top_n]


async def fetch_liquid_tickers(
    config: AppConfig,
    run: RunContext,
    *,
    top_n: int,
    sink: Callable[[RawMessage], object] | None = None,
) -> list[str]:
    """Ticker-only convenience over :func:`fetch_liquid_markets`."""
    return [m.ticker for m in await fetch_liquid_markets(config, run, top_n=top_n, sink=sink)]
