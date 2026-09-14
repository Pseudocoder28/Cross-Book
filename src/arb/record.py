"""``arb record``: stream raw venue messages into Postgres.

Wiring: source(s) → recorder queue → batching writer → ``raw_messages``.
Every long-running task is supervised; discovery REST responses are recorded
too. Metrics are served on ``metrics_host:metrics_port``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial

from prometheus_client import start_http_server

from arb.config import AppConfig
from arb.recorder import Recorder
from arb.run import RunContext
from arb.storage.db import insert_raw_messages, make_engine
from arb.supervise import supervise
from arb.venues.kalshi.discovery import fetch_liquid_tickers
from arb.venues.kalshi.source import KalshiWSSource

log = logging.getLogger(__name__)

DRAIN_TIMEOUT_S = 10.0


async def run_record(
    config: AppConfig,
    *,
    tickers: list[str] | None,
    top_n: int,
    duration_s: float | None,
) -> None:
    run = RunContext(config.run_id or None)
    log.info("run_id=%s", run.run_id)
    start_http_server(config.metrics_port, addr=config.metrics_host)

    engine = make_engine(config.database_url)
    recorder = Recorder(
        partial(insert_raw_messages, engine),
        queue_max=config.recorder_queue_max,
        batch_max=config.recorder_batch_max,
    )
    writer = asyncio.create_task(supervise(recorder.run, name="recorder-writer"))
    try:
        if not tickers:
            tickers = await fetch_liquid_tickers(config, run, top_n=top_n, sink=recorder.enqueue)
            log.info("discovered %d liquid kalshi markets", len(tickers))
        source = KalshiWSSource(config=config, run=run, market_tickers=tickers)

        async def consume() -> None:
            async for message in source.stream():
                recorder.enqueue(message)

        consumer = asyncio.create_task(supervise(consume, name="kalshi-ws-consume"))
        try:
            if duration_s is not None:
                await asyncio.sleep(duration_s)
            else:
                await consumer  # runs until cancelled (Ctrl-C)
        finally:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
    finally:
        # Flush what's queued before tearing the writer down.
        if not await recorder.drain(DRAIN_TIMEOUT_S):
            log.warning("recorder drain timed out; some queued messages were not written")
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        await engine.dispose()
