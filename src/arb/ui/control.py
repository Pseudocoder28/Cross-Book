"""UI control plane: one choke point for every runtime change.

The server's mutable runtime — the Kalshi source, the Polymarket US poller,
the book manager, the paper trader, the recorder switch, the engine and the
:class:`~arb.run.RunContext` — used to be locals of ``run_ui``. A request
handler could not reach any of it. :class:`ControlPlane` owns handles to all
of them and hangs off the server state, so a route can.

Everything an operator can change goes through ONE method,
:meth:`ControlPlane.execute`. That is deliberate: the audit write, the
read-only refusal, the confirm-token check and the metric are things that get
re-forgotten once per route when each route implements them itself. Here they
are structural — an action is a row in :data:`ControlPlane._actions` and
cannot opt out of any of them.

Three rules this module exists to keep
--------------------------------------

**Nothing blocks the ingest loop.** ``ws.py`` runs with ``ping_timeout_s =
10.0``, so ten seconds of blocked event loop drops the Kalshi socket, and any
block at all corrupts the one-way latency measurement. So: ``propose`` and
``backfill`` are awaited (``arb.pairs.run`` already carries its blocking
scorer off to a worker thread and its contract says do NOT wrap the coroutine
in ``to_thread``); ``replay`` — whose per-row loop has no ``await`` at all —
runs as a SUBPROCESS with its output streamed back; ``doctor`` is cheap and
in-process.

**Nothing here is fatal.** Every failure path returns a typed
:class:`ControlError` (which carries the HTTP status the next phase's routes
should answer with), counts a metric and logs. A job that dies leaves a
finished record behind, not a dead server.

**A control that is not audited did not happen.** Confirmation is server-side:
a first call with no token is *armed* — that mints a single-use, short-TTL
token bound to a hash of ``(action, params)`` and writes the audit row that
records what the operator was told. A client-side-only confirm is invisible to
the audit log and to ``curl``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from arb.arbmon import ArbMonitor
from arb.books import BookManager
from arb.config import AppConfig
from arb.doctor import exit_code as doctor_exit_code
from arb.doctor import format_results as doctor_format_results
from arb.doctor import run_doctor
from arb.metrics import (
    CONTROL_ACTIONS,
    CONTROL_AUDIT_FAILURES,
    CONTROL_JOB_OUTPUT_DROPPED,
    CONTROL_JOBS,
)
from arb.pairs import run as pairs_run
from arb.pairs.tracked import load_tracked_pairs
from arb.paper import PaperLimits, PaperTrader
from arb.run import RunContext
from arb.storage.models import insert_control_action, list_control_actions
from arb.types import QTY_PER_CONTRACT, RawMessage
from arb.ui.security import BindInfo
from arb.venues.kalshi.rest import market_id as kalshi_market_id
from arb.venues.polymarket_us.rest import market_id as pm_market_id
from arb.venues.polymarket_us.source import PolymarketUSRestSource

log = logging.getLogger(__name__)

# How long an armed action stays armed. Long enough to read the sentence and
# click, short enough that a token left in a tab is not a loaded gun.
CONFIRM_TTL_S = 90.0
# Armed-but-unconfirmed tokens held at once. Bounded so a script that arms in
# a loop cannot grow the process; the oldest are evicted first.
CONFIRM_MAX_PENDING = 32
# Output lines kept per job. A job's log is a debugging aid, not a record —
# the record is the audit row — so it is bounded and the overflow is counted.
JOB_OUTPUT_MAX_LINES = 500
# Finished jobs kept for after-the-fact inspection. Completion must survive
# the browser disconnecting and coming back, so a finished job stays here.
JOB_HISTORY_MAX = 32
# Job frames are coalesced to this interval (finish always broadcasts).
JOB_BROADCAST_INTERVAL_S = 0.25
# Grace between SIGTERM and SIGKILL for a cancelled subprocess job.
JOB_TERMINATE_GRACE_S = 5.0
# Polled books are only as fresh as their poll cycle; allow three cycles (one
# 429 cooldown) before calling them stale. Same rule as ``run_ui``'s startup.
PM_STALENESS_CYCLES = 3

JOB_RUNNING = "running"
JOB_OK = "ok"
JOB_ERROR = "error"
JOB_CANCELLED = "cancelled"

type Sink = Callable[[RawMessage], object]
type Broadcast = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ControlError(Exception):
    """A control could not be executed as asked.

    ``status_code`` is the HTTP status the route layer should answer with, so
    the mapping lives with the failure rather than in a translation table that
    drifts.
    """

    status_code = 400


class UnknownAction(ControlError):
    status_code = 404


class InvalidParams(ControlError):
    status_code = 400


class ReadOnlyRefused(ControlError):
    """The server was started read-only; every mutating control is refused."""

    status_code = 403


class NotAvailable(ControlError):
    """The runtime piece this control drives is not attached in this process."""

    status_code = 409


class JobBusy(ControlError):
    """A job of this kind is already running."""

    status_code = 409


class AuditFailed(ControlError):
    """The audit row could not be written, so the action was not performed."""

    status_code = 503


class ConfirmInvalid(ControlError):
    """The confirm token was unknown, expired, or minted for other params."""

    status_code = 409


class ConfirmRequired(ControlError):
    """First half of arm-then-confirm: here is the token and the sentence."""

    status_code = 428

    def __init__(self, action: str, token: str, effect: str, expires_in_s: float) -> None:
        super().__init__(f"confirm required: {effect}")
        self.action = action
        self.token = token
        self.effect = effect
        self.expires_in_s = expires_in_s

    def payload(self) -> dict[str, Any]:
        return {
            "error": "confirm required",
            "action": self.action,
            "confirm_token": self.token,
            "effect": self.effect,
            "expires_in_s": round(self.expires_in_s, 1),
        }


# ---------------------------------------------------------------------------
# server-side confirmation
# ---------------------------------------------------------------------------


def params_fingerprint(action: str, params: Mapping[str, Any]) -> str:
    """Stable hash of an action and its parameters.

    The token is bound to this, so the parameters cannot change between the
    call that armed the action and the call that confirms it: arming
    ``propose(min_score=0.95)`` and confirming ``propose(min_score=0.10)``
    would otherwise be a confirmed action nobody agreed to.
    """
    canonical = json.dumps(
        {"action": action, "params": params}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ConfirmToken:
    token: str
    action: str
    fingerprint: str
    effect: str
    expires_mono_ns: int

    def expires_in_s(self, *, now_mono_ns: int | None = None) -> float:
        now = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
        return max(0.0, (self.expires_mono_ns - now) / 1e9)


class ConfirmRegistry:
    """Single-use, short-TTL confirm tokens bound to ``(action, params)``."""

    def __init__(self, *, ttl_s: float = CONFIRM_TTL_S, max_pending: int = CONFIRM_MAX_PENDING):
        self._ttl_ns = int(ttl_s * 1e9)
        self._max_pending = max_pending
        self._tokens: dict[str, ConfirmToken] = {}

    def mint(self, action: str, params: Mapping[str, Any], effect: str) -> ConfirmToken:
        self._prune()
        while len(self._tokens) >= self._max_pending:
            self._tokens.pop(next(iter(self._tokens)))
        entry = ConfirmToken(
            token=secrets.token_urlsafe(24),
            action=action,
            fingerprint=params_fingerprint(action, params),
            effect=effect,
            expires_mono_ns=time.monotonic_ns() + self._ttl_ns,
        )
        self._tokens[entry.token] = entry
        return entry

    def consume(self, token: str, action: str, params: Mapping[str, Any]) -> ConfirmToken:
        """Spend a token. Any failed attempt also burns it.

        Burning on mismatch is the point: a token is one authorisation for one
        exact set of parameters, so an attempt to spend it on different ones
        ends that authorisation rather than letting the caller keep trying.
        """
        self._prune()
        entry = self._tokens.pop(token, None)
        if entry is None:
            raise ConfirmInvalid("confirm token is unknown or has expired; arm the action again")
        if entry.action != action or not hmac.compare_digest(
            entry.fingerprint, params_fingerprint(action, params)
        ):
            raise ConfirmInvalid("the parameters changed since this action was armed; arm it again")
        return entry

    def _prune(self) -> None:
        now = time.monotonic_ns()
        for token in [t for t, e in self._tokens.items() if e.expires_mono_ns <= now]:
            self._tokens.pop(token, None)

    @property
    def pending(self) -> int:
        self._prune()
        return len(self._tokens)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class JobRecord:
    """One long action, observable while it runs and after it finishes."""

    job_id: str
    name: str
    group: str
    params: dict[str, Any]
    started_ts_ns: int
    status: str = JOB_RUNNING
    phase: str = ""
    message: str = ""
    step: int = 0
    total: int = 0
    finished_ts_ns: int | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=JOB_OUTPUT_MAX_LINES))
    lines_total: int = 0
    lines_dropped: int = 0
    _broadcast: Broadcast | None = None
    _last_frame_mono_ns: int = 0

    # -- writes (all from the loop thread) ---------------------------------

    def progress(self, phase: str, message: str, step: int = 0, total: int = 0) -> None:
        changed_phase = phase != self.phase
        self.phase, self.message, self.step, self.total = phase, message, step, total
        self.publish(force=changed_phase)

    def append(self, line: str) -> None:
        if self.lines.maxlen is not None and len(self.lines) == self.lines.maxlen:
            self.lines_dropped += 1
            CONTROL_JOB_OUTPUT_DROPPED.labels(job=self.name).inc()
        self.lines.append(line)
        self.lines_total += 1
        self.publish()

    def finish(
        self, status: str, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        self.status = status
        self.result = result
        self.error = error
        self.finished_ts_ns = time.time_ns()
        self.publish(force=True)

    def publish(self, *, force: bool = False) -> None:
        """Emit a job frame, coalesced unless ``force``.

        Never raises: a broadcast failure must not fail the job it describes.
        """
        if self._broadcast is None:
            return
        now = time.monotonic_ns()
        if not force and now - self._last_frame_mono_ns < JOB_BROADCAST_INTERVAL_S * 1e9:
            return
        self._last_frame_mono_ns = now
        try:
            self._broadcast({"t": "job", "job": self.payload(), "tail": list(self.lines)[-20:]})
        except Exception:
            log.warning("job frame broadcast failed for %s", self.job_id, exc_info=True)

    # -- reads -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.status == JOB_RUNNING

    def elapsed_s(self) -> float:
        end = self.finished_ts_ns if self.finished_ts_ns is not None else time.time_ns()
        return (end - self.started_ts_ns) / 1e9

    def payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "group": self.group,
            "params": self.params,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "step": self.step,
            "total": self.total,
            "started_ts_ns": self.started_ts_ns,
            "finished_ts_ns": self.finished_ts_ns,
            "elapsed_s": round(self.elapsed_s(), 3),
            "error": self.error,
            "result": self.result,
            "lines_total": self.lines_total,
            "lines_dropped": self.lines_dropped,
        }

    def log_payload(self, *, tail: int = JOB_OUTPUT_MAX_LINES) -> dict[str, Any]:
        lines = list(self.lines)
        return {**self.payload(), "lines": lines[-tail:] if tail > 0 else lines}


type JobBody = Callable[[JobRecord], Awaitable[dict[str, Any] | None]]


class JobRunner:
    """Starts, tracks and cancels the long actions.

    Single-flight is per *group*, not per action name, because ``propose`` and
    ``backfill`` fetch the same two universes and write the same table —
    ``arb.pairs.run`` refuses the second one anyway, and refusing it here
    gives the caller a 409 instead of a job record that failed.
    """

    def __init__(self, *, broadcast: Broadcast | None = None, history: int = JOB_HISTORY_MAX):
        self._broadcast = broadcast
        self._history = history
        self._jobs: dict[str, JobRecord] = {}
        self._active: dict[str, str] = {}  # group -> job_id
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(
        self, name: str, body: JobBody, *, group: str | None = None, params: dict[str, Any]
    ) -> JobRecord:
        key = group or name
        active = self._active.get(key)
        if active is not None:
            raise JobBusy(f"{name} is already running as job {active}")
        record = JobRecord(
            job_id=f"{name.rsplit('.', 1)[-1]}-{secrets.token_hex(3)}",
            name=name,
            group=key,
            params=dict(params),
            started_ts_ns=time.time_ns(),
        )
        record._broadcast = self._broadcast
        self._jobs[record.job_id] = record
        self._active[key] = record.job_id
        self._prune()
        task = asyncio.create_task(self._run(record, body), name=f"control-job-{record.job_id}")
        self._tasks[record.job_id] = task
        record.publish(force=True)
        return record

    async def _run(self, record: JobRecord, body: JobBody) -> None:
        try:
            result = await body(record)
        except asyncio.CancelledError:
            record.finish(JOB_CANCELLED, error="cancelled by the operator")
            CONTROL_JOBS.labels(job=record.name, result=JOB_CANCELLED).inc()
            log.info("control job %s cancelled", record.job_id)
            raise
        except Exception as exc:
            record.append(f"error: {exc}")
            record.finish(JOB_ERROR, error=str(exc))
            CONTROL_JOBS.labels(job=record.name, result=JOB_ERROR).inc()
            log.warning("control job %s failed", record.job_id, exc_info=True)
        else:
            record.finish(JOB_OK, result=result)
            CONTROL_JOBS.labels(job=record.name, result=JOB_OK).inc()
            log.info("control job %s finished in %.1fs", record.job_id, record.elapsed_s())
        finally:
            if self._active.get(record.group) == record.job_id:
                self._active.pop(record.group, None)
            self._tasks.pop(record.job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Ask a running job to stop. False if it is unknown or already done."""
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        """Cancel every running job and wait for it, on server teardown."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- reads -------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def running(self, group: str) -> str | None:
        return self._active.get(group)

    @property
    def jobs(self) -> list[JobRecord]:
        """Newest first."""
        return sorted(self._jobs.values(), key=lambda r: r.started_ts_ns, reverse=True)

    def payload(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return [record.payload() for record in self.jobs[:limit]]

    def _prune(self) -> None:
        """Drop the oldest finished jobs once history is over budget."""
        finished = [r for r in sorted(self._jobs.values(), key=lambda r: r.started_ts_ns)]
        excess = len(finished) - self._history
        for record in finished:
            if excess <= 0:
                break
            if record.running:
                continue
            self._jobs.pop(record.job_id, None)
            excess -= 1


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL if it will not go. Never raises."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        async with asyncio.timeout(JOB_TERMINATE_GRACE_S):
            await proc.wait()
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


# ---------------------------------------------------------------------------
# what the control plane needs from the server state
# ---------------------------------------------------------------------------


class SubscriptionSource(Protocol):
    """The subscribed-set half of a streaming source.

    ``KalshiWSSource`` satisfies this structurally. It is a Protocol so a test
    can drive the universe control without a socket — the alternative is
    constructing a real source and never connecting it, which tests the
    constructor instead of the control.
    """

    @property
    def tickers(self) -> list[str]: ...

    def set_tickers(self, market_tickers: Sequence[str]) -> bool: ...

    async def force_resync(self) -> None: ...


class ControlHost(Protocol):
    """The mutable server state a control acts on. ``ServerState`` satisfies it."""

    run_id: str
    recording: bool
    books: BookManager
    arbmon: ArbMonitor | None
    trader: PaperTrader | None

    def broadcast(self, payload: dict[str, Any]) -> None: ...

    def mark_dirty(self, market_ids: set[str]) -> None: ...

    def hello_markets(self) -> list[dict[str, Any]]: ...

    def set_markets(self, markets: list[dict[str, Any]]) -> None: ...


# ---------------------------------------------------------------------------
# the action table
# ---------------------------------------------------------------------------


type Validate = Callable[[Mapping[str, Any]], dict[str, Any]]
type Effect = Callable[[dict[str, Any]], str]
type Apply = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
type NeedsConfirm = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One control. Grades are the consequence grades in docs/decisions.md."""

    name: str
    grade: str
    summary: str
    validate: Validate
    effect: Effect
    apply: Apply
    mutates: bool = True
    confirm: NeedsConfirm | None = None

    def needs_confirm(self, params: dict[str, Any]) -> bool:
        return self.confirm is not None and self.confirm(params)

    def payload(self) -> dict[str, Any]:
        return {
            "action": self.name,
            "grade": self.grade,
            "summary": self.summary,
            "mutates": self.mutates,
            "confirm": self.confirm is not None,
        }


