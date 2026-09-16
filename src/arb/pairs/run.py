"""``arb pairs`` jobs: fetch both universes (recorded), score, persist.

Each job (:func:`run_propose`, :func:`run_backfill`) runs two ways:

* **standalone** — the CLI owns the process, so the job builds its own
  engine, :class:`~arb.run.RunContext` and :class:`~arb.recorder.Recorder`
  (plus the supervised writer behind it) and tears all of that down again;
* **in-process** — a live ``arb ui`` server lends its runtime through
  :class:`JobDeps` and the job *borrows* it: it constructs nothing, disposes
  nothing, drains nothing.

Borrowing is not tidiness, it is the difference between recording and not.
A second :class:`~arb.run.RunContext` on the *same* ``run_id`` hands out a
second ``ingest_seq`` sequence starting at 0; ``raw_messages`` is UNIQUE on
``(run_id, ingest_seq)``, ``insert_raw_messages`` has no ON CONFLICT, and
``Recorder._write_with_retry`` retries a failing batch *forever*. One
duplicate therefore wedges the server's recorder permanently while the REC
pill still reads ON. The owned path never reuses a configured ``RUN_ID``
either — that same collision happens just as happily between two processes
(``arb pairs propose`` alongside a running ``arb ui``), so an owned runtime
always allocates a fresh ``run_id`` and says so in the result.

Threading contract — the caller ``await``s these on its event loop; it must
NOT wrap the whole coroutine in :func:`asyncio.to_thread`:

* **event loop**: the venue fetches, the recorder sink, ``RunContext``
  sequencing and every database write. ``AsyncEngine``, ``RunContext`` and
  the sink are loop-affine and must never cross a thread boundary.
* **worker thread**: the scoring stage only (:func:`_score_universes` /
  :func:`_event_slug_map`), offloaded here with :func:`asyncio.to_thread`.
  It is pure CPU over immutable data — no loop, no engine, no ``RunContext``
  — which is what makes the offload safe. It is also the dominant cost
  (~minutes), so this is what keeps the ingest loop from stalling past
  ``ws.py``'s 10 s ping timeout.
* **progress callback**: only ever called from the caller's event loop
  thread, never from the worker. It must not block and must not raise; a
  raising callback is logged once and then ignored for the rest of the job.

Cancellation: cancelling the awaiting task takes effect at any ``await`` —
in practice at a venue page boundary or a phase boundary. The scoring stage
is a plain function in a thread and cannot be interrupted, so a cancel
during scoring returns to the caller at once while that thread runs on to
completion (it touches nothing shared).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from arb.config import AppConfig
from arb.pairs import store as pairs_store
from arb.pairs.matcher import PairCandidate, propose_pairs
from arb.pairs.store import upsert_proposals
from arb.recorder import Recorder
from arb.run import RunContext, new_run_id
from arb.storage.db import insert_raw_messages, make_engine
from arb.supervise import supervise
from arb.types import RawMessage
from arb.venues.kalshi import discovery as kalshi_discovery
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket
from arb.venues.kalshi.rest import market_id as kalshi_market_id
from arb.venues.polymarket_us import discovery as pm_discovery
from arb.venues.polymarket_us.rest import event_url as pm_event_url
from arb.venues.polymarket_us.rest import market_id as pm_market_id

log = logging.getLogger(__name__)

DRAIN_TIMEOUT_S = 15.0
KALSHI_MAX_PAGES = 80
PM_MAX_PAGES = pm_discovery.MAX_PAGES

# Progress phases, in the order a job passes through them.
PHASE_KALSHI = "kalshi"
PHASE_POLYMARKET = "polymarket_us"
PHASE_SCORE = "score"
PHASE_PERSIST = "persist"
PHASE_DONE = "done"

type Sink = Callable[[RawMessage], object]


@dataclass(frozen=True, slots=True)
class JobProgress:
    """One progress tick. ``step``/``total`` are 0 for phases without pages.

    ``step`` may exceed ``total``: Polymarket US retries a page after a 429
    and each attempt is a real response, so the honest count is responses
    seen, not pages the budget allowed.
    """

    phase: str
    message: str
    step: int = 0
    total: int = 0


type ProgressFn = Callable[[JobProgress], None]


@dataclass(frozen=True, slots=True)
class JobDeps:
    """A live process lending its runtime to a job.

    Every field is loop-affine: pass the objects belonging to the event loop
    that will ``await`` the job, and never share them with a worker thread.
    ``sink`` is the recorder's non-blocking enqueue (the server's
    ``record_raw``); ``None`` records nothing while still borrowing the
    engine and the run.
    """

    engine: AsyncEngine
    run: RunContext
    sink: Sink | None = None


@dataclass(frozen=True, slots=True)
class ProposeResult:
    """What a propose run did, for a caller with no stdout."""

    run_id: str
    min_score: float
    kalshi_events: int
    polymarket_events: int
    kalshi_pages: int
    polymarket_pages: int
    rows_written: int
    elapsed_s: float
    candidates: list[PairCandidate] = field(default_factory=list)

    @property
    def proposed(self) -> int:
        return len(self.candidates)

    def summary(self) -> str:
        return (
            f"proposed {self.proposed} pairs at min_score {self.min_score:.2f} "
            f"({self.rows_written} rows written) from {self.kalshi_events} Kalshi and "
            f"{self.polymarket_events} Polymarket US events "
            f"[{self.kalshi_pages}+{self.polymarket_pages} pages, {self.elapsed_s:.1f}s, "
            f"run {self.run_id}]"
        )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """What a backfill run did: slugs resolved vs. pairs actually changed."""

    run_id: str
    kalshi_events: int
    polymarket_events: int
    kalshi_pages: int
    polymarket_pages: int
    resolved: int
    updated: int
    elapsed_s: float

    def summary(self) -> str:
        return (
            f"backfilled event slugs on {self.updated} pairs "
            f"from {self.resolved} resolved market ids "
            f"[{self.kalshi_pages}+{self.polymarket_pages} pages, {self.elapsed_s:.1f}s, "
            f"run {self.run_id}]"
        )


class JobAlreadyRunning(RuntimeError):
    """A pairs job was requested while one was already in flight."""


# Single-flight across both jobs: they fetch the same two universes and write
# the same table, so two at once doubles the venue load for a worse answer.
# A threading.Lock (acquired non-blocking, so it can never park the event
# loop) rather than an asyncio.Lock, because an asyncio.Lock belongs to one
# loop and a job may be started from a different one (CLI vs. server).
_JOB_SLOT = threading.Lock()


def job_running() -> bool:
    """True while a pairs job holds the single-flight slot."""
    return _JOB_SLOT.locked()


@contextlib.contextmanager
def _single_flight(name: str) -> Iterator[None]:
    if not _JOB_SLOT.acquire(blocking=False):
        raise JobAlreadyRunning(f"a pairs job is already running; {name} was not started")
    try:
        yield
    finally:
        _JOB_SLOT.release()


class _Progress:
    """Reports progress without ever being able to fail the job."""

    def __init__(self, fn: ProgressFn | None) -> None:
        self._fn = fn
        self.failures = 0

    def emit(self, phase: str, message: str, *, step: int = 0, total: int = 0) -> None:
        if self._fn is None:
            return
        try:
            self._fn(JobProgress(phase=phase, message=message, step=step, total=total))
        except Exception:
            self.failures += 1
            # Not fatal, and not repeated: a callback that raises once will
            # raise every page, so drop it and finish the job in silence.
            log.exception("pairs job progress callback failed; progress reporting disabled")
            self._fn = None


_VENUE_LABEL = {PHASE_KALSHI: "Kalshi", PHASE_POLYMARKET: "Polymarket US"}


def _paging_sink(
    base: Sink | None, progress: _Progress, *, kalshi_total: int, pm_total: int
) -> tuple[Sink, dict[str, int]]:
    """Wrap the recorder sink so every page fetched also reports progress.

    Discovery calls the sink once per ``rest:events`` response, which is the
    only page boundary either venue exposes; counting here means neither
    venue module needs a progress hook. The recorder still sees the message
    first, unchanged, exactly as the recording rule requires.
    """
    counts = {PHASE_KALSHI: 0, PHASE_POLYMARKET: 0}
    totals = {PHASE_KALSHI: kalshi_total, PHASE_POLYMARKET: pm_total}

    def sink(message: RawMessage) -> None:
        if base is not None:
            base(message)
        if message.stream != "rest:events" or message.venue not in counts:
            return
        phase = message.venue
        counts[phase] += 1
        progress.emit(
            phase,
            f"fetching {_VENUE_LABEL[phase]} page {counts[phase]} of {totals[phase]}",
            step=counts[phase],
            total=totals[phase],
        )

    return sink, counts


@dataclass(slots=True)
class _Runtime:
    engine: AsyncEngine
    run: RunContext
    sink: Sink | None


@contextlib.asynccontextmanager
async def _runtime(
    config: AppConfig, deps: JobDeps | None, *, record: bool
) -> AsyncIterator[_Runtime]:
    """Borrow the caller's runtime, or own one for the standalone path."""
    if deps is not None:
        # Borrowed: not ours to build, drain or dispose.
        yield _Runtime(engine=deps.engine, run=deps.run, sink=deps.sink)
        return

    run = RunContext(new_run_id())
    if config.run_id:
        log.warning(
            "RUN_ID=%s is pinned, but this job records under run_id=%s: "
            "two ingest_seq counters on one run_id collide on "
            "uq_raw_messages_run_seq and wedge the recorder",
            config.run_id,
            run.run_id,
        )
    engine = make_engine(config.database_url)
    recorder: Recorder | None = None
    writer: asyncio.Task[object] | None = None
    if record:
        recorder = Recorder(partial(insert_raw_messages, engine))
        writer = asyncio.create_task(supervise(recorder.run, name="recorder-writer"))
    try:
        yield _Runtime(
            engine=engine, run=run, sink=recorder.enqueue if recorder is not None else None
        )
    finally:
        if recorder is not None and writer is not None:
            # Cleanup runs on the cancellation path too, where every await
            # re-raises CancelledError immediately; suppress it here so the
            # rest of the teardown still happens (the original cancellation
            # keeps propagating regardless).
            with contextlib.suppress(Exception, asyncio.CancelledError):
                if not await recorder.drain(DRAIN_TIMEOUT_S):
                    log.warning("recorder drain timed out")
            writer.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await writer
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await engine.dispose()


