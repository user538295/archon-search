"""T-3: each interface-parity guard fails in a real pytest process on a real violation (plan S29).

The boundary test (``tests/test_layer_import_boundary.py``) and the parity test (``tests/parity/test_parity.py``) are
unit-tested against synthetic input and run against the real tree in the default suite, which only shows that they pass.
Here each guard is copied, with the code it scans, into ``tmp_path``, a real violation is injected into the copy, and the
copy is run as a child pytest (``sys.executable -m pytest``, never ``uv run``: ``tests/CLAUDE.md``). The child must exit 1
and name the violation. BE-8 and BE-11 keep both guards free of repo-conftest dependencies so a bare copy can run.

Every test first runs the unmodified copy and expects exit 0 (positive control): a copy that cannot run (a missing helper
module, an import error) would otherwise fail the child for the wrong reason and pass the test vacuously.

Lives in the local smoke lane (``tests/smoke/``, not in CI); the module-level ``pytestmark`` serialises it on one xdist worker.
"""

from __future__ import annotations

import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from archon_search.constants import MIN_API_KEY_LENGTH

pytestmark = pytest.mark.xdist_group("smoke_e2e")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD_TIMEOUT_S = 300
_PKG = "archon_search"
_BOUNDARY_TEST = "tests/test_layer_import_boundary.py"
_BOUNDARY_NODE = "test_real_tree_has_no_unlisted_boundary_violation"
_FORBIDDEN_IMPORT_FILE = f"{_PKG}/services/validators.py"
_FORBIDDEN_IMPORT = "import fastapi  # injected by the T-3 smoke test\n"
_PARITY_TEST = "tests/parity/test_parity.py"
_PARITY_REGISTRY = "tests/parity/registry.py"
_PARITY_FILES = ("tests/__init__.py", "tests/_mcp_tools.py", "tests/parity/__init__.py", _PARITY_TEST, _PARITY_REGISTRY)
_PARITY_NODE = "test_parity_passes_on_live_surfaces"
_DELETED_ROW = "S1 Search (pilot)"
_CLAIMED_BY_DELETED_ROW = "POST /search"
_CHILD_API_KEY = "0" * MIN_API_KEY_LENGTH
# Whatever the developer shell or the outer pytest run holds for these must not reach the child.
_ENV_PREFIXES_DROPPED_FROM_CHILD = ("PYTEST_", "ARCHON_SEARCH_")
_ENV_DROPPED_FROM_CHILD = ("PYTHONPATH", "PYTHONSAFEPATH")


def _child_env(workdir: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _ENV_DROPPED_FROM_CHILD and not key.startswith(_ENV_PREFIXES_DROPPED_FROM_CHILD)
    }
    env["ARCHON_SEARCH_DATA_DIR"] = str(workdir / "data")
    env["ARCHON_SEARCH_API_KEY"] = _CHILD_API_KEY
    return env


def _run_in_copy(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, *args],
            cwd=workdir,
            env=_child_env(workdir),
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"the child run exceeded {_CHILD_TIMEOUT_S}s:\n{exc.stdout!s}\n{exc.stderr!s}")


def _run_pytest_copy(workdir: Path, test_file: str, node: str) -> subprocess.CompletedProcess[str]:
    """Run one test of the copy in a child pytest that knows nothing of the repo's ini, conftest or fixtures."""
    return _run_in_copy(workdir, "-m", "pytest", f"{test_file}::{node}", "-p", "no:cacheprovider", "-vv", "--tb=short")


def _prepare_copy(workdir: Path, files: tuple[str, ...]) -> None:
    """Copy the package and ``files`` into ``workdir``; an empty ini pins the child's rootdir so it never reads the repo's."""
    shutil.copytree(_REPO_ROOT / _PKG, workdir / _PKG, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in files:
        destination = workdir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative, destination)
    (workdir / "pytest.ini").write_text("", encoding="utf-8")


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


def _assert_child_imports_the_copy(workdir: Path) -> None:
    """The child must run the copy, not the editable install of the repo it was copied from."""
    probe = _run_in_copy(workdir, "-c", f"import {_PKG}; print({_PKG}.__file__)")
    assert probe.returncode == 0, _output(probe)
    assert Path(probe.stdout.strip()).resolve().is_relative_to(workdir.resolve()), _output(probe)


def _assert_positive_control(workdir: Path, test_file: str, node: str) -> None:
    _assert_child_imports_the_copy(workdir)
    control = _run_pytest_copy(workdir, test_file, node)
    assert control.returncode == pytest.ExitCode.OK, f"the unmodified copy must pass, or a failure below proves nothing:\n{_output(control)}"
    assert re.search(r"(?<!\d)1 passed", control.stdout), f"the control must run exactly the guard test:\n{_output(control)}"


def test_smoke_boundary_test_fails_in_a_real_pytest_run_on_an_injected_forbidden_import(tmp_path: Path) -> None:
    """S29: a ``fastapi`` import added to ``services/validators.py`` makes the copied boundary test exit 1 and name the edge."""
    _prepare_copy(tmp_path, (_BOUNDARY_TEST,))
    _assert_positive_control(tmp_path, _BOUNDARY_TEST, _BOUNDARY_NODE)

    validators = tmp_path / _FORBIDDEN_IMPORT_FILE
    source = validators.read_text(encoding="utf-8")
    validators.write_text(source.rstrip("\n") + "\n" + _FORBIDDEN_IMPORT, encoding="utf-8")
    result = _run_pytest_copy(tmp_path, _BOUNDARY_TEST, _BOUNDARY_NODE)

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, _output(result)
    assert re.search(rf"Edge\(file='{_FORBIDDEN_IMPORT_FILE}', module='fastapi', scope=<Scope\.MODULE_LEVEL", result.stdout), _output(result)


def test_smoke_parity_test_fails_in_a_real_pytest_run_on_a_deleted_registry_row(tmp_path: Path) -> None:
    """S29: deleting the Search row from a registry copy makes the copied parity test exit 1 and name the unclaimed surface."""
    _prepare_copy(tmp_path, _PARITY_FILES)
    _assert_positive_control(tmp_path, _PARITY_TEST, _PARITY_NODE)

    registry = tmp_path / _PARITY_REGISTRY
    rows = runpy.run_path(str(registry))["REGISTRY"]
    kept = [row for row in rows if row["name"] != _DELETED_ROW]
    assert len(kept) == len(rows) - 1, f"the registry has no row named {_DELETED_ROW!r}"
    registry.write_text(f"REGISTRY = {kept!r}\n", encoding="utf-8")
    result = _run_pytest_copy(tmp_path, _PARITY_TEST, _PARITY_NODE)

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, _output(result)
    finding = rf"ParityFailure\.UNCLAIMED_SURFACE.*?identifier='{_CLAIMED_BY_DELETED_ROW}'"
    assert re.search(finding, result.stdout, re.DOTALL), _output(result)
