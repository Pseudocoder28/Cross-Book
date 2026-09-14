"""REST poller: pacing, round-robin, 429 pause, error backoff — with a fake
transport and a recorded fake sleep (no network, no real waiting)."""

from contextlib import aclosing

import pytest

from arb.config import AppConfig
from arb.run import RunContext
from arb.venues.polymarket_us.source import RATE_LIMIT_PAUSE_S, PolymarketUSRestSource


def config() -> AppConfig:
    return AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]


class Harness:
    def __init__(self, script: list[tuple[int, bytes] | Exception]) -> None:
        self.script = list(script)
        self.urls: list[str] = []
        self.sleeps: list[float] = []

    async def fetch(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


async def collect(source: PolymarketUSRestSource, n: int) -> list[bytes]:
    out: list[bytes] = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            out.append(message.payload)
            if len(out) >= n:
                break
    return out


async def test_round_robin_paced_polling() -> None:
    h = Harness([(200, b"a"), (200, b"b"), (200, b"c")])
    source = PolymarketUSRestSource(
        config=config(),
        run=RunContext(run_id="r"),
        slugs=["one", "two"],
        rate_per_s=4.0,
        fetch=h.fetch,
        sleep=h.sleep,
    )
    payloads = await collect(source, 3)
    assert payloads == [b"a", b"b", b"c"]
    assert [u.rsplit("/", 2)[1] for u in h.urls] == ["one", "two", "one"]
    # Each poll is followed by a pacing sleep of at most one interval (0.25s).
    assert len(h.sleeps) >= 2 and all(0 < s <= 0.25 for s in h.sleeps)
    assert source.polls == 3 and source.rate_limited == 0
    assert source.venue == "polymarket_us"


async def test_429_pauses_polling_and_counts() -> None:
    h = Harness([(429, b"{}"), (200, b"ok")])
    source = PolymarketUSRestSource(
        config=config(), run=RunContext(), slugs=["x"], rate_per_s=4.0, fetch=h.fetch, sleep=h.sleep
    )
    payloads = await collect(source, 1)
    assert payloads == [b"ok"]
    assert source.rate_limited == 1
    assert h.sleeps[0] == RATE_LIMIT_PAUSE_S  # the documented-by-measurement cooldown


async def test_transport_error_backs_off_and_continues() -> None:
    h = Harness([OSError("boom"), (200, b"ok")])
    source = PolymarketUSRestSource(
        config=config(), run=RunContext(), slugs=["x"], rate_per_s=4.0, fetch=h.fetch, sleep=h.sleep
    )
    assert await collect(source, 1) == [b"ok"]
    assert source.errors == 1
    assert h.sleeps[0] >= 0.5  # backoff, not a pacing sleep


def test_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        PolymarketUSRestSource(config=config(), run=RunContext(), slugs=[])
    with pytest.raises(ValueError):
        PolymarketUSRestSource(config=config(), run=RunContext(), slugs=["x"], rate_per_s=0)
