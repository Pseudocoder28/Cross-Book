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
    ui.add_argument(
        "--pairs-top",
        type=int,
        default=10,
        help="confirmed pairs (by score) to track on the ARB screen (default: 10)",
    )
    _add_paper_args(ui)

    replay = subparsers.add_parser("replay", help="replay a recorded run through the pipeline")
    replay.add_argument("run_id", nargs="?", default=None, help="run id (default: latest)")
    replay.add_argument(
        "--pairs-top", type=int, default=10, help="confirmed pairs to quote (0 = none)"
    )
    _add_paper_args(replay)
    replay.add_argument(
        "--persist", action="store_true", help="store paper trades as replay:<run_id>"
    )

    pairs = subparsers.add_parser("pairs", help="cross-venue pair matching (propose / review)")
    pairs_sub = pairs.add_subparsers(dest="pairs_command")
    propose = pairs_sub.add_parser("propose", help="fetch both universes and propose pairs")
    propose.add_argument("--min-score", type=float, default=0.75)
    propose.add_argument("--no-record", action="store_true")
    listing = pairs_sub.add_parser("list", help="list stored pairs")
    listing.add_argument("--status", choices=["proposed", "confirmed", "rejected"], default=None)
    for name in ("confirm", "reject"):
        sub = pairs_sub.add_parser(name, help=f"{name} a proposed pair by id")
        sub.add_argument("pair_id", type=int)
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


def _add_paper_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--paper", action="store_true", help="simulate fills on measured edges")
    sub.add_argument(
        "--min-net-ticks", type=int, default=50, help="paper: min net edge per contract (ticks)"
    )
    sub.add_argument(
        "--max-cts-per-pair", type=int, default=100, help="paper: max contracts per pair"
    )
    sub.add_argument(
        "--max-notional", type=float, default=1000.0, help="paper: max total cost in dollars"
    )


def _paper_limits(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from arb.paper import PaperLimits

    return PaperLimits(
        min_net_ticks=args.min_net_ticks,
        max_qty_per_pair=args.max_cts_per_pair * 10_000,
        max_notional_ticks=int(args.max_notional * 10_000),
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
                pairs_top=args.pairs_top,
                paper=args.paper,
                paper_limits=_paper_limits(args),
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


def _run_replay(args: argparse.Namespace) -> int:
    from arb.config import AppConfig
    from arb.replay import run_replay

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    report = uvloop.run(
        run_replay(
            AppConfig(),
            args.run_id,
            pairs_top=args.pairs_top,
            paper=args.paper,
            limits=_paper_limits(args),
            persist=args.persist,
        )
    )
    print(report.summary())
    return 0


def _run_pairs(args: argparse.Namespace) -> int:
    from arb.config import AppConfig
    from arb.pairs import run as pairs_run
    from arb.pairs.store import decide, list_pairs
    from arb.storage.db import make_engine

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = AppConfig()
    if args.pairs_command == "propose":
        candidates = uvloop.run(
            pairs_run.propose(config, min_score=args.min_score, record=not args.no_record)
        )
        print(pairs_run.format_candidates(candidates))
        return 0

    async def with_engine(coro_fn):  # type: ignore[no-untyped-def]
        engine = make_engine(config.database_url)
        try:
            return await coro_fn(engine)
        finally:
            await engine.dispose()

    if args.pairs_command == "list":
        rows = uvloop.run(with_engine(lambda e: list_pairs(e, status=args.status)))
        print(pairs_run.format_rows(rows))
        return 0
    if args.pairs_command in ("confirm", "reject"):
        status = "confirmed" if args.pairs_command == "confirm" else "rejected"
        row = uvloop.run(with_engine(lambda e: decide(e, args.pair_id, status)))
        if row is None:
            print(f"pair {args.pair_id} not found", file=sys.stderr)
            return 1
        print(pairs_run.format_rows([row]))
        return 0
    print("usage: arb pairs {propose|list|confirm|reject}", file=sys.stderr)
    return 2


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
    if args.command == "pairs":
        return _run_pairs(args)
    if args.command == "replay":
        return _run_replay(args)
    print(f"arb: command {args.command!r} is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
