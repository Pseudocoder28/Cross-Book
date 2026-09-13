"""Raw-message recorder.

Every raw inbound message is enqueued here *before* it's parsed, so the
archive is complete even when parsing fails. ``enqueue`` never blocks the
ingest path: on overflow the message is dropped and counted
(``arb_recorder_dropped_total``) rather than stalling a venue feed.

The writer (``run``) drains the queue into batches and hands them to the
sink (Postgres in production, anything in tests). A failing sink is retried
in place with backoff — batches are never reordered or silently discarded.
``run`` is meant to live under ``supervise()``.
"""

from __future__ import annotations

import logging
from asyncio import Queue, QueueEmpty, QueueFull, sleep
from collections.abc import Awaitable, Callable, Sequence
from typing import Never

from arb.metrics import (
    RECORDER_DROPPED,
    RECORDER_ENQUEUED,
    RECORDER_QUEUE_DEPTH,
    RECORDER_WRITE_FAILURES,
    RECORDER_WRITTEN,
)
from arb.supervise import Backoff
from arb.types import RawMessage

log = logging.getLogger(__name__)

type Sink = Callable[[Sequence[RawMessage]], Awaitable[None]]


class Recorder:
    def __init__(
        self,
        sink: Sink,
        *,
        queue_max: int = 100_000,
        batch_max: int = 500,
        retry_backoff: Backoff | None = None,
    ) -> None:
        if batch_max <= 0:
            raise ValueError("batch_max must be positive")
        self._sink = sink
        self._batch_max = batch_max
        self._retry_backoff = retry_backoff if retry_backoff is not None else Backoff()
        self._queue: Queue[RawMessage] = Queue(maxsize=queue_max)

    def enqueue(self, message: RawMessage) -> bool:
        """Non-blocking. Returns False (and counts a drop) when the queue is full."""
        try:
            self._queue.put_nowait(message)
        except QueueFull:
            RECORDER_DROPPED.labels(venue=message.venue).inc()
            return False
        RECORDER_ENQUEUED.labels(venue=message.venue).inc()
        RECORDER_QUEUE_DEPTH.set(self._queue.qsize())
        return True

    async def run(self) -> Never:
        """Writer loop: drain → batch → sink, retrying failed writes in place."""
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_max:
                try:
                    batch.append(self._queue.get_nowait())
                except QueueEmpty:
                    break
            RECORDER_QUEUE_DEPTH.set(self._queue.qsize())
            await self._write_with_retry(batch)

    async def _write_with_retry(self, batch: list[RawMessage]) -> None:
        self._retry_backoff.reset()
        while True:
            try:
                await self._sink(batch)
            except Exception:
                RECORDER_WRITE_FAILURES.inc()
                log.exception("recorder sink failed for %d messages; retrying", len(batch))
                await sleep(self._retry_backoff.next_delay())
            else:
                RECORDER_WRITTEN.inc(len(batch))
                return
