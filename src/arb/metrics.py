"""Prometheus metric definitions.

Every metric name in the system is declared here so names stay consistent and
discoverable. Every new failure mode gets a metric.
"""

from prometheus_client import Counter

# Incremented by the book manager whenever a Book transitions to invalid.
# reason is an arb.book.InvalidReason value.
BOOK_INVALIDATIONS = Counter(
    "arb_book_invalidations_total",
    "Book invalidations by venue and reason",
    labelnames=["venue", "reason"],
)

# Incremented wherever an adapter's parse() raises ParseError. Parse errors
# are counted and logged, never fatal.
PARSE_ERRORS = Counter(
    "arb_parse_errors_total",
    "Malformed venue payloads rejected by adapters",
    labelnames=["venue", "stream"],
)
