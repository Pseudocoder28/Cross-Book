"""Prometheus metric definitions.

Every metric name in the system is declared here so names stay consistent and
discoverable. Every new failure mode gets a metric.
"""

from prometheus_client import Counter, Gauge, Histogram

# Incremented by the book manager whenever a Book transitions to invalid.
# reason is an arb.book.InvalidReason value.
BOOK_INVALIDATIONS = Counter(
    "arb_book_invalidations_total",
    "Book invalidations by venue and reason",
    labelnames=["venue", "reason"],
)

# Incremented wherever an adapter's parse() raises ParseError. Parse errors
# are counted and logged, never fatal.
PAIRS_EXPIRED_SKIPPED = Counter(
    "arb_pairs_expired_skipped_total",
    "Tracked pairs skipped at load because a venue says the market is over",
    labelnames=["venue"],
)

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

# One-way push delay: local receive wall clock minus the venue's own timestamp
# on the message. It measures true_transit + (local clock - venue clock), so
# the buckets deliberately extend below zero: a delay cannot physically be
# negative, and any count at or below the 0 bucket is proof of a clock offset
# rather than of fast networking. The sample is stored raw — never corrected.
# Note: prometheus_client suppresses the _sum series when the lowest bucket is
# negative, so bucket counts are this histogram's only output — there is no
# arb_ws_one_way_latency_ms_sum to average with.
WS_ONE_WAY_LATENCY_MS = Histogram(
    "arb_ws_one_way_latency_ms",
    "Venue-stamped one-way WebSocket delay (includes local-vs-venue clock offset)",
    labelnames=["venue"],
    buckets=(-100, -50, -25, -10, -5, 0, 5, 10, 25, 50, 100, 250, 500, 1000),
)

# The crisp failure mode: any nonzero rate here means the local clock is
# running behind the venue's, so every one-way reading is biased low.
WS_ONE_WAY_LATENCY_NEGATIVE = Counter(
    "arb_ws_one_way_latency_negative_total",
    "One-way latency samples that came out negative (impossible without clock skew)",
    labelnames=["venue"],
)

# Keepalive round-trip time from the WebSocket transport. Only the local clock
# is involved, so this is immune to skew — the figure to trust when the
# one-way number goes strange.
WS_RTT_MS = Gauge(
    "arb_ws_rtt_ms",
    "WebSocket keepalive round-trip time (skew-immune)",
    labelnames=["venue", "stream"],
)

# Derived estimate: median one-way latency minus rtt/2. Sign convention —
# negative means the LOCAL clock is running behind the venue's.
CLOCK_SKEW_MS = Gauge(
    "arb_clock_skew_ms",
    "Estimated local clock offset from the venue's (negative: local is behind)",
    labelnames=["venue"],
)


# --- UI control plane -------------------------------------------------------
# result is "ok" | "error" | "refused" | "armed" | "read_only" |
# "confirm_invalid" | "unknown": every way a control can end, including the
# ones that are a refusal rather than a failure.
CONTROL_ACTIONS = Counter(
    "arb_control_actions_total",
    "UI control actions by action and outcome",
    labelnames=["action", "result"],
)

# An audit row that could not be written. For a G3 action this also refuses
# the action (fail closed); for a G2 one the action happened and the record of
# it did not, which is exactly the thing worth alerting on.
CONTROL_AUDIT_FAILURES = Counter(
    "arb_control_audit_failures_total",
    "Control-action audit writes that failed",
    labelnames=["action"],
)

CONTROL_JOBS = Counter(
    "arb_control_jobs_total",
    "Control-plane background jobs by name and outcome (ok/error/cancelled)",
    labelnames=["job", "result"],
)

# A job's output buffer is bounded, so a chatty job loses its oldest lines
# rather than the process losing memory. Nonzero means the log is incomplete.
CONTROL_JOB_OUTPUT_DROPPED = Counter(
    "arb_control_job_output_dropped_total",
    "Job output lines dropped from the bounded per-job buffer",
    labelnames=["job"],
)

# Incremented by the UI's origin/host guard when a request is refused. The UI
# has no authentication, so this is the only signal that something off-origin
# tried to drive the control plane.
UI_REQUESTS_REJECTED = Counter(
    "arb_ui_requests_rejected_total",
    "UI requests refused by the origin/host guard",
    labelnames=["scope", "reason"],
)
