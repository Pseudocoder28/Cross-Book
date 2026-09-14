"""Cross-venue arbitrage edge for one confirmed pair, depth-aware.

Both legs are the same binary outcome. Buying YES on venue A and NO on venue
B locks in $1.00 at settlement for ``yes_ask_A + no_ask_B`` — and buying NO
is selling YES, so ``no_ask_B = 10000 - yes_bid_B``. Gross edge per contract
is therefore ``yes_bid_B - yes_ask_A`` in ticks. Both directions are
evaluated, walking both ladders level by level in increasing-cost order and
stopping at the first fill whose marginal edge no longer covers both
venues' taker fees.

Units, all integers:
- prices in ticks ($0.0001); quantities in Qty units (0.0001 contracts)
- fee functions return ticks for the whole fill
- ``*_tq`` fields are tick·Qty products (ticks per contract * Qty units);
  divide by ``QTY_PER_CONTRACT`` for ticks (dollars * 10^4)

This module never places orders — it only measures.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from arb.book import Level
from arb.types import QTY_PER_CONTRACT, TICKS_PER_DOLLAR, Qty, Ticks

type FeeFn = Callable[[Qty, Ticks], Ticks]  # (qty, price) -> taker fee for the fill, ticks


@dataclass(frozen=True, slots=True)
class Leg:
    venue: str
    market_id: str
    side: str  # "buy_yes" | "buy_no"
    worst_price: Ticks  # marginal price paid (YES terms for buy_yes, NO terms for buy_no)
    qty: Qty
    cost_tq: int  # sum price * qty over fills (tick·Qty)
    fee_ticks: Ticks


@dataclass(frozen=True, slots=True)
class EdgeQuote:
    direction: str  # "yes_a_no_b" | "yes_b_no_a"
    qty: Qty  # size the edge holds for (0 = no edge)
    gross_tq: int  # sum (yes_bid_B - yes_ask_A) * qty
    fee_ticks: Ticks  # both legs' taker fees
    net_tq: int  # gross_tq - fee_ticks * QTY_PER_CONTRACT
    legs: tuple[Leg, Leg]

    @property
    def net_ticks(self) -> int:
        """Total net edge in ticks (dollars * 10^4)."""
        return self.net_tq // QTY_PER_CONTRACT

    @property
    def net_per_contract_ticks(self) -> Ticks:
        return self.net_tq // self.qty if self.qty else 0

    @property
    def gross_per_contract_ticks(self) -> Ticks:
        return self.gross_tq // self.qty if self.qty else 0


def _walk(asks: Sequence[Level], bids: Sequence[Level]) -> list[tuple[Ticks, Ticks, Qty]]:
    """Merge-walk two ladders (best first): (ask, bid, qty) fills, qty
    limited by whichever level runs out first."""
    fills: list[tuple[Ticks, Ticks, Qty]] = []
    i = j = 0
    ask_left = asks[0].qty if asks else 0
    bid_left = bids[0].qty if bids else 0
    while i < len(asks) and j < len(bids):
        take = min(ask_left, bid_left)
        if take > 0:
            fills.append((asks[i].price, bids[j].price, take))
        ask_left -= take
        bid_left -= take
        if ask_left == 0:
            i += 1
            ask_left = asks[i].qty if i < len(asks) else 0
        if bid_left == 0:
            j += 1
            bid_left = bids[j].qty if j < len(bids) else 0
    return fills


def compute_direction(
    *,
    direction: str,
    venue_a: str,
    market_a: str,
    yes_asks_a: Sequence[Level],
    fee_a: FeeFn,
    venue_b: str,
    market_b: str,
    yes_bids_b: Sequence[Level],
    fee_b: FeeFn,
) -> EdgeQuote:
    """Buy YES on A (lifting A's asks) and NO on B (hitting B's YES bids)."""
    qty: Qty = 0
    gross_tq = 0
    fees: Ticks = 0
    fee_a_total: Ticks = 0
    fee_b_total: Ticks = 0
    cost_a_tq = 0
    cost_b_tq = 0
    worst_a: Ticks = 0
    worst_b: Ticks = 0
    for ask, bid, take in _walk(yes_asks_a, yes_bids_b):
        marginal_gross = bid - ask  # ticks per contract
        fill_fee_a = fee_a(take, ask)
        fill_fee_b = fee_b(take, bid)
        # Fill is worth taking only if its gross covers its own fees:
        # marginal_gross * take / QTY_PER_CONTRACT  >  fees   (both in ticks)
        if marginal_gross * take <= (fill_fee_a + fill_fee_b) * QTY_PER_CONTRACT:
            break
        no_price = TICKS_PER_DOLLAR - bid
        qty += take
        gross_tq += marginal_gross * take
        fees += fill_fee_a + fill_fee_b
        fee_a_total += fill_fee_a
        fee_b_total += fill_fee_b
        cost_a_tq += ask * take
        cost_b_tq += no_price * take
        worst_a, worst_b = ask, no_price
    return EdgeQuote(
        direction=direction,
        qty=qty,
        gross_tq=gross_tq,
        fee_ticks=fees,
        net_tq=gross_tq - fees * QTY_PER_CONTRACT,
        legs=(
            Leg(venue_a, market_a, "buy_yes", worst_a, qty, cost_a_tq, fee_a_total),
            Leg(venue_b, market_b, "buy_no", worst_b, qty, cost_b_tq, fee_b_total),
        ),
    )


def best_edge(
    *,
    venue_a: str,
    market_a: str,
    bids_a: Sequence[Level],
    asks_a: Sequence[Level],
    fee_a: FeeFn,
    venue_b: str,
    market_b: str,
    bids_b: Sequence[Level],
    asks_b: Sequence[Level],
    fee_b: FeeFn,
) -> tuple[EdgeQuote, EdgeQuote]:
    """Both directions; callers pick the better ``net_per_contract_ticks``."""
    yes_a_no_b = compute_direction(
        direction="yes_a_no_b",
        venue_a=venue_a,
        market_a=market_a,
        yes_asks_a=asks_a,
        fee_a=fee_a,
        venue_b=venue_b,
        market_b=market_b,
        yes_bids_b=bids_b,
        fee_b=fee_b,
    )
    yes_b_no_a = compute_direction(
        direction="yes_b_no_a",
        venue_a=venue_b,
        market_a=market_b,
        yes_asks_a=asks_b,
        fee_a=fee_b,
        venue_b=venue_a,
        market_b=market_a,
        yes_bids_b=bids_a,
        fee_b=fee_a,
    )
    return yes_a_no_b, yes_b_no_a
