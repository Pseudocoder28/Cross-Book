"""The control plane: the executor's one choke point, the job runner, and the
landmines the runtime controls exist to avoid.

Nothing here touches a venue or Postgres — the audit table round-trips on
SQLite (the same metadata the migrations build), the sources are fakes and the
jobs are stubbed bodies. What is being tested is the *ordering* of the checks
in :meth:`ControlPlane.execute`, because that ordering is the only thing
standing between a control and an unaudited, unconfirmed runtime change.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arb.arbmon import ArbMonitor
from arb.book import BookSnapshot, Level
from arb.books import BookManager
from arb.config import AppConfig
from arb.edge import Leg
from arb.paper import PaperLimits, PaperTrade, PaperTrader
from arb.run import RunContext
from arb.storage.models import Base, list_control_actions
from arb.ui.control import (
    CONFIRM_TTL_S,
    JOB_OUTPUT_MAX_LINES,
    AuditFailed,
    ConfirmInvalid,
    ConfirmRegistry,
    ConfirmRequired,
    ControlPlane,
    InvalidParams,
    JobBusy,
    JobRunner,
    NotAvailable,
    ReadOnlyRefused,
    UnknownAction,
    params_fingerprint,
)

CONFIG = AppConfig(database_url="sqlite+aiosqlite:///:memory:", book_staleness_limit_ms=5_000)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeHost:
    """The slice of ``ServerState`` a control touches."""

    def __init__(self, *, books: BookManager | None = None) -> None:
        self.run_id = "testrun"
        self.recording = True
        self.books = books if books is not None else BookManager(staleness_limit_ns=10**12)
        self.arbmon: ArbMonitor | None = None
        self.trader: PaperTrader | None = None
        self.frames: list[dict[str, Any]] = []
        self.markets: list[dict[str, Any]] = []
        self.dirty: set[str] = set()

    def broadcast(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)

    def mark_dirty(self, market_ids: set[str]) -> None:
        self.dirty |= market_ids

    def hello_markets(self) -> list[dict[str, Any]]:
        return self.markets

    def set_markets(self, markets: list[dict[str, Any]]) -> None:
        self.markets = markets

    def control_frames(self) -> list[dict[str, Any]]:
        return [f for f in self.frames if f["t"] == "control"]


class FakeKalshi:
    """Stands in for ``KalshiWSSource``: same set_tickers/force_resync contract."""

    def __init__(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self.resyncs = 0

    @property
    def tickers(self) -> list[str]:
        return list(self._tickers)

    def set_tickers(self, market_tickers: Any) -> bool:
        new = list(market_tickers)
        if not new:
            raise ValueError("market_tickers must not be empty")
        if new == self._tickers:
            return False
        self._tickers = new
        return True

    async def force_resync(self) -> None:
        self.resyncs += 1


async def sqlite_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


class BrokenEngine:
    """Every audit write fails. Stands in for "Postgres just went away"."""

    def begin(self) -> Any:
        raise RuntimeError("database is gone")

    def connect(self) -> Any:
        raise RuntimeError("database is gone")


def plane(
    host: FakeHost,
    *,
    engine: Any = None,
    read_only: bool = False,
    pairs_top: int = 0,
) -> ControlPlane:
    return ControlPlane(
        config=CONFIG,
        host=host,
        run=RunContext("testrun"),
        engine=engine,
        read_only=read_only,
        pairs_top=pairs_top,
    )


# ---------------------------------------------------------------------------
# the executor: unknown / invalid / read-only
# ---------------------------------------------------------------------------


async def test_unknown_action_is_a_404_not_a_crash() -> None:
    control = plane(FakeHost())
    with pytest.raises(UnknownAction) as excinfo:
        await control.execute("recording.explode")
    assert excinfo.value.status_code == 404


async def test_parameters_are_validated_before_anything_happens() -> None:
    host = FakeHost()
    control = plane(host)
    with pytest.raises(InvalidParams):
        await control.execute("universe.kalshi", {"tickers": []})
    with pytest.raises(InvalidParams):
        await control.execute("pairs.top", {"n": -1})
    assert host.frames == []  # nothing was broadcast, so nothing changed


async def test_read_only_refuses_every_mutating_control() -> None:
    """A tunnelled port can be shown to someone without handing them the
    controls — but they can still run doctor."""
    engine = await sqlite_engine()
    host = FakeHost()
    control = plane(host, engine=engine, read_only=True)

    with pytest.raises(ReadOnlyRefused) as excinfo:
        await control.execute("recording.stop")

    assert excinfo.value.status_code == 403
    assert host.recording is True  # the effect never ran
    rows = await list_control_actions(engine)
    assert [(r["action"], r["result"]) for r in rows] == [("recording.stop", "refused")]
    assert control.actions["jobs.doctor"].mutates is False  # the G0 exception
    await engine.dispose()


# ---------------------------------------------------------------------------
# server-side confirmation
# ---------------------------------------------------------------------------


async def test_confirm_flow_arms_then_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await sqlite_engine()
    host = FakeHost()
    control = plane(host, engine=engine)
    started: list[float] = []

    async def fake_propose(*_args: Any, min_score: float, **_kwargs: Any) -> Any:
        started.append(min_score)
        raise AssertionError("not reached: the job body is replaced below")

    monkeypatch.setattr("arb.pairs.run.run_propose", fake_propose)

    with pytest.raises(ConfirmRequired) as armed:
        await control.execute("jobs.propose", {"min_score": 0.8})

    assert armed.value.status_code == 428
    assert "0.80" in armed.value.effect  # the sentence states what it will do
    assert armed.value.payload()["confirm_token"] == armed.value.token
    assert started == []  # nothing ran on the arm call
    rows = await list_control_actions(engine)
    assert [(r["action"], r["result"]) for r in rows] == [("jobs.propose", "armed")]
    assert rows[0]["effect"] == armed.value.effect  # what the operator was shown

    result = await control.execute("jobs.propose", {"min_score": 0.8}, confirm=armed.value.token)
    assert result.detail["job"]["name"] == "jobs.propose"
    await control.jobs.shutdown()
    await engine.dispose()


async def test_changed_params_invalidate_the_token() -> None:
    """The token is bound to a hash of (action, params): arming a cheap
    propose and confirming an expensive one must not work."""
    engine = await sqlite_engine()
    control = plane(FakeHost(), engine=engine)

    with pytest.raises(ConfirmRequired) as armed:
        await control.execute("jobs.propose", {"min_score": 0.95})
    with pytest.raises(ConfirmInvalid) as excinfo:
        await control.execute("jobs.propose", {"min_score": 0.10}, confirm=armed.value.token)

    assert excinfo.value.status_code == 409
    assert "parameters changed" in str(excinfo.value)
    # ...and the mismatch burned it, so the original parameters cannot sneak in
    # behind it either.
    with pytest.raises(ConfirmInvalid):
        await control.execute("jobs.propose", {"min_score": 0.95}, confirm=armed.value.token)
    await engine.dispose()


async def test_a_token_is_single_use_and_action_bound() -> None:
    engine = await sqlite_engine()
    control = plane(FakeHost(), engine=engine)
    with pytest.raises(ConfirmRequired) as armed:
        await control.execute("jobs.backfill", {})
    with pytest.raises(ConfirmInvalid):
        # Same token, different action.
        await control.execute("jobs.propose", {"min_score": 0.75}, confirm=armed.value.token)
    await engine.dispose()


def test_confirm_registry_expiry_and_bounds() -> None:
    registry = ConfirmRegistry(ttl_s=0.0, max_pending=2)
    token = registry.mint("jobs.propose", {"min_score": 0.5}, "effect")
    with pytest.raises(ConfirmInvalid):
        registry.consume(token.token, "jobs.propose", {"min_score": 0.5})

    registry = ConfirmRegistry(ttl_s=CONFIRM_TTL_S, max_pending=2)
    first = registry.mint("a", {}, "e")
    registry.mint("b", {}, "e")
    registry.mint("c", {}, "e")  # evicts the oldest rather than growing
    assert registry.pending == 2
    with pytest.raises(ConfirmInvalid):
        registry.consume(first.token, "a", {})


def test_fingerprint_ignores_key_order_only() -> None:
    assert params_fingerprint("a", {"x": 1, "y": 2}) == params_fingerprint("a", {"y": 2, "x": 1})
    assert params_fingerprint("a", {"x": 1}) != params_fingerprint("a", {"x": 2})
    assert params_fingerprint("a", {"x": 1}) != params_fingerprint("b", {"x": 1})


async def test_a_g3_action_fails_closed_when_the_audit_write_fails() -> None:
    """If we cannot record that it happened, it does not happen."""
    host = FakeHost()
    control = plane(host, engine=BrokenEngine())
    with pytest.raises(AuditFailed) as excinfo:
        await control.execute("jobs.propose", {"min_score": 0.75})
    assert excinfo.value.status_code == 503
    assert control.jobs.jobs == []  # no job was started


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


async def test_a_g2_action_writes_its_audit_row() -> None:
    """Recording off punches a hole replay reads straight across; this row is
    the only evidence the hole was deliberate."""
    engine = await sqlite_engine()
    host = FakeHost()
    control = plane(host, engine=engine)

    result = await control.execute("recording.stop", actor="ui:test")

    assert host.recording is False and result.changed is True
    rows = await list_control_actions(engine)
    assert len(rows) == 1
    assert rows[0]["action"] == "recording.stop"
    assert rows[0]["result"] == "ok"
    assert rows[0]["actor"] == "ui:test"
    assert rows[0]["run_id"] == "testrun"
    assert "replay reads straight across the gap" in rows[0]["effect"]
    assert rows[0]["params"] == {}

    again = await control.execute("recording.start")
    assert host.recording is True and again.changed is True
    assert [r["action"] for r in await list_control_actions(engine)] == [
        "recording.start",
        "recording.stop",
    ]
    await engine.dispose()


async def test_a_g2_action_survives_a_failed_audit_write() -> None:
    """G2 is advisory: the counter moves, the action still happens, nothing
    reaches the ingest path."""
    host = FakeHost()
    control = plane(host, engine=BrokenEngine())
    result = await control.execute("recording.stop")
    assert host.recording is False
    assert result.audit_id is None


async def test_every_mutating_action_broadcasts_the_new_state() -> None:
    """Two tabs cannot disagree about whether paper trading is on."""
    host = FakeHost()
    host.trader = PaperTrader(PaperLimits())
    control = plane(host)
    await control.execute("paper.suspend")
    frames = host.control_frames()
    assert frames and frames[-1]["control"]["paper"]["suspended"] is True
    await control.execute("paper.resume")
    assert host.control_frames()[-1]["control"]["paper"]["suspended"] is False


# ---------------------------------------------------------------------------
# paper
# ---------------------------------------------------------------------------


def _fill(trader: PaperTrader, cost_ticks: int) -> None:
    leg = Leg(
        venue="kalshi",
        market_id="kalshi:A",
        side="buy_yes",
        worst_price=5000,
        qty=10_000,
        cost_tq=cost_ticks * 10_000,
        fee_ticks=0,
    )
    trader.trades.append(
        PaperTrade(
            trade_id=trader._next_id,
            ts_ms=0,
            pair_id=1,
            label="A/B",
            direction="k_yes",
            qty=10_000,
            cost_ticks=cost_ticks,
            fee_ticks=0,
            net_ticks=100,
            legs=(leg, leg),
        )
    )
    trader._next_id += 1
    trader.notional_ticks += cost_ticks


async def test_suspend_resume_never_reopens_the_notional_budget() -> None:
    """The landmine: rebuilding the trader on toggle-on silently re-opens the
    whole max_notional budget and throws the ledger away."""
    host = FakeHost()
    trader = PaperTrader(PaperLimits(max_notional_ticks=1_000_000))
    _fill(trader, 400_000)
    host.trader = trader
    control = plane(host)

    await control.execute("paper.suspend")
    await control.execute("paper.resume")

    assert host.trader is trader  # the same instance, not a fresh one
    assert trader.notional_ticks == 400_000  # spend still committed
    assert len(trader.trades) == 1  # ledger intact
    assert trader.enabled is True


async def test_suspend_and_resume_are_idempotent() -> None:
    host = FakeHost()
    host.trader = PaperTrader(PaperLimits())
    control = plane(host)
    assert (await control.execute("paper.suspend")).changed is True
    assert (await control.execute("paper.suspend")).changed is False
    assert (await control.execute("paper.resume")).changed is True


async def test_paper_controls_refuse_cleanly_without_a_trader() -> None:
    control = plane(FakeHost())
    with pytest.raises(NotAvailable) as excinfo:
        await control.execute("paper.suspend")
    assert excinfo.value.status_code == 409


async def test_paper_limits_change_live_and_keep_committed_spend() -> None:
    host = FakeHost()
    trader = PaperTrader(PaperLimits(max_notional_ticks=10_000_000))
    _fill(trader, 4_000_000)
    host.trader = trader
    control = plane(host)

    result = await control.execute(
        "paper.limits", {"min_net_ticks": 75, "max_notional_ticks": 1_000_000}
    )

    assert trader.limits.min_net_ticks == 75
    assert trader.limits.max_notional_ticks == 1_000_000
    assert trader.limits.max_qty_per_pair == PaperLimits().max_qty_per_pair  # unstated: kept
    assert trader.notional_ticks == 4_000_000  # tightening unwinds nothing
    assert result.detail["previous"]["min_net_ticks"] == 50


async def test_paper_limits_accept_contracts_like_the_cli() -> None:
    host = FakeHost()
    host.trader = PaperTrader(PaperLimits())
    control = plane(host)
    await control.execute("paper.limits", {"max_cts_per_pair": 7})
    assert host.trader.limits.max_qty_per_pair == 70_000


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------


def _snapshot(market_id: str) -> BookSnapshot:
    return BookSnapshot(
        market_id=market_id,
        bids=(Level(price=4_000, qty=10),),
        asks=(Level(price=6_000, qty=10),),
        seq=1,
    )


async def test_kalshi_universe_change_resubscribes_and_evicts() -> None:
    books = BookManager(staleness_limit_ns=10**12)
    books.apply([_snapshot("kalshi:OLD"), _snapshot("kalshi:KEEP")], mono_ns=1)
    books.apply([_snapshot("polymarket_us:pm-a")], mono_ns=1)
    host = FakeHost(books=books)
    control = plane(host)
    source = FakeKalshi(["OLD", "KEEP"])
    control.attach_kalshi(source)
    control.set_base_universe(kalshi=["OLD", "KEEP"])

    result = await control.execute("universe.kalshi", {"tickers": ["KEEP", "NEW"]})

    assert result.detail["evicted"] == ["kalshi:OLD"]
    assert source.resyncs == 1  # a reconnect IS how a new ticker set applies
    assert set(books.books) == {"kalshi:KEEP", "polymarket_us:pm-a"}  # other venue untouched
    assert [m["market_id"] for m in host.markets] == ["kalshi:KEEP", "kalshi:NEW"]


async def test_an_unchanged_kalshi_set_does_not_reconnect() -> None:
    host = FakeHost()
    control = plane(host)
    source = FakeKalshi(["A", "B"])
    control.attach_kalshi(source)
    control.set_base_universe(kalshi=["A", "B"])
    result = await control.execute("universe.kalshi", {"tickers": ["A", "B"]})
    assert result.changed is False and source.resyncs == 0


async def test_polymarket_universe_retunes_staleness_and_evicts(
    polymarket_source: Any,
) -> None:
    """The landmine pair: the poll cycle changed, so every surviving book's
    staleness budget is wrong, and the dropped book would linger forever."""
    books = BookManager(staleness_limit_ns=5_000_000_000)
    books.apply([_snapshot("polymarket_us:keep"), _snapshot("polymarket_us:drop")], mono_ns=1)
    host = FakeHost(books=books)
    control = plane(host)
    control.attach_polymarket(polymarket_source)
    control.set_base_universe(polymarket=["keep", "drop"])
    control.retune_polymarket_staleness()
    before = books.staleness_limit_ns("polymarket_us:keep")

    result = await control.execute(
        "universe.polymarket", {"slugs": ["keep", "a", "b", "c", "d", "e"]}
    )

    after = books.staleness_limit_ns("polymarket_us:keep")
    assert after > before  # more targets = slower cycle = a bigger budget
    # ...and the LIVE book was retuned, not only books created from now on.
    keep = books.get("polymarket_us:keep")
    assert keep is not None and keep._staleness_limit_ns == after
    assert result.detail["evicted"] == ["polymarket_us:drop"]
    assert "polymarket_us:keep" in host.dirty  # re-published with its new verdict


async def test_an_empty_polymarket_target_set_is_legal(polymarket_source: Any) -> None:
    """It used to be a ZeroDivisionError inside a supervised task: a crash
    loop on a venue, forever."""
    host = FakeHost()
    control = plane(host)
    control.attach_polymarket(polymarket_source)
    control.set_base_universe(polymarket=["a", "b"])
    result = await control.execute("universe.polymarket", {"slugs": []})
    assert result.changed is True
    assert polymarket_source.targets == []
    assert polymarket_source.cycle_s == 0.0


@pytest.fixture
def polymarket_source() -> Any:
    from arb.venues.polymarket_us.source import PolymarketUSRestSource

    return PolymarketUSRestSource(config=CONFIG, run=RunContext("testrun"), slugs=["keep", "drop"])


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


async def test_jobs_are_single_flight_per_group() -> None:
    runner = JobRunner()
    gate = asyncio.Event()

    async def body(record: Any) -> dict[str, Any]:
        record.append("working")
        await gate.wait()
        return {"done": True}

    first = runner.start("jobs.propose", body, group="pairs", params={})
    with pytest.raises(JobBusy) as excinfo:
        runner.start("jobs.backfill", body, group="pairs", params={})
    assert excinfo.value.status_code == 409
    assert first.job_id in str(excinfo.value)

    # A different group runs happily alongside it.
    other = runner.start("jobs.doctor", body, params={})
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert first.status == "ok" and other.status == "ok"
    # ...and the slot is free again once it finished.
    runner.start("jobs.backfill", body, group="pairs", params={})
    await runner.shutdown()


async def test_a_finished_job_stays_readable() -> None:
    """Completion must survive the browser disconnecting and coming back."""
    runner = JobRunner()

    async def body(record: Any) -> dict[str, Any]:
        record.append("line one")
        return {"rows": 12}

    record = runner.start("jobs.propose", body, group="pairs", params={"min_score": 0.75})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    found = runner.get(record.job_id)
    assert found is not None
    payload = found.log_payload()
    assert payload["status"] == "ok"
    assert payload["result"] == {"rows": 12}
    assert payload["lines"] == ["line one"]
    assert payload["finished_ts_ns"] is not None


async def test_a_failing_job_is_recorded_not_raised() -> None:
    runner = JobRunner()

    async def body(_record: Any) -> dict[str, Any]:
        raise RuntimeError("venue said no")

    record = runner.start("jobs.propose", body, group="pairs", params={})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert record.status == "error"
    assert record.error == "venue said no"
    assert "venue said no" in record.lines[-1]


async def test_a_cancelled_job_is_recorded_as_cancelled() -> None:
    runner = JobRunner()
    started = asyncio.Event()

    async def body(_record: Any) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(3600)
        return {}

    record = runner.start("jobs.replay", body, group="replay", params={})
    await started.wait()
    assert runner.cancel(record.job_id) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert record.status == "cancelled"
    assert runner.cancel(record.job_id) is False  # already done
    assert runner.cancel("nope") is False


async def test_job_output_is_bounded_and_the_loss_is_counted() -> None:
    runner = JobRunner()
    overflow = 25

    async def body(record: Any) -> dict[str, Any]:
        for i in range(JOB_OUTPUT_MAX_LINES + overflow):
            record.append(f"line {i}")
        return {}

    record = runner.start("jobs.doctor", body, params={})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(record.lines) == JOB_OUTPUT_MAX_LINES
    assert record.lines_dropped == overflow
    assert record.lines_total == JOB_OUTPUT_MAX_LINES + overflow
    assert record.lines[0] == f"line {overflow}"  # oldest went, newest stayed


async def test_job_frames_reach_the_browser() -> None:
    host = FakeHost()
    runner = JobRunner(broadcast=host.broadcast)

    async def body(record: Any) -> dict[str, Any]:
        record.progress("kalshi", "fetching Kalshi page 1 of 80", 1, 80)
        return {}

    runner.start("jobs.propose", body, group="pairs", params={})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    frames = [f for f in host.frames if f["t"] == "job"]
    assert frames[0]["job"]["status"] == "running"
    assert any(f["job"]["phase"] == "kalshi" for f in frames)
    assert frames[-1]["job"]["status"] == "ok"


async def test_a_broadcast_that_raises_never_fails_the_job() -> None:
    def explode(_payload: dict[str, Any]) -> None:
        raise RuntimeError("no clients, no problem")

    runner = JobRunner(broadcast=explode)

    async def body(record: Any) -> dict[str, Any]:
        record.append("still fine")
        return {"ok": True}

    record = runner.start("jobs.doctor", body, params={})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert record.status == "ok"


async def test_job_history_is_bounded_but_keeps_the_running_one() -> None:
    runner = JobRunner(history=3)
    gate = asyncio.Event()

    async def long_body(_record: Any) -> dict[str, Any]:
        await gate.wait()
        return {}

    async def quick(_record: Any) -> dict[str, Any]:
        return {}

    running = runner.start("jobs.replay", long_body, group="replay", params={})
    for _ in range(6):
        runner.start("jobs.doctor", quick, params={})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert len(runner.jobs) <= 4
    assert runner.get(running.job_id) is not None  # never pruned while running
    gate.set()
    await runner.shutdown()


async def test_propose_borrows_the_servers_run_and_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorder-deadlock landmine: a second RunContext on one run_id hands
    out a colliding ingest_seq and wedges the recorder permanently."""
    engine = await sqlite_engine()
    host = FakeHost()
    run = RunContext("testrun")
    sink_calls: list[Any] = []

    def sink(message: Any) -> None:
        sink_calls.append(message)

    control = ControlPlane(config=CONFIG, host=host, run=run, engine=engine, sink=sink)
    seen: dict[str, Any] = {}

    async def fake_run_propose(_config: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("stop here: the deps are what this test is about")

    monkeypatch.setattr("arb.pairs.run.run_propose", fake_run_propose)

    with pytest.raises(ConfirmRequired) as armed:
        await control.execute("jobs.propose", {"min_score": 0.75})
    await control.execute("jobs.propose", {"min_score": 0.75}, confirm=armed.value.token)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    deps = seen["deps"]
    assert deps.run is run  # the server's own run, not a second one
    assert deps.engine is engine  # the server's engine, never disposed by the job
    assert deps.sink is sink  # the server's recorder enqueue, not a new one
    await engine.dispose()


async def test_cancelling_an_unknown_job_is_a_404() -> None:
    control = plane(FakeHost())
    with pytest.raises(UnknownAction):
        await control.execute("jobs.cancel", {"job_id": "nope"})


async def test_replay_runs_as_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process, replay's per-row loop holds the event loop past ws.py's
    10 s ping timeout and drops the Kalshi socket. It gets its own process."""
    engine = await sqlite_engine()
    control = plane(FakeHost(), engine=engine)
    result = await control.execute("jobs.replay", {"run_id": "20260101T000000Z-abcd"})
    argv = result.detail["argv"]
    assert argv[1:4] == ["-m", "arb.cli", "replay"]
    assert "20260101T000000Z-abcd" in argv
    assert "--persist" not in argv  # not asked for, so not passed
    control.jobs.cancel(result.detail["job"]["job_id"])
    await control.shutdown()
    await engine.dispose()


async def test_replay_only_asks_for_confirmation_when_it_writes() -> None:
    engine = await sqlite_engine()
    control = plane(FakeHost(), engine=engine)
    spec = control.actions["jobs.replay"]
    assert spec.needs_confirm(spec.validate({"persist": True})) is True
    assert spec.needs_confirm(spec.validate({"persist": False})) is False
    with pytest.raises(ConfirmRequired):
        await control.execute("jobs.replay", {"persist": True})
    await control.shutdown()
    await engine.dispose()


async def test_a_replay_run_id_is_not_free_form() -> None:
    control = plane(FakeHost())
    with pytest.raises(InvalidParams):
        await control.execute("jobs.replay", {"run_id": "; rm -rf /"})


# ---------------------------------------------------------------------------
# state payload
# ---------------------------------------------------------------------------


async def test_payload_describes_the_whole_control_surface() -> None:
    host = FakeHost()
    host.trader = PaperTrader(PaperLimits())
    control = plane(host, pairs_top=5)
    control.attach_kalshi(FakeKalshi(["A"]))
    control.set_base_universe(kalshi=["A"])
    payload = control.payload()
    assert payload["run_id"] == "testrun"
    assert payload["read_only"] is False
    assert payload["recording"] is True
    assert payload["pairs_top"] == 5
    assert payload["universe"]["kalshi"]["tickers"] == ["A"]
    assert payload["paper"]["attached"] is True
    names = {a["action"] for a in payload["actions"]}
    assert {"recording.stop", "paper.suspend", "jobs.propose", "jobs.doctor"} <= names
    # Grades ride along so the UI can price a click without a second table.
    grades = {a["action"]: a["grade"] for a in payload["actions"]}
    assert grades["jobs.propose"] == "G3" and grades["recording.stop"] == "G2"
