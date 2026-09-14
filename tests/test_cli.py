"""CLI entry-point behavior that doesn't need a venue or a database."""

import subprocess
import sys

from arb.cli import build_parser, main

# Floods stdout past the OS pipe buffer (64 KiB). A smaller write just lands in
# the buffer and never raises, which is why `arb --help | head` looked fine
# while `arb pairs list | head` (thousands of rows) dumped a traceback.
_FLOOD = """
from arb import cli
cli._dispatch = lambda argv: [print("x" * 100) for _ in range(20_000)] and 0
raise SystemExit(cli.main([]))
"""


def test_no_command_prints_help() -> None:
    assert main([]) == 0


def test_pairs_list_limit_defaults_to_50_and_accepts_0() -> None:
    parser = build_parser()
    assert parser.parse_args(["pairs", "list"]).limit == 50
    assert parser.parse_args(["pairs", "list", "--limit", "0"]).limit == 0


def test_closed_stdout_pipe_exits_quietly() -> None:
    """Hanging up on the writer mid-flood, as `| head -30` does, is an exit
    code and not a traceback.

    Runs in a subprocess for two reasons: it is the real shape of the bug,
    and the handler redirects fd 1 to devnull, which would tear out pytest's
    own stdout capture if it ran in-process.
    """
    flooder = subprocess.Popen(
        [sys.executable, "-c", _FLOOD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert flooder.stdout is not None
    flooder.stdout.readline()  # take one line, then hang up
    flooder.stdout.close()
    _, stderr = flooder.communicate(timeout=60)
    assert b"BrokenPipeError" not in stderr
    assert b"Traceback" not in stderr
    assert flooder.returncode == 141  # 128 + SIGPIPE
