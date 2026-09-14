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
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from arb.config import AppConfig
from arb.pairs.matcher import EventRef, MarketRef
from arb.run import RunContext
from arb.types import RawMessage
from arb.venues.polymarket_us.rest import (
    PolymarketUSEvent,
    PolymarketUSMarket,
    market_id,
    parse_events_response,
    parse_markets_response,
)

PAGE_SIZE = 500  # accepted by the gateway (verified live); fewer tokens spent
MAX_PAGES = 12
PAGE_PACE_S = 2.2  # one token per ~2 s (venue-notes)
RATE_LIMIT_PAUSE_S = 10.0
RATE_LIMIT_RETRIES = 3


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
            params = {
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "active": "true",
                "closed": "false",
            }
            raw: RawMessage | None = None
            response: httpx.Response | None = None
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                response = await client.get(
                    f"{config.polymarket_us_gateway_base}/v1/events", params=params
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
                if response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                    break
                # Another poller on this IP may have drained the bucket; wait
                # for a full refill and retry the same page.
                await asyncio.sleep(RATE_LIMIT_PAUSE_S)
            assert response is not None and raw is not None
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


async def fetch_markets_by_slug(
    config: AppConfig,
    run: RunContext,
    slugs: Sequence[str],
    *,
    sink: Callable[[RawMessage], object] | None = None,
) -> list[PolymarketUSMarket]:
    """``GET /v1/markets?slug=A&slug=B`` (documented ``slug[]`` filter): fee
    coefficient, tick size and top-of-book for specific markets in one call."""
    if not slugs:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{config.polymarket_us_gateway_base}/v1/markets",
            params={"slug": list(slugs), "limit": str(len(slugs))},
        )
    raw = RawMessage(
        venue="polymarket_us",
        stream="rest:markets",
        payload=response.content,
        recv_ts_ns=time.time_ns(),
        recv_mono_ns=time.monotonic_ns(),
        run_id=run.run_id,
        ingest_seq=run.next_ingest_seq(),
    )
    if sink is not None:
        sink(raw)
    response.raise_for_status()
    return parse_markets_response(raw)


def event_refs(markets: list[DiscoveredPMMarket]) -> list[EventRef]:
    """Matcher view: one EventRef per Polymarket event with its outcomes."""
    by_event: dict[str, list[DiscoveredPMMarket]] = {}
    for m in markets:
        by_event.setdefault(m.event.slug, []).append(m)
    refs: list[EventRef] = []
    for group in by_event.values():
        event = group[0].event
        # Per-market question beats the event title when they differ.
        title = group[0].market.question or event.title
        refs.append(
            EventRef(
                venue="polymarket_us",
                event_id=event.slug,
                title=title,
                category=event.category or group[0].market.category,
                markets=tuple(
                    MarketRef(
                        venue="polymarket_us",
                        market_id=market_id(m.slug),
                        ticker=m.slug,
                        outcome=m.market.title or m.slug,
                        rules=m.market.description,
                        close_time=m.market.end_date,
                        event_slug=event.slug,
                        fee_coefficient=(
                            str(m.market.fee_coefficient)
                            if m.market.fee_coefficient is not None
                            else None
                        ),
                    )
                    for m in group
                ),
                end_time=event.end_date or group[0].market.end_date,
            )
        )
    return refs


def select_poll_targets(markets: list[DiscoveredPMMarket], top_n: int) -> list[DiscoveredPMMarket]:
    """Non-sports categories first (that is where Kalshi overlap lives), then
    sports, preserving listing order within each group."""
    non_sports = [m for m in markets if (m.market.category or "").lower() != "sports"]
    sports = [m for m in markets if (m.market.category or "").lower() == "sports"]
    return (non_sports + sports)[:top_n]
