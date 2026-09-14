"""Fee models against the venues' documented examples, and the depth-aware
edge walk on synthetic ladders (engine logic, not venue payloads)."""

from decimal import Decimal
from fractions import Fraction

import pytest

from arb.book import Level
from arb.edge import best_edge, compute_direction
from arb.fees import kalshi_fee_ticks, polymarket_fee_ticks

C = 10_000  # one contract in Qty units


class TestKalshiFees:
    def test_doc_example_one_contract_at_5_5_cents(self) -> None:
        # fee_rounding.md: model fee $0.00363825 -> trade fee ceil6 $0.003639
        # -> direct-member tick alignment $0.0037 = 37 ticks.
        assert kalshi_fee_ticks(C, 550, taker=True) == 37

    def test_2022_table_rows(self) -> None:
        # 0.07 * 100 * P * (1-P): $0.05 -> 0.3325, $0.50 -> 1.75, $0.95 -> 0.3325
        assert kalshi_fee_ticks(100 * C, 500, taker=True) == 3325
        assert kalshi_fee_ticks(100 * C, 5000, taker=True) == 17_500
        assert kalshi_fee_ticks(100 * C, 9500, taker=True) == 3325

    def test_maker_rules_by_fee_type(self) -> None:
        assert kalshi_fee_ticks(100 * C, 5000, taker=False, fee_type="quadratic") == 0
        assert (
            kalshi_fee_ticks(100 * C, 5000, taker=False, fee_type="quadratic_with_maker_fees")
            == 4375
        )  # 0.25 * $1.75
        assert (
            kalshi_fee_ticks(100 * C, 5000, taker=False, fee_type="quadratic_with_combo_maker_fees")
            == 8750
        )  # 0.5 * $1.75

    def test_multiplier_scales_coefficient(self) -> None:
        # 2022 INX schedule: 0.035 = 0.5 * 0.07 -> 100 @ $0.50 = $0.875
        assert kalshi_fee_ticks(100 * C, 5000, taker=True, fee_multiplier=Fraction(1, 2)) == 8750
        assert kalshi_fee_ticks(100 * C, 5000, taker=True, fee_multiplier=0) == 0

    def test_fractional_contracts_and_bounds(self) -> None:
        assert kalshi_fee_ticks(C // 100, 5000, taker=True) >= 1  # 0.01 contract still costs a tick
        with pytest.raises(ValueError):
            kalshi_fee_ticks(C, 0, taker=True)
        with pytest.raises(ValueError):
            kalshi_fee_ticks(-1, 5000, taker=True)


class TestPolymarketFees:
    def test_doc_maxima_at_50_cents(self) -> None:
        # Θ * 100 * 0.5 * 0.5: taker 0.06 -> $1.50; maker -0.0125 -> -$0.3125 -> -$0.31
        assert polymarket_fee_ticks(100 * C, 5000, taker=True) == 15_000
        assert polymarket_fee_ticks(100 * C, 5000, taker=False) == -3100

    def test_bankers_rounding_to_the_cent(self) -> None:
        # 0.06 * 1 * 0.055 * 0.945 = $0.003118 -> rounds to $0.00
        assert polymarket_fee_ticks(C, 550, taker=True) == 0
        # 0.06 * 25 * 0.5 * 0.5 = $0.375 -> half-even -> $0.38? no: 37.5 cents -> 38 (even)
        assert polymarket_fee_ticks(25 * C, 5000, taker=True) == 3800
        # 0.06 * 75 * 0.5 * 0.5 = $1.125 -> 112.5 cents -> 112 (even)
        assert polymarket_fee_ticks(75 * C, 5000, taker=True) == 11_200

    def test_per_market_coefficient(self) -> None:
        assert (
            polymarket_fee_ticks(100 * C, 5000, taker=True, fee_coefficient=Decimal("0.02")) == 5000
        )


def kalshi_taker(qty: int, price: int) -> int:
    return kalshi_fee_ticks(qty, price, taker=True)


def pm_taker(qty: int, price: int) -> int:
    return polymarket_fee_ticks(qty, price, taker=True)


def zero_fee(qty: int, price: int) -> int:
    return 0


class TestEdge:
    def test_fees_can_erase_a_three_cent_gross(self) -> None:
        # Kalshi YES ask 40¢ vs Polymarket YES bid 43¢: 3¢ gross per contract,
        # but taker fees on 100 contracts are $1.68 + $1.47 = 3.15¢/contract.
        q = compute_direction(
            direction="yes_a_no_b",
            venue_a="kalshi",
            market_a="k",
            yes_asks_a=[Level(4000, 100 * C)],
            fee_a=kalshi_taker,
            venue_b="polymarket_us",
            market_b="p",
            yes_bids_b=[Level(4300, 100 * C)],
            fee_b=pm_taker,
        )
        assert q.qty == 0 and q.net_tq == 0 and q.net_per_contract_ticks == 0

    def test_eight_cent_gross_nets_after_fees(self) -> None:
        q = compute_direction(
            direction="yes_a_no_b",
            venue_a="kalshi",
            market_a="k",
            yes_asks_a=[Level(4000, 100 * C)],
            fee_a=kalshi_taker,
            venue_b="polymarket_us",
            market_b="p",
            yes_bids_b=[Level(4800, 100 * C)],
            fee_b=pm_taker,
        )
        assert q.qty == 100 * C
        assert q.gross_per_contract_ticks == 800
        assert q.fee_ticks == 16_800 + 15_000  # $1.68 Kalshi + $1.50 Polymarket
        assert q.net_ticks == 80_000 - 31_800  # $8.00 gross - $3.18 fees = $4.82
        assert q.net_per_contract_ticks == 482
        yes_leg, no_leg = q.legs
        assert (yes_leg.side, yes_leg.worst_price, yes_leg.fee_ticks) == ("buy_yes", 4000, 16_800)
        assert (no_leg.side, no_leg.worst_price, no_leg.fee_ticks) == ("buy_no", 5200, 15_000)

    def test_walks_depth_in_cost_order_and_stops_when_marginal_edge_dies(self) -> None:
        q = compute_direction(
            direction="yes_a_no_b",
            venue_a="a",
            market_a="a",
            yes_asks_a=[Level(4000, 50 * C), Level(4100, 50 * C), Level(4600, 50 * C)],
            fee_a=zero_fee,
            venue_b="b",
            market_b="b",
            yes_bids_b=[Level(4800, 30 * C), Level(4500, 100 * C), Level(4400, 100 * C)],
            fee_b=zero_fee,
        )
        # Fills: (4000,4800)*30, (4000,4500)*20, (4100,4500)*50; (4600,4500) is negative: stop.
        assert q.qty == 100 * C
        assert q.gross_tq == (800 * 30 + 500 * 20 + 400 * 50) * C
        assert q.net_per_contract_ticks == 540
        assert q.legs[0].worst_price == 4100 and q.legs[1].worst_price == 10_000 - 4500

    def test_both_directions_and_empty_books(self) -> None:
        d1, d2 = best_edge(
            venue_a="a",
            market_a="a",
            bids_a=[Level(4900, C)],
            asks_a=[Level(5100, C)],
            fee_a=zero_fee,
            venue_b="b",
            market_b="b",
            bids_b=[Level(5300, C)],
            asks_b=[Level(5400, C)],
            fee_b=zero_fee,
        )
        assert d1.direction == "yes_a_no_b" and d1.net_per_contract_ticks == 200  # 5300 - 5100
        assert d2.direction == "yes_b_no_a" and d2.qty == 0  # 4900 - 5400 < 0
        empty, _ = best_edge(
            venue_a="a",
            market_a="a",
            bids_a=[],
            asks_a=[],
            fee_a=zero_fee,
            venue_b="b",
            market_b="b",
            bids_b=[],
            asks_b=[],
            fee_b=zero_fee,
        )
        assert empty.qty == 0
