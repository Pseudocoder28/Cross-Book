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


def test_suspend_keeps_the_spend_budget_and_the_ledger() -> None:
    # The bug this guards: toggling paper off and on by rebuilding the trader
    # re-opens max_notional from zero and wipes the ledger — the simulator
    # then reports profits on money it already spent.
    trader = PaperTrader(
        PaperLimits(min_net_ticks=50, max_qty_per_pair=100 * C, max_notional_ticks=98 * 10_000)
    )
    first = trader.consider(pair(), quote(2, 100), ts_ms=1)
    assert first is not None and first.qty == 100 * C
    assert trader.notional_ticks == 100 * 9800  # the whole $98 budget

    assert trader.suspend() is True
    assert trader.suspended and not trader.enabled
    other = TrackedPair(2, 1.0, "kalshi:K2", "polymarket_us:p2", zero_fee, zero_fee, "BTC — 30k")
    assert trader.consider(other, quote(5, 100), ts_ms=2) is None  # declines everything
    assert trader.skipped_suspended == 1
    assert trader.suspend() is False  # idempotent

    assert trader.resume() is True
    assert trader.enabled and trader.resume() is False
    # Budget and ledger survived the toggle: still exhausted, still one trade.
    assert trader.notional_ticks == 100 * 9800
    assert trader.consider(other, quote(5, 100), ts_ms=3) is None
    assert len(trader.trades) == 1 and trader.trades[0] is first
    assert trader.positions[1].qty == 100 * C


def test_payload_reports_the_live_suspend_state() -> None:
    trader = PaperTrader(PaperLimits(min_net_ticks=50))
    assert trader.payload()["enabled"] is True
    trader.suspend()
    payload = trader.payload()
    assert payload["enabled"] is False and payload["suspended"] is True
    assert payload["skipped_suspended"] == 0
    assert trader.payload(enabled=True)["enabled"] is True  # explicit override still wins


def test_limits_retune_live_and_tightening_is_a_hard_stop() -> None:
    trader = PaperTrader(PaperLimits(min_net_ticks=50, max_notional_ticks=200 * 10_000))
    first = trader.consider(pair(), quote(2, 100), ts_ms=1)
    assert first is not None and trader.notional_ticks == 100 * 9800  # $98 spent

    previous = trader.set_limits(PaperLimits(min_net_ticks=50, max_notional_ticks=50 * 10_000))
    assert previous.max_notional_ticks == 200 * 10_000
    assert trader.limits.max_notional_ticks == 50 * 10_000
    # Tightening below what is already committed blocks new trades; it does
    # not unwind the position, because there is no un-filling a fill.
    other = TrackedPair(2, 1.0, "kalshi:K2", "polymarket_us:p2", zero_fee, zero_fee, "BTC — 30k")
    assert trader.consider(other, quote(5, 100), ts_ms=2) is None
    assert trader.positions[1].qty == 100 * C and trader.notional_ticks == 100 * 9800

    # Raising the floor is felt on the very next quote, both ways.
    trader.set_limits(PaperLimits(min_net_ticks=400, max_notional_ticks=400 * 10_000))
    assert trader.consider(other, quote(2, 100), ts_ms=3) is None  # 2¢ < 4¢ floor
    trader.set_limits(PaperLimits(min_net_ticks=50, max_notional_ticks=400 * 10_000))
    resumed = trader.consider(other, quote(2, 100), ts_ms=4)
    assert resumed is not None and resumed.qty == 100 * C


def test_a_nearly_full_book_stops_instead_of_minting_free_contracts() -> None:
    """A book one tick under its cap must stop, not fill forever at $0.00.

    Regression for a live failure: the trader sat at 99,999,999 of 100,000,000
    ticks and kept admitting trades. With one tick of room it sized the fill at
    a single Qty unit (1e-4 contracts), whose true cost is 0.98 ticks — and
    ``int()`` truncated that to 0. ``notional_ticks`` never advanced, so the cap
    was never reached and the same dust trade repeated indefinitely: 163 of them
    in one minute, each costing nothing, with the spend meter frozen.
    """
    trader = PaperTrader(PaperLimits(min_net_ticks=50, max_notional_ticks=100 * 10_000))
    first = trader.consider(pair(), quote(2, 100), ts_ms=1)
    assert first is not None
    # 98¢/contract against $100 of room: 100 contracts would be $98, and the
    # remaining $2 is what the next trade has to work with.
    assert trader.notional_ticks == 100 * 9800

    # Tighten to exactly one tick of headroom. The next fill is one Qty unit,
    # costing a fraction of a tick — which must still be charged as a whole one.
    trader.set_limits(PaperLimits(min_net_ticks=50, max_notional_ticks=100 * 9800 + 1))
    other = TrackedPair(2, 1.0, "kalshi:K2", "polymarket_us:p2", zero_fee, zero_fee, "BTC — 30k")
    dust = trader.consider(other, quote(2, 100), ts_ms=2)
    assert dust is not None
    assert dust.cost_ticks >= 1, "a fill that costs a fraction of a tick must be charged one"
    assert trader.notional_ticks > 100 * 9800, "the spend meter has to advance"

    # ...and now the book really is full, and stays that way.
    for ts in range(3, 13):
        assert trader.consider(other, quote(5, 100), ts_ms=ts) is None
    assert trader.totals()["trades"] == 2
