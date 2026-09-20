"""arb command-line entry point.

Three commands, and only three, because the UI control plane
(:mod:`arb.ui.control`) is how this system is operated: recording, paper
trading, the market universe, pair proposal/backfill/review and replay are all
buttons now. What is left is what a button cannot be.

- ``arb ui`` starts the server the buttons live in. Its flags are *starting*
  values for things the UI then changes at runtime; ``docker-compose.yml``
  launches the container with ``arb ui --top 20 --host 0.0.0.0``.
- ``arb doctor`` is what you run when the UI will **not** start. A button
  inside a dead server diagnoses nothing.
- ``arb replay`` is a **machine interface**: the UI's ``jobs.replay`` action
  spawns it as a subprocess (``python -m arb.cli replay [RUN_ID] --pairs-top N
  [--paper] [--persist]``, built in ``ControlPlane._apply_replay``) because
  replay's per-row loop has no ``await`` and would stall the event loop past
  the WS ping timeout. Its argv is a contract — changing a flag name, a
  default or the positional's optionality breaks the UI silently, so
  ``tests/test_cli.py`` pins it. It stays a human debugging tool on the VM too.

Every command works both locally (``uv run arb ...``) and on the VM
(``docker compose exec app arb ...``).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvloop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb", description="arb command-line entry point.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check env, keys, clock skew, database, venue reachability and disk",
    )
    ui = subparsers.add_parser(
        "ui",
        help="serve the terminal UI; every flag below is a starting value the UI can change",
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
        help="start with recording off (the UI can turn it back on)",
    )
    ui.add_argument(
        "--read-only",
        action="store_true",
        help="serve every view but refuse every control (also settable with UI_READ_ONLY)",
    )
    _add_poly_args(ui)
    ui.add_argument(
        "--pairs-top",
        type=int,
        default=10,
        help="confirmed pairs (by score) to track on the ARB screen (default: 10)",
    )
    _add_paper_args(ui)

    # argv contract: the UI spawns this as a subprocess. See the module
    # docstring; tests/test_cli.py pins the flags ControlPlane passes.
    replay = subparsers.add_parser(
        "replay",
        help="replay a recorded run through the pipeline (also the worker the UI's REPLAY spawns)",
    )
    replay.add_argument("run_id", nargs="?", default=None, help="run id (default: latest)")
    replay.add_argument(
        "--pairs-top", type=int, default=10, help="confirmed pairs to quote (0 = none)"
    )
    _add_paper_args(replay)
    replay.add_argument(
        "--persist", action="store_true", help="store paper trades as replay:<run_id>"
    )
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


def _run_ui(args: argparse.Namespace) -> int:
    from arb.config import AppConfig
    from arb.ui.server import run_ui

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = AppConfig()
    # The flag only ever turns read-only *on*: UI_READ_ONLY in the environment
    # stays in force when it isn't passed.
    if args.read_only:
        config = config.model_copy(update={"ui_read_only": True})
    try:
        uvloop.run(
            run_ui(
                config,
                tickers=_split(args.tickers),
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
        # Ctrl+C during startup, or a second Ctrl+C that cut cleanup short.
        return 130
    # A requested stop — Ctrl+C or SIGTERM (`docker compose stop`) — whose
    # cleanup ran to the end. See arb.shutdown for why SIGTERM gets here.
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


def main(argv: list[str] | None = None) -> int:
    """Entry point. Translates a closed stdout pipe into a quiet exit.

    Piping output into ``head``/``less`` closes the pipe early; without this,
    Python raises BrokenPipeError mid-print and again while flushing stdout at
    exit, dumping a traceback over an otherwise fine result. Handled here
    rather than by restoring the default SIGPIPE disposition, because the same
    entry point launches the long-running server (``arb ui``) where a broken
    client socket must never kill the process — and because ``arb replay``'s
    stdout is a pipe the UI reads, which it closes when a job is cancelled.
    """
    try:
        return _dispatch(argv)
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 141  # 128 + SIGPIPE, what a shell reports for a pipe death


def _dispatch(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _run_doctor()
    if args.command == "ui":
        return _run_ui(args)
    if args.command == "replay":
        return _run_replay(args)
    print(f"arb: command {args.command!r} is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