async def _checkpoint() -> None:
    """Phase boundary: lets a pending cancellation land, and the loop breathe."""
    await asyncio.sleep(0)


def _score_universes(
    kalshi_universe: list[tuple[KalshiEvent, list[KalshiMarket]]],
    pm_markets: list[pm_discovery.DiscoveredPMMarket],
    *,
    min_score: float,
) -> tuple[int, int, list[PairCandidate]]:
    """Pure CPU, thread-safe: no event loop, no engine, no ``RunContext``.

    Reads only immutable/already-parsed venue data and returns fresh objects,
    which is what lets :func:`asyncio.to_thread` carry it off the loop.
    """
    kalshi_refs = kalshi_discovery.event_refs(kalshi_universe)
    pm_refs = pm_discovery.event_refs(pm_markets)
    return len(kalshi_refs), len(pm_refs), propose_pairs(kalshi_refs, pm_refs, min_score=min_score)


def _event_slug_map(
    kalshi_universe: list[tuple[KalshiEvent, list[KalshiMarket]]],
    pm_markets: list[pm_discovery.DiscoveredPMMarket],
) -> tuple[int, int, dict[str, str]]:
    """Pure CPU, thread-safe (same contract as :func:`_score_universes`)."""
    by_market_id: dict[str, str] = {}
    for event, markets in kalshi_universe:
        for m in markets:
            by_market_id[kalshi_market_id(m.ticker)] = event.event_ticker
    for dm in pm_markets:
        by_market_id[pm_market_id(dm.slug)] = dm.event.slug
    return len(kalshi_universe), len({m.event.slug for m in pm_markets}), by_market_id


