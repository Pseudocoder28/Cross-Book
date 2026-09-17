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

from collections.abc import Iterable, Mapping, Sequence

from arb.book import Book, BookLevelUpdate, BookSnapshot, BookStatus, InvalidReason, UpdateMode
from arb.interfaces import BookEvent, ResyncRequired
from arb.metrics import BOOK_INVALIDATIONS
from arb.types import BookSide


def venue_of(market_id: str) -> str:
    """Venue prefix of a normalized ``"<venue>:<native-id>"`` market id."""
    return market_id.split(":", 1)[0]


def level_deltas(before: Book | None, after: BookSnapshot) -> list[BookLevelUpdate]:
    """Level changes between a book's current ladders and an incoming snapshot.

    Poll-based venues only ever deliver full snapshots; diffing consecutive
    ones yields the same DELTA events a streaming venue would, so the tape
    and downstream consumers see one shape. Best levels come first.
    """
    out: list[BookLevelUpdate] = []
    sides = (
        (BookSide.BID, before.bids() if before is not None else (), after.bids, True),
        (BookSide.ASK, before.asks() if before is not None else (), after.asks, False),
    )
    for side, old_levels, new_levels, best_high in sides:
        old = {level.price: level.qty for level in old_levels}
        new = {level.price: level.qty for level in new_levels}
        for price in sorted(set(old) | set(new), reverse=best_high):
            change = new.get(price, 0) - old.get(price, 0)
            if change:
                out.append(
                    BookLevelUpdate(
                        market_id=after.market_id,
                        side=side,
                        price=price,
                        qty=change,
                        mode=UpdateMode.DELTA,
                        seq=None,
                    )
                )
    return out


def _retune(book: Book, limit_ns: int) -> None:
    """Change a live book's staleness budget in place.

    ``Book`` captures the limit at construction and exposes no setter, and
    ``arbmon``/the UI call ``book.status()`` directly rather than through this
    manager, so a manager-side override would not reach them. Rebuilding the
    book would throw away its ladders and sequence state — a real outage in
    exchange for a cosmetic one. So: one documented private write, in one
    place. See followups: ``Book`` should grow a public setter.
    """
    book._staleness_limit_ns = limit_ns


class BookManager:
    def __init__(self, *, staleness_limit_ns: int) -> None:
        self._staleness_limit_ns = staleness_limit_ns
        self._staleness_by_venue: dict[str, int] = {}
        self._books: dict[str, Book] = {}
        self._last_apply_mono_ns: dict[str, int] = {}

    def set_venue_staleness(self, venue: str, limit_ns: int) -> set[str]:
        """Set a venue's staleness budget, live. Returns the books retuned.

        A polled venue refreshes each book once per poll cycle, so the
        streaming default would flag every book stale between polls — and the
        cycle length changes whenever the target count or the poll rate does.
        The new budget therefore applies to books that ALREADY exist as well
        as to ones created from now on; otherwise growing the poll universe
        leaves every live book flapping STALE against a budget computed for a
        smaller one, which is exactly the bug this used to have.
        """
        if limit_ns <= 0:
            raise ValueError("limit_ns must be positive")
        self._staleness_by_venue[venue] = limit_ns
        retuned = {mid for mid in self._books if venue_of(mid) == venue}
        for mid in retuned:
            _retune(self._books[mid], limit_ns)
        return retuned

    def evict(self, market_ids: Iterable[str]) -> set[str]:
        """Forget these books entirely. Returns the ids actually removed.

        A book whose market is no longer subscribed stops updating, ages into
        permanent staleness and is re-sent to every newly connected UI client
        forever, because nothing else ever removes it. Whoever changes a
        venue's universe owns calling this for what it dropped.

        Unknown ids are ignored, so this is safe to call with the full "old
        universe" set.
        """
        removed: set[str] = set()
        for mid in market_ids:
            if self._books.pop(mid, None) is not None:
                removed.add(mid)
            self._last_apply_mono_ns.pop(mid, None)
        return removed

    def retain(self, market_ids: Iterable[str], *, venue: str | None = None) -> set[str]:
        """Evict every book NOT in ``market_ids``. Returns the ids removed.

        With ``venue`` set, only that venue's books are candidates — which is
        what a per-venue universe change wants, since changing the Kalshi
        subscription must not evict Polymarket's books.
        """
        keep = set(market_ids)
        doomed = [
            mid
            for mid in self._books
            if mid not in keep and (venue is None or venue_of(mid) == venue)
        ]
        return self.evict(doomed)

    def staleness_limit_ns(self, market_id: str) -> int:
        return self._staleness_by_venue.get(venue_of(market_id), self._staleness_limit_ns)

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
                book = Book(mid, staleness_limit_ns=self.staleness_limit_ns(mid))
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
