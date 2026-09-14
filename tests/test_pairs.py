"""Pair matcher on verbatim event fixtures from both venues."""

from pathlib import Path

from arb.pairs.matcher import propose_pairs
from arb.pairs.text import name_similarity, name_tokens, title_tokens
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
