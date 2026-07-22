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

    # Fake python3 -m pip: record invocation
    fake_pip_module = bin_dir / "pip_module_marker"
    # We intercept `python3` entirely: handle both `-m pip install` and
    # `-c "import en_core_web_sm"` (return 0 for both so spacy skips) and
    # `-m spacy download` (record + return 0).
    fake_python3 = bin_dir / "python3"
    fake_python3.write_text(
        f"#!/bin/sh\n"
        f'case "$*" in\n'
        f'  *"import importlib"*) echo "0.0.1+test" ;;\n'  # version probe
        f'  *"import en_core_web_sm"*) exit 0 ;;\n'       # spacy import check: present
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
