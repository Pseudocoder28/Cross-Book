"""Kalshi WebSocket EventSource.

Composes the shared :class:`~arb.ws.ReconnectingWebSocket` with Kalshi's
signed handshake (headers recomputed per attempt) and orderbook
resubscription on every (re)connect. Yields raw frames only — parsing
happens downstream, after the recorder has the bytes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence

from arb.config import AppConfig
from arb.run import RunContext
from arb.types import RawMessage
from arb.venues.kalshi.auth import load_private_key, ws_auth_headers
from arb.venues.kalshi.ws import subscribe_orderbook_cmd
from arb.ws import Connector, ReconnectingWebSocket, WSConfig, WSConnection, websockets_connector

VENUE = "kalshi"
STREAM = "ws"


class KalshiWSSource:
    """Streams raw orderbook frames for a fixed set of market tickers."""

    def __init__(
        self,
        *,
        config: AppConfig,
        run: RunContext,
        market_tickers: Sequence[str],
        ws_config: WSConfig | None = None,
        connector: Connector | None = None,
    ) -> None:
        if not market_tickers:
            raise ValueError("market_tickers must not be empty")
        if not config.kalshi_api_key_id:
            raise ValueError("kalshi_api_key_id is not configured")
        self._tickers = list(market_tickers)
        self._cmd_id = 0

        if connector is None:
            key_id = config.kalshi_api_key_id
            private_key = load_private_key(config.kalshi_private_key_path)

            async def headers_factory() -> dict[str, str]:
                return ws_auth_headers(key_id=key_id, private_key=private_key)

            connector = websockets_connector(config.kalshi_ws_url, headers_factory=headers_factory)

        self._ws = ReconnectingWebSocket(
            venue=VENUE,
            stream_name=STREAM,
            run=run,
            connector=connector,
            config=ws_config,
            on_connected=self._resubscribe,
        )

    @property
    def venue(self) -> str:
        return VENUE

    @property
    def tickers(self) -> list[str]:
        """The ticker set the next (re)connect will subscribe to."""
        return list(self._tickers)

    def set_tickers(self, market_tickers: Sequence[str]) -> bool:
        """Replace the subscribed ticker set. Returns True if it changed.

        The new set does not take effect until the socket reconnects;
        :meth:`force_resync` is how a caller makes that happen now. Kalshi has
        no unsubscribe-and-add flow we rely on — ``_resubscribe`` reads
        ``self._tickers`` fresh on every connect, so a reconnect *is* the
        apply mechanism.

        What a change COSTS, every time:

        - a full reconnect: the live socket is closed and redialled, so the
          stream stops for the dial plus one backoff sleep (``WSConfig``
          defaults, seconds), and the one-way latency clock restarts;
        - every delta in flight is lost — the books for markets that stay in
          the set are invalidated on the seq gap and are only correct again
          once their fresh snapshot lands;
        - a snapshot burst: Kalshi answers the resubscribe with one full
          ``orderbook_snapshot`` per market in the *whole* set, not just the
          added ones, so the recorder and the parse path see |tickers| big
          frames at once;
        - books for dropped tickers stop updating and age into permanent
          staleness unless the caller also evicts them from the
          :class:`~arb.books.BookManager`.

        Cheap enough to do on a UI click, far too expensive to do per tick.

        This is a plain attribute swap (never an in-place mutation), so a
        concurrently running ``_resubscribe`` sees either the old list or the
        new one, never a half-built one, and nothing blocks the ingest loop.
        """
        tickers = list(market_tickers)
        if not tickers:
            raise ValueError("market_tickers must not be empty")
        if tickers == self._tickers:
            return False
        self._tickers = tickers
        return True

    async def _resubscribe(self, conn: WSConnection) -> None:
        self._cmd_id += 1
        await conn.send(subscribe_orderbook_cmd(self._cmd_id, self._tickers))

    def rtt_ms(self) -> float | None:
        rtt = self._ws.rtt_s()
        return rtt * 1000.0 if rtt is not None else None

    async def force_resync(self) -> None:
        """Arrange fresh snapshots after a detected seq gap.

        Kalshi answers every (re)subscribe with a full ``orderbook_snapshot``
        per market (observed live, docs/venue-notes.md), so forcing a
        reconnect — which resubscribes — recovers all books.
        """
        await self._ws.force_reconnect()

    def stream(self) -> AsyncGenerator[RawMessage, None]:
        return self._ws.stream()
