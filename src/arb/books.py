"""Venue-agnostic book manager.

Owns one :class:`~arb.book.Book` per market (created lazily), applies the
normalized event stream from adapters and reports which books changed so the
UI (or any consumer) knows what to re-publish. Metrics live here, not in the
pure ``Book``.

Market ids follow the shared ``"<venue>:<native-id>"`` convention (see the
``market_id`` helpers in each venue package), which is how a venue-wide
:class:`~arb.interfaces.ResyncRequired` finds its books.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from arb.book import Book, BookSnapshot, BookStatus, InvalidReason
from arb.interfaces import BookEvent, ResyncRequired
from arb.metrics import BOOK_INVALIDATIONS


def venue_of(market_id: str) -> str:
    """Venue prefix of a normalized ``"<venue>:<native-id>"`` market id."""
    return market_id.split(":", 1)[0]


class BookManager:
    def __init__(self, *, staleness_limit_ns: int) -> None:
        self._staleness_limit_ns = staleness_limit_ns
        self._books: dict[str, Book] = {}
        self._last_apply_mono_ns: dict[str, int] = {}

    @property
    def books(self) -> Mapping[str, Book]:
        """Live read view of all books by market id. Do not mutate."""
        return self._books

    def get(self, market_id: str) -> Book | None:
        return self._books.get(market_id)

    def status(self, market_id: str, *, now_mono_ns: int) -> BookStatus | None:
        book = self._books.get(market_id)
        return book.status(now_mono_ns=now_mono_ns) if book is not None else None

    def last_update_mono_ns(self, market_id: str) -> int | None:
        """Monotonic time of the last event applied to this market's book."""
        return self._last_apply_mono_ns.get(market_id)

    def apply(self, events: Sequence[BookEvent], *, mono_ns: int) -> set[str]:
        """Apply normalized events; returns the market ids whose book changed.

        Books are created lazily. A :class:`ResyncRequired` marks the venue's
        books (all of them when ``market_ids`` is None) invalid with reason
        ``seq_gap``; they stay invalid until their next snapshot.
        """
        changed: set[str] = set()
        for event in events:
            if isinstance(event, ResyncRequired):
                targets = (
                    [mid for mid in self._books if venue_of(mid) == event.venue]
                    if event.market_ids is None
                    else [mid for mid in event.market_ids if mid in self._books]
                )
                for mid in targets:
                    self._books[mid].mark_invalid(InvalidReason.SEQ_GAP)
                    BOOK_INVALIDATIONS.labels(
                        venue=event.venue, reason=InvalidReason.SEQ_GAP.value
                    ).inc()
                    changed.add(mid)
                continue

            mid = event.market_id
            book = self._books.get(mid)
            if book is None:
                book = Book(mid, staleness_limit_ns=self._staleness_limit_ns)
                self._books[mid] = book
            if isinstance(event, BookSnapshot):
                status = book.apply_snapshot(event, mono_ns=mono_ns)
            else:
                if book.needs_resync:
                    # The book ignores level updates while awaiting a fresh
                    # snapshot; nothing changed, nothing to count.
                    continue
                status = book.apply_level(event, mono_ns=mono_ns)
            self._last_apply_mono_ns[mid] = mono_ns
            changed.add(mid)
            if not status.valid and status.reason is not None:
                BOOK_INVALIDATIONS.labels(venue=venue_of(mid), reason=status.reason.value).inc()
        return changed
