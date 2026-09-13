"""arb command-line entry point.

Scaffold only. Subcommands (e.g. ``doctor``) are wired up in later milestones.
Works both locally (``uv run arb ...``) and on the VM (``docker compose exec app arb ...``).
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb", description="arb command-line entry point.")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"arb: command '{args.command}' is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
