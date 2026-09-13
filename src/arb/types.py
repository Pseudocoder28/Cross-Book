"""Venue-agnostic core types.

Prices are integer ticks of $0.0001 (``Ticks``): 1¢ is 100 and $0.555 is 5550.
No floats for prices or fees in book state or storage; floats are fine for
analytics. Quantities are integer contracts unless a venue's docs prove
otherwise.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

type Ticks = int

TICKS_PER_DOLLAR: int = 10_000

_ASCII_DIGITS = frozenset("0123456789")


def is_valid_price(price: Ticks) -> bool:
    """A quotable price lies strictly inside (0, 10000) ticks.

    0 and 10000 are settlement values, not prices a resting order can have.
    """
    return 0 < price < TICKS_PER_DOLLAR


def complement(price: Ticks) -> Ticks:
    """NO price for a YES price (and vice versa), in ticks."""
    return TICKS_PER_DOLLAR - price


def ticks_from_dollars(text: str) -> Ticks:
    """Parse a decimal dollar string (``"0.4200"``, ``"1"``, ``".5"``) to exact ticks.

    Rejects signs, exponents, whitespace, non-ASCII digits and anything finer
    than $0.0001 — venue payloads must land exactly on a tick or fail loudly.
    """
    whole, sep, frac = text.partition(".")
    if not whole and not frac:
        raise ValueError(f"not a decimal dollar amount: {text!r}")
    if whole and not _ASCII_DIGITS.issuperset(whole):
        raise ValueError(f"not a decimal dollar amount: {text!r}")
    if frac and not _ASCII_DIGITS.issuperset(frac):
        raise ValueError(f"not a decimal dollar amount: {text!r}")
    if sep and not frac and not whole:
        raise ValueError(f"not a decimal dollar amount: {text!r}")
    if len(frac) > 4:
        raise ValueError(f"finer than one $0.0001 tick: {text!r}")
    frac_ticks = int(frac.ljust(4, "0")) if frac else 0
    return int(whole or "0") * TICKS_PER_DOLLAR + frac_ticks


def dollars_from_ticks(ticks: Ticks) -> str:
    """Format ticks as a four-decimal dollar string (``4200`` → ``"0.4200"``)."""
    if ticks < 0:
        raise ValueError(f"negative ticks: {ticks}")
    whole, frac = divmod(ticks, TICKS_PER_DOLLAR)
    return f"{whole}.{frac:04d}"


class BookSide(enum.Enum):
    """Side of the normalized YES book. Venue NO-side data is mapped here by
    complement before it reaches shared code."""

    BID = "bid"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class RawMessage:
    """Envelope for every raw inbound venue message (WebSocket or REST).

    Enqueued for the recorder before any parsing. ``payload`` is the exact
    bytes received.
    """

    venue: str
    stream: str  # e.g. "ws", "rest:orderbook"
    payload: bytes
    recv_ts_ns: int  # time.time_ns() at receipt (UTC wall clock)
    recv_mono_ns: int  # time.monotonic_ns() at receipt
    run_id: str
    ingest_seq: int  # per-run, monotonically increasing across all sources
