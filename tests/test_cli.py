"""The surviving CLI surface, and the one contract a machine depends on.

Operation moved into the UI control plane, so the CLI is down to ``ui``,
``doctor`` and ``replay``. Two of those have callers that are not a human at a
prompt — ``docker-compose.yml`` starts the container with ``arb ui --top 20
--host 0.0.0.0``, and ``ControlPlane._apply_replay`` spawns ``python -m
arb.cli replay ...`` — so their argv is pinned here.
"""

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from arb.cli import build_parser, main

# Floods stdout past the OS pipe buffer (64 KiB). A smaller write just lands in
# the buffer and never raises, which is why `arb --help | head` looked fine
# while a long listing (thousands of rows) dumped a traceback.
_FLOOD = """
from arb import cli
cli._dispatch = lambda argv: [print("x" * 100) for _ in range(20_000)] and 0
raise SystemExit(cli.main([]))
"""

# What ControlPlane._apply_replay appends to `[sys.executable, "-m",
# "arb.cli", "replay"]`. Adding a flag there without adding it to the parser
# makes every REPLAY job in the UI die with "exited 2" and no other clue.
_CONTROL_REPLAY_FLAGS = {"--pairs-top", "--paper", "--persist"}


def test_no_command_prints_help() -> None:
    assert main([]) == 0


@pytest.mark.parametrize("command", ["doctor", "ui", "replay"])
def test_surviving_commands_parse(command: str) -> None:
    assert build_parser().parse_args([command]).command == command


@pytest.mark.parametrize(
    "argv",
    [
        ["record"],
        ["record", "--top", "10"],
        ["pairs"],
        ["pairs", "propose"],
        ["pairs", "list", "--limit", "0"],
        ["pairs", "backfill"],
        ["pairs", "show", "1"],
        ["pairs", "confirm", "1"],
        ["pairs", "reject", "1"],
    ],
)
def test_deleted_commands_are_gone(argv: list[str]) -> None:
    """`arb record` and the `arb pairs` subtree are UI actions now.

    argparse exits 2 on an unknown subcommand, so a script still calling one
    fails loudly instead of silently doing nothing.
    """
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(argv)
    assert excinfo.value.code == 2


def test_compose_ui_argv_still_parses() -> None:
    """docker-compose.yml runs exactly this; break it and the container
    restart-loops."""
    args = build_parser().parse_args(["ui", "--top", "20", "--host", "0.0.0.0"])
    assert (args.command, args.top, args.host) == ("ui", 20, "0.0.0.0")


def test_dockerfile_puts_the_console_script_on_path() -> None:
    """``docker compose exec app arb ...`` is the form CLAUDE.md and the docs
    promise, and ``exec`` does not go through ``uv run``: it needs the venv's
    ``bin`` on PATH. Drop the line and the image still builds and the container
    still runs (its command is ``uv run arb ui``); only the documented ``exec``
    dies, with "executable file not found in $PATH". Text cannot prove a
    container finds the script — a build did, see docs/decisions.md — but it
    fails when the line goes."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    workdir = re.search(r"^WORKDIR (\S+)$", dockerfile, re.MULTILINE)
    assert workdir is not None
    assert f'ENV PATH="{workdir.group(1)}/.venv/bin:$PATH"' in dockerfile.splitlines()


def test_ui_flags_are_starting_values_with_defaults() -> None:
    args = build_parser().parse_args(["ui"])
    # Each of these is also a runtime control; the CLI only seeds it.
    assert (args.top, args.poly_top, args.pairs_top) == (8, 8, 10)
    assert (args.no_record, args.paper, args.read_only) == (False, False, False)
    assert build_parser().parse_args(["ui", "--read-only"]).read_only is True


def test_replay_accepts_the_argv_the_control_plane_builds() -> None:
    parsed = build_parser().parse_args(
        ["replay", "run-2026-09-15", "--pairs-top", "7", "--paper", "--persist"]
    )
    assert parsed.run_id == "run-2026-09-15"
    assert (parsed.pairs_top, parsed.paper, parsed.persist) == (7, True, True)

    # The omitted-run_id form: the control plane drops the positional when the
    # operator asks for "the latest recorded run".
    latest = build_parser().parse_args(["replay", "--pairs-top", "0"])
    assert latest.run_id is None
    assert (latest.pairs_top, latest.paper, latest.persist) == (0, False, False)
    # It passes --paper with no limit flags, so the limits must keep defaults.
    assert (latest.min_net_ticks, latest.max_cts_per_pair, latest.max_notional) == (
        50,
        100,
        1000.0,
    )


def test_control_plane_builds_only_flags_this_parser_knows() -> None:
    """Pins both ends of the subprocess contract.

    Reads the flags out of the source that builds the argv, so adding one
    there fails here rather than at 3am in a job log.
    """
    from arb.ui.control import ControlPlane

    source = inspect.getsource(ControlPlane._apply_replay)
    assert '[sys.executable, "-m", "arb.cli", "replay"]' in source
    assert set(re.findall(r'"(--[a-z-]+)"', source)) == _CONTROL_REPLAY_FLAGS


def test_replay_is_runnable_as_python_m_arb_cli() -> None:
    """`arb.cli` must stay importable-and-executable as a module: the control
    plane spawns the interpreter, not the console script."""
    proc = subprocess.run(
        [sys.executable, "-m", "arb.cli", "replay", "--help"],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    help_text = proc.stdout.decode()
    for flag in _CONTROL_REPLAY_FLAGS:
        assert flag in help_text


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
