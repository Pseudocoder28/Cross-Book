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
from arb.venues.polymarket_us.discovery import fetch_active_markets, select_poll_targets
from arb.venues.polymarket_us.source import PolymarketUSRestSource

log = logging.getLogger(__name__)

DRAIN_TIMEOUT_S = 10.0


async def run_record(
    config: AppConfig,
    *,
    tickers: list[str] | None,
    top_n: int,
    duration_s: float | None,
    poly_top: int = 8,
    poly_slugs: list[str] | None = None,
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

        consumers = [asyncio.create_task(supervise(consume, name="kalshi-ws-consume"))]

        # Polymarket US over public REST until WS credentials exist; a failure
        # here never takes Kalshi recording down.
        pm_targets = list(poly_slugs or [])
        if not pm_targets and poly_top > 0:
            try:
                pm_markets = await fetch_active_markets(config, run, sink=recorder.enqueue)
                pm_targets = [m.slug for m in select_poll_targets(pm_markets, poly_top)]
            except Exception:
                log.warning("polymarket_us discovery failed; REST polling disabled", exc_info=True)
        if pm_targets:
            pm_source = PolymarketUSRestSource(config=config, run=run, slugs=pm_targets)
            log.info("polymarket_us: polling %d markets over REST", len(pm_targets))

            async def consume_poly() -> None:
                async for message in pm_source.stream():
                    recorder.enqueue(message)

            consumers.append(
                asyncio.create_task(supervise(consume_poly, name="polymarket-us-poll"))
            )

        try:
            if duration_s is not None:
                await asyncio.sleep(duration_s)
            else:
                await asyncio.gather(*consumers)  # runs until cancelled (Ctrl-C)
        finally:
            for consumer in consumers:
                consumer.cancel()
            for consumer in consumers:
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