async def run_propose(
    config: AppConfig,
    *,
    min_score: float,
    record: bool = True,
    kalshi_pages: int = KALSHI_MAX_PAGES,
    pm_pages: int = PM_MAX_PAGES,
    deps: JobDeps | None = None,
    progress: ProgressFn | None = None,
) -> ProposeResult:
    """Fetch both venues' open universes, propose pairs, upsert them.

    ``deps`` borrows a live process's engine/run/sink (see module docstring);
    omit it and the job owns and tears down its own. ``record`` applies to
    the owned path only — a borrowed job records exactly when ``deps.sink``
    is set. Either way the fetch consumes one ``ingest_seq`` per response,
    recorded or not; the sequence only has to increase, not be contiguous.
    """
    started = time.monotonic()
    prog = _Progress(progress)
    with _single_flight("propose"):
        async with _runtime(config, deps, record=record) as rt:
            sink, pages = _paging_sink(rt.sink, prog, kalshi_total=kalshi_pages, pm_total=pm_pages)
            prog.emit(PHASE_KALSHI, f"fetching the Kalshi universe (up to {kalshi_pages} pages)")
            kalshi_universe = await kalshi_discovery.fetch_universe(
                config, rt.run, sink=sink, max_pages=kalshi_pages
            )
            await _checkpoint()
            prog.emit(
                PHASE_POLYMARKET,
                f"fetching the Polymarket US universe (up to {pm_pages} pages)",
            )
            pm_markets = await pm_discovery.fetch_active_markets(
                config, rt.run, sink=sink, max_pages=pm_pages
            )
            await _checkpoint()
            prog.emit(PHASE_SCORE, "scoring the cross product (this is the slow part)")
            # Minutes of blocked CPU: off the loop or the Kalshi socket dies.
            kalshi_events, pm_events, candidates = await asyncio.to_thread(
                _score_universes, kalshi_universe, pm_markets, min_score=min_score
            )
            log.info(
                "universes: %d kalshi events, %d polymarket_us events, %d candidates",
                kalshi_events,
                pm_events,
                len(candidates),
            )
            prog.emit(PHASE_PERSIST, f"writing {len(candidates)} proposals")
            written = await upsert_proposals(rt.engine, candidates)
            result = ProposeResult(
                run_id=rt.run.run_id,
                min_score=min_score,
                kalshi_events=kalshi_events,
                polymarket_events=pm_events,
                kalshi_pages=pages[PHASE_KALSHI],
                polymarket_pages=pages[PHASE_POLYMARKET],
                rows_written=written,
                elapsed_s=time.monotonic() - started,
                candidates=candidates,
            )
            log.info("%s", result.summary())
            prog.emit(PHASE_DONE, result.summary())
            return result


