"""Polymarket US REST-polling EventSource.

Until WebSocket credentials exist, books come from the public gateway's
``GET /v1/markets/{slug}/book``, polled round-robin. The documented budget is
20 req/s per IP; measured, the endpoint behaves like a 5-token bucket
refilling one token per ~2 s — exactly five requests succeed, the sixth is a
429, at any spacing under that (venue-notes). So the sustainable rate is
~0.5 req/s; one 429 pauses polling for ``RATE_LIMIT_PAUSE_S`` (a full
refill); transport errors back off. Yields raw bytes only — parsing happens
downstream, after the recorder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

import httpx

from arb.config import AppConfig
from arb.metrics import REST_POLLS, REST_RATE_LIMITED
from arb.run import RunContext
from arb.supervise import Backoff
from arb.types import RawMessage

log = logging.getLogger(__name__)

VENUE = "polymarket_us"
STREAM = "rest:book"
RATE_LIMIT_PAUSE_S = 10.0

type Fetch = Callable[[str], Awaitable[tuple[int, bytes]]]
type Sleep = Callable[[float], Awaitable[None]]


class PolymarketUSRestSource:
    def __init__(
        self,
        *,
        config: AppConfig,
        run: RunContext,
        slugs: Sequence[str],
        rate_per_s: float | None = None,
        fetch: Fetch | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not slugs:
            raise ValueError("slugs must not be empty")
        rate = rate_per_s if rate_per_s is not None else config.polymarket_us_poll_rate
        if rate <= 0:
            raise ValueError("rate_per_s must be positive")
        self._base = config.polymarket_us_gateway_base
        self._run = run
        self._slugs = list(slugs)
        self._interval_s = 1.0 / rate
        self._fetch = fetch
        self._sleep = sleep
        self.rate_per_s = rate
        self.polls = 0
        self.rate_limited = 0
        self.errors = 0
        self.last_ok_mono_ns: int | None = None

    @property
    def venue(self) -> str:
        return VENUE

    @property
    def targets(self) -> list[str]:
        return list(self._slugs)

    async def stream(self) -> AsyncGenerator[RawMessage, None]:
        client: httpx.AsyncClient | None = None
        fetch = self._fetch
        if fetch is None:
            client = httpx.AsyncClient(timeout=10)

            async def http_fetch(url: str) -> tuple[int, bytes]:
                assert client is not None
                response = await client.get(url)
                return response.status_code, response.content

            fetch = http_fetch
        backoff = Backoff(initial_s=1.0, max_s=30.0)
        index = 0
        try:
            while True:
                slug = self._slugs[index % len(self._slugs)]
                index += 1
                started = time.monotonic()
                try:
                    status, body = await fetch(f"{self._base}/v1/markets/{slug}/book")
                except Exception:
                    self.errors += 1
                    REST_POLLS.labels(venue=VENUE, status="error").inc()
                    log.warning("%s: poll failed for %s", VENUE, slug, exc_info=True)
                    await self._sleep(backoff.next_delay())
                    continue
                self.polls += 1
                REST_POLLS.labels(venue=VENUE, status=str(status)).inc()
                if status == 429:
                    self.rate_limited += 1
                    REST_RATE_LIMITED.labels(venue=VENUE).inc()
                    log.warning("%s: rate limited; pausing %.0fs", VENUE, RATE_LIMIT_PAUSE_S)
                    await self._sleep(RATE_LIMIT_PAUSE_S)
                    continue
                if status == 200:
                    backoff.reset()
                    self.last_ok_mono_ns = time.monotonic_ns()
                    yield RawMessage(
                        venue=VENUE,
                        stream=STREAM,
                        payload=body,
                        recv_ts_ns=time.time_ns(),
                        recv_mono_ns=time.monotonic_ns(),
                        run_id=self._run.run_id,
                        ingest_seq=self._run.next_ingest_seq(),
                    )
                else:
                    log.warning("%s: HTTP %d for %s", VENUE, status, slug)
                remaining = self._interval_s - (time.monotonic() - started)
                if remaining > 0:
                    await self._sleep(remaining)
        finally:
            if client is not None:
                await client.aclose()
