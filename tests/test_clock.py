import struct
from typing import Self

import pytest

from arb.clock import (
    NTP_EPOCH_OFFSET_S,
    ClockQueryError,
    ClockSample,
    best_sample,
    decode_response,
    decode_timestamp,
    encode_request,
    encode_timestamp,
    query_sync,
    sample_from_timestamps,
)


def make_response(t1: float, t2: float, t3: float, *, header: int = 0x24) -> bytes:
    """A 48-byte server reply (LI=0, VN=4, Mode=4, stratum 2) with the three timestamps."""
    return (
        bytes([header, 2, 0, 0])
        + bytes(20)
        + encode_timestamp(t1)
        + encode_timestamp(t2)
        + encode_timestamp(t3)
    )


class TestWireFormat:
    def test_timestamp_round_trip(self) -> None:
        for ts in (0.0, 1_757_000_000.5, 1_757_000_000.123456):
            assert decode_timestamp(encode_timestamp(ts)) == pytest.approx(ts, abs=1e-6)

    def test_request_is_a_48_byte_client_packet(self) -> None:
        packet = encode_request(1_757_000_000.25)
        assert len(packet) == 48
        assert packet[0] == 0x23  # LI=0, VN=4, Mode=3
        assert packet[1:40] == bytes(39)
        seconds, _ = struct.unpack("!II", packet[40:48])
        assert seconds == 1_757_000_000 + NTP_EPOCH_OFFSET_S

    def test_response_round_trip(self) -> None:
        t1, t2, t3 = 1_757_000_000.0, 1_757_000_000.032, 1_757_000_000.033
        origin, recv, transmit = decode_response(make_response(t1, t2, t3))
        assert origin == pytest.approx(t1, abs=1e-6)
        assert recv == pytest.approx(t2, abs=1e-6)
        assert transmit == pytest.approx(t3, abs=1e-6)

    def test_malformed_replies_raise_the_typed_error(self) -> None:
        with pytest.raises(ClockQueryError, match="short"):
            decode_response(b"\x24" * 20)
        with pytest.raises(ClockQueryError, match="mode"):
            decode_response(b"\xff" * 48)
        with pytest.raises(ClockQueryError, match="kiss"):
            decode_response(bytes([0x24, 0, 0, 0]) + bytes(44))
        with pytest.raises(ClockQueryError, match="no server timestamps"):
            decode_response(bytes([0x24, 2, 0, 0]) + bytes(44))
        with pytest.raises(ClockQueryError, match="8"):
            decode_timestamp(b"\x00\x00")


class TestOffsetArithmetic:
    def test_local_behind_server_reads_negative(self) -> None:
        # Local clock 27 ms behind the server, 5 ms of true transit each way:
        # this is the exact regression the check exists to catch.
        t1 = 1_757_000_000.000  # local send
        t2 = 1_757_000_000.032  # server receive = local 0.005 + 0.027 skew
        t3 = 1_757_000_000.032  # server transmit (instant turnaround)
        t4 = 1_757_000_000.010  # local receive
        sample = sample_from_timestamps(t1, t2, t3, t4)
        assert sample.skew_ms == pytest.approx(-27.0, abs=1e-3)
        assert sample.round_trip_ms == pytest.approx(10.0, abs=1e-3)

    def test_local_ahead_of_server_reads_positive(self) -> None:
        t1 = 1_757_000_000.000
        t2 = 1_756_999_999.955  # local is 50 ms ahead; 5 ms transit
        t3 = 1_756_999_999.955
        t4 = 1_757_000_000.010
        sample = sample_from_timestamps(t1, t2, t3, t4)
        assert sample.skew_ms == pytest.approx(50.0, abs=1e-3)
        assert sample.round_trip_ms == pytest.approx(10.0, abs=1e-3)

    def test_perfectly_synced_clock_reads_zero(self) -> None:
        sample = sample_from_timestamps(100.0, 100.004, 100.006, 100.010)
        assert sample.skew_ms == pytest.approx(0.0, abs=1e-9)
        assert sample.round_trip_ms == pytest.approx(8.0, abs=1e-6)


class TestBestSample:
    def test_lowest_delay_sample_wins(self) -> None:
        samples = [
            ClockSample(skew_ms=-40.0, round_trip_ms=120.0),
            ClockSample(skew_ms=-27.0, round_trip_ms=9.0),
            ClockSample(skew_ms=-33.0, round_trip_ms=61.0),
        ]
        assert best_sample(samples).skew_ms == -27.0

    def test_no_samples_raises_the_typed_error(self) -> None:
        with pytest.raises(ClockQueryError):
            best_sample([])


class _FakeSocket:
    """Stands in for a UDP socket; ``replies`` is consumed one datagram per recv."""

    def __init__(self, replies: list[bytes | Exception]) -> None:
        self.replies = replies
        self.sent: list[bytes] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        self.sent.append(data)
        return len(data)

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, ("127.0.0.1", 123)


def install_fake_socket(
    monkeypatch: pytest.MonkeyPatch, replies: list[bytes | Exception]
) -> _FakeSocket:
    sock = _FakeSocket(replies)
    monkeypatch.setattr("arb.clock._resolve", lambda server: "127.0.0.1")
    monkeypatch.setattr("arb.clock._udp_socket", lambda timeout_s: sock)
    return sock


class TestQuerySync:
    def test_timeouts_raise_the_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fake_socket(monkeypatch, [TimeoutError("timed out")] * 2)
        with pytest.raises(ClockQueryError, match="no usable reply"):
            query_sync("ntp.invalid", samples=2)

    def test_blocked_port_raises_the_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fake_socket(monkeypatch, [OSError("Network is unreachable")])
        with pytest.raises(ClockQueryError, match="no usable reply"):
            query_sync("ntp.invalid", samples=1)

    def test_garbage_reply_raises_the_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fake_socket(monkeypatch, [b"\xde\xad\xbe\xef"])
        with pytest.raises(ClockQueryError, match="no usable reply"):
            query_sync("ntp.invalid", samples=1)

    def test_reply_that_does_not_echo_our_request_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_socket(monkeypatch, [make_response(1.0, 2.0, 3.0)])
        with pytest.raises(ClockQueryError, match="no usable reply"):
            query_sync("ntp.invalid", samples=1)

    def test_good_exchange_returns_a_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sock = install_fake_socket(monkeypatch, [])

        # Echo our own transmit timestamp back as both server timestamps: the
        # server then appears to sit exactly at our T1, i.e. we are "ahead" by
        # half the round trip.
        def echo(size: int) -> tuple[bytes, tuple[str, int]]:
            sent = decode_timestamp(sock.sent[-1][40:48])
            return make_response(sent, sent, sent), ("127.0.0.1", 123)

        monkeypatch.setattr(sock, "recvfrom", echo)
        sample = query_sync("ntp.invalid", samples=2)
        assert sample.round_trip_ms >= 0.0
        assert sample.skew_ms == pytest.approx(sample.round_trip_ms / 2, abs=1e-3)
