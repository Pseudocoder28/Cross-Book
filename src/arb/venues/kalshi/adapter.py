"""Kalshi :class:`~arb.interfaces.MarketDataAdapter`.

Wraps the WS parser with subscription-level gap detection. The envelope
``seq`` is per-subscription (``sid``) and shared across every market in it
(observed live, docs/venue-notes.md), so per-market seq contiguity does NOT
hold: the adapter tracks ``seq`` per ``sid`` and hands books *unsequenced*
events (``seq=None``). On a gap it emits
:class:`~arb.interfaces.ResyncRequired` for the whole venue before the
normally-parsed event, counts ``arb_seq_gaps_total`` and adopts the new seq.
"""

from __future__ import annotations

import dataclasses
import json

from arb.interfaces import BookEvent, ParseError, ResyncRequired
from arb.metrics import SEQ_GAPS
from arb.types import RawMessage
from arb.venues.kalshi.ws import book_events_from_doc

VENUE = "kalshi"

_SEQUENCED_TYPES = frozenset({"orderbook_snapshot", "orderbook_delta"})


class KalshiMarketDataAdapter:
    """Turns one raw Kalshi WS frame into zero or more normalized events."""

    def __init__(self) -> None:
        self._last_seq_by_sid: dict[int, int] = {}
        # Exchange ts of the last parsed orderbook_delta (ms since epoch),
        # None when the last parsed frame was not a delta. Lets the caller
        # measure push latency without re-parsing the payload.
        self.last_delta_ts_ms: int | None = None

    @property
    def venue(self) -> str:
        return VENUE

    def parse(self, raw: RawMessage) -> list[BookEvent]:
        """Raises :class:`ParseError` on malformed payloads."""
        self.last_delta_ts_ms = None
        try:
            doc = json.loads(raw.payload)
        except ValueError as exc:
            raise ParseError(f"kalshi ws message: {exc}") from exc
        parsed = book_events_from_doc(doc)
        if not parsed:
            return []

        events: list[BookEvent] = []
        if doc.get("type") in _SEQUENCED_TYPES:
            sid = doc.get("sid")
            seq = doc.get("seq")
            if not isinstance(sid, int) or not isinstance(seq, int):
                raise ParseError(f"kalshi ws envelope missing sid/seq: {sid!r}/{seq!r}")
            last = self._last_seq_by_sid.get(sid)
            if last is not None and seq > last + 1:
                # Gap taints every market in the subscription — seq is
                # subscription-scoped, so we can't tell which market missed.
                SEQ_GAPS.labels(venue=VENUE).inc()
                events.append(ResyncRequired(VENUE, None))
            # seq <= last is NOT a gap: it is a new subscription. Kalshi
            # restarts at sid=1, seq=1 on every connection (observed live,
            # docs/venue-notes.md) and opens it with a full snapshot per
            # market, so nothing was missed. Reading that restart as a gap
            # asked for a resync — which reconnects, which restarts at seq=1,
            # which read as a gap: after the first reconnect of a run the
            # socket never stayed up again, and every Kalshi book sat
            # untrusted until the process was restarted.
            self._last_seq_by_sid[sid] = seq
            if doc.get("type") == "orderbook_delta":
                ts_ms = doc["msg"].get("ts_ms")
                if isinstance(ts_ms, int):
                    self.last_delta_ts_ms = ts_ms

        # Books get unsequenced events; gap detection happened above.
        events.extend(dataclasses.replace(event, seq=None) for event in parsed)
        return events
