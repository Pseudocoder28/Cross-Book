"""Shared interfaces every venue implements.

Venue-specific code lives only under ``src/arb/venues/<venue>/``; everything
else depends on these protocols and the normalized ``Book`` model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from arb.book import BookLevelUpdate, BookSnapshot
from arb.types import RawMessage


@dataclass(frozen=True, slots=True)
class ResyncRequired:
    """Adapter-detected loss of stream continuity (e.g. a subscription-level
    sequence gap).

    ``market_ids`` is ``None`` when every book on the venue is affected —
    Kalshi's ``seq`` is per-subscription, so a gap taints all markets in it.
    Consumers invalidate the affected books and arrange fresh snapshots.
    """

    venue: str
    market_ids: tuple[str, ...] | None


type BookEvent = BookSnapshot | BookLevelUpdate | ResyncRequired


class ParseError(ValueError):
    """Raised by adapters on a malformed venue payload.

    Callers count these (they are a metric, never fatal) and keep consuming.
    """


class EventSource(Protocol):
    """Async stream of raw venue messages.

    Implementations own reconnect with backoff and jitter, heartbeats, stall
    detection, resubscribe after reconnect and snapshot refresh after gaps;
    consumers just iterate. Every yielded message has already been enqueued
    for the recorder.
    """

    @property
    def venue(self) -> str: ...

    def stream(self) -> AsyncIterator[RawMessage]: ...


class MarketDataAdapter(Protocol):
    """Turns one raw venue message into zero or more normalized book events.

    Adapters do all venue-specific mapping — field names, price parsing to
    ticks, and folding NO-side data into the YES book by complement — so the
    events they emit are venue-agnostic.
    """

    @property
    def venue(self) -> str: ...

    def parse(self, raw: RawMessage) -> Sequence[BookEvent]:
        """Raises :class:`ParseError` on malformed payloads."""
        ...
