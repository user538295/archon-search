"""Smoke tests for REST endpoints (subprocess-server-level).

These tests issue real HTTP requests (via ``httpx``) against the
session-scoped ``smoke_server`` fixture's live ``archon-search serve``
subprocess, and assert responses are correctly shaped and human-readable
(no raw ``CollectionMeta(`` repr, etc.).

Module-level ``pytestmark`` serialises this file on one xdist worker so that
all smoke tests share the single session-scoped server subprocess (matches
the pattern in ``tests/smoke/test_cli.py``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.xdist_group("smoke_e2e")

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Walking-skeleton REST test (S8) — requires the smoke_server fixture
# ---------------------------------------------------------------------------


def test_health_no_auth_returns_200(smoke_server) -> None:
    """``GET /health`` is auth-exempt (``middleware_auth.py`` ``_EXEMPT_PATHS``)
    and must return 200 with ``{"status": "running"}`` even with no
    ``Authorization`` header.
    """
    response = httpx.get(f"{smoke_server.base_url}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert "CollectionMeta(" not in response.text


# ---------------------------------------------------------------------------
# Default-run exclusion check (S17) — no smoke_server dependency
# ---------------------------------------------------------------------------


def test_smoke_excluded_from_default_run() -> None:
    """The default ``uv run pytest`` (no path argument) must not collect
    ``tests/smoke/`` — collecting it would spawn the smoke server subprocess
    on every default run.

    Runs pytest in ``--collect-only`` mode as a subprocess, with
    ``PYTEST_ADDOPTS`` cleared so the outer test runner's own addopts (e.g. an
    IDE integration) cannot mask the assertion, and asserts the path token
    ``"tests/smoke/"`` is absent from the collected output (not the bare word
    ``"smoke"``, which would false-positive on ``tests/test_docker_smoke.py``).

    Uses ``-o addopts=`` (not ``-p no:xdist``) to override the ini's
    ``addopts``: the ini bakes in ``-n 4 --dist=loadgroup`` (xdist flags), and
    disabling the xdist plugin outright makes pytest reject those flags as
    unrecognized before collection even starts — that would make this
    assertion pass vacuously on a usage-error message rather than real
    collection output. Note ``-o addopts=`` also strips the ini's own
    ``-m "not live_benchmark and not smoke"`` filter for this subprocess call,
    so this test exercises only the ``norecursedirs``-based half of the
    documented dual guard (the mechanism that actually prevents the default
    run from collecting ``tests/smoke/``); it does not separately verify the
    ``-m`` marker filter, which ``test_smoke_marker_in_pyproject`` (in
    ``test_cli.py``) already asserts statically.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "--no-cov",
            "-o",
            "addopts=",
        ],
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"collection subprocess failed (returncode={result.returncode}): {output}"
    )
    assert "tests/test_docker_smoke.py" in output, (
        "positive control failed: a known non-smoke test path token is "
        "missing from the output, so collection did not genuinely run"
    )
    assert "tests/smoke/" not in output
