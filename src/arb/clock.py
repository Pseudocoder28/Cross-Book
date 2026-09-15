"""Minimal SNTP client for measuring the local clock against a time server.

``arb doctor`` needs millisecond resolution: the venues' HTTP ``Date`` header
is truncated to whole seconds, which hides exactly the tens-of-milliseconds
offsets that make the UI's one-way latency read negative. SNTP (RFC 4330, the
subset of RFC 5905 a client needs) gives sub-millisecond resolution over one
UDP exchange, with stdlib socket/struct and no new dependency.

The wire encode/decode and the offset arithmetic are pure functions so they can
be pinned in tests without a network; only :func:`query_sync` touches a socket.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass

# Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
NTP_EPOCH_OFFSET_S = 2_208_988_800
NTP_PORT = 123

_PACKET_SIZE = 48
# LI=0 (no warning), VN=4, Mode=3 (client).
_CLIENT_HEADER = 0x23
_FRACTION_SCALE = 2**32
_ZERO_TIMESTAMP = b"\x00" * 8


class ClockQueryError(RuntimeError):
    """An SNTP exchange could not be completed or its reply was unusable."""


@dataclass(frozen=True, slots=True)
class ClockSample:
    """One SNTP exchange.

    ``skew_ms`` follows this repo's convention everywhere else (the UI's
    ``clock_skew_ms``, ``docs/ui.md``): local minus server, so a negative value
    means the local clock is running *behind* the server's.
    """

    skew_ms: float
    round_trip_ms: float


def encode_timestamp(unix_ts: float) -> bytes:
    """Encode Unix seconds as a 64-bit NTP fixed-point timestamp."""
    ntp_ts = unix_ts + NTP_EPOCH_OFFSET_S
    seconds = int(ntp_ts)
    fraction = int((ntp_ts - seconds) * _FRACTION_SCALE)
    return struct.pack("!II", seconds & 0xFFFFFFFF, fraction & 0xFFFFFFFF)


def decode_timestamp(raw: bytes) -> float:
    """Decode a 64-bit NTP fixed-point timestamp to Unix seconds."""
    if len(raw) != 8:
        raise ClockQueryError(f"timestamp field is {len(raw)} bytes, want 8")
    seconds, fraction = struct.unpack("!II", raw)
    return seconds - NTP_EPOCH_OFFSET_S + fraction / _FRACTION_SCALE


def encode_request(transmit_ts: float) -> bytes:
    """Build the 48-byte client request, T1 in the Transmit Timestamp field."""
    return _CLIENT_HEADER.to_bytes(1, "big") + bytes(39) + encode_timestamp(transmit_ts)


def decode_response(packet: bytes) -> tuple[float, float, float]:
    """Return (T1 echoed, T2 server receive, T3 server transmit) in Unix seconds."""
    if len(packet) < _PACKET_SIZE:
        raise ClockQueryError(f"short SNTP reply: {len(packet)} bytes")
    mode = packet[0] & 0b111
    if mode != 4:  # 4 = server
        raise ClockQueryError(f"unexpected SNTP mode {mode}")
    if packet[1] == 0:  # stratum 0 is a kiss-o'-death: no time in it
        raise ClockQueryError("kiss-o'-death reply (stratum 0)")
    if packet[32:40] == _ZERO_TIMESTAMP or packet[40:48] == _ZERO_TIMESTAMP:
        raise ClockQueryError("reply carries no server timestamps")
    return (
        decode_timestamp(packet[24:32]),
        decode_timestamp(packet[32:40]),
        decode_timestamp(packet[40:48]),
    )


def sample_from_timestamps(t1: float, t2: float, t3: float, t4: float) -> ClockSample:
    """Turn the four SNTP timestamps into a skew/round-trip sample."""
    # RFC 5905's offset is the correction to APPLY to the local clock, so it is
    # positive when the local clock is behind. This repo reports the opposite
    # sign (local minus venue, negative = behind) so that doctor and the UI's
    # clock_skew_ms read the same for the same reality — hence the negation.
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return ClockSample(skew_ms=-offset * 1e3, round_trip_ms=delay * 1e3)


def best_sample(samples: Sequence[ClockSample]) -> ClockSample:
    """Pick the exchange with the smallest round-trip delay.

    Queueing delay is one-sided noise that only ever inflates the error in the
    offset estimate, so the fastest round trip is the most trustworthy one.
    """
    if not samples:
        raise ClockQueryError("no usable SNTP replies")
    return min(samples, key=lambda sample: sample.round_trip_ms)


def _resolve(server: str) -> str:
    try:
        return socket.gethostbyname(server)
    except OSError as exc:
        raise ClockQueryError(f"cannot resolve {server}: {exc}") from exc


def _udp_socket(timeout_s: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_s)
    return sock


def _exchange(sock: socket.socket, address: tuple[str, int]) -> ClockSample:
    t1 = time.time()
    sock.sendto(encode_request(t1), address)
    reply, _ = sock.recvfrom(_PACKET_SIZE * 2)
    t4 = time.time()
    origin, t2, t3 = decode_response(reply)
    # The originate field is our own T1 echoed back; anything else is a stray
    # or spoofed datagram, not an answer to this request.
    if origin != decode_timestamp(encode_timestamp(t1)):
        raise ClockQueryError("originate timestamp does not echo the request")
    return sample_from_timestamps(origin, t2, t3, t4)


def query_sync(server: str, samples: int = 4, timeout_s: float = 1.5) -> ClockSample:
    """Blocking: run ``samples`` exchanges and keep the lowest-delay one."""
    address = (_resolve(server), NTP_PORT)
    collected: list[ClockSample] = []
    last_error: Exception | None = None
    with _udp_socket(timeout_s) as sock:
        for _ in range(samples):
            try:
                collected.append(_exchange(sock, address))
            except (OSError, ClockQueryError) as exc:
                last_error = exc
    if not collected:
        raise ClockQueryError(
            f"no usable reply from {server}: {type(last_error).__name__}: {last_error}"
        )
    return best_sample(collected)


async def query_clock_offset(server: str, samples: int = 4, timeout_s: float = 1.5) -> ClockSample:
    """Measure the local clock against ``server`` without blocking the loop."""
    return await asyncio.to_thread(query_sync, server, samples, timeout_s)
