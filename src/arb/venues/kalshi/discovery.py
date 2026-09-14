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
from arb.venues.kalshi.rest import (
    KalshiEvent,
    KalshiMarket,
    parse_event_response,
    parse_market_response,
)

MAX_PAGES = 10
ENOUGH_CANDIDATES = 200


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    ticker: str
    volume_24h: float  # analytics/display only — float is fine here
    title: str  # "<event title> — <yes_sub_title>", best effort
    market: KalshiMarket
    event: KalshiEvent


def _stamp(run: RunContext, stream: str, payload: bytes) -> RawMessage:
    return RawMessage(
        venue="kalshi",
        stream=stream,
        payload=payload,
        recv_ts_ns=time.time_ns(),
        recv_mono_ns=time.monotonic_ns(),
        run_id=run.run_id,
        ingest_seq=run.next_ingest_seq(),
    )


async def fetch_market(
    config: AppConfig,
    run: RunContext,
    ticker: str,
    *,
    sink: Callable[[RawMessage], object] | None = None,
) -> KalshiMarket:
    """``GET /markets/{ticker}`` (documented), recorded before parsing."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{config.kalshi_api_base}/markets/{ticker}")
    raw = _stamp(run, "rest:market", response.content)
    if sink is not None:
        sink(raw)
    response.raise_for_status()
    return parse_market_response(raw)


async def fetch_event(
    config: AppConfig,
    run: RunContext,
    event_ticker: str,
    *,
    sink: Callable[[RawMessage], object] | None = None,
) -> KalshiEvent:
    """``GET /events/{event_ticker}`` (documented), recorded before parsing."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{config.kalshi_api_base}/events/{event_ticker}")
    raw = _stamp(run, "rest:event", response.content)
    if sink is not None:
        sink(raw)
    response.raise_for_status()
    return parse_event_response(raw)


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
                sink(_stamp(run, "rest:events", response.content))
            response.raise_for_status()
            doc = json.loads(response.content)
            for event_doc in doc["events"]:
                # Nested markets are full Market objects (get-events.md: items
                # $ref Market), so the same models apply.
                try:
                    event = KalshiEvent.model_validate(event_doc)
                except ValueError:
                    continue
                for market_doc in event_doc.get("markets") or []:
                    try:
                        market = KalshiMarket.model_validate(market_doc)
                    except ValueError:
                        continue
                    volume = float(market.volume_24h_fp or 0)
                    if volume <= 0:
                        continue
                    sub = market.yes_sub_title
                    title = f"{event.title} — {sub}" if sub else event.title
                    found[market.ticker] = DiscoveredMarket(
                        ticker=market.ticker,
                        volume_24h=volume,
                        title=title,
                        market=market,
                        event=event,
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
