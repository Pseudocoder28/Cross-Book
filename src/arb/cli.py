"""arb command-line entry point.

Every command works both locally (``uv run arb ...``) and on the VM
(``docker compose exec app arb ...``).
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvloop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb", description="arb command-line entry point.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check env, keys, clock skew, database, venue reachability and disk",
    )
    record = subparsers.add_parser(
        "record",
        help="stream raw venue market data into Postgres (read-only)",
    )
    record.add_argument(
        "--venue", choices=["kalshi"], default="kalshi", help="venue to record (default: kalshi)"
    )
    record.add_argument(
        "--tickers",
        default=None,
        help="comma-separated market tickers; omit to auto-discover the most liquid",
    )
    record.add_argument(
        "--top",
        type=int,
        default=10,
        help="number of liquid markets to auto-discover (default: 10)",
    )
    record.add_argument(
        "--duration",
        type=float,
        default=None,
        help="seconds to run; omit to run until interrupted",
    )
    return parser


def _run_doctor() -> int:
    from arb.doctor import exit_code, format_results, run_doctor

    results = uvloop.run(run_doctor())
    print(format_results(results))
    return exit_code(results)


def _run_record(args: argparse.Namespace) -> int:
    from arb.config import AppConfig
    from arb.record import run_record

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
    try:
        uvloop.run(
            run_record(
                AppConfig(),
                tickers=tickers,
                top_n=args.top,
                duration_s=args.duration,
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _run_doctor()
    if args.command == "record":
        return _run_record(args)
    print(f"arb: command {args.command!r} is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
