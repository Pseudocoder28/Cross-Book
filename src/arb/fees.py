"""Venue fee models in exact integer arithmetic.

Inputs are ticks ($0.0001) and Qty units (0.0001 contracts); outputs are
fee amounts in ticks for the whole quantity. No floats anywhere: the
formulas are evaluated with :class:`fractions.Fraction` and rounded exactly
the way each venue documents.

Kalshi (docs/venue-notes.md, fee research 2026-09-14):
    model_fee = 0.07 * multiplier * C * P * (1 - P)   [dollars]
    maker on ``quadratic_with_maker_fees`` = 0.25 * that; on
    ``quadratic_with_combo_maker_fees`` = 0.5 * that; maker on plain
    ``quadratic`` = 0. ``trade_fee`` = model fee rounded UP to $0.000001;
    a direct member's balance is aligned to $0.0001, so the net cost is
    the trade fee rounded up to the next tick (sub-tick residue is banked
    and rebated later in the same order — ignored here, conservative).

Polymarket US (https://docs.polymarket.us/fees):
    fee = Θ * C * p * (1 - p), taker Θ = market ``feeCoefficient`` (0.06),
    maker Θ = -0.0125 (a rebate); banker's rounding to the cent per fill.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from arb.types import QTY_PER_CONTRACT, TICKS_PER_DOLLAR, Qty, Ticks

KALSHI_TAKER_COEFF = Fraction(7, 100)
KALSHI_MAKER_MULT = {
    "quadratic": Fraction(0),
    "quadratic_with_maker_fees": Fraction(1, 4),
    "quadratic_with_combo_maker_fees": Fraction(1, 2),
}
POLYMARKET_MAKER_COEFF = Fraction(-125, 10_000)  # -0.0125 rebate
MICRO_PER_DOLLAR = 1_000_000


def _c_p_1mp(qty: Qty, price: Ticks) -> Fraction:
    """C * P * (1 - P) in contract·dollar² units, exact."""
    if qty < 0 or not 0 < price < TICKS_PER_DOLLAR:
        raise ValueError("qty must be >= 0 and price strictly inside (0, 10000)")
    contracts = Fraction(qty, QTY_PER_CONTRACT)
    p = Fraction(price, TICKS_PER_DOLLAR)
    return contracts * p * (1 - p)


def kalshi_fee_ticks(
    qty: Qty,
    price: Ticks,
    *,
    taker: bool,
    fee_type: str = "quadratic",
    fee_multiplier: Fraction | Decimal | int = 1,
) -> Ticks:
    """Kalshi trading fee for filling ``qty`` at ``price``, in ticks (>= 0).

    Unknown ``fee_type`` values (e.g. ``flat``, whose table is not in the API)
    are treated as ``quadratic`` for takers — the conservative reading — and
    as no-maker-fee.
    """
    mult = Fraction(fee_multiplier)
    coeff = KALSHI_TAKER_COEFF * mult
    if not taker:
        coeff *= KALSHI_MAKER_MULT.get(fee_type, Fraction(0))
    if coeff == 0:
        return 0
    model_fee = coeff * _c_p_1mp(qty, price)  # dollars
    trade_fee_micro = math.ceil(model_fee * MICRO_PER_DOLLAR)
    # Direct-member balance precision is one tick: round up to it.
    return math.ceil(Fraction(trade_fee_micro, MICRO_PER_DOLLAR // TICKS_PER_DOLLAR))


def polymarket_fee_ticks(
    qty: Qty,
    price: Ticks,
    *,
    taker: bool,
    fee_coefficient: Fraction | Decimal | str = Decimal("0.06"),
) -> Ticks:
    """Polymarket US fee in ticks; negative for a maker rebate."""
    theta = Fraction(Decimal(str(fee_coefficient))) if taker else POLYMARKET_MAKER_COEFF
    fee = theta * _c_p_1mp(qty, price)  # dollars, exact
    cents = Decimal(fee.numerator) / Decimal(fee.denominator) * 100
    rounded_cents = cents.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return int(rounded_cents) * (TICKS_PER_DOLLAR // 100)
