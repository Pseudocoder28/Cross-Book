"""``scripts/capture_kalshi_ws.py`` does nothing it was not told to do.

It used to have no argument parsing at all, so *any* invocation — ``--help``
included — opened an authenticated Kalshi WebSocket and replaced the tracked
``ws_orderbook_capture.jsonl`` that the parser tests pin. A reviewer asking for
usage text got a live capture instead.

Nothing here can reach Kalshi, whatever the script regresses to. The
subprocesses run in an empty directory (``AppConfig`` finds no ``.env``) with
every ``KALSHI_*`` variable removed, so the script's own "KALSHI_API_KEY_ID not
set" exit comes before any socket; the in-process tests replace ``capture``.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_kalshi_ws.py"
FIXTURE = Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture.jsonl"

# Runs the script as __main__ with --help, then reports what it dragged in.
# Importing arb.config is not itself a connection, but it is the first step of
# one, and keeping it out of the --help path is what makes that path obviously
# safe rather than safe by reading every import's side effects.
_IMPORT_PROBE = """
import runpy, sys
script = sys.argv[1]
sys.argv = [script, "--help"]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, exc.code
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("arb", "websockets"))
print("LOADED=" + ",".join(loaded), file=sys.stderr)
"""


def _run(
    argv: list[str], cwd: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KALSHI_")}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        timeout=120,
        cwd=cwd,
        env=env,
    )


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_kalshi_ws", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_prints_usage_and_touches_nothing(tmp_path: Path) -> None:
    before = FIXTURE.read_bytes()
    proc = _run([str(SCRIPT), "--help"], tmp_path)
    assert proc.returncode == 0, proc.stderr.decode()
    help_text = proc.stdout.decode()
    assert help_text.startswith("usage:")
    for flag in ("--overwrite", "--seconds", "--min-deltas", "--top", "--require-side"):
        assert flag in help_text
    # The pre-fix script, run here, exits 1 on the missing key id instead.
    assert b"KALSHI_API_KEY_ID" not in proc.stderr
    assert FIXTURE.read_bytes() == before
    assert list(tmp_path.iterdir()) == []


def test_help_imports_neither_config_nor_the_websocket_client(tmp_path: Path) -> None:
    proc = _run(["-c", _IMPORT_PROBE, str(SCRIPT)], tmp_path)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stderr.decode().splitlines()[-1] == "LOADED="


def test_bare_invocation_is_a_usage_error_not_a_capture(tmp_path: Path) -> None:
    """There is no default OUT, so there is nothing to overwrite by accident."""
    before = FIXTURE.read_bytes()
    proc = _run([str(SCRIPT)], tmp_path)
    assert proc.returncode == 2
    assert b"usage:" in proc.stderr
    assert b"KALSHI_API_KEY_ID" not in proc.stderr
    assert FIXTURE.read_bytes() == before
    assert list(tmp_path.iterdir()) == []


def test_old_environment_knobs_are_inert(tmp_path: Path) -> None:
    """CAPTURE_OUT used to pick the file; a stale export must not pick it now."""
    target = tmp_path / "from-env.jsonl"
    proc = _run([str(SCRIPT)], tmp_path, {"CAPTURE_OUT": str(target)})
    assert proc.returncode == 2
    assert not target.exists()


def test_the_tracked_fixture_is_refused_before_anything_connects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()

    async def must_not_run(**_: object) -> None:
        raise AssertionError("capture() ran for an OUT that should have been refused")

    monkeypatch.setattr(script, "capture", must_not_run)
    before = FIXTURE.read_bytes()
    with pytest.raises(SystemExit) as excinfo:
        script.main([str(FIXTURE)])
    assert excinfo.value.code == 2
    assert "--overwrite" in capsys.readouterr().err
    assert FIXTURE.read_bytes() == before


def test_a_missing_directory_is_refused_before_the_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()

    async def must_not_run(**_: object) -> None:
        raise AssertionError("capture() ran with nowhere to write its frames")

    monkeypatch.setattr(script, "capture", must_not_run)
    with pytest.raises(SystemExit) as excinfo:
        script.main([str(tmp_path / "no-such-dir" / "out.jsonl")])
    assert excinfo.value.code == 2


def test_overwrite_is_the_only_way_to_replace_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _load_script()
    frames = FIXTURE.read_bytes().split(b"\n")[:3]
    seen: list[dict[str, object]] = []

    async def fake_capture(**kwargs: object) -> tuple[list[bytes], dict[str, int], set[str]]:
        seen.append(kwargs)
        return frames, {"orderbook_snapshot": len(frames)}, {"T"}

    monkeypatch.setattr(script, "capture", fake_capture)
    out = tmp_path / "capture.jsonl"

    script.main([str(out), "--require-side", "no", "--top", "2"])
    assert out.read_bytes() == b"\n".join(frames) + b"\n"
    assert seen == [{"seconds": 90.0, "min_deltas": 15, "top_n": 2, "require_side": "no"}]

    out.write_bytes(b"pinned\n")
    with pytest.raises(SystemExit):
        script.main([str(out)])
    assert out.read_bytes() == b"pinned\n"

    script.main([str(out), "--overwrite"])
    assert out.read_bytes() == b"\n".join(frames) + b"\n"
