"""ArbMonitor over synthetic books (engine logic, not venue payloads)."""

from functools import partial

from arb.arbmon import ArbMonitor, TrackedPair
from arb.book import BookSnapshot, Level
from arb.books import BookManager
from arb.fees import kalshi_fee_ticks, polymarket_fee_ticks

C = 10_000


def make() -> tuple[BookManager, ArbMonitor, TrackedPair]:
    books = BookManager(staleness_limit_ns=10**12)
    pair = TrackedPair(
        pair_id=7,
        score=1.0,
        kalshi_market_id="kalshi:K",
        polymarket_market_id="polymarket_us:p",
        kalshi_fee=partial(kalshi_fee_ticks, taker=True),
        polymarket_fee=partial(polymarket_fee_ticks, taker=True),
        label="Bitcoin — 25,000 to 29,999.99",
        kalshi_ticker="K",
        polymarket_ticker="p",
    )
    return books, ArbMonitor(books, [pair]), pair


def test_quote_without_books_is_flat_and_flags_missing_legs() -> None:
    _, mon, pair = make()
    q = mon.quote(pair)
    assert q["best"]["qty"] == 0 and q["best"]["net_per_contract_ticks"] == 0
    assert q["kalshi"]["has_book"] is False and q["kalshi"]["reason"] == "no_book"


def test_quote_picks_the_profitable_direction_and_affected_lookup() -> None:
    books, mon, pair = make()
    # Kalshi YES ask 40¢ vs Polymarket YES bid 48¢ → buy YES on Kalshi, NO on Polymarket.
    books.apply(
        [BookSnapshot("kalshi:K", (Level(3900, 100 * C),), (Level(4000, 100 * C),), None)],
        mono_ns=0,
    )
    books.apply(
        [BookSnapshot("polymarket_us:p", (Level(4800, 100 * C),), (Level(4900, 100 * C),), None)],
        mono_ns=0,
    )
    q = mon.quote(pair, now_mono_ns=1)
    assert q["best"]["direction"] == "yes_a_no_b"
    assert q["best"]["contracts"] == 100.0
    assert q["best"]["net_per_contract_ticks"] == 482  # 8¢ gross - 3.18¢ fees
    assert q["best"]["legs"][0]["venue"] == "kalshi" and q["best"]["legs"][1]["side"] == "buy_no"
    assert q["kalshi"]["valid"] and q["kalshi"]["best_ask"] == [4000, 100 * C]
    assert q["other"]["qty"] == 0  # the reverse direction has no edge
    assert [p.pair_id for p in mon.affected(["polymarket_us:p"])] == [7]
    assert mon.affected(["kalshi:nope"]) == []
    assert mon.snapshot()[0]["pair_id"] == 7
