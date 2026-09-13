"""Normalized order book.

One book per market: YES bids and YES asks as ladders, best first. NO ladders
are derived by complement (NO price = 10000 - YES price, in ticks). Adapters
map venue-specific data (e.g. Kalshi NO bids) into YES-side events before they
reach this module.

A book is valid only while all of these hold:

- it has a snapshot and no sequence gap since
- it isn't crossed (best bid strictly below best ask)
- every level has a positive quantity and a price inside (0, 10000)
- it updated within the staleness limit

Anything else marks the book invalid; structural invalidation is sticky until
the next snapshot (``needs_resync``), while staleness clears on its own when a
contiguous update arrives.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from arb.types import BookSide, Ticks, complement, is_valid_price


@dataclass(frozen=True, slots=True)
class Level:
    price: Ticks
    qty: int


class UpdateMode(enum.Enum):
    SET = "set"  # qty is the new absolute quantity at the price
    DELTA = "delta"  # qty is added to the existing quantity (may be negative)


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Full YES-side state of one market's book, as normalized by an adapter."""

    market_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    seq: int | None


@dataclass(frozen=True, slots=True)
class BookLevelUpdate:
    """One incremental change to a single YES-side price level."""

    market_id: str
    side: BookSide
    price: Ticks
    qty: int
    mode: UpdateMode
    seq: int | None


class InvalidReason(enum.Enum):
    NO_SNAPSHOT = "no_snapshot"
    SEQ_GAP = "seq_gap"
    CROSSED = "crossed"
    BAD_LEVEL = "bad_level"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class BookStatus:
    valid: bool
    reason: InvalidReason | None = None


_STRUCTURAL = frozenset(
    {
        InvalidReason.NO_SNAPSHOT,
        InvalidReason.SEQ_GAP,
        InvalidReason.CROSSED,
        InvalidReason.BAD_LEVEL,
    }
)


class Book:
    """Mutable book state for one market.

    Pure data structure: no I/O, no clocks (callers pass monotonic
    timestamps), no metrics. Sequence numbers are venue/subscription scoped;
    when the book holds no sequence yet it adopts the first one it sees, and
    updates without sequence numbers are accepted without advancing it.
    """

    def __init__(self, market_id: str, *, staleness_limit_ns: int) -> None:
        if staleness_limit_ns <= 0:
            raise ValueError("staleness_limit_ns must be positive")
        self.market_id = market_id
        self._staleness_limit_ns = staleness_limit_ns
        self._bids: dict[Ticks, int] = {}
        self._asks: dict[Ticks, int] = {}
        self._last_seq: int | None = None
        self._last_update_mono_ns: int | None = None
        self._invalid: InvalidReason | None = InvalidReason.NO_SNAPSHOT

    # -- state application ---------------------------------------------------

    def apply_snapshot(self, snap: BookSnapshot, *, mono_ns: int) -> BookStatus:
        """Replace all state from a fresh snapshot. Clears any invalidation."""
        bids: dict[Ticks, int] = {}
        asks: dict[Ticks, int] = {}
        for ladder, levels in ((bids, snap.bids), (asks, snap.asks)):
            for level in levels:
                if not is_valid_price(level.price) or level.qty <= 0:
                    return self._invalidate(InvalidReason.BAD_LEVEL)
                ladder[level.price] = level.qty
        self._bids = bids
        self._asks = asks
        self._last_seq = snap.seq
        self._last_update_mono_ns = mono_ns
        self._invalid = None
        if self._is_crossed():
            return self._invalidate(InvalidReason.CROSSED)
        return BookStatus(valid=True)

    def apply_level(self, update: BookLevelUpdate, *, mono_ns: int) -> BookStatus:
        """Apply one incremental level change.

        Ignored (state untouched) while the book needs a resync — the caller
        is expected to be fetching a fresh snapshot.
        """
        if self._invalid is not None:
            return BookStatus(valid=False, reason=self._invalid)
        if update.seq is not None:
            if self._last_seq is not None and update.seq != self._last_seq + 1:
                return self._invalidate(InvalidReason.SEQ_GAP)
            self._last_seq = update.seq
        if not is_valid_price(update.price):
            return self._invalidate(InvalidReason.BAD_LEVEL)
        ladder = self._bids if update.side is BookSide.BID else self._asks
        if update.mode is UpdateMode.SET:
            new_qty = update.qty
        else:
            new_qty = ladder.get(update.price, 0) + update.qty
        if new_qty < 0:
            return self._invalidate(InvalidReason.BAD_LEVEL)
        if new_qty == 0:
            ladder.pop(update.price, None)
        else:
            ladder[update.price] = new_qty
        self._last_update_mono_ns = mono_ns
        if self._is_crossed():
            return self._invalidate(InvalidReason.CROSSED)
        return BookStatus(valid=True)

    def _invalidate(self, reason: InvalidReason) -> BookStatus:
        self._invalid = reason
        return BookStatus(valid=False, reason=reason)

    # -- validity ------------------------------------------------------------

    def status(self, *, now_mono_ns: int) -> BookStatus:
        if self._invalid is not None:
            return BookStatus(valid=False, reason=self._invalid)
        assert self._last_update_mono_ns is not None  # set by every snapshot
        if now_mono_ns - self._last_update_mono_ns > self._staleness_limit_ns:
            return BookStatus(valid=False, reason=InvalidReason.STALE)
        return BookStatus(valid=True)

    @property
    def needs_resync(self) -> bool:
        """True when only a fresh snapshot can make this book valid again."""
        return self._invalid in _STRUCTURAL

    @property
    def has_snapshot(self) -> bool:
        return self._last_update_mono_ns is not None

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    def _is_crossed(self) -> bool:
        # A locked book (bid == ask) cannot persist on these venues either:
        # matching engines would have crossed the orders, so it means our
        # state is wrong.
        if not self._bids or not self._asks:
            return False
        return max(self._bids) >= min(self._asks)

    # -- views (YES side) ----------------------------------------------------

    def bids(self) -> tuple[Level, ...]:
        """YES bids, best (highest price) first."""
        return tuple(Level(p, self._bids[p]) for p in sorted(self._bids, reverse=True))

    def asks(self) -> tuple[Level, ...]:
        """YES asks, best (lowest price) first."""
        return tuple(Level(p, self._asks[p]) for p in sorted(self._asks))

    def best_bid(self) -> Level | None:
        if not self._bids:
            return None
        price = max(self._bids)
        return Level(price, self._bids[price])

    def best_ask(self) -> Level | None:
        if not self._asks:
            return None
        price = min(self._asks)
        return Level(price, self._asks[price])

    # -- views (NO side, derived by complement) ------------------------------

    def no_bids(self) -> tuple[Level, ...]:
        """NO bids, best first: the complement of the YES asks."""
        return tuple(Level(complement(level.price), level.qty) for level in self.asks())

    def no_asks(self) -> tuple[Level, ...]:
        """NO asks, best first: the complement of the YES bids."""
        return tuple(Level(complement(level.price), level.qty) for level in self.bids())