async def run_backfill(
    config: AppConfig,
    *,
    record: bool = False,
    kalshi_pages: int = KALSHI_MAX_PAGES,
    pm_pages: int = PM_MAX_PAGES,
    deps: JobDeps | None = None,
    progress: ProgressFn | None = None,
) -> BackfillResult:
    """Record event slugs onto pairs proposed before the matcher captured them.

    Fetches both universes exactly as :func:`run_propose` does (same
    ``deps``/``record``/``progress`` contract), but writes only the missing
    ``event_slug`` on each leg — no re-scoring, no new rows, and no human
    decision is touched.
    """
    started = time.monotonic()
    prog = _Progress(progress)
    with _single_flight("backfill"):
        async with _runtime(config, deps, record=record) as rt:
            sink, pages = _paging_sink(rt.sink, prog, kalshi_total=kalshi_pages, pm_total=pm_pages)
            prog.emit(PHASE_KALSHI, f"fetching the Kalshi universe (up to {kalshi_pages} pages)")
            kalshi_universe = await kalshi_discovery.fetch_universe(
                config, rt.run, sink=sink, max_pages=kalshi_pages
            )
            await _checkpoint()
            prog.emit(
                PHASE_POLYMARKET,
                f"fetching the Polymarket US universe (up to {pm_pages} pages)",
            )
            pm_markets = await pm_discovery.fetch_active_markets(
                config, rt.run, sink=sink, max_pages=pm_pages
            )
            await _checkpoint()
            prog.emit(PHASE_SCORE, "resolving market ids to event slugs")
            kalshi_events, pm_events, by_market_id = await asyncio.to_thread(
                _event_slug_map, kalshi_universe, pm_markets
            )
            log.info("backfill: resolved %d market -> event slugs", len(by_market_id))
            prog.emit(PHASE_PERSIST, f"updating pairs from {len(by_market_id)} market ids")
            updated = await pairs_store.backfill_event_slugs(rt.engine, by_market_id)
            result = BackfillResult(
                run_id=rt.run.run_id,
                kalshi_events=kalshi_events,
                polymarket_events=pm_events,
                kalshi_pages=pages[PHASE_KALSHI],
                polymarket_pages=pages[PHASE_POLYMARKET],
                resolved=len(by_market_id),
                updated=updated,
                elapsed_s=time.monotonic() - started,
            )
            log.info("%s", result.summary())
            prog.emit(PHASE_DONE, result.summary())
            return result


async def propose(
    config: AppConfig,
    *,
    min_score: float,
    record: bool = True,
    kalshi_pages: int = KALSHI_MAX_PAGES,
    pm_pages: int = PM_MAX_PAGES,
    deps: JobDeps | None = None,
    progress: ProgressFn | None = None,
) -> list[PairCandidate]:
    """Candidates only — the standalone CLI's view of :func:`run_propose`."""
    result = await run_propose(
        config,
        min_score=min_score,
        record=record,
        kalshi_pages=kalshi_pages,
        pm_pages=pm_pages,
        deps=deps,
        progress=progress,
    )
    return result.candidates


