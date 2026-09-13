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

WS_CONNECTS = Counter(
    "arb_ws_connects_total",
    "Successful WebSocket connections (including reconnects)",
    labelnames=["venue", "stream"],
)

WS_CONNECT_FAILURES = Counter(
    "arb_ws_connect_failures_total",
    "WebSocket connection attempts that failed before establishment",
    labelnames=["venue", "stream"],
)

# reason is "stall" or the exception class name that dropped the connection.
WS_DISCONNECTS = Counter(
    "arb_ws_disconnects_total",
    "WebSocket disconnects after establishment, by reason",
    labelnames=["venue", "stream", "reason"],
)

SUPERVISOR_RESTARTS = Counter(
    "arb_supervisor_restarts_total",
    "Supervised task restarts (crash or unexpected exit)",
    labelnames=["task"],
)
