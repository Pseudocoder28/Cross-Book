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

# REST polling sources (Polymarket US until its WS credentials exist).
# status is the HTTP status code, or "error" for transport failures.
REST_POLLS = Counter(
    "arb_rest_polls_total",
    "REST market-data poll attempts by venue and outcome",
    labelnames=["venue", "status"],
)

REST_RATE_LIMITED = Counter(
    "arb_rest_rate_limited_total",
    "REST polls answered 429 (each one pauses the poller)",
    labelnames=["venue"],
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

# Incremented by a venue adapter when the envelope sequence number skips —
# a subscription-level gap. The venue's books get invalidated (reason
# "seq_gap") and resynced from fresh snapshots.
SEQ_GAPS = Counter(
    "arb_seq_gaps_total",
    "Subscription-level sequence gaps detected by venue adapters",
    labelnames=["venue"],
)

# Number of currently connected terminal-UI WebSocket clients.
UI_WS_CLIENTS = Gauge(
    "arb_ui_ws_clients",
    "Connected terminal-UI WebSocket clients",
)

# Incremented when a UI WebSocket client is dropped because its send queue
# overflowed. A slow UI consumer must never stall market-data ingest.
UI_WS_CLIENTS_DROPPED = Counter(
    "arb_ui_ws_clients_dropped_total",
    "UI WebSocket clients dropped for not keeping up with the send queue",
)
