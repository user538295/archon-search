"""Tests for the custom CalVer version scheme used by hatch-vcs."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_PKG_ROOT = Path(__file__).resolve().parent.parent


def _load_version_scheme():
    """Load _version_scheme.py from the package root without polluting sys.path."""
    path = _PKG_ROOT / "_version_scheme.py"
    spec = importlib.util.spec_from_file_location("_archon_search_version_scheme", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_version_scheme = _load_version_scheme()

_CALVER_RE = re.compile(r"^\d{2}\.\d{1,2}\.\d+$")


def test_calver_format() -> None:
    """calver_total_count returns YY.M.<count> using current UTC date and HEAD count."""
    result = _version_scheme.calver_total_count(None)
    assert _CALVER_RE.match(result), f"unexpected version string: {result!r}"


def test_calver_on_git_failure() -> None:
    """If git rev-list fails, returns YY.M.0 instead of raising."""
    fake = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal")
    with patch.object(_version_scheme.subprocess, "run", return_value=fake):
        result = _version_scheme.calver_total_count(None)
    assert _CALVER_RE.match(result), f"unexpected version string: {result!r}"
    assert result.endswith(".0"), f"expected fallback count 0, got {result!r}"


def test_calver_uses_git_rev_list_count() -> None:
    """calver_total_count parses 'git rev-list --count HEAD' stdout as the patch component."""
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="1234\n", stderr="")
    with patch.object(_version_scheme.subprocess, "run", return_value=fake):
        result = _version_scheme.calver_total_count(None)
    assert result.endswith(".1234"), f"expected count 1234 suffix, got {result!r}"


def test_main_module_invocable() -> None:
    """python -m archon_search --help exits 0 (the module entrypoint is wired)."""
    result = subprocess.run(
        [sys.executable, "-m", "archon_search", "--help"],
        cwd=str(_PKG_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_main_module_version_option() -> None:
    """python -m archon_search --version prints a parsable version string and exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "archon_search", "--version"],
        cwd=str(_PKG_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    # Click prints "archon-search, version <X.Y.Z>". Assert a version token is present.
    assert re.search(r"version\s+\S+", result.stdout), (
        f"expected --version output to contain a version token, got {result.stdout!r}"
    )


def test_calver_on_git_missing() -> None:
    """If the git binary cannot be invoked at all, returns YY.M.0 instead of raising."""
    with patch.object(_version_scheme.subprocess, "run", side_effect=FileNotFoundError("git")):
        result = _version_scheme.calver_total_count(None)
    assert _CALVER_RE.match(result), f"unexpected version string: {result!r}"
    assert result.endswith(".0"), f"expected fallback count 0, got {result!r}"
