"""Pair matcher on verbatim event fixtures from both venues."""

from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arb.pairs import run as pairs_run
from arb.pairs import store
from arb.pairs.matcher import MarketRef, PairCandidate, propose_pairs
from arb.pairs.text import name_similarity, name_tokens, title_tokens
from arb.storage.models import Base, PairRow
from arb.types import RawMessage
from arb.venues.kalshi import discovery as kd
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket
from arb.venues.polymarket_us import discovery as pd
from arb.venues.polymarket_us.rest import parse_events_response

FIX = Path(__file__).parent / "fixtures"


def raw(venue: str, payload: bytes) -> RawMessage:
    return RawMessage(venue, "rest:events", payload, 1, 1, "t", 0)


def kalshi_refs():  # type: ignore[no-untyped-def]
    import json

    doc = json.loads((FIX / "kalshi" / "rest_events_pairs_subset.json").read_bytes())
    pairs = [
        (KalshiEvent.model_validate(e), [KalshiMarket.model_validate(m) for m in e["markets"]])
        for e in doc["events"]
    ]
    return kd.event_refs(pairs)


def pm_refs():  # type: ignore[no-untyped-def]
    payload = (FIX / "polymarket_us" / "rest_events_pairs_subset.json").read_bytes()
    found = [
        pd.DiscoveredPMMarket(slug=m.slug, title=m.question, market=m, event=e)
        for e, ms in parse_events_response(raw("polymarket_us", payload))
        for m in ms
    ]
    return pd.event_refs(found)


class TestText:
    def test_title_tokens_unify_venue_wording(self) -> None:
        assert title_tokens("Which party will win the U.S. House?") == title_tokens(
            "U.S House Midterm Winner"
        )
        # "Award" is extra vocabulary on one side; everything else must agree.
        assert title_tokens("NL Cy Young Winner?") <= title_tokens("National League Cy Young Award")
        assert title_tokens("NL Cy Young Winner?") >= {"national", "league", "cy", "young"}

    def test_name_similarity_ignores_party_and_suffix(self) -> None:
        assert (
            name_similarity(name_tokens("Sherrod Brown (D)"), name_tokens("Sherrod Brown")) == 1.0
        )
        assert (
            name_similarity(name_tokens("Fernando Tatis Jr."), name_tokens("Fernando Tatís Jr"))
            == 1.0
        )
        assert name_similarity(name_tokens("Los Angeles Dodgers"), name_tokens("Dodgers")) == 1.0
        # One shared common token must not pair different people.
        assert name_similarity(name_tokens("Chris Sale"), name_tokens("Chris Paddack")) < 0.75


class TestMatcher:
    def test_pairs_house_control_and_cy_young(self) -> None:
        cands = propose_pairs(kalshi_refs(), pm_refs(), min_score=0.5)
        by_pm = {c.polymarket.ticker: c for c in cands}

        dem = by_pm["paccc-usho-midterms-2026-11-03-dem"]
        assert dem.kalshi.ticker == "CONTROLH-2026-D"
        assert dem.polymarket.outcome == "Democratic Party"
        assert dem.features["outcome_similarity"] == 1.0
        rep = by_pm["paccc-usho-midterms-2026-11-03-rep"]
        assert rep.kalshi.ticker == "CONTROLH-2026-R"

        # Cy Young: every Polymarket player with a Kalshi twin pairs to that twin.
        cy = [c for c in cands if c.polymarket_event == "National League Cy Young Award"]
        assert len(cy) >= 10
        assert all(c.kalshi.ticker.startswith("KXMLBNLCY-26") for c in cy)
        assert all(name_tokens(c.kalshi.outcome) == name_tokens(c.polymarket.outcome) for c in cy)
        # Never pairs a Polymarket market twice, never below the floor.
        assert len({c.polymarket.market_id for c in cands}) == len(cands)
        assert all(c.score >= 0.5 for c in cands)
        # Detail carries both rules texts for the reviewer.
        assert dem.kalshi.rules and dem.polymarket.rules


