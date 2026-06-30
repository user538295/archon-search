"""BE-7 integration tests: ACL warning propagation via HTTP /ingest and MCP ingest_file.

Covers:
- test_async_ingest_warnings_in_job_result: POST /ingest with oversized ACL sidecar;
  poll GET /jobs/{id}; assert job result contains warnings.
- test_mcp_ingest_file_tool_returns_warnings_for_oversized_sidecar: call MCP
  ingest_file tool with oversized ACL sidecar; assert response contains warnings.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# HTTP: async ingest job result carries warnings
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_async_ingest_warnings_in_job_result(tmp_path: Path, monkeypatch) -> None:
    """POST /ingest with oversized ACL sidecar → job result includes warnings list."""
    from tests.integration.conftest import ingest_file_via_path, make_real_app

    # Write a document + oversized .acl sidecar
    doc = tmp_path / "doc.md"
    doc.write_text("Content for ACL warning integration test.\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"tenantA\n" + b"x" * 65537)  # > 64 KB

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/ingest",
            json={"collection": "test-col", "path": str(doc)},
            headers=headers,
        )
        assert resp.status_code == 202, f"POST /ingest failed: {resp.status_code} {resp.text}"
        job_id = resp.json()["job_id"]

        # Poll until DONE
        deadline = time.monotonic() + 15.0
        result_body = None
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            body = r.json()
            status = body["status"]
            if status == "DONE":
                result_body = body
                break
            if status == "FAILED":
                pytest.fail(f"ingest job FAILED: {body}")
            time.sleep(0.05)

        assert result_body is not None, f"Ingest job did not complete in time (job_id={job_id})"
        result = result_body.get("result")
        assert result is not None, "Job result should not be None after DONE"
        assert "warnings" in result, f"Job result should contain 'warnings', got: {result}"
        warnings = result["warnings"]
        assert isinstance(warnings, list), f"warnings should be a list, got: {type(warnings)}"
        assert len(warnings) > 0, "Expected at least one warning for oversized ACL sidecar"
        assert any("64 KB" in w or "exceeds" in w.lower() for w in warnings)


@pytest.mark.integration
def test_async_ingest_no_warnings_for_normal_sidecar(tmp_path: Path, monkeypatch) -> None:
    """POST /ingest with valid ACL sidecar → job result has empty warnings list."""
    from tests.integration.conftest import make_real_app

    doc = tmp_path / "doc.md"
    doc.write_text("Content for clean ingest test.\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("tenantA\n")

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/ingest",
            json={"collection": "test-col", "path": str(doc)},
            headers=headers,
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        deadline = time.monotonic() + 15.0
        result_body = None
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            body = r.json()
            if body["status"] == "DONE":
                result_body = body
                break
            if body["status"] == "FAILED":
                pytest.fail(f"ingest job FAILED: {body}")
            time.sleep(0.05)

        assert result_body is not None
        result = result_body.get("result")
        assert result is not None, "Job result should not be None after DONE"
        assert "warnings" in result
        assert result["warnings"] == [], f"Expected empty warnings, got: {result['warnings']}"


@pytest.mark.integration
def test_async_ingest_directory_warnings_aggregated_in_job_result(tmp_path: Path, monkeypatch) -> None:
    """POST /ingest (directory) with one oversized ACL sidecar → job result aggregates warnings."""
    from tests.integration.conftest import make_real_app

    subdir = tmp_path / "corpus"
    subdir.mkdir()

    doc1 = subdir / "doc1.md"
    doc1.write_text("First document.\n")
    sidecar1 = subdir / "doc1.md.acl"
    sidecar1.write_bytes(b"tenantA\n" + b"x" * 65537)  # oversized

    doc2 = subdir / "doc2.md"
    doc2.write_text("Second document.\n")
    sidecar2 = subdir / "doc2.md.acl"
    sidecar2.write_text("tenantB\n")  # valid

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/ingest",
            json={"collection": "test-col", "path": str(subdir)},
            headers=headers,
        )
        assert resp.status_code == 202, f"POST /ingest failed: {resp.status_code} {resp.text}"
        job_id = resp.json()["job_id"]

        deadline = time.monotonic() + 15.0
        result_body = None
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            body = r.json()
            if body["status"] == "DONE":
                result_body = body
                break
            if body["status"] == "FAILED":
                pytest.fail(f"ingest job FAILED: {body}")
            time.sleep(0.05)

        assert result_body is not None, "Ingest job did not complete in time"
        result = result_body.get("result")
        assert result is not None
        assert "warnings" in result
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        assert len(warnings) > 0, "Expected warnings aggregated from oversized sidecar in directory ingest"


# ---------------------------------------------------------------------------
# MCP: ingest_file tool response carries warnings
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.xdist_group("mcp")
def test_mcp_ingest_file_tool_returns_warnings_for_oversized_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    """MCP ingest_file tool response includes warnings when ACL sidecar > 64 KB."""
    import json

    from tests.integration.conftest import make_real_app

    doc = tmp_path / "doc.md"
    doc.write_text("Content for MCP ACL warning test.\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_bytes(b"tenantA\n" + b"x" * 65537)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        # Step 1: initialize MCP session
        init_resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers=headers,
        )
        assert init_resp.status_code == 200, f"MCP initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        assert session_id, "MCP session ID not returned"

        session_headers = {**headers, "mcp-session-id": session_id}

        # Step 2: send initialized notification
        notif_resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            headers=session_headers,
        )
        assert notif_resp.status_code < 400, f"Initialized notification failed: {notif_resp.text}"

        # Step 3: call ingest_file tool
        tool_resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "ingest_file",
                    "arguments": {
                        "path": str(doc),
                        "collection": "test-col",
                    },
                },
            },
            headers=session_headers,
        )
        assert tool_resp.status_code == 200, f"MCP ingest_file failed: {tool_resp.text}"

        # MCP responses are SSE-formatted: parse "data: {...}" lines
        payload = None
        for line in tool_resp.text.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                break
        assert payload is not None, f"No SSE data line in tool response: {tool_resp.text!r}"
        assert "error" not in payload, f"MCP tool returned error: {payload}"

        result_content = payload.get("result", {}).get("content", [])
        assert result_content, "MCP tool response content is empty"
        tool_result = json.loads(result_content[0]["text"])

        assert "warnings" in tool_result, f"Tool response missing 'warnings' field: {tool_result}"
        assert isinstance(tool_result["warnings"], list)
        assert len(tool_result["warnings"]) > 0, "Expected warnings for oversized ACL sidecar"
        assert any(
            "64 KB" in w or "exceeds" in w.lower() for w in tool_result["warnings"]
        )
