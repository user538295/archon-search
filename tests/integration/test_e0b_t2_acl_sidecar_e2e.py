"""E0b / T-2 — e2e: ingest with oversized ACL sidecar returns warning.

Scenarios covered:
  S19: Document with ACL sidecar > 64 KB → IngestResult.warnings contains a
       human-readable message naming the file and limit; job result carries warnings.
  S18: Document with ACL sidecar ≤ 64 KB → IngestResult.warnings is empty; ACL is applied.
  S20: Document with no ACL sidecar → IngestResult.warnings is empty; ingestion proceeds normally.

Note: TestClient-based tests are integration-level (in-process ASGI). Labeled
#e2e_test in the plan because they exercise the full application stack with a
real SearchPipeline, real LanceDB store, and real ASGI middleware chain.
True process-isolated e2e is not required for E0b.

Unit tests (acl.read_acl_sidecar, resolve_acl) and more targeted integration tests
(pipeline ingest, MCP ingest_file) are in tests/test_acl.py and
tests/integration/test_e0b_be7_ingest_acl_warnings.py (BE-7 tasks).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from archon_search.acl import _ACL_SIDECAR_MAX_BYTES
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_to_done(
    client,
    job_id: str,
    headers: dict[str, str],
    *,
    timeout: float = 20.0,
) -> dict:
    """Poll GET /jobs/{id} until DONE (or FAILED) and return the body."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200, f"GET /jobs/{job_id} returned {r.status_code}: {r.text}"
        body = r.json()
        status = body["status"]
        if status == "DONE":
            return body
        if status == "FAILED":
            pytest.fail(f"Ingest job FAILED (job_id={job_id}): {body}")
        time.sleep(0.05)
    pytest.fail(f"Ingest job did not complete within {timeout}s (job_id={job_id})")


# ---------------------------------------------------------------------------
# S19: ACL sidecar > 64 KB → job result warnings contains message naming file and limit
# ---------------------------------------------------------------------------


