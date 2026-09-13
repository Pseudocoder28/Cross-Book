"""Prometheus metric definitions.

Every metric name in the system is declared here so names stay consistent and
discoverable. Every new failure mode gets a metric.
"""

from prometheus_client import Counter, Gauge

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

RECORDER_ENQUEUED = Counter(
    "arb_recorder_enqueued_total",
    "Raw messages accepted onto the recorder queue",
    labelnames=["venue"],
)

RECORDER_DROPPED = Counter(
    "arb_recorder_dropped_total",
    "Raw messages dropped because the recorder queue was full",
    labelnames=["venue"],
)

RECORDER_WRITTEN = Counter(
    "arb_recorder_written_total",
    "Raw messages durably written by the recorder sink",
)

RECORDER_WRITE_FAILURES = Counter(
    "arb_recorder_write_failures_total",
    "Recorder sink write attempts that failed (batch retried in place)",
)

RECORDER_QUEUE_DEPTH = Gauge(
    "arb_recorder_queue_depth",
    "Current recorder queue depth",
)
