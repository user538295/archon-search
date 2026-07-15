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
import time
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
# Authenticated REST endpoint assertions (S9-S13) — require the smoke_server
# fixture and its pre-seeded ``smoke`` collection
# ---------------------------------------------------------------------------


def test_ready_returns_200(smoke_server) -> None:
    """``GET /ready`` is auth-exempt and must return 200 with ``ready: true``
    once the fixture's health/ready poll has already succeeded (S9).
    """
    response = httpx.get(f"{smoke_server.base_url}/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True


def test_status_returns_json_no_reprs(smoke_server) -> None:
    """``GET /status`` with a valid Bearer token must return 200, parse as
    JSON, and contain no raw Python reprs or embedding vector arrays (S10,
    S16 partial).
    """
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    response = httpx.get(f"{smoke_server.base_url}/status", headers=headers)

    assert response.status_code == 200
    response.json()
    assert "CollectionMeta(" not in response.text
    assert "[0." not in response.text


def test_collections_list_has_at_least_one_entry(smoke_server) -> None:
    """``GET /collections/`` with a valid Bearer token must return 200 and a
    JSON array with at least the pre-seeded ``smoke`` collection (S11).
    """
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    response = httpx.get(f"{smoke_server.base_url}/collections/", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) >= 1


def test_collection_detail_doc_count_positive(smoke_server) -> None:
    """``GET /collections/smoke`` with a valid Bearer token must return 200,
    a JSON body with no Python reprs, and ``doc_count > 0`` (S12).
    """
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    response = httpx.get(f"{smoke_server.base_url}/collections/smoke", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["doc_count"] > 0
    assert "CollectionMeta(" not in response.text


@pytest.mark.skipif(
    os.environ.get("SMOKE_NO_TIMING") == "1", reason="timing disabled"
)
def test_search_returns_results_within_5s(smoke_server) -> None:
    """``POST /search`` against the pre-seeded ``smoke`` collection must
    return 200 within 5 seconds, with ``results`` as a JSON array and no raw
    Python reprs or embedding vector arrays in the output (S13, S16).
    """
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}

    t0 = time.monotonic()
    response = httpx.post(
        f"{smoke_server.base_url}/search",
        json={"query": "test", "collection": "smoke"},
        headers=headers,
    )
    elapsed = time.monotonic() - t0

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["results"], list)
    assert "[0." not in response.text
    assert "CollectionMeta(" not in response.text
    assert elapsed < 5.0


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
