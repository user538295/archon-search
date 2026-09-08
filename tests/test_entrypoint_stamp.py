"""Minimal coverage for docker-entrypoint.sh stamp-cache branching logic.

Three cases: (1) no stamp → install, (2) stamp matches EXTRAS → skip,
(3) stamp differs → reinstall. Uses ARCHON_STAMP to redirect the stamp
path to a temp dir (no /pip-packages required).
"""
import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).parents[1] / "scripts" / "docker-entrypoint.sh"


def _run(tmp_path: Path, extras: str = "graph") -> tuple[subprocess.CompletedProcess, Path, Path]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    stamp = tmp_path / ".extras-installed"
    pip_called = tmp_path / "pip_called"

    # We intercept `python3` entirely: handle the version probe (`-c "import
    # importlib..."`) and record `-m pip install`.
    fake_python3 = bin_dir / "python3"
    fake_python3.write_text(
        f"#!/bin/sh\n"
        f'case "$*" in\n'
        f'  *"import importlib"*) echo "0.0.1+test" ;;\n'  # version probe
        f'  *pip*install*) touch {pip_called}; exit 0 ;;\n'
        f'  *) exit 0 ;;\n'
        f'esac\n'
    )
    fake_python3.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "ARCHON_STAMP": str(stamp),
        "ARCHON_EXTRAS": extras,
    }

    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "true"],
        env=env, capture_output=True, text=True
    )
    return result, stamp, pip_called


def test_stamp_absent_triggers_install(tmp_path: Path) -> None:
    result, stamp, pip_called = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert pip_called.exists(), "pip install should have run when stamp is absent"
    assert stamp.read_text() == "graph"


def test_stamp_matches_skips_install(tmp_path: Path) -> None:
    stamp = tmp_path / ".extras-installed"
    stamp.write_text("graph")
    result, _, pip_called = _run(tmp_path, extras="graph")
    assert result.returncode == 0, result.stderr
    assert not pip_called.exists(), "pip install should be skipped when stamp matches"


def test_stamp_differs_triggers_reinstall(tmp_path: Path) -> None:
    stamp = tmp_path / ".extras-installed"
    stamp.write_text("graph")  # old extras
    result, _, pip_called = _run(tmp_path, extras="graph,code")  # changed
    assert result.returncode == 0, result.stderr
    assert pip_called.exists(), "pip install should run when stamp differs"
    assert stamp.read_text() == "graph,code"


def test_empty_extras_skips_install(tmp_path: Path) -> None:
    result, _, pip_called = _run(tmp_path, extras="")
    assert result.returncode == 0, result.stderr
    assert not pip_called.exists(), "pip install should be skipped when ARCHON_EXTRAS is empty"


def _run_with_pip_behavior(
    tmp_path: Path, pip_script: str, extras: str = "graph"
) -> tuple[subprocess.CompletedProcess, Path]:
    """Like ``_run`` but lets the caller script the fake pip install's behavior
    (to simulate a transient failure) instead of always succeeding.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    stamp = tmp_path / ".extras-installed"

    fake_python3 = bin_dir / "python3"
    fake_python3.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"import importlib"*) echo "0.0.1+test" ;;\n'
        f"  *pip*install*) {pip_script} ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    fake_python3.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "ARCHON_STAMP": str(stamp),
        "ARCHON_EXTRAS": extras,
        "ARCHON_PIP_RETRY_DELAY_S": "0",  # keep the test fast
    }
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "true"], env=env, capture_output=True, text=True
    )
    return result, stamp


def test_pip_install_retries_transient_failure_then_succeeds(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    attempts.write_text("0")
    pip_script = (
        f'n=$(cat {attempts}); n=$((n+1)); echo "$n" > {attempts}; '
        f'if [ "$n" -lt 3 ]; then exit 1; else exit 0; fi'
    )
    result, stamp = _run_with_pip_behavior(tmp_path, pip_script)
    assert result.returncode == 0, result.stderr
    assert attempts.read_text().strip() == "3", "pip install should have been retried until the 3rd attempt succeeded"
    assert stamp.read_text() == "graph"


def test_pip_install_gives_up_after_max_retries(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    attempts.write_text("0")
    pip_script = f"n=$(cat {attempts}); n=$((n+1)); echo \"$n\" > {attempts}; exit 1"
    result, stamp = _run_with_pip_behavior(tmp_path, pip_script)
    assert result.returncode != 0, "entrypoint must fail once retries are exhausted"
    assert attempts.read_text().strip() == "3", "should stop retrying after 3 attempts"
    assert not stamp.exists(), "stamp must not be written when install never succeeds"


def test_pip_install_raises_default_timeout_above_pip_default(tmp_path: Path) -> None:
    timeout_seen = tmp_path / "timeout_seen"
    pip_script = f'echo "$PIP_DEFAULT_TIMEOUT" > {timeout_seen}; exit 0'
    result, _ = _run_with_pip_behavior(tmp_path, pip_script)
    assert result.returncode == 0, result.stderr
    seen = timeout_seen.read_text().strip()
    assert seen.isdigit() and int(seen) > 15, (
        f"PIP_DEFAULT_TIMEOUT={seen!r} must be raised above pip's tight 15s default"
    )
