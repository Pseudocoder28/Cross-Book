"""Venue-agnostic core types.

Prices are integer ticks of $0.0001 (``Ticks``): 1¢ is 100 and $0.555 is 5550.
Quantities are integer units of 0.0001 contracts (``Qty``): live Kalshi books
contain fractional contract counts (e.g. ``"15.17"``), so plain integer
contracts don't survive contact with real data — see docs/venue-notes.md.
No floats for prices, quantities or fees in book state or storage; floats are
fine for analytics.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

type Ticks = int
type Qty = int

TICKS_PER_DOLLAR: int = 10_000
QTY_PER_CONTRACT: int = 10_000

_ASCII_DIGITS = frozenset("0123456789")


def is_valid_price(price: Ticks) -> bool:
    """A quotable price lies strictly inside (0, 10000) ticks.

    0 and 10000 are settlement values, not prices a resting order can have.
    """
    return 0 < price < TICKS_PER_DOLLAR


def complement(price: Ticks) -> Ticks:
    """NO price for a YES price (and vice versa), in ticks."""
    return TICKS_PER_DOLLAR - price


def _fixed_point_from_str(text: str, *, scale: int) -> int:
    """Parse a non-negative decimal string to an int in ``10**-scale`` units.

    Rejects signs, exponents, whitespace, non-ASCII digits and anything finer
    than the scale — venue payloads must land exactly on a unit or fail loudly.
    """
    whole, _, frac = text.partition(".")
    if not whole and not frac:
        raise ValueError(f"not a decimal amount: {text!r}")
    if whole and not _ASCII_DIGITS.issuperset(whole):
        raise ValueError(f"not a decimal amount: {text!r}")
    if frac and not _ASCII_DIGITS.issuperset(frac):
        raise ValueError(f"not a decimal amount: {text!r}")
    if len(frac) > scale:
        raise ValueError(f"finer than 1e-{scale}: {text!r}")
    frac_units = int(frac.ljust(scale, "0")) if frac else 0
    return int(whole or "0") * 10**scale + frac_units


def _fixed_point_to_str(units: int, *, scale: int) -> str:
    if units < 0:
        raise ValueError(f"negative amount: {units}")
    whole, frac = divmod(units, 10**scale)
    return f"{whole}.{frac:0{scale}d}"


def ticks_from_dollars(text: str) -> Ticks:
    """Parse a decimal dollar string (``"0.4200"``, ``"1"``, ``".5"``) to exact ticks."""
    return _fixed_point_from_str(text, scale=4)


def dollars_from_ticks(ticks: Ticks) -> str:
    """Format ticks as a four-decimal dollar string (``4200`` → ``"0.4200"``)."""
    return _fixed_point_to_str(ticks, scale=4)


def qty_from_contracts(text: str) -> Qty:
    """Parse a decimal contract count (``"15.17"``, ``"45.0000"``) to exact Qty units."""
    return _fixed_point_from_str(text, scale=4)


def contracts_from_qty(qty: Qty) -> str:
    """Format Qty units as a four-decimal contract count (``151700`` → ``"15.1700"``)."""
    return _fixed_point_to_str(qty, scale=4)


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