async def backfill(
    config: AppConfig,
    *,
    record: bool = False,
    kalshi_pages: int = KALSHI_MAX_PAGES,
    pm_pages: int = PM_MAX_PAGES,
    deps: JobDeps | None = None,
    progress: ProgressFn | None = None,
) -> int:
    """Updated-row count only — the standalone CLI's view of :func:`run_backfill`."""
    result = await run_backfill(
        config,
        record=record,
        kalshi_pages=kalshi_pages,
        pm_pages=pm_pages,
        deps=deps,
        progress=progress,
    )
    return result.updated


def _widths(pairs: list[tuple[str, str]], headers: tuple[str, str]) -> tuple[int, int]:
    """Column widths that fit every identifier in full.

    Tickers and slugs are *identifiers*: a reader copies them into a venue
    search box or an API call, so a truncated one is worse than useless — it
    looks real and resolves to nothing. Descriptive text may be elided;
    identifiers never are.
    """
    k = max([len(headers[0]), *(len(a) for a, _ in pairs)] or [len(headers[0])])
    p = max([len(headers[1]), *(len(b) for _, b in pairs)] or [len(headers[1])])
    return k, p


def _elide(text: str, width: int) -> str:
    """Shorten descriptive (non-identifier) text, marking that it was cut."""
    return text if len(text) <= width else text[: width - 1] + "…"


def format_candidates(candidates: list[PairCandidate], *, limit: int = 40) -> str:
    shown = candidates[:limit]
    kw, pw = _widths(
        [(c.kalshi.ticker, c.polymarket.ticker) for c in shown], ("KALSHI", "POLYMARKET US")
    )
    lines = [f"{'SCORE':>5}  {'KALSHI':<{kw}} {'POLYMARKET US':<{pw}} OUTCOME"]
    for c in shown:
        lines.append(
            f"{c.score:5.2f}  {c.kalshi.ticker:<{kw}} {c.polymarket.ticker:<{pw}} "
            f"{_elide(c.kalshi.outcome, 24)} ↔ {_elide(c.polymarket.outcome, 24)}"
        )
    if len(candidates) > limit:
        lines.append(f"… {len(candidates) - limit} more")
    return "\n".join(lines)


def format_rows(rows: list[dict[str, Any]]) -> str:
    legs = [(r["kalshi"].get("ticker", ""), r["polymarket_us"].get("ticker", "")) for r in rows]
    kw, pw = _widths(legs, ("KALSHI", "POLYMARKET US"))
    lines = [f"{'ID':>5} {'STATUS':<9} {'SCORE':>5}  {'KALSHI':<{kw}} {'POLYMARKET US':<{pw}}"]
    for r, (k_ticker, p_ticker) in zip(rows, legs, strict=True):
        lines.append(
            f"{r['id']:5} {r['status']:<9} {r['score']:5.2f}  {k_ticker:<{kw}} {p_ticker:<{pw}}"
        )
    return "\n".join(lines)


def format_pair_detail(row: dict[str, Any]) -> str:
    """Everything about one pair, with identifiers printed in full.

    Exists because a table row cannot carry what a human needs to actually
    look a market up: neither venue's website matches a market slug/ticker in
    its search box, so this prints the *event title* (which does match) and,
    for Polymarket US, the verified event URL.
    """
    k, p = row["kalshi"], row["polymarket_us"]
    out = [
        f"PAIR {row['id']}  [{row['status']}]  score {row['score']:.4f}",
        "",
    ]
    for label, leg in (("KALSHI", k), ("POLYMARKET US", p)):
        out.append(f"{label}")
        out.append(f"  market id   {leg.get('market_id', '')}")
        out.append(f"  ticker      {leg.get('ticker', '')}")
        if leg.get("event_slug"):
            out.append(f"  event       {leg['event_slug']}")
        out.append(f"  event title {leg.get('event_title', '')}")
        out.append(f"  outcome     {leg.get('outcome', '')}")
        if leg.get("close_time"):
            out.append(f"  closes      {leg['close_time']}")
        url = (
            pm_event_url(leg.get("event_slug", "")) if leg.get("venue") == "polymarket_us" else None
        )
        if url:
            out.append(f"  url         {url}")
        elif leg.get("venue") == "polymarket_us":
            # Pre-existing rows were stored before event_slug was captured.
            out.append("  url         (re-run `arb pairs propose` to record the event slug)")
        rules = (leg.get("rules") or "").strip()
        if rules:
            out.append(f"  rules       {_elide(rules, 300)}")
        out.append("")
    feats = row.get("features") or {}
    if feats:
        out.append("FEATURES  " + "  ".join(f"{name}={value}" for name, value in feats.items()))
    out.append(
        "\nNote: neither venue's site searches by slug/ticker — search the event title above,"
        "\nor open the url."
    )
    return "\n".join(out)