@dataclass(frozen=True, slots=True)
class ControlResult:
    action: str
    effect: str
    changed: bool
    detail: dict[str, Any]
    audit_id: int | None

    def payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "effect": self.effect,
            "changed": self.changed,
            "audit_id": self.audit_id,
            **self.detail,
        }


# -- parameter helpers ------------------------------------------------------


def _int_param(
    params: Mapping[str, Any], key: str, *, minimum: int, maximum: int, default: int | None = None
) -> int:
    raw = params.get(key, default)
    if raw is None:
        raise InvalidParams(f"{key} is required")
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise InvalidParams(f"{key} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidParams(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise InvalidParams(f"{key} must be between {minimum} and {maximum}")
    return value


def _float_param(
    params: Mapping[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
    default: float | None = None,
) -> float:
    raw = params.get(key, default)
    if raw is None:
        raise InvalidParams(f"{key} is required")
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise InvalidParams(f"{key} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidParams(f"{key} must be a number") from exc
    if not minimum <= value <= maximum:
        raise InvalidParams(f"{key} must be between {minimum} and {maximum}")
    return value


def _bool_param(params: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    raw = params.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    raise InvalidParams(f"{key} must be a boolean")


def _str_list_param(
    params: Mapping[str, Any], key: str, *, max_items: int, allow_empty: bool
) -> list[str]:
    raw = params.get(key)
    if raw is None:
        raise InvalidParams(f"{key} is required")
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, Sequence):
        items = [str(part).strip() for part in raw]
    else:
        raise InvalidParams(f"{key} must be a list of strings")
    values = list(dict.fromkeys(item for item in items if item))
    if not values and not allow_empty:
        raise InvalidParams(f"{key} must not be empty")
    if len(values) > max_items:
        raise InvalidParams(f"{key} takes at most {max_items} entries")
    return values


# A run id is argv for the replay subprocess. There is no shell involved, but
# an id is still only ever the sortable token ``new_run_id`` makes.
_RUN_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.")


def _run_id_param(params: Mapping[str, Any]) -> str | None:
    raw = params.get("run_id")
    if raw is None or raw == "":
        return None
    value = str(raw).strip()
    if not value or len(value) > 64 or not set(value) <= _RUN_ID_CHARS:
        raise InvalidParams("run_id must be a short alphanumeric run identifier")
    return value


# ---------------------------------------------------------------------------
# the control plane
# ---------------------------------------------------------------------------


class ControlPlane:
    """Handles on the live runtime, plus the single action executor."""

    def __init__(
        self,
        *,
        config: AppConfig,
        host: ControlHost,
        run: RunContext,
        engine: AsyncEngine | None = None,
        sink: Sink | None = None,
        bind: BindInfo | None = None,
        read_only: bool | None = None,
        pairs_top: int = 0,
        jobs: JobRunner | None = None,
        confirms: ConfirmRegistry | None = None,
    ) -> None:
        self._config = config
        self._host = host
        self._run = run
        self._engine = engine
        self._sink = sink
        self.bind = bind
        self.read_only = config.ui_read_only if read_only is None else read_only
        self.pairs_top = pairs_top
        self.jobs = jobs if jobs is not None else JobRunner(broadcast=host.broadcast)
        self._confirms = confirms if confirms is not None else ConfirmRegistry()
        self.kalshi: SubscriptionSource | None = None
        self.polymarket: PolymarketUSRestSource | None = None
        # The universe is the union of the operator's base set and whatever
        # the tracked pairs need; tracking them separately is what lets a
        # pairs reload drop its old legs without dropping the operator's.
        self._base_kalshi: list[str] = []
        self._base_polymarket: list[str] = []
        self._pair_kalshi: list[str] = []
        self._pair_polymarket: list[str] = []
        # market_id -> the hello-frame dict, so a universe change can keep the
        # client's market list in step without re-running discovery.
        self._market_meta: dict[str, dict[str, Any]] = {}
        self._actions: dict[str, ActionSpec] = {}
        self._register_actions()

    # -- wiring (called by run_ui as it builds the runtime) -----------------

    def attach_kalshi(self, source: SubscriptionSource) -> None:
        self.kalshi = source

    def attach_polymarket(self, source: PolymarketUSRestSource) -> None:
        self.polymarket = source

    def set_base_universe(
        self, *, kalshi: Sequence[str] | None = None, polymarket: Sequence[str] | None = None
    ) -> None:
        if kalshi is not None:
            self._base_kalshi = list(dict.fromkeys(kalshi))
        if polymarket is not None:
            self._base_polymarket = list(dict.fromkeys(polymarket))

    def set_pair_universe(
        self, *, kalshi: Sequence[str] | None = None, polymarket: Sequence[str] | None = None
    ) -> None:
        if kalshi is not None:
            self._pair_kalshi = list(dict.fromkeys(kalshi))
        if polymarket is not None:
            self._pair_polymarket = list(dict.fromkeys(polymarket))

    def seed_markets(self, markets: Iterable[Mapping[str, Any]]) -> None:
        """Remember the hello-frame rows so universe changes can rebuild them."""
        for market in markets:
            market_id = str(market.get("market_id", ""))
            if market_id:
                self._market_meta[market_id] = dict(market)

    @property
    def kalshi_tickers(self) -> list[str]:
        return list(dict.fromkeys([*self._base_kalshi, *self._pair_kalshi]))

    @property
    def polymarket_slugs(self) -> list[str]:
        return list(dict.fromkeys([*self._base_polymarket, *self._pair_polymarket]))

    # -- state for the browser ---------------------------------------------

    def payload(self) -> dict[str, Any]:
        trader = self._host.trader
        pm = self.polymarket
        return {
            "run_id": self._host.run_id,
            "read_only": self.read_only,
            "recording": self._host.recording,
            "bind": (
                None
                if self.bind is None
                else {
                    "host": self.bind.host,
                    "loopback": self.bind.loopback,
                    "remote_allowed": self.bind.remote_allowed,
                }
            ),
            "paper": {
                "attached": trader is not None,
                "enabled": trader is not None and trader.enabled,
                "suspended": trader is not None and trader.suspended,
                "limits": (trader.limits if trader is not None else PaperLimits()).payload(),
                "notional_ticks": trader.notional_ticks if trader is not None else 0,
                "skipped_suspended": trader.skipped_suspended if trader is not None else 0,
            },
            "pairs_top": self.pairs_top,
            "tracked_pairs": len(self._host.arbmon.pairs) if self._host.arbmon else 0,
            "universe": {
                "kalshi": {
                    "tickers": self.kalshi_tickers,
                    "base": list(self._base_kalshi),
                    "pairs": list(self._pair_kalshi),
                    "attached": self.kalshi is not None,
                },
                "polymarket_us": {
                    "slugs": self.polymarket_slugs,
                    "base": list(self._base_polymarket),
                    "pairs": list(self._pair_polymarket),
                    "attached": pm is not None,
                    "interval_s": pm.interval_s if pm is not None else None,
                    "cycle_s": pm.cycle_s if pm is not None else None,
                },
            },
            "jobs": self.jobs.payload(),
            "actions": [spec.payload() for spec in self._actions.values()],
            "confirm_ttl_s": CONFIRM_TTL_S,
            "ts_ms": time.time_ns() // 1_000_000,
        }

    async def recent_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        """The audit trail, newest first. Empty when there is no database —
        the UI renders that as "not recorded", never as "nothing happened"."""
        if self._engine is None:
            return []
        try:
            return await list_control_actions(self._engine, limit=limit)
        except Exception:
            log.warning("control audit read failed", exc_info=True)
            return []

    def broadcast_state(self) -> None:
        """Push the whole control state so two tabs cannot disagree."""
        try:
            self._host.broadcast({"t": "control", "control": self.payload()})
        except Exception:
            log.warning("control frame broadcast failed", exc_info=True)

    # -- the executor ------------------------------------------------------

    @property
    def actions(self) -> Mapping[str, ActionSpec]:
        return self._actions

    async def execute(
        self,
        action: str,
        params: Mapping[str, Any] | None = None,
        *,
        confirm: str | None = None,
        actor: str = "ui",
    ) -> ControlResult:
        """Run one control. The only way anything in the runtime changes.

        Order matters and is the whole point: unknown action, then parameter
        validation, then the read-only refusal, then arm-or-confirm (with the
        arming audited and fail-closed), then the effect, then the audit row,
        then the broadcast. Nothing can be skipped per-route because no route
        implements any of it.
        """
        spec = self._actions.get(action)
        if spec is None:
            CONTROL_ACTIONS.labels(action=action, result="unknown").inc()
            raise UnknownAction(f"unknown control action {action!r}")
        clean = spec.validate(params or {})
        effect = spec.effect(clean)
        if self.read_only and spec.mutates:
            CONTROL_ACTIONS.labels(action=action, result="read_only").inc()
            await self._audit(
                action,
                clean,
                effect,
                result="refused",
                actor=actor,
                error="read-only mode",
                required=False,
            )
            raise ReadOnlyRefused(
                f"this server is read-only; {action} was refused (start without --read-only)"
            )
        if spec.needs_confirm(clean):
            if confirm is None:
                # Arming is a database write on purpose: a G3 action that
                # cannot be recorded must not become available to confirm.
                await self._audit(action, clean, effect, result="armed", actor=actor, required=True)
                token = self._confirms.mint(action, clean, effect)
                CONTROL_ACTIONS.labels(action=action, result="armed").inc()
                raise ConfirmRequired(action, token.token, effect, token.expires_in_s())
            try:
                self._confirms.consume(confirm, action, clean)
            except ConfirmInvalid:
                CONTROL_ACTIONS.labels(action=action, result="confirm_invalid").inc()
                raise
        try:
            detail = await spec.apply(clean)
        except asyncio.CancelledError:
            raise
        except ControlError as exc:
            CONTROL_ACTIONS.labels(action=action, result="refused").inc()
            await self._audit(
                action, clean, effect, result="error", actor=actor, error=str(exc), required=False
            )
            raise
        except Exception as exc:
            CONTROL_ACTIONS.labels(action=action, result="error").inc()
            log.warning("control action %s failed", action, exc_info=True)
            await self._audit(
                action, clean, effect, result="error", actor=actor, error=str(exc), required=False
            )
            raise ControlError(f"{action} failed: {exc}") from exc
        changed = bool(detail.pop("changed", True))
        audit_id = await self._audit(
            action, clean, effect, result="ok", actor=actor, required=False
        )
        CONTROL_ACTIONS.labels(action=action, result="ok").inc()
        log.info("control %s by %s: %s", action, actor, effect)
        if spec.mutates:
            self.broadcast_state()
        return ControlResult(
            action=action, effect=effect, changed=changed, detail=detail, audit_id=audit_id
        )

    async def _audit(
        self,
        action: str,
        params: Mapping[str, Any],
        effect: str,
        *,
        result: str,
        actor: str,
        error: str | None = None,
        required: bool,
    ) -> int | None:
        """Write the audit row. ``required`` means the caller fails closed."""
        if self._engine is None:
            if required:
                raise AuditFailed(
                    f"{action} needs an audit row and this process has no database engine"
                )
            return None
        try:
            return await insert_control_action(
                self._engine,
                run_id=self._host.run_id,
                action=action,
                effect=effect,
                actor=actor,
                result=result,
                params=dict(params),
                error=error,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            CONTROL_AUDIT_FAILURES.labels(action=action).inc()
            log.warning("control audit write failed for %s", action, exc_info=True)
            if required:
                raise AuditFailed(
                    f"{action} was not performed: its audit row could not be written ({exc})"
                ) from exc
            return None

    # -- actions -----------------------------------------------------------

    def _register(self, spec: ActionSpec) -> None:
        self._actions[spec.name] = spec

    def _register_actions(self) -> None:
        no_params: Validate = lambda _params: {}  # noqa: E731

        self._register(
            ActionSpec(
                name="recording.start",
                grade="G2",
                summary="write raw venue messages to Postgres again",
                validate=no_params,
                effect=lambda _p: (
                    f"start recording raw messages for run {self._host.run_id}; "
                    "the messages received while it was off are gone for good"
                ),
                apply=self._apply_recording_start,
            )
        )
        self._register(
            ActionSpec(
                name="recording.stop",
                grade="G2",
                summary="stop writing raw venue messages to Postgres",
                validate=no_params,
                effect=lambda _p: (
                    f"stop recording raw messages for run {self._host.run_id}; replay reads "
                    "straight across the gap, so this audit row is the only sign it exists"
                ),
                apply=self._apply_recording_stop,
            )
        )
        self._register(
            ActionSpec(
                name="paper.suspend",
                grade="G2",
                summary="stop taking paper edges (positions and spend are kept)",
                validate=no_params,
                effect=lambda _p: (
                    "suspend paper trading; open positions, committed spend and the ledger "
                    "are kept, so resuming continues the same book"
                ),
                apply=self._apply_paper_suspend,
            )
        )
        self._register(
            ActionSpec(
                name="paper.resume",
                grade="G2",
                summary="take paper edges again, against the spend already committed",
                validate=no_params,
                effect=lambda _p: (
                    "resume paper trading against the same trader: the notional budget "
                    "continues from what is already committed, it does not reopen"
                ),
                apply=self._apply_paper_resume,
            )
        )
        self._register(
            ActionSpec(
                name="paper.limits",
                grade="G2",
                summary="change the paper risk limits, live",
                validate=self._validate_paper_limits,
                effect=lambda p: (
                    f"set paper limits to min_net {p['min_net_ticks']} ticks, "
                    f"{p['max_qty_per_pair'] // QTY_PER_CONTRACT} contracts per pair, "
                    f"${p['max_notional_ticks'] / 10_000:,.0f} total notional; tightening "
                    "below what is already committed stops new trades and unwinds nothing"
                ),
                apply=self._apply_paper_limits,
            )
        )
        self._register(
            ActionSpec(
                name="pairs.top",
                grade="G2",
                summary="reload the tracked confirmed pairs (top N by score)",
                validate=lambda p: {"n": _int_param(p, "n", minimum=0, maximum=200, default=None)},
                effect=lambda p: (
                    f"track the top {p['n']} confirmed pairs: re-fetches both venues' fee "
                    "parameters and resubscribes Kalshi (a reconnect, so a brief gap)"
                ),
                apply=self._apply_pairs_top,
            )
        )
        self._register(
            ActionSpec(
                name="universe.kalshi",
                grade="G2",
                summary="replace the Kalshi market subscription",
                validate=lambda p: {
                    "tickers": _str_list_param(p, "tickers", max_items=200, allow_empty=False)
                },
                effect=lambda p: (
                    f"subscribe to {len(p['tickers'])} Kalshi markets: the socket reconnects "
                    "(seconds of gap, every book resnapshots) and dropped books are evicted"
                ),
                apply=self._apply_universe_kalshi,
            )
        )
        self._register(
            ActionSpec(
                name="universe.polymarket",
                grade="G2",
                summary="replace the Polymarket US poll targets",
                validate=lambda p: {
                    "slugs": _str_list_param(p, "slugs", max_items=100, allow_empty=True)
                },
                effect=lambda p: (
                    f"poll {len(p['slugs'])} Polymarket US markets (no reconnect); the "
                    "staleness budget is retuned to the new cycle and dropped books evicted"
                ),
                apply=self._apply_universe_polymarket,
            )
        )
        self._register(
            ActionSpec(
                name="jobs.propose",
                grade="G3",
                summary="fetch both universes, score them and write pair proposals",
                validate=self._validate_propose,
                effect=lambda p: (
                    f"fetch both venues' open universes and upsert every pair scoring "
                    f"{p['min_score']:.2f} or better into the pairs table — thousands of rows, "
                    "about three minutes, recorded under this run"
                ),
                confirm=lambda _p: True,
                apply=self._apply_propose,
            )
        )
        self._register(
            ActionSpec(
                name="jobs.backfill",
                grade="G3",
                summary="fill in missing event slugs on already-proposed pairs",
                validate=self._validate_backfill,
                effect=lambda _p: (
                    "fetch both venues' open universes and write the missing event slug on "
                    "every pair they resolve — updates existing rows, about three minutes"
                ),
                confirm=lambda _p: True,
                apply=self._apply_backfill,
            )
        )
        self._register(
            ActionSpec(
                name="jobs.replay",
                grade="G3",
                summary="replay a recorded run in a subprocess",
                validate=self._validate_replay,
                effect=self._effect_replay,
                confirm=lambda p: bool(p["persist"]),
                apply=self._apply_replay,
            )
        )
        self._register(
            ActionSpec(
                name="jobs.doctor",
                grade="G0",
                summary="run the environment, clock, database and venue checks",
                validate=no_params,
                effect=lambda _p: "run doctor: env, keys, clock skew, database, venues, disk",
                mutates=False,
                apply=self._apply_doctor,
            )
        )
        self._register(
            ActionSpec(
                name="jobs.cancel",
                grade="G2",
                summary="cancel a running job",
                validate=lambda p: {"job_id": str(p.get("job_id", "")).strip()},
                effect=lambda p: f"cancel job {p['job_id'] or '(none)'}",
                apply=self._apply_cancel,
            )
        )

    # -- recording ---------------------------------------------------------

    async def _apply_recording_start(self, _params: dict[str, Any]) -> dict[str, Any]:
        changed = not self._host.recording
        self._host.recording = True
        return {"recording": True, "changed": changed}

    async def _apply_recording_stop(self, _params: dict[str, Any]) -> dict[str, Any]:
        changed = self._host.recording
        self._host.recording = False
        return {"recording": False, "changed": changed}

    # -- paper -------------------------------------------------------------

    def _trader(self) -> PaperTrader:
        trader = self._host.trader
        if trader is None:
            raise NotAvailable(
                "paper trading is not running in this process (start the UI with --paper)"
            )
        return trader

    def _validate_paper_limits(self, params: Mapping[str, Any]) -> dict[str, Any]:
        current = self._host.trader.limits if self._host.trader is not None else PaperLimits()
        max_cts = params.get("max_cts_per_pair")
        if max_cts is not None and "max_qty_per_pair" not in params:
            # The CLI speaks contracts; the wire and PaperLimits speak Qty.
            qty = _int_param(params, "max_cts_per_pair", minimum=1, maximum=10_000_000)
            qty *= QTY_PER_CONTRACT
        else:
            qty = _int_param(
                params,
                "max_qty_per_pair",
                minimum=1,
                maximum=10_000_000 * QTY_PER_CONTRACT,
                default=current.max_qty_per_pair,
            )
        return {
            "min_net_ticks": _int_param(
                params, "min_net_ticks", minimum=0, maximum=10_000, default=current.min_net_ticks
            ),
            "max_qty_per_pair": qty,
            "max_notional_ticks": _int_param(
                params,
                "max_notional_ticks",
                minimum=0,
                maximum=10**12,
                default=current.max_notional_ticks,
            ),
        }

    async def _apply_paper_suspend(self, _params: dict[str, Any]) -> dict[str, Any]:
        trader = self._trader()
        changed = trader.suspend()
        return {"changed": changed, "paper": trader.payload(max_trades=0)}

    async def _apply_paper_resume(self, _params: dict[str, Any]) -> dict[str, Any]:
        trader = self._trader()
        changed = trader.resume()
        return {"changed": changed, "paper": trader.payload(max_trades=0)}

    async def _apply_paper_limits(self, params: dict[str, Any]) -> dict[str, Any]:
        trader = self._trader()
        limits = PaperLimits(
            min_net_ticks=params["min_net_ticks"],
            max_qty_per_pair=params["max_qty_per_pair"],
            max_notional_ticks=params["max_notional_ticks"],
        )
        previous = trader.set_limits(limits)
        return {
            "changed": previous != limits,
            "limits": limits.payload(),
            "previous": previous.payload(),
        }

    # -- universe ----------------------------------------------------------

    def _sync_markets(self) -> None:
        """Rebuild the client market list from the live universe."""
        wanted: list[str] = [kalshi_market_id(t) for t in self.kalshi_tickers]
        wanted += [pm_market_id(s) for s in self.polymarket_slugs]
        markets: list[dict[str, Any]] = []
        for market_id in dict.fromkeys(wanted):
            meta = self._market_meta.get(market_id)
            if meta is None:
                venue, _, native = market_id.partition(":")
                meta = {
                    "market_id": market_id,
                    "ticker": native,
                    "title": "",
                    "volume_24h": 0.0,
                    "venue": venue,
                }
                self._market_meta[market_id] = meta
            markets.append(meta)
        self._host.set_markets(markets)

    async def _apply_kalshi_universe(self) -> dict[str, Any]:
        """Push the union set to the source and reconcile the books."""
        source = self.kalshi
        tickers = self.kalshi_tickers
        if source is None:
            raise NotAvailable("the Kalshi source is not attached in this process")
        if not tickers:
            raise InvalidParams("the Kalshi subscription cannot be empty")
        changed = source.set_tickers(tickers)
        evicted: set[str] = set()
        if changed:
            # A dropped market's book never updates again: it would age into
            # permanent staleness and still be shipped to every new client.
            evicted = self._host.books.retain(
                {kalshi_market_id(t) for t in tickers}, venue="kalshi"
            )
            self._sync_markets()
            # Kalshi answers a resubscribe with a full snapshot per market, so
            # a reconnect IS how a new ticker set takes effect.
            await source.force_resync()
        return {"changed": changed, "tickers": tickers, "evicted": sorted(evicted)}

    async def _apply_universe_kalshi(self, params: dict[str, Any]) -> dict[str, Any]:
        self.set_base_universe(kalshi=params["tickers"])
        return await self._apply_kalshi_universe()

    def retune_polymarket_staleness(self) -> set[str]:
        """Match the venue staleness budget to the current poll cycle.

        ``Book`` captures its budget at construction, so this retunes the
        LIVE books too (the manager returns the ids it touched): changing the
        target count without this leaves every existing book judged against a
        budget computed for a different cycle, flapping STALE.
        """
        source = self.polymarket
        if source is None:
            return set()
        cycle_ns = int(source.cycle_s * 1e9)
        limit_ns = max(
            self._config.book_staleness_limit_ms * 1_000_000, PM_STALENESS_CYCLES * cycle_ns
        )
        return self._host.books.set_venue_staleness("polymarket_us", limit_ns)

    async def _apply_polymarket_universe(self) -> dict[str, Any]:
        source = self.polymarket
        slugs = self.polymarket_slugs
        if source is None:
            raise NotAvailable("the Polymarket US poller is not attached in this process")
        changed = source.set_targets(slugs)
        evicted: set[str] = set()
        retuned: set[str] = set()
        if changed:
            # Order: retune first (the cycle length just changed, so every
            # surviving book's budget is wrong until it does), then evict.
            retuned = self.retune_polymarket_staleness()
            evicted = self._host.books.retain(
                {pm_market_id(s) for s in slugs}, venue="polymarket_us"
            )
            self._sync_markets()
            # Re-publish what survived: its staleness verdict may have flipped.
            self._host.mark_dirty(retuned - evicted)
        return {
            "changed": changed,
            "slugs": slugs,
            "evicted": sorted(evicted),
            "retuned": len(retuned - evicted),
            "cycle_s": source.cycle_s,
        }

    async def _apply_universe_polymarket(self, params: dict[str, Any]) -> dict[str, Any]:
        self.set_base_universe(polymarket=params["slugs"])
        return await self._apply_polymarket_universe()

    # -- tracked pairs -----------------------------------------------------

    async def _apply_pairs_top(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._engine is None:
            raise NotAvailable("pairs cannot be reloaded without a database engine")
        top_n = params["n"]
        previous = self.pairs_top
        if top_n == 0:
            self._host.arbmon = None
            self.pairs_top = 0
            self.set_pair_universe(kalshi=[], polymarket=[])
        else:
            load = await load_tracked_pairs(
                self._config, self._run, self._engine, top_n=top_n, sink=self._sink
            )
            self.pairs_top = top_n
            self._host.arbmon = ArbMonitor(self._host.books, load.tracked) if load.tracked else None
            for pair in load.tracked:
                self._market_meta.setdefault(
                    pair.kalshi_market_id,
                    {
                        "market_id": pair.kalshi_market_id,
                        "ticker": pair.kalshi_ticker,
                        "title": pair.label,
                        "volume_24h": 0.0,
                        "venue": "kalshi",
                    },
                )
            self.set_pair_universe(
                kalshi=[p.kalshi_ticker for p in load.tracked],
                polymarket=load.polymarket_slugs,
            )
        polymarket = await self._apply_polymarket_universe() if self.polymarket else {}
        kalshi = await self._apply_kalshi_universe() if self.kalshi else {}
        tracked = len(self._host.arbmon.pairs) if self._host.arbmon is not None else 0
        return {
            "changed": top_n != previous
            or bool(kalshi.get("changed") or polymarket.get("changed")),
            "pairs_top": self.pairs_top,
            "tracked_pairs": tracked,
            "kalshi": kalshi,
            "polymarket_us": polymarket,
        }

    # -- jobs --------------------------------------------------------------

    def _validate_propose(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "min_score": round(
                _float_param(params, "min_score", minimum=0.0, maximum=1.0, default=0.75), 4
            ),
            "kalshi_pages": _int_param(
                params,
                "kalshi_pages",
                minimum=1,
                maximum=500,
                default=pairs_run.KALSHI_MAX_PAGES,
            ),
            "pm_pages": _int_param(
                params, "pm_pages", minimum=1, maximum=500, default=pairs_run.PM_MAX_PAGES
            ),
        }

    def _validate_backfill(self, params: Mapping[str, Any]) -> dict[str, Any]:
        clean = self._validate_propose({**params, "min_score": 0.0})
        clean.pop("min_score")
        return clean

    def _validate_replay(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_id": _run_id_param(params),
            "pairs_top": _int_param(params, "pairs_top", minimum=0, maximum=200, default=10),
            "paper": _bool_param(params, "paper"),
            "persist": _bool_param(params, "persist"),
        }

    def _effect_replay(self, params: dict[str, Any]) -> str:
        target = params["run_id"] or "the latest recorded run"
        persisted = (
            " and store its paper trades as replay:<run_id>"
            if params["persist"]
            else " (nothing is written)"
        )
        return f"replay {target} in a subprocess{persisted}"

    def _job_deps(self) -> pairs_run.JobDeps:
        if self._engine is None:
            raise NotAvailable("this process has no database engine")
        # The UI's OWN run and sink: a second RunContext on one run_id hands
        # out a colliding ingest_seq and wedges the recorder forever.
        return pairs_run.JobDeps(engine=self._engine, run=self._run, sink=self._sink)

    def _progress(self, record: JobRecord) -> pairs_run.ProgressFn:
        def on_progress(update: pairs_run.JobProgress) -> None:
            record.progress(update.phase, update.message, update.step, update.total)

        return on_progress

    async def _apply_propose(self, params: dict[str, Any]) -> dict[str, Any]:
        deps = self._job_deps()
        if pairs_run.job_running():
            raise JobBusy("a pairs job is already running")

        async def body(record: JobRecord) -> dict[str, Any]:
            result = await pairs_run.run_propose(
                self._config,
                min_score=params["min_score"],
                kalshi_pages=params["kalshi_pages"],
                pm_pages=params["pm_pages"],
                deps=deps,
                progress=self._progress(record),
            )
            record.append(result.summary())
            return {
                "run_id": result.run_id,
                "proposed": result.proposed,
                "rows_written": result.rows_written,
                "kalshi_events": result.kalshi_events,
                "polymarket_events": result.polymarket_events,
                "elapsed_s": round(result.elapsed_s, 1),
                "summary": result.summary(),
            }

        record = self.jobs.start("jobs.propose", body, group="pairs", params=params)
        return {"job": record.payload()}

    async def _apply_backfill(self, params: dict[str, Any]) -> dict[str, Any]:
        deps = self._job_deps()
        if pairs_run.job_running():
            raise JobBusy("a pairs job is already running")

        async def body(record: JobRecord) -> dict[str, Any]:
            result = await pairs_run.run_backfill(
                self._config,
                kalshi_pages=params["kalshi_pages"],
                pm_pages=params["pm_pages"],
                deps=deps,
                progress=self._progress(record),
            )
            record.append(result.summary())
            return {
                "run_id": result.run_id,
                "resolved": result.resolved,
                "updated": result.updated,
                "elapsed_s": round(result.elapsed_s, 1),
                "summary": result.summary(),
            }

        record = self.jobs.start("jobs.backfill", body, group="pairs", params=params)
        return {"job": record.payload()}

    async def _apply_replay(self, params: dict[str, Any]) -> dict[str, Any]:
        argv = [sys.executable, "-m", "arb.cli", "replay"]
        if params["run_id"]:
            argv.append(params["run_id"])
        argv += ["--pairs-top", str(params["pairs_top"])]
        if params["paper"]:
            argv.append("--paper")
        if params["persist"]:
            argv.append("--persist")

        async def body(record: JobRecord) -> dict[str, Any]:
            return await self._run_replay_subprocess(record, argv)

        record = self.jobs.start("jobs.replay", body, group="replay", params=params)
        return {"job": record.payload(), "argv": argv}

    async def _run_replay_subprocess(self, record: JobRecord, argv: list[str]) -> dict[str, Any]:
        """Replay in a child process, streamed back line by line.

        In-process this would be fatal: ``replay.py``'s per-row loop has no
        ``await`` in it and yields only every 2000 rows, so it holds the event
        loop for seconds to minutes — past ``ws.py``'s 10 s ping timeout, which
        drops the Kalshi socket. A subprocess cannot stall this loop at all.
        """
        record.progress("replay", "starting " + " ".join(argv[-4:]))
        record.append("$ " + " ".join(argv))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1 << 20,
            env=os.environ.copy(),
        )
        try:
            stream = proc.stdout
            while stream is not None:
                try:
                    chunk = await stream.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    record.append("(output line too long; truncated)")
                    continue
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").rstrip()
                if line:
                    record.append(line)
                    record.message = line
            code = await proc.wait()
        except asyncio.CancelledError:
            await _terminate(proc)
            raise
        record.progress("done", f"arb replay exited {code}")
        if code != 0:
            raise ControlError(f"arb replay exited {code}")
        return {"exit_code": code, "argv": argv}

    async def _apply_doctor(self, _params: dict[str, Any]) -> dict[str, Any]:
        async def body(record: JobRecord) -> dict[str, Any]:
            record.progress("doctor", "running checks")
            results = await run_doctor(self._config)
            for line in doctor_format_results(results).splitlines():
                record.append(line)
            return {
                "ok": doctor_exit_code(results) == 0,
                "checks": [
                    {"name": r.name, "status": r.status, "detail": r.detail} for r in results
                ],
            }

        record = self.jobs.start("jobs.doctor", body, params={})
        return {"job": record.payload()}

    async def _apply_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = params["job_id"]
        record = self.jobs.get(job_id)
        if record is None:
            raise UnknownAction(f"unknown job {job_id!r}")
        cancelled = self.jobs.cancel(job_id)
        return {"changed": cancelled, "job_id": job_id, "cancelled": cancelled}

    # -- teardown ----------------------------------------------------------

    async def shutdown(self) -> None:
        await self.jobs.shutdown()
