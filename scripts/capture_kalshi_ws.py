"""Capture real Kalshi WS orderbook frames into a fixture file you name.

Usage:
  uv run python scripts/capture_kalshi_ws.py OUT.jsonl
  uv run python scripts/capture_kalshi_ws.py OUT.jsonl --require-side no --seconds 300
  uv run python scripts/capture_kalshi_ws.py \\
      tests/fixtures/kalshi/ws_orderbook_capture.jsonl --overwrite

OUT is required and an existing file is never replaced without `--overwrite`:
the captures under tests/fixtures/kalshi/ are what the parser tests pin, so
re-capturing over one is a decision, not a default. Arguments are parsed and
OUT is checked before config, keys or the network are touched, so `--help`,
a bare invocation and a refused OUT all exit without connecting.

Read-only: subscribes to public orderbook data over the authenticated
WebSocket. Requires KALSHI_API_KEY_ID and the private key file (see
.env.example). Never prints or logs key material.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path, help="file to write, one raw frame per line")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="replace OUT if it exists (a tracked fixture is pinned by the parser tests)",
    )
    ap.add_argument(
        "--seconds", type=float, default=90.0, help="give up after this long (default: 90)"
    )
    ap.add_argument(
        "--min-deltas",
        type=int,
        default=15,
        help="keep going until this many deltas have arrived (default: 15)",
    )
    ap.add_argument(
        "--top", type=int, default=5, help="subscribe to this many liquid markets (default: 5)"
    )
    ap.add_argument(
        "--require-side",
        choices=("yes", "no"),
        help='keep going until a delta on this side is seen ("no" is rarer: it is a '
        "NO-bid change, i.e. a YES-ask change after complement)",
    )
    return ap


async def capture(
    *, seconds: float, min_deltas: int, top_n: int, require_side: str | None
) -> tuple[list[bytes], dict[str, int], set[str]]:
    # Imported here, not at module level: nothing that can read a key or open
    # a socket is in the process until the arguments have been accepted.
    from websockets.asyncio.client import connect

    from arb.config import AppConfig
    from arb.run import RunContext
    from arb.venues.kalshi.auth import load_private_key, ws_auth_headers
    from arb.venues.kalshi.discovery import fetch_liquid_tickers
    from arb.venues.kalshi.ws import subscribe_orderbook_cmd

    config = AppConfig()
    if not config.kalshi_api_key_id:
        raise SystemExit("KALSHI_API_KEY_ID not set")
    private_key = load_private_key(config.kalshi_private_key_path)

    targets = await fetch_liquid_tickers(config, RunContext(), top_n=top_n)
    if not targets:
        raise SystemExit("no liquid markets found to subscribe to")
    print(f"subscribing to {len(targets)} markets: {targets}")

    headers = ws_auth_headers(key_id=config.kalshi_api_key_id, private_key=private_key)
    counts: dict[str, int] = {}
    snapshot_tickers: set[str] = set()
    captured: list[bytes] = []
    deltas = 0
    seen_required = False
    deadline = time.monotonic() + seconds

    async with connect(config.kalshi_ws_url, additional_headers=headers) as ws:
        await ws.send(subscribe_orderbook_cmd(1, targets))
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
            except TimeoutError:
                break
            data = frame.encode() if isinstance(frame, str) else bytes(frame)
            captured.append(data)
            doc = json.loads(data)
            frame_type = str(doc.get("type", "?"))
            counts[frame_type] = counts.get(frame_type, 0) + 1
            if frame_type == "orderbook_snapshot":
                snapshot_tickers.add(doc["msg"]["market_ticker"])
            elif frame_type == "orderbook_delta":
                deltas += 1
                if doc["msg"].get("side") == require_side:
                    seen_required = True
            if (
                len(snapshot_tickers) >= len(targets)
                and deltas >= min_deltas
                and (require_side is None or seen_required)
            ):
                break

    return captured, counts, snapshot_tickers


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    out: Path = args.out
    # Both refusals come before the capture, not after it: a 90 s capture that
    # cannot be written is lost, and one that should not be written is worse.
    if out.exists() and not args.overwrite:
        ap.error(f"{out} exists; pass --overwrite to replace it")
    if not out.parent.is_dir():
        ap.error(f"{out.parent} is not a directory")

    captured, counts, snapshot_tickers = asyncio.run(
        capture(
            seconds=args.seconds,
            min_deltas=args.min_deltas,
            top_n=args.top,
            require_side=args.require_side,
        )
    )
    # "xb" keeps the promise even if OUT appeared while the capture ran.
    with out.open("wb" if args.overwrite else "xb") as fh:
        fh.write(b"\n".join(captured) + b"\n")
    print(f"captured {sum(counts.values())} frames: {counts}")
    print(f"snapshots for: {sorted(snapshot_tickers)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
