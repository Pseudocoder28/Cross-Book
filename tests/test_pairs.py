"""Pair matcher on verbatim event fixtures from both venues, and the
``propose``/``backfill`` jobs that wrap it."""

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arb.config import AppConfig
from arb.pairs import run as pairs_run
from arb.pairs import store
from arb.pairs.matcher import EventRef, MarketRef, PairCandidate, propose_pairs
from arb.pairs.text import name_similarity, name_tokens, title_tokens
from arb.recorder import Recorder
from arb.run import RunContext
from arb.storage.models import Base, PairRow
from arb.types import RawMessage
from arb.venues.kalshi import discovery as kd
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket
from arb.venues.polymarket_us import discovery as pd
from arb.venues.polymarket_us.rest import parse_events_response

FIX = Path(__file__).parent / "fixtures"


def raw(venue: str, payload: bytes) -> RawMessage:
    return RawMessage(venue, "rest:events", payload, 1, 1, "t", 0)


def kalshi_universe() -> list[tuple[KalshiEvent, list[KalshiMarket]]]:
    doc = json.loads((FIX / "kalshi" / "rest_events_pairs_subset.json").read_bytes())
    return [
        (KalshiEvent.model_validate(e), [KalshiMarket.model_validate(m) for m in e["markets"]])
        for e in doc["events"]
    ]


def pm_discovered() -> list[pd.DiscoveredPMMarket]:
    payload = (FIX / "polymarket_us" / "rest_events_pairs_subset.json").read_bytes()
    return [
        pd.DiscoveredPMMarket(slug=m.slug, title=m.question, market=m, event=e)
        for e, ms in parse_events_response(raw("polymarket_us", payload))
        for m in ms
    ]


def kalshi_refs() -> list[EventRef]:
    return kd.event_refs(kalshi_universe())


def pm_refs() -> list[EventRef]:
    return pd.event_refs(pm_discovered())


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


async def test_backfill_event_slugs_fills_only_what_is_missing() -> None:
    """Pairs proposed before event slugs existed get linkable, without
    re-scoring and without disturbing a human decision."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            insert(PairRow),
            [
                {  # legacy row: no event_slug on either leg
                    "kalshi_market_id": "kalshi:K1",
                    "polymarket_market_id": "polymarket_us:p1",
                    "status": "confirmed",
                    "score": 0.9,
                    "detail": {
                        "kalshi": {"market_id": "kalshi:K1", "ticker": "K1"},
                        "polymarket_us": {"market_id": "polymarket_us:p1", "ticker": "p1"},
                    },
                },
                {  # already has one; must not be overwritten
                    "kalshi_market_id": "kalshi:K2",
                    "polymarket_market_id": "polymarket_us:p2",
                    "status": "proposed",
                    "score": 0.8,
                    "detail": {
                        "kalshi": {"market_id": "kalshi:K2", "ticker": "K2"},
                        "polymarket_us": {
                            "market_id": "polymarket_us:p2",
                            "ticker": "p2",
                            "event_slug": "keep-me",
                        },
                    },
                },
            ],
        )
    try:
        updated = await store.backfill_event_slugs(
            engine,
            {
                "kalshi:K1": "KEVENT1",
                "polymarket_us:p1": "pevent1",
                "polymarket_us:p2": "should-not-apply",
            },
        )
        assert updated == 1  # only the legacy row changed
        rows = {r["kalshi"]["ticker"]: r for r in await store.list_pairs(engine)}
        assert rows["K1"]["kalshi"]["event_slug"] == "KEVENT1"
        assert rows["K1"]["polymarket_us"]["event_slug"] == "pevent1"
        assert rows["K1"]["status"] == "confirmed"  # decision untouched
        assert rows["K1"]["score"] == 0.9  # score untouched
        assert rows["K2"]["polymarket_us"]["event_slug"] == "keep-me"  # not overwritten
        # A market id absent from the map is simply left alone.
        assert await store.backfill_event_slugs(engine, {"kalshi:nope": "x"}) == 0
    finally:
        await engine.dispose()


# --- jobs: `propose`/`backfill` as things a live server can run -------------
#
# The job must be able to borrow a running process's engine, RunContext and
# recorder sink. Building its own inside a live server is the recorder
# deadlock: a second RunContext on the same run_id restarts ingest_seq at 0,
# raw_messages is UNIQUE on (run_id, ingest_seq), and the recorder retries the
# rejected batch forever — recording stops for good while the REC pill stays
# lit. These tests pin the borrowing down.


class FakeEngine:
    """Stands in for the server's AsyncEngine; records a dispose() it must
    never see when the engine was lent to the job."""

    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


def _engine(fake: FakeEngine) -> AsyncEngine:
    return cast(AsyncEngine, fake)


def _page(venue: str, run: RunContext) -> RawMessage:
    return RawMessage(
        venue=venue,
        stream="rest:events",
        payload=b"{}",
        recv_ts_ns=1,
        recv_mono_ns=1,
        run_id=run.run_id,
        ingest_seq=run.next_ingest_seq(),
    )


class Gate:
    """Holds a fake fetch open so a test can act on a job that is in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()