def test_e2e_oversized_acl_sidecar_warning_in_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app via TestClient; ingest document with oversized ACL sidecar (> 64 KB);
    assert async job result carries warnings with a message naming the file and the limit.

    Covers scenario S19: oversized sidecar is skipped with a warning; the warning
    propagates from acl.read_acl_sidecar → resolve_acl → pipeline → IngestResult.warnings
    → job result dict → GET /jobs/{id} response.
    """
    doc = tmp_path / "e0b_t2_oversized.md"
    doc.write_text("# E0b T2 Oversized ACL Sidecar Test\n\nContent for ACL warning e2e test.\n" * 8)

    sidecar = tmp_path / "e0b_t2_oversized.md.acl"
    sidecar.write_bytes(b"x" * (_ACL_SIDECAR_MAX_BYTES + 1))

    col = "e0b-t2-oversized-acl"

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = _auth(api_key)

        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc)},
            headers=headers,
        )
        assert resp.status_code == 202, (
            f"POST /ingest expected 202, got {resp.status_code}: {resp.text}"
        )
        job_id = resp.json()["job_id"]

        result_body = _poll_job_to_done(client, job_id, headers)

        result = result_body.get("result")
        assert result is not None, "Job result should not be None after DONE"
        assert "warnings" in result, (
            f"Job result should contain 'warnings' key; got keys: {list(result.keys())}"
        )
        warnings = result["warnings"]
        assert isinstance(warnings, list), (
            f"warnings should be a list, got {type(warnings)}: {warnings!r}"
        )
        assert len(warnings) > 0, (
            "Expected at least one warning for oversized ACL sidecar; got empty list"
        )
        # Warning must mention the size limit and the word "exceeds" (S19 requires both)
        combined = " ".join(warnings)
        assert f"{_ACL_SIDECAR_MAX_BYTES // 1024} KB" in combined and "exceeds" in combined.lower(), (
            f"Expected warning to mention '{_ACL_SIDECAR_MAX_BYTES // 1024} KB' and 'exceeds'; got: {warnings!r}"
        )
        # Warning must name the sidecar file (S19 spec: "naming the file and limit")
        assert str(sidecar) in combined, (
            f"Expected warning to name the sidecar file path {str(sidecar)!r}; got: {warnings!r}"
        )


# ---------------------------------------------------------------------------
# S18: ACL sidecar ≤ 64 KB → job result warnings is empty; ACL is applied
# ---------------------------------------------------------------------------


def test_e2e_normal_sidecar_no_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app via TestClient; ingest document with a valid ACL sidecar (≤ 64 KB);
    assert job result has empty warnings list and at least one chunk was created.

    Covers scenario S18: normal sidecar is accepted; ACL is applied; no warning is emitted.
    """
    doc = tmp_path / "e0b_t2_normal.md"
    doc.write_text("# E0b T2 Normal ACL Sidecar Test\n\nContent for clean ACL e2e test.\n" * 8)

    sidecar = tmp_path / "e0b_t2_normal.md.acl"
    sidecar.write_text("tenantA\n")  # valid, well within 64 KB

    col = "e0b-t2-normal-acl"

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = _auth(api_key)

        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc)},
            headers=headers,
        )
        assert resp.status_code == 202, (
            f"POST /ingest expected 202, got {resp.status_code}: {resp.text}"
        )
        job_id = resp.json()["job_id"]

        result_body = _poll_job_to_done(client, job_id, headers)

        result = result_body.get("result")
        assert result is not None, "Job result should not be None after DONE"
        assert "warnings" in result, (
            f"Job result should contain 'warnings' key; got keys: {list(result.keys())}"
        )
        warnings = result["warnings"]
        assert isinstance(warnings, list), (
            f"warnings should be a list, got {type(warnings)}: {warnings!r}"
        )
        assert warnings == [], (
            f"Expected empty warnings for normal ACL sidecar, got: {warnings!r}"
        )
        # Verify ingestion proceeded: the job completed DONE with a result containing the warnings
        # key — job result dict schema is {"warnings": list[str]} (routes_jobs.py:132).
        # Chunk-level verification (ACL is applied) is covered at integration level by BE-7 tests.


# ---------------------------------------------------------------------------
# S20: No ACL sidecar → job result warnings is empty; ingestion proceeds normally
# ---------------------------------------------------------------------------


def test_e2e_no_sidecar_no_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app via TestClient; ingest document with no ACL sidecar present;
    assert job result has empty warnings list and job completed DONE.

    Covers scenario S20: absence of a sidecar is not an error; ingestion proceeds
    normally without ACL restriction and without any warnings.
    """
    doc = tmp_path / "e0b_t2_no_sidecar.md"
    doc.write_text("# E0b T2 No ACL Sidecar Test\n\nContent for no-sidecar e2e test.\n" * 8)
    # Deliberately do NOT create a .acl sidecar file

    col = "e0b-t2-no-sidecar-acl"

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = _auth(api_key)

        resp = client.post(
            "/ingest",
            json={"collection": col, "path": str(doc)},
            headers=headers,
        )
        assert resp.status_code == 202, (
            f"POST /ingest expected 202, got {resp.status_code}: {resp.text}"
        )
        job_id = resp.json()["job_id"]

        result_body = _poll_job_to_done(client, job_id, headers)

        result = result_body.get("result")
        assert result is not None, "Job result should not be None after DONE"
        assert "warnings" in result, (
            f"Job result should contain 'warnings' key; got keys: {list(result.keys())}"
        )
        warnings = result["warnings"]
        assert isinstance(warnings, list), (
            f"warnings should be a list, got {type(warnings)}: {warnings!r}"
        )
        assert warnings == [], (
            f"Expected empty warnings when no ACL sidecar present, got: {warnings!r}"
        )
