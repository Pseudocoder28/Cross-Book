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
    _add_poly_args(record)
    ui = subparsers.add_parser(
        "ui",
        help="serve the live market-data terminal UI (read-only)",
    )
    ui.add_argument(
        "--tickers",
        default=None,
        help="comma-separated market tickers; omit to auto-discover the most liquid",
    )
    ui.add_argument(
        "--top",
        type=int,
        default=8,
        help="number of liquid markets to auto-discover (default: 8)",
    )
    ui.add_argument("--host", default=None, help="bind host (default: UI_HOST or 127.0.0.1)")
    ui.add_argument("--port", type=int, default=None, help="bind port (default: UI_PORT or 8080)")
    ui.add_argument(
        "--no-record",
        action="store_true",
        help="don't write raw messages to Postgres while serving the UI",
    )
    _add_poly_args(ui)
    return parser


def _add_poly_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--poly-top",
        type=int,
        default=8,
        help="Polymarket US markets to poll over public REST (0 disables; default: 8)",
    )
    sub.add_argument(
        "--poly-slugs",
        default=None,
        help="comma-separated Polymarket US market slugs to poll instead of discovery",
    )


def _split(csv: str | None) -> list[str] | None:
    return [t.strip() for t in csv.split(",") if t.strip()] if csv else None


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
                poly_top=args.poly_top,
                poly_slugs=_split(args.poly_slugs),
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


def _run_ui(args: argparse.Namespace) -> int:
    from arb.config import AppConfig
    from arb.ui.server import run_ui

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = AppConfig()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
    try:
        uvloop.run(
            run_ui(
                config,
                tickers=tickers,
                top_n=args.top,
                record=not args.no_record,
                host=args.host if args.host is not None else config.ui_host,
                port=args.port if args.port is not None else config.ui_port,
                poly_top=args.poly_top,
                poly_slugs=_split(args.poly_slugs),
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
    if args.command == "ui":
        return _run_ui(args)
    print(f"arb: command {args.command!r} is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