def _fake_fetch(
    venue: str, pages: int, result: object, *, gate: Gate | None = None
) -> Callable[..., Awaitable[object]]:
    """A discovery call that emits ``pages`` recorded pages, then returns."""

    async def fetch(config: object, run: RunContext, **kw: object) -> object:
        if gate is not None:
            gate.entered.set()
            await gate.release.wait()
        sink = kw.get("sink")
        for _ in range(pages):
            if callable(sink):
                sink(_page(venue, run))
        return result

    return fetch


def _patch_discovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kalshi_pages: int = 3,
    pm_pages: int = 2,
    gate: Gate | None = None,
) -> None:
    monkeypatch.setattr(
        kd, "fetch_universe", _fake_fetch("kalshi", kalshi_pages, kalshi_universe(), gate=gate)
    )
    monkeypatch.setattr(
        pd, "fetch_active_markets", _fake_fetch("polymarket_us", pm_pages, pm_discovered())
    )


def _patch_owned_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make building an engine/recorder of its own a loud failure."""

    def no_engine(url: str) -> AsyncEngine:
        raise AssertionError("a borrowed job must not build its own engine")

    def no_recorder(*a: object, **k: object) -> Recorder:
        raise AssertionError("a borrowed job must not build its own recorder")

    monkeypatch.setattr(pairs_run, "make_engine", no_engine)
    monkeypatch.setattr(pairs_run, "Recorder", no_recorder)


async def test_propose_borrows_the_callers_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected engine, RunContext and sink are used as-is: one ingest_seq
    counter, one run_id, and the lent engine survives the job."""
    _patch_discovery(monkeypatch, kalshi_pages=3, pm_pages=2)
    _patch_owned_construction(monkeypatch)
    fake = FakeEngine()
    seen_engine: list[object] = []

    async def fake_upsert(engine: object, candidates: list[PairCandidate]) -> int:
        seen_engine.append(engine)
        return len(candidates)

    monkeypatch.setattr(pairs_run, "upsert_proposals", fake_upsert)

    run = RunContext("pinned-run")
    run.next_ingest_seq()  # the server is already recording
    recorded: list[RawMessage] = []
    result = await pairs_run.run_propose(
        AppConfig(run_id="pinned-run"),
        min_score=0.5,
        deps=pairs_run.JobDeps(engine=_engine(fake), run=run, sink=recorded.append),
        kalshi_pages=5,
        pm_pages=4,
    )

    assert result.run_id == "pinned-run"
    # One counter: the job continued the server's sequence instead of
    # restarting at 0 and colliding on uq_raw_messages_run_seq.
    assert [m.ingest_seq for m in recorded] == [1, 2, 3, 4, 5]
    assert {m.run_id for m in recorded} == {"pinned-run"}
    assert run.next_ingest_seq() == 6
    # Every page still reached the recorder sink, both venues.
    assert [m.venue for m in recorded] == ["kalshi"] * 3 + ["polymarket_us"] * 2
    assert seen_engine == [_engine(fake)]
    assert fake.disposed == 0  # lent, not owned
    assert result.kalshi_pages == 3 and result.polymarket_pages == 2
    assert result.rows_written == len(result.candidates)
    assert result.elapsed_s >= 0.0
    # Plumbing only: the same candidates the matcher produces directly.
    expected = propose_pairs(kalshi_refs(), pm_refs(), min_score=0.5)
    assert [(c.kalshi.ticker, c.polymarket.ticker) for c in result.candidates] == [
        (c.kalshi.ticker, c.polymarket.ticker) for c in expected
    ]
    assert result.proposed == len(expected)
    assert result.kalshi_events == len(kalshi_refs())
    assert "proposed" in result.summary() and "pinned-run" in result.summary()


