"""Smoke tests for REST endpoints (subprocess-server-level), plus meta-tests
about the smoke suite as a whole.

Most tests here issue real HTTP requests (via ``httpx``) against the
session-scoped ``smoke_server`` fixture's live ``archon-search serve``
subprocess, and assert responses are correctly shaped and human-readable
(no raw ``CollectionMeta(`` repr, etc.). Two tests instead invoke ``pytest``
itself as a subprocess to assert properties of the smoke suite's collection
and execution (``test_smoke_excluded_from_default_run``,
``test_full_smoke_suite_passes``) — grouped here alongside the REST tests
rather than in a new file, since the plan's architecture names only
``conftest.py``/``test_cli.py``/``test_rest.py`` as smoke-suite files.

Module-level ``pytestmark`` serialises this file on one xdist worker so that
all smoke tests share the single session-scoped server subprocess (matches
the pattern in ``tests/smoke/test_cli.py``).
"""

from __future__ import annotations

import os
import re
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
    ``addopts``: the ini bakes in ``-n 8 --dist=loadgroup`` (xdist flags), and
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


# ---------------------------------------------------------------------------
# Full-suite e2e meta-test (T-1) — no smoke_server dependency (spawns its own)
# ---------------------------------------------------------------------------

# Reuses the repo-wide subprocess-e2e recursion-guard convention (see
# tests/integration/test_e2h_be10_ppr_subprocess_gate.py,
# tests/eval/test_e2e_graph_eval_gate_v2.py, tests/eval/test_code_lane_eval_gate.py)
# rather than inventing a new env var name.
_RECURSION_GUARD_ENV = "_ARCHON_E2E_SUBPROCESS"

# Number of tests in tests/smoke/ that pass outright inside the child run.
# 18 tests collected total: 1 expected xfail (test_collection_info_no_repr,
# S4/bug-007) + 1 expected skip (this very test, skipped inside the child via
# the recursion guard above) + 16 passed. Verified empirically: "16 passed,
# 1 skipped, 1 xfailed" on a machine with the fastembed model cache populated.
# A drop below this floor means the child suite silently collected fewer
# tests than expected (subset-collection regression) even though it still
# exited 0.
_EXPECTED_MIN_PASSED = 16

# Child budget: fixture startup (30s health/ready poll + 60s ingest job poll,
# tests/smoke/conftest.py) plus ~17 tests, several of which spawn their own
# `uv run archon-search` subprocess (each paying its own cold-start cost).
# Measured wall-clock on this machine: ~45s. 480s leaves ample margin while
# staying under the 600s foreground Bash ceiling this test may run inside.
_CHILD_SUITE_TIMEOUT_S = 480


@pytest.mark.skipif(
    os.environ.get(_RECURSION_GUARD_ENV) == "1",
    reason="recursion guard: this test is itself running inside the nested "
    "child suite it spawned",
)
def test_full_smoke_suite_passes() -> None:
    """Run the full ``tests/smoke/`` suite as a child subprocess and confirm
    every test passes or xfails correctly (T-1).

    Runs serially to avoid nested xdist worker pools, and with ``--no-cov``
    (matching the documented manual smoke-run command). ``PYTEST_ADDOPTS`` is
    cleared so the outer runner's own addopts cannot mask the child run.

    Uses ``-o addopts=`` (not ``-p no:xdist``) to override the ini's
    ``addopts``: the ini bakes in ``-n 8 --dist=loadgroup`` (xdist flags), and
    disabling the xdist plugin outright makes pytest reject those flags as
    unrecognized before collection even starts (matches the documented
    ``test_smoke_excluded_from_default_run`` gotcha above). This also strips
    the ini's ``-m "not smoke"`` filter, which is harmless here — no test in
    ``tests/smoke/`` actually carries a ``@pytest.mark.smoke`` marker (the
    default run excludes the directory solely via ``norecursedirs``), so
    that half of the filter was never load-bearing for this child anyway.

    The child suite includes this very test module — the recursion guard
    env var (set only in the child's environment) makes this exact test
    skip inside that nested run so it does not spawn a further child.

    Assertions (in order):
    - the child process exits 0 (all tests passed or xfailed);
    - no xdist workers were spawned in the child (OOM guard, matches the
      established pattern in ``test_e2h_be10_ppr_subprocess_gate.py``);
    - no "server did not stop cleanly" line appears anywhere in the
      captured output (S14 teardown must be clean, no SIGKILL escalation);
    - at least ``_EXPECTED_MIN_PASSED`` tests passed — a floor that guards
      against a subset-collection regression silently passing this gate
      (returncode 0 alone does not prove the whole suite ran: a collection
      error that drops most tests still exits 0 on the surviving subset);
    - no nonzero "failed"/"error(s)" count appears in the summary line;
    - ``test_collection_info_no_repr`` (S4) reports as ``xfail``, never
      ``xpass`` (bug-007 must still be open; an ``xpass`` here means the
      bug was fixed and the ``xfail`` marker in ``test_cli.py`` must be
      removed as a follow-up, not silently pass this gate).
    """
    child_env = {
        **os.environ,
        _RECURSION_GUARD_ENV: "1",
        "PYTEST_ADDOPTS": "",
    }

    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/smoke/", "--no-cov", "-o", "addopts=", "-v"],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=_CHILD_SUITE_TIMEOUT_S,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"full smoke suite timed out after {_CHILD_SUITE_TIMEOUT_S}s\n"
            f"--- stdout ---\n{exc.stdout}\n--- stderr ---\n{exc.stderr}"
        )

    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"full smoke suite failed (returncode={result.returncode}):\n{output}"
    )
    assert "[gw" not in output, (
        "child subprocess appears to have spawned xdist workers despite "
        f"'-o addopts=' — critical OOM risk, check the addopts override:\n{output}"
    )
    assert "server did not stop cleanly" not in output, (
        "teardown required SIGKILL escalation — SIGTERM teardown must be clean:\n"
        f"{output}"
    )
    passed_match = re.search(r"(\d+) passed", output)
    assert passed_match and int(passed_match.group(1)) >= _EXPECTED_MIN_PASSED, (
        f"expected at least {_EXPECTED_MIN_PASSED} passed tests (subset-collection "
        f"regression?):\n{output}"
    )
    assert not re.search(r"\d+ failed", output), (
        f"child suite reported one or more failed tests:\n{output}"
    )
    # Matches pytest's own summary-line error count ("1 error"/"2 errors" in
    # the final "== N passed, ... in Xs ==" line), not a bare "error"
    # substring — several smoke tests legitimately exercise error paths and
    # have "error" in their name or asserted output (e.g.
    # test_startup_failure_error_includes_stderr, S6's "Error contacting
    # server" assertion), which would false-positive on a naive substring
    # check even on a fully green run.
    assert not re.search(r"\d+ errors?\b", output), (
        f"child suite reported one or more errors:\n{output}"
    )
    assert "test_collection_info_no_repr" in output, (
        "positive control failed: the S4 xfail test name is missing from the "
        "output (child run uses -v so test names must appear), so collection "
        "did not genuinely include it"
    )
    assert re.search(r"test_collection_info_no_repr\b.*\bXFAIL\b", output), (
        "S4 must report as xfail (bug-007 still open), not pass or fail "
        f"outright:\n{output}"
    )
    assert not re.search(r"test_collection_info_no_repr\b.*\bXPASS\b", output), (
        "collection info repr bug (bug-007) appears fixed (xpass) — "
        "remove the xfail marker on test_collection_info_no_repr:\n"
        f"{output}"
    )