async def _pairs_engine(n: int) -> AsyncEngine:
    """In-memory store holding ``n`` proposals, descending score."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            insert(PairRow),
            [
                {
                    "kalshi_market_id": f"kalshi:K{i}",
                    "polymarket_market_id": f"polymarket_us:p{i}",
                    "status": "proposed",
                    "score": 1.0 - i / 1000,
                    "detail": {"kalshi": {"ticker": f"K{i}"}, "polymarket_us": {"ticker": f"p{i}"}},
                }
                for i in range(n)
            ],
        )
    return engine


async def test_list_pairs_limit_caps_rows_best_score_first() -> None:
    """A full proposal run stores thousands; the CLI must be able to ask for
    the top few without pulling (or printing) all of them."""
    engine = await _pairs_engine(25)
    try:
        top = await store.list_pairs(engine, limit=5)
        assert [r["kalshi"]["ticker"] for r in top] == ["K0", "K1", "K2", "K3", "K4"]
        # Unlimited stays the default, so the UI's PAIRS screen is untouched.
        assert len(await store.list_pairs(engine)) == 25
        # A limit above the row count is not an error.
        assert len(await store.list_pairs(engine, limit=100)) == 25
    finally:
        await engine.dispose()


def test_format_rows_never_truncates_identifiers() -> None:
    """A cut slug looks real and resolves to nothing.

    Regression: fixed-width columns sliced tickers to 34/40 chars, so
    ``cpc-btc-pricerange-yr-12-31-2026-above-150k`` (43) printed as a
    prefix that 404s on the venue.
    """
    long_slug = "cpc-btc-pricerange-yr-12-31-2026-above-150k"
    long_ticker = "KXBTCY-27JAN0100-T149999.99-EXTRA-LONG-SUFFIX"
    out = pairs_run.format_rows(
        [
            {
                "id": 28,
                "status": "confirmed",
                "score": 1.0,
                "kalshi": {"ticker": long_ticker},
                "polymarket_us": {"ticker": long_slug},
                "features": {},
            }
        ]
    )
    assert long_slug in out
    assert long_ticker in out


def test_format_candidates_keeps_full_tickers_but_may_elide_prose() -> None:
    ref = MarketRef(
        venue="polymarket_us",
        market_id="polymarket_us:cpc-btc-pricerange-yr-12-31-2026-above-150k",
        ticker="cpc-btc-pricerange-yr-12-31-2026-above-150k",
        outcome="an outcome name far longer than the twenty-four column budget",
        rules="",
        close_time=None,
    )
    k = MarketRef(
        venue="kalshi",
        market_id="kalshi:K",
        ticker="KXBTCY-27JAN0100-T149999.99",
        outcome="150,000 or above",
        rules="",
        close_time=None,
    )
    out = pairs_run.format_candidates([PairCandidate(kalshi=k, polymarket=ref, score=1.0)])
    assert ref.ticker in out  # identifier: intact
    assert "…" in out  # prose: elided, and marked as such


def test_pair_detail_surfaces_searchable_title_and_url() -> None:
    """Neither venue's site matches a slug, so detail must carry the title
    (which does match) and the verified event URL."""
    out = pairs_run.format_pair_detail(
        {
            "id": 1,
            "status": "proposed",
            "score": 0.99,
            "kalshi": {"venue": "kalshi", "ticker": "KXMUSKNW-26DEC31-T600", "event_title": "x"},
            "polymarket_us": {
                "venue": "polymarket_us",
                "ticker": "pnwpc-elonmusk-2026-12-31-gt600b",
                "event_slug": "elonmusk-2026-12-31",
                "event_title": "Elon Musk Net Worth on December 31?",
                "outcome": "Above $600 Billion",
            },
            "features": {},
        }
    )
    assert "Elon Musk Net Worth on December 31?" in out
    assert "https://polymarket.us/event/elonmusk-2026-12-31" in out
    assert "pnwpc-elonmusk-2026-12-31-gt600b" in out
