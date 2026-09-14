"""Polymarket US market discovery over the public gateway (documented params).

``GET /v1/events?active=true&closed=false&limit&offset`` with nested markets
(https://docs.polymarket.us/api-reference/events/get-events). Live listings
carry no volume fields, so targets are chosen by category (non-sports first,
where the cross-venue overlap with Kalshi lives) rather than by volume.
Every response is offered to the recorder before parsing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from arb.config import AppConfig
from arb.run import RunContext
from arb.types import RawMessage
from arb.venues.polymarket_us.rest import (
    PolymarketUSEvent,
    PolymarketUSMarket,
    parse_events_response,
)

PAGE_SIZE = 50
MAX_PAGES = 8
PAGE_PACE_S = 2.2  # one token per ~2 s (venue-notes)


@dataclass(frozen=True, slots=True)
class DiscoveredPMMarket:
    slug: str
    title: str  # "<question> — <outcome>"
    market: PolymarketUSMarket
    event: PolymarketUSEvent


async def fetch_active_markets(
    config: AppConfig,
    run: RunContext,
    *,
    sink: Callable[[RawMessage], object] | None = None,
    max_pages: int = MAX_PAGES,
) -> list[DiscoveredPMMarket]:
    found: dict[str, DiscoveredPMMarket] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for page in range(max_pages):
            response = await client.get(
                f"{config.polymarket_us_gateway_base}/v1/events",
                params={
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "active": "true",
                    "closed": "false",
                },
            )
            raw = RawMessage(
                venue="polymarket_us",
                stream="rest:events",
                payload=response.content,
                recv_ts_ns=time.time_ns(),
                recv_mono_ns=time.monotonic_ns(),
                run_id=run.run_id,
                ingest_seq=run.next_ingest_seq(),
            )
            if sink is not None:
                sink(raw)
            response.raise_for_status()
            pairs = parse_events_response(raw)
            for event, markets in pairs:
                for market in markets:
                    if not market.active or market.closed:
                        continue
                    outcome = market.title
                    title = f"{market.question} — {outcome}" if outcome else market.question
                    found[market.slug] = DiscoveredPMMarket(
                        slug=market.slug, title=title, market=market, event=event
                    )
            if len(pairs) < PAGE_SIZE:
                break
            # Paged discovery must not read as a burst to the gateway's
            # limiter (a 429 costs a ~10 s cooldown — venue-notes).
            await asyncio.sleep(PAGE_PACE_S)
    return list(found.values())


def select_poll_targets(markets: list[DiscoveredPMMarket], top_n: int) -> list[DiscoveredPMMarket]:
    """Non-sports categories first (that is where Kalshi overlap lives), then
    sports, preserving listing order within each group."""
    non_sports = [m for m in markets if (m.market.category or "").lower() != "sports"]
    sports = [m for m in markets if (m.market.category or "").lower() == "sports"]
    return (non_sports + sports)[:top_n]
