"""Kalshi adapter tests against the real captured WS frames (never invented).

The adapter owns subscription-level gap detection: the envelope ``seq`` is
per-``sid`` and shared across every market in the subscription, so books
receive unsequenced events and a gap taints the whole venue.
"""

import json
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

from arb.book import BookLevelUpdate, BookSnapshot
from arb.interfaces import ParseError, ResyncRequired
from arb.types import RawMessage
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture.jsonl"


def frames() -> list[bytes]:
    return [line for line in FIXTURE.read_bytes().split(b"\n") if line.strip()]


def raw(payload: bytes) -> RawMessage:
    return RawMessage(
        venue="kalshi",
        stream="ws",
        payload=payload,
        recv_ts_ns=1,
        recv_mono_ns=1,
        run_id="testrun",
        ingest_seq=0,
    )


def seq_gap_count() -> float:
    return REGISTRY.get_sample_value("arb_seq_gaps_total", {"venue": "kalshi"}) or 0.0


def test_venue() -> None:
    assert KalshiMarketDataAdapter().venue == "kalshi"


def test_contiguous_capture_yields_no_resyncs_and_unsequenced_events() -> None:
    adapter = KalshiMarketDataAdapter()
    before = seq_gap_count()
    events = [event for frame in frames() for event in adapter.parse(raw(frame))]
    assert not any(isinstance(e, ResyncRequired) for e in events)
    assert seq_gap_count() == before
    snapshots = [e for e in events if isinstance(e, BookSnapshot)]
    deltas = [e for e in events if isinstance(e, BookLevelUpdate)]
    assert len(snapshots) == 5
    assert len(deltas) == 15
    # seq is subscription-scoped: books must receive unsequenced events.
    assert all(e.seq is None for e in snapshots)
    assert all(e.seq is None for e in deltas)


def test_non_book_frame_yields_no_events() -> None:
    adapter = KalshiMarketDataAdapter()
    assert frames()[0].startswith(b'{"type":"subscribed"')  # the subscribe ack
    assert adapter.parse(raw(frames()[0])) == []


def test_seq_gap_emits_resync_before_event_and_adopts_new_seq() -> None:
    adapter = KalshiMarketDataAdapter()
    all_frames = frames()
    delta_indexes = [
        i for i, frame in enumerate(all_frames) if b'"type":"orderbook_delta"' in frame
    ]
    skipped = delta_indexes[1]  # drop one mid-stream delta → seq skips by one
    first_after_gap = delta_indexes[2]
    before = seq_gap_count()
    resyncs = 0
    for i, frame in enumerate(all_frames):
        if i == skipped:
            continue
        events = adapter.parse(raw(frame))
        gap_events = [e for e in events if isinstance(e, ResyncRequired)]
        resyncs += len(gap_events)
        if i == first_after_gap:
            # ResyncRequired for the whole venue, then the parsed event.
            assert [type(e) for e in events] == [ResyncRequired, BookLevelUpdate]
            assert events[0] == ResyncRequired("kalshi", None)
    # The new seq was adopted: exactly one gap for one dropped frame.
    assert resyncs == 1
    assert seq_gap_count() == before + 1


def test_last_delta_ts_ms_tracks_exchange_timestamp() -> None:
    adapter = KalshiMarketDataAdapter()
    snapshot_frame = next(f for f in frames() if b'"type":"orderbook_snapshot"' in f)
    delta_frame = next(f for f in frames() if b'"type":"orderbook_delta"' in f)
    adapter.parse(raw(snapshot_frame))
    assert adapter.last_delta_ts_ms is None
    adapter.parse(raw(delta_frame))
    assert adapter.last_delta_ts_ms == json.loads(delta_frame)["msg"]["ts_ms"]
    adapter.parse(raw(frames()[0]))  # non-book frame clears it
    assert adapter.last_delta_ts_ms is None


def test_malformed_payloads_raise_parse_error() -> None:
    adapter = KalshiMarketDataAdapter()
    with pytest.raises(ParseError):
        adapter.parse(raw(b"not json"))
    with pytest.raises(ParseError):
        adapter.parse(raw(b'{"type": "orderbook_delta"}'))
    # A book frame stripped of its envelope sid must fail loudly — without it
    # gap detection would silently degrade.
    doc = json.loads(next(f for f in frames() if b'"type":"orderbook_snapshot"' in f))
    del doc["sid"]
    with pytest.raises(ParseError):
        adapter.parse(raw(json.dumps(doc).encode()))


def test_a_reconnect_restarting_at_seq_one_is_not_a_gap() -> None:
    """Kalshi restarts at sid=1, seq=1 on every connection (recorded live:
    every `subscribed` frame of a run is followed by snapshot seq 1, 2, ...).

    The adapter outlives the connection, so it used to compare that seq=1
    against the old connection's last seq, call it a gap and ask for a resync.
    A resync IS a reconnect, which restarts at seq=1 again: from the first
    reconnect of a run onward the socket was torn down within a second of
    every connect, forever, and every Kalshi book stayed marked seq_gap."""
    adapter = KalshiMarketDataAdapter()
    all_frames = frames()
    before = seq_gap_count()
    for frame in all_frames:  # the first connection, to its end
        adapter.parse(raw(frame))
    resyncs = 0
    for _ in range(3):  # three reconnects: the same stream from seq=1 again
        for frame in all_frames:
            resyncs += sum(isinstance(e, ResyncRequired) for e in adapter.parse(raw(frame)))
    assert resyncs == 0
    assert seq_gap_count() == before