async def test_propose_reports_page_progress_and_survives_a_bad_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three minutes of silence is not a job a human can supervise; a
    progress callback that throws is still never allowed to fail one."""
    _patch_discovery(monkeypatch, kalshi_pages=2, pm_pages=1)
    _patch_owned_construction(monkeypatch)
    monkeypatch.setattr(pairs_run, "upsert_proposals", _count_upsert)
    ticks: list[pairs_run.JobProgress] = []

    result = await pairs_run.run_propose(
        AppConfig(),
        min_score=0.5,
        deps=pairs_run.JobDeps(engine=_engine(FakeEngine()), run=RunContext("r")),
        kalshi_pages=80,
        pm_pages=12,
        progress=ticks.append,
    )

    assert "fetching Kalshi page 1 of 80" in [t.message for t in ticks]
    assert "fetching Kalshi page 2 of 80" in [t.message for t in ticks]
    assert "fetching Polymarket US page 1 of 12" in [t.message for t in ticks]
    assert [t.phase for t in ticks][-4:] == ["polymarket_us", "score", "persist", "done"]
    assert ticks[-1].message == result.summary()
    paged = [t for t in ticks if t.total]
    assert all(1 <= t.step <= t.total for t in paged)

    def explode(tick: pairs_run.JobProgress) -> None:
        raise RuntimeError("the browser went away")

    again = await pairs_run.run_propose(
        AppConfig(),
        min_score=0.5,
        deps=pairs_run.JobDeps(engine=_engine(FakeEngine()), run=RunContext("r")),
        progress=explode,
    )
    assert again.proposed == result.proposed  # counted, logged, not fatal


async def _count_upsert(engine: object, candidates: list[PairCandidate]) -> int:
    return len(candidates)


async def test_scoring_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scorer blocks for minutes. On the ingest loop that is longer than
    ws.py's 10 s ping timeout, so the Kalshi socket would drop."""
    _patch_discovery(monkeypatch)
    _patch_owned_construction(monkeypatch)
    monkeypatch.setattr(pairs_run, "upsert_proposals", _count_upsert)
    real = pairs_run.propose_pairs
    threads: list[int] = []

    def spy(*args: object, **kw: object) -> list[PairCandidate]:
        threads.append(threading.get_ident())
        return real(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(pairs_run, "propose_pairs", spy)
    await pairs_run.run_propose(
        AppConfig(),
        min_score=0.5,
        deps=pairs_run.JobDeps(engine=_engine(FakeEngine()), run=RunContext("r")),
    )
    assert threads and threading.get_ident() not in threads


async def test_owned_runtime_builds_and_disposes_its_own_and_never_reuses_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone path. The pinned RUN_ID is deliberately *not* reused: two
    processes sharing one run_id collide on ingest_seq exactly like two
    RunContexts in one process do."""
    _patch_discovery(monkeypatch)
    fake = FakeEngine()
    monkeypatch.setattr(pairs_run, "make_engine", lambda url: _engine(fake))
    monkeypatch.setattr(pairs_run, "upsert_proposals", _count_upsert)

    candidates = await pairs_run.propose(
        AppConfig(run_id="pinned-run"), min_score=0.5, record=False
    )

    assert candidates  # the CLI's return shape is unchanged
    assert fake.disposed == 1  # owned, so torn down


async def test_only_one_pairs_job_runs_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both jobs fetch the same two universes and write the same table."""
    gate = Gate()
    _patch_discovery(monkeypatch, gate=gate)
    _patch_owned_construction(monkeypatch)
    monkeypatch.setattr(pairs_run, "upsert_proposals", _count_upsert)
    deps = pairs_run.JobDeps(engine=_engine(FakeEngine()), run=RunContext("r"))

    first = asyncio.create_task(pairs_run.run_propose(AppConfig(), min_score=0.5, deps=deps))
    await gate.entered.wait()
    assert pairs_run.job_running()
    with pytest.raises(pairs_run.JobAlreadyRunning):
        await pairs_run.run_propose(AppConfig(), min_score=0.5, deps=deps)
    with pytest.raises(pairs_run.JobAlreadyRunning):
        await pairs_run.run_backfill(AppConfig(), deps=deps)
    gate.release.set()
    assert (await first).proposed > 0
    assert not pairs_run.job_running()  # slot released for the next trigger


async def test_cancelling_a_job_frees_the_slot_and_spares_the_lent_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UI-triggered job must be stoppable without taking the server's
    engine — or the next job — down with it."""
    gate = Gate()
    _patch_discovery(monkeypatch, gate=gate)
    _patch_owned_construction(monkeypatch)
    monkeypatch.setattr(pairs_run, "upsert_proposals", _count_upsert)
    fake = FakeEngine()
    deps = pairs_run.JobDeps(engine=_engine(fake), run=RunContext("r"))

    task = asyncio.create_task(pairs_run.run_propose(AppConfig(), min_score=0.5, deps=deps))
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.disposed == 0
    assert not pairs_run.job_running()
    gate.release.set()
    assert (await pairs_run.run_propose(AppConfig(), min_score=0.5, deps=deps)).proposed > 0


async def test_backfill_borrows_a_live_engine_and_leaves_it_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end against a real database handle the caller keeps."""
    _patch_discovery(monkeypatch)
    _patch_owned_construction(monkeypatch)
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            insert(PairRow),
            [
                {
                    "kalshi_market_id": "kalshi:CONTROLH-2026-D",
                    "polymarket_market_id": "polymarket_us:p1",
                    "status": "proposed",
                    "score": 0.9,
                    "detail": {
                        "kalshi": {"market_id": "kalshi:CONTROLH-2026-D", "ticker": "K1"},
                        "polymarket_us": {"market_id": "polymarket_us:p1", "ticker": "p1"},
                    },
                }
            ],
        )
    try:
        ticks: list[pairs_run.JobProgress] = []
        result = await pairs_run.run_backfill(
            AppConfig(),
            deps=pairs_run.JobDeps(engine=engine, run=RunContext("r")),
            progress=ticks.append,
        )
        assert result.updated == 1
        assert result.resolved > 0
        assert result.kalshi_pages == 3 and result.polymarket_pages == 2
        assert [t.phase for t in ticks][-1] == "done"
        rows = await store.list_pairs(engine)  # engine still alive: not disposed
        assert rows[0]["kalshi"]["event_slug"] == "CONTROLH-2026"
    finally:
        await engine.dispose()
