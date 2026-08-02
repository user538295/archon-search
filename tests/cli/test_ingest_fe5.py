"""Unit tests for ``archon-search ingest`` CLI proxy (FE-5).

Tests:
- test_ingest_submits_job_with_explicit_collection: --collection explicit → correct collection in request body, job_id printed, exit 0
- test_ingest_derives_collection_name_from_path_when_omitted: omitting --collection → path_to_collection_name used; derived name sent in request body
- test_ingest_wait_polls_to_done: mocked poll → completion marker, exit 0
- test_ingest_server_not_running: ConnectError → "archon-search serve is not running. Start it first.", exit 1
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.cli.ingest import ingest


def _mock_response(status_code: int, body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = text or str(body or "")
    return resp


def _job_response(status: str, progress: dict | None = None) -> MagicMock:
    body: dict = {"job_id": "job-ingest-123", "status": status}
    if progress:
        body["progress"] = progress
    return _mock_response(200, body)


# ---------------------------------------------------------------------------
# test_ingest_submits_job_with_explicit_collection
# ---------------------------------------------------------------------------


def test_ingest_submits_job_with_explicit_collection(tmp_path: Path) -> None:
    """--collection explicit → correct collection in request body, job_id printed, exit 0."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")
    post_resp = _mock_response(202, {"job_id": "job-ingest-001", "status": "RUNNING"})

    with (
        patch("archon_search.cli.ingest.httpx.post", return_value=post_resp) as mock_post,
        patch("archon_search.cli._helpers.httpx.get") as mock_get,
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "my-explicit-collection",
                "--api-url", "http://localhost:8765",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "job-ingest-001" in result.output
    assert "Ingest complete" not in result.output
    mock_post.assert_called_once()
    _, call_kwargs = mock_post.call_args
    request_body = call_kwargs.get("json", {})
    assert request_body.get("collection") == "my-explicit-collection"
    assert request_body.get("path") == str(test_file)
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"
    # S51: a CLI-initiated ingest must attribute chunks as "cli" via the
    # X-Ingested-By header the server already honors — otherwise it is recorded
    # as "http" and the documented "cli" value is unreachable.
    assert call_kwargs.get("headers", {}).get("X-Ingested-By") == "cli"
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# test_ingest_derives_collection_name_from_path_when_omitted
# ---------------------------------------------------------------------------


def test_ingest_derives_collection_name_from_path_when_omitted(tmp_path: Path) -> None:
    """Omitting --collection → path_to_collection_name used; derived name sent in request body."""
    runner = CliRunner()
    test_file = tmp_path / "my_documents.md"
    test_file.write_text("some content")
    post_resp = _mock_response(202, {"job_id": "job-ingest-002", "status": "RUNNING"})

    with (
        patch("archon_search.cli.ingest.httpx.post", return_value=post_resp) as mock_post,
        patch("archon_search.cli._helpers.httpx.get"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    _, call_kwargs = mock_post.call_args
    request_body = call_kwargs.get("json", {})
    # path_to_collection_name uses the full filename (including extension), sanitized:
    # "my_documents.md" → "my_documents_md"
    assert request_body.get("collection") == "my_documents_md"
    assert request_body.get("path") == str(test_file)


# ---------------------------------------------------------------------------
# test_ingest_wait_polls_to_done
# ---------------------------------------------------------------------------


def test_ingest_wait_polls_to_done(tmp_path: Path) -> None:
    """--wait causes polling until DONE; completion marker printed, exit 0."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")
    post_resp = _mock_response(202, {"job_id": "job-ingest-003", "status": "RUNNING"})

    get_job_sequence = [
        _job_response("RUNNING", progress={"phase": "ingest", "processed": 5, "total": 10}),
        _job_response("DONE"),
    ]

    with (
        patch("archon_search.cli.ingest.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "mycol",
                "--wait",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "job-ingest-003" in result.output
    assert "Ingest complete" in result.output
    assert "5/10" in result.output


# ---------------------------------------------------------------------------
# test_ingest_server_not_running
# ---------------------------------------------------------------------------


def test_ingest_server_not_running(tmp_path: Path) -> None:
    """ConnectError → 'archon-search serve is not running. Start it first.', exit 1."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")

    with patch(
        "archon_search.cli.ingest.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "mycol",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 1
    assert "not running" in result.output.lower()
    assert "start it first" in result.output.lower()


# ---------------------------------------------------------------------------
# test_ingest_resolves_relative_path_to_absolute
# ---------------------------------------------------------------------------


def test_ingest_resolves_relative_path_to_absolute() -> None:
    """--path with a relative path → request body contains an absolute path."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-ingest-rel", "status": "RUNNING"})

    with (
        patch("archon_search.cli.ingest.httpx.post", return_value=post_resp) as mock_post,
        patch("archon_search.cli._helpers.httpx.get"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", "relative/path",
                "--collection", "mycol",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    _, call_kwargs = mock_post.call_args
    request_body = call_kwargs.get("json", {})
    sent_path = request_body.get("path", "")
    assert sent_path.startswith("/"), f"Expected absolute path, got: {sent_path!r}"


# ---------------------------------------------------------------------------
# test_ingest_non202_prints_status_and_body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [409, 503, 413])
def test_ingest_non202_prints_status_and_body(tmp_path: Path, status_code: int) -> None:
    """Non-202 response → status code and error text in output, exit 1."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")
    error_text = f"error-body-{status_code}"
    error_resp = _mock_response(status_code, text=error_text)

    with patch("archon_search.cli.ingest.httpx.post", return_value=error_resp):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "mycol",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 1
    assert str(status_code) in result.output
    assert error_text in result.output


# ---------------------------------------------------------------------------
# test_ingest_generic_http_error_exits_1
# ---------------------------------------------------------------------------


def test_ingest_generic_http_error_exits_1(tmp_path: Path) -> None:
    """Generic HTTPError → 'Error contacting server' in output, exit 1."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")

    with patch(
        "archon_search.cli.ingest.httpx.post",
        side_effect=httpx.ReadTimeout("timed out"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "mycol",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 1
    assert "Error contacting server" in result.output


# ---------------------------------------------------------------------------
# test_ingest_wait_exits_1_on_failed
# ---------------------------------------------------------------------------


def test_ingest_wait_exits_1_on_failed(tmp_path: Path) -> None:
    """--wait with a FAILED job → exit 1, 'Ingest complete' NOT in output."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")
    post_resp = _mock_response(202, {"job_id": "job-ingest-fail", "status": "RUNNING"})

    get_job_sequence = [
        _job_response("FAILED"),
    ]

    with (
        patch("archon_search.cli.ingest.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "mycol",
                "--wait",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 1
    assert "Ingest complete" not in result.output


# ---------------------------------------------------------------------------
# test_ingest_missing_path_exits_1
# ---------------------------------------------------------------------------


def test_ingest_missing_path_exits_1() -> None:
    """Invoking ingest with no --path → exit 1, 'Error: --path is required.' in output."""
    runner = CliRunner()
    result = runner.invoke(ingest, ["--api-key", "test-key"])
    assert result.exit_code == 1
    assert "Error: --path is required." in result.output


# ---------------------------------------------------------------------------
# test_ingest_wait_keyboard_interrupt_no_false_complete
# ---------------------------------------------------------------------------


def test_ingest_wait_keyboard_interrupt_no_false_complete(tmp_path: Path) -> None:
    """Ctrl-C during --wait polling → 'Polling stopped' in output, 'Ingest complete' NOT in output, exit 0."""
    runner = CliRunner()
    test_file = tmp_path / "docs.md"
    test_file.write_text("some content")
    post_resp = _mock_response(202, {"job_id": "job-ingest-kbi", "status": "RUNNING"})

    def _raise_kbi(*args, **kwargs):
        raise KeyboardInterrupt

    with (
        patch("archon_search.cli.ingest.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=_raise_kbi),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            ingest,
            [
                "--path", str(test_file),
                "--collection", "mycol",
                "--wait",
                "--api-key", "test-key",
            ],
        )

    assert result.exit_code == 0
    assert "Polling stopped" in result.output
    assert "Ingest complete" not in result.output
