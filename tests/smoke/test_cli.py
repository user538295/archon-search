"""Smoke tests for CLI commands (subprocess-level).

These tests spawn real ``archon-search`` subprocesses and assert that
commands complete within timing budgets and produce human-readable output
(no raw ``CollectionMeta(`` repr, no embedding vectors, etc.).

Server-dependent tests (added in BE-3 and later) require the session-scoped
``smoke_server`` fixture from ``conftest.py`` (added in BE-2). The only test
currently in this file — ``test_smoke_marker_in_pyproject`` — is a
configuration guard that does not require the server fixture.

Module-level ``pytestmark`` serialises this file on one xdist worker so that
all smoke tests share the single session-scoped server subprocess.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("smoke_e2e")

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# Marker registration guard (always-on — no @pytest.mark.smoke gate)
# ---------------------------------------------------------------------------


def test_smoke_marker_in_pyproject() -> None:
    """``addopts`` uses ``--strict-markers`` — the smoke marker must be
    registered in ``pyproject.toml`` or pytest rejects the marker at
    collection time and every smoke test silently disappears.

    This test also verifies that ``tests/smoke`` is in ``norecursedirs``
    (preventing the default ``uv run pytest`` from collecting smoke tests and
    spawning the server subprocess) and that the ``-m`` addopts filter
    excludes ``smoke`` (dual guard matching the ``live_benchmark`` pattern).
    """
    with PYPROJECT.open("rb") as fp:
        data = tomllib.load(fp)

    ini = data["tool"]["pytest"]["ini_options"]
    markers: list[str] = ini["markers"]
    norecursedirs: list[str] = ini["norecursedirs"]
    addopts: str = ini["addopts"]

    assert any(m.startswith("smoke:") or m.startswith("smoke ") for m in markers), (
        "pyproject.toml [tool.pytest.ini_options].markers must register a "
        "'smoke:' marker so --strict-markers does not reject @pytest.mark.smoke"
    )

    assert "tests/smoke" in norecursedirs, (
        "pyproject.toml [tool.pytest.ini_options].norecursedirs must include "
        "'tests/smoke' to prevent the default suite from auto-collecting smoke "
        "tests and spawning the server subprocess"
    )

    assert "not smoke" in addopts, (
        "pyproject.toml [tool.pytest.ini_options].addopts must contain "
        "'not smoke' in its -m filter (dual guard: norecursedirs + -m filter)"
    )
