"""Paper trader limits and fill scaling (engine logic, synthetic quotes)."""

from arb.arbmon import TrackedPair
from arb.book import Level
from arb.edge import compute_direction
from arb.paper import PaperLimits, PaperTrader

C = 10_000


def zero_fee(qty: int, price: int) -> int:
    return 0


def pair() -> TrackedPair:
    return TrackedPair(1, 1.0, "kalshi:K", "polymarket_us:p", zero_fee, zero_fee, "BTC — 25k")


def quote(gross_cents: int, contracts: int):  # type: ignore[no-untyped-def]
    return compute_direction(
        direction="yes_a_no_b",
        venue_a="kalshi",
        market_a="kalshi:K",
        yes_asks_a=[Level(4000, contracts * C)],
        fee_a=zero_fee,
        venue_b="polymarket_us",
        market_b="polymarket_us:p",
        yes_bids_b=[Level(4000 + gross_cents * 100, contracts * C)],
        fee_b=zero_fee,
    )


def test_ignores_edges_below_the_floor_and_no_edge() -> None:
    trader = PaperTrader(PaperLimits(min_net_ticks=150))  # 1.5 cents per contract
    assert trader.consider(pair(), quote(0, 100), ts_ms=1) is None  # no edge at all
    assert trader.consider(pair(), quote(1, 100), ts_ms=1) is None  # 1 cent < 1.5 cent floor
    trade = trader.consider(pair(), quote(2, 100), ts_ms=1)  # 2 cents clears it
    assert trade is not None and trade.net_ticks == 100 * 200  # 2 cents x 100 contracts = $2.00


def test_caps_per_pair_and_scales_partial_fills() -> None:
    trader = PaperTrader(PaperLimits(min_net_ticks=50, max_qty_per_pair=30 * C))
    trade = trader.consider(pair(), quote(2, 100), ts_ms=5)  # 2¢ edge on 100 contracts
    assert trade is not None
    assert trade.qty == 30 * C  # capped by the per-pair limit
    # Cost scales with the fill: YES at 40¢ + NO at (100 - 42)¢ = 98¢/contract → $29.40.
    assert trade.cost_ticks == 30 * 9800
    assert trade.net_ticks == 30 * 200  # 2¢ x 30 = $0.60
    assert trade.legs[0].qty == trade.legs[1].qty == 30 * C
    # The pair is now full: no further fills at any edge.
    assert trader.consider(pair(), quote(5, 100), ts_ms=6) is None
    assert trader.positions[1].qty == 30 * C and trader.totals()["trades"] == 1


def test_notional_cap_across_pairs() -> None:
    trader = PaperTrader(PaperLimits(min_net_ticks=50, max_notional_ticks=49 * 10_000))  # $49
    trade = trader.consider(
        pair(), quote(2, 100), ts_ms=1
    )  # 98¢ per contract → 50 contracts would be $49
    assert trade is not None and trade.qty == 50 * C
    assert trader.notional_ticks == 50 * 9800
    other = TrackedPair(2, 1.0, "kalshi:K2", "polymarket_us:p2", zero_fee, zero_fee, "BTC — 30k")
    assert trader.consider(other, quote(2, 100), ts_ms=2) is None  # budget exhausted
    payload = trader.payload()
    assert payload["totals"]["trades"] == 1 and payload["trades"][0]["pair_id"] == 1
