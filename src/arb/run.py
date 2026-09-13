"""Run identity: every process run gets a ``run_id`` and a per-run ingest
sequence shared by all sources, stamped onto every :class:`~arb.types.RawMessage`."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def new_run_id() -> str:
    """Sortable, unique run identifier: UTC timestamp + random suffix."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


class RunContext:
    """Allocates the per-run, cross-source monotonically increasing ingest_seq.

    Single-event-loop use only; not thread safe.
    """

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id if run_id is not None else new_run_id()
        self._next_seq = 0

    def next_ingest_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq
