"""arb command-line entry point.

Every command works both locally (``uv run arb ...``) and on the VM
(``docker compose exec app arb ...``).
"""

from __future__ import annotations

import argparse
import sys

import uvloop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb", description="arb command-line entry point.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check env, keys, clock skew, database, venue reachability and disk",
    )
    return parser


def _run_doctor() -> int:
    from arb.doctor import exit_code, format_results, run_doctor

    results = uvloop.run(run_doctor())
    print(format_results(results))
    return exit_code(results)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _run_doctor()
    print(f"arb: command {args.command!r} is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
