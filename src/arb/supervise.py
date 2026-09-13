"""Backoff and task supervision.

Every long-running task is supervised and restarts with backoff. One venue
failing never takes down another: each venue's tasks get their own supervisor.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Never

from arb.metrics import SUPERVISOR_RESTARTS

log = logging.getLogger(__name__)


class Backoff:
    """Exponential backoff with jitter.

    Delays grow by ``factor`` from ``initial_s`` up to ``max_s``; each delay is
    scaled by a random factor in ``[1 - jitter_frac, 1]`` so reconnecting
    clients don't thundering-herd a recovering venue.
    """

    def __init__(
        self,
        *,
        initial_s: float = 0.5,
        max_s: float = 30.0,
        factor: float = 2.0,
        jitter_frac: float = 0.5,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if not (initial_s > 0 and max_s >= initial_s and factor >= 1 and 0 <= jitter_frac <= 1):
            raise ValueError("invalid backoff parameters")
        self._initial_s = initial_s
        self._max_s = max_s
        self._factor = factor
        self._jitter_frac = jitter_frac
        self._rng = rng
        self._attempt = 0

    def next_delay(self) -> float:
        base = min(self._max_s, self._initial_s * self._factor**self._attempt)
        self._attempt += 1
        return base * (1 - self._jitter_frac * self._rng())

    def reset(self) -> None:
        self._attempt = 0


async def supervise(
    task_factory: Callable[[], Awaitable[object]],
    *,
    name: str,
    backoff: Backoff | None = None,
    healthy_after_s: float = 60.0,
) -> Never:
    """Run ``task_factory()`` forever, restarting it with backoff.

    Exceptions are logged and counted (``arb_supervisor_restarts_total``),
    never propagated — except cancellation, which always propagates so
    shutdown works. A run that lasted at least ``healthy_after_s`` resets the
    backoff, so a long-lived task that finally dies restarts quickly.
    """
    backoff = backoff if backoff is not None else Backoff()
    while True:
        started = time.monotonic()
        try:
            await task_factory()
            log.warning("supervised task %r exited; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("supervised task %r crashed; restarting", name)
        SUPERVISOR_RESTARTS.labels(task=name).inc()
        if time.monotonic() - started >= healthy_after_s:
            backoff.reset()
        await asyncio.sleep(backoff.next_delay())
