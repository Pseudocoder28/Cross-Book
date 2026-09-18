"""Reconnecting WebSocket client.

Venue-agnostic: venue code supplies a ``connector`` (which owns the URL and
recomputes auth headers on every attempt — both venues sign a timestamp into
the handshake) and an ``on_connected`` callback that re-subscribes after every
(re)connect. This layer owns reconnect with exponential backoff and jitter,
stall detection, and stamping the :class:`~arb.types.RawMessage` envelope.

Protocol-level ping/pong (the heartbeat) is configured in the default
websockets connector; stall detection here is the second line of defense for
half-open connections where pings survive but data stops. Snapshot refresh
after a gap is the book layer's job (`needs_resync`), not this one's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel
from websockets.asyncio.client import connect as _websockets_connect

from arb.metrics import WS_CONNECT_FAILURES, WS_CONNECTS, WS_DISCONNECTS
from arb.run import RunContext
from arb.supervise import Backoff
from arb.types import RawMessage

log = logging.getLogger(__name__)


class WSConnection(Protocol):
    """The slice of a WebSocket connection this layer needs."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str | bytes) -> None: ...

    async def close(self) -> None: ...


type Connector = Callable[[], Awaitable[WSConnection]]
type OnConnected = Callable[[WSConnection], Awaitable[None]]


def websockets_connector(
    url: str,
    *,
    headers_factory: Callable[[], Awaitable[dict[str, str]]] | None = None,
    ping_interval_s: float = 15.0,
    ping_timeout_s: float = 10.0,
) -> Connector:
    """Build a Connector over the ``websockets`` library.

    ``headers_factory`` runs on every attempt so timestamped auth signatures
    (both venues sign the handshake) are always fresh. Protocol-level
    ping/pong is the heartbeat.
    """

    async def _connect() -> WSConnection:
        headers = await headers_factory() if headers_factory is not None else None
        return await _websockets_connect(
            url,
            additional_headers=headers,
            ping_interval=ping_interval_s,
            ping_timeout=ping_timeout_s,
        )

    return _connect


class WSConfig(BaseModel):
    connect_timeout_s: float = 10.0
    # No inbound frame for this long → assume half-open, reconnect. Must be
    # comfortably above the venue's heartbeat/ping cadence.
    stall_timeout_s: float = 30.0
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 30.0
    backoff_jitter_frac: float = 0.5
    # A connection that lived this long resets the backoff.
    healthy_after_s: float = 60.0


class ReconnectingWebSocket:
    """An :class:`~arb.interfaces.EventSource` over one WebSocket endpoint."""

    def __init__(
        self,
        *,
        venue: str,
        stream_name: str,
        run: RunContext,
        connector: Connector,
        config: WSConfig | None = None,
        on_connected: OnConnected | None = None,
    ) -> None:
        self._venue = venue
        self._stream_name = stream_name
        self._run = run
        self._connector = connector
        self._config = config if config is not None else WSConfig()
        self._on_connected = on_connected
        self._conn: WSConnection | None = None
        # Connections established this run; 1 is a socket that never dropped.
        self.connects = 0

    @property
    def venue(self) -> str:
        return self._venue

    def rtt_s(self) -> float | None:
        """Round-trip time from the transport's last keepalive ping, seconds.

        Immune to clock skew (only the local clock is involved), which makes
        it the trustworthy latency figure when one-way estimates go strange.
        """
        latency = getattr(self._conn, "latency", None)
        if isinstance(latency, int | float) and latency > 0:
            return float(latency)
        return None

    async def force_reconnect(self) -> None:
        """Close the live connection so the stream loop reconnects.

        Reconnecting re-runs ``on_connected`` (the resubscribe), which is how
        a consumer arranges a fresh snapshot after a detected gap. No-op when
        no connection is live (a reconnect is already underway).
        """
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    async def stream(self) -> AsyncGenerator[RawMessage, None]:
        cfg = self._config
        backoff = Backoff(
            initial_s=cfg.backoff_initial_s,
            max_s=cfg.backoff_max_s,
            jitter_frac=cfg.backoff_jitter_frac,
        )
        labels = {"venue": self._venue, "stream": self._stream_name}
        while True:
            try:
                conn = await asyncio.wait_for(self._connector(), timeout=cfg.connect_timeout_s)
            except Exception:
                WS_CONNECT_FAILURES.labels(**labels).inc()
                log.exception("%s/%s: connect failed", self._venue, self._stream_name)
                await asyncio.sleep(backoff.next_delay())
                continue
            WS_CONNECTS.labels(**labels).inc()
            self.connects += 1
            connected_mono = time.monotonic()
            self._conn = conn
            try:
                if self._on_connected is not None:
                    await self._on_connected(conn)
                while True:
                    try:
                        frame = await asyncio.wait_for(conn.recv(), timeout=cfg.stall_timeout_s)
                    except TimeoutError:
                        WS_DISCONNECTS.labels(**labels, reason="stall").inc()
                        log.warning("%s/%s: stalled, reconnecting", self._venue, self._stream_name)
                        break
                    yield RawMessage(
                        venue=self._venue,
                        stream=self._stream_name,
                        payload=frame.encode() if isinstance(frame, str) else bytes(frame),
                        recv_ts_ns=time.time_ns(),
                        recv_mono_ns=time.monotonic_ns(),
                        run_id=self._run.run_id,
                        ingest_seq=self._run.next_ingest_seq(),
                    )
            except Exception as exc:
                WS_DISCONNECTS.labels(**labels, reason=type(exc).__name__).inc()
                log.warning(
                    "%s/%s: connection lost (%s), reconnecting",
                    self._venue,
                    self._stream_name,
                    type(exc).__name__,
                )
            finally:
                self._conn = None
                with contextlib.suppress(Exception):
                    await conn.close()
            if time.monotonic() - connected_mono >= cfg.healthy_after_s:
                backoff.reset()
            await asyncio.sleep(backoff.next_delay())
