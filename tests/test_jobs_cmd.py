"""Unit tests for archon_search.cli.jobs_cmd — FE-3 (CSP120 S24)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.jobs_cmd import jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job_response(
    job_id: str = "job-abc-123",
    status: str = "DONE",
    collection: str = "smoke",
    created_at: str = "2026-07-15T10:00:00Z",
    progress: dict | None = None,
    error: str | None = None,
) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "collection": collection,
        "created_at": created_at,
        "progress": progress,
        "error": error,
    }


def _mock_httpx_get(json_data: dict, status_code: int = 200) -> MagicMock:
    """Return a mock for httpx.get that returns the given json and status_code."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.text = str(json_data)
    return mock_resp


# ---------------------------------------------------------------------------
# test_jobs_status_done_exits_0
# ---------------------------------------------------------------------------

class TestJobsStatusDone:
    def test_jobs_status_done_exits_0(self):
        """DONE response → prints all status fields (job_id, status, collection, created_at), exit 0."""
        runner = CliRunner()
        job_data = _make_job_response(status="DONE")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0, result.output
        assert "job-abc-123" in result.output
        assert "DONE" in result.output
        assert "smoke" in result.output
        assert "2026-07-15T10:00:00Z" in result.output

    def test_jobs_status_done_no_progress_printed_when_null(self):
        """When progress is null, no progress line is printed."""
        runner = CliRunner()
        job_data = _make_job_response(status="DONE", progress=None)
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0
        assert "progress:" not in result.output

    def test_jobs_status_done_progress_printed_when_present(self):
        """When progress is non-null, progress is printed."""
        runner = CliRunner()
        job_data = _make_job_response(
            status="DONE",
            progress={"phase": "indexing", "processed": 5, "total": 10},
        )
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0
        assert "indexing" in result.output


# ---------------------------------------------------------------------------
# test_jobs_status_failed_exits_1
# ---------------------------------------------------------------------------

class TestJobsStatusFailed:
    @pytest.mark.parametrize("status", ["FAILED", "FAILED_EXPIRED", "CANCELLED"])
    def test_jobs_status_terminal_failure_exits_1(self, status: str):
        """FAILED, FAILED_EXPIRED, CANCELLED → exit 1."""
        runner = CliRunner()
        job_data = _make_job_response(status=status, error="something went wrong")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1, f"Expected exit 1 for status {status}"

    def test_jobs_status_failed_prints_error_field(self):
        """FAILED → error field is printed."""
        runner = CliRunner()
        job_data = _make_job_response(status="FAILED", error="disk full")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1
        assert "disk full" in result.output

    def test_jobs_status_failed_expired_prints_error(self):
        """FAILED_EXPIRED → error field is printed, exit 1."""
        runner = CliRunner()
        job_data = _make_job_response(status="FAILED_EXPIRED", error="max retries exceeded")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1
        assert "max retries exceeded" in result.output


# ---------------------------------------------------------------------------
# test_jobs_status_in_progress_exits_0
# ---------------------------------------------------------------------------

class TestJobsStatusInProgress:
    @pytest.mark.parametrize("status", ["RUNNING", "QUEUED", "PENDING", "CANCELLING"])
    def test_jobs_status_in_progress_exits_0(self, status: str):
        """RUNNING/QUEUED/PENDING/CANCELLING → prints status, exit 0."""
        runner = CliRunner()
        job_data = _make_job_response(status=status)
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0, f"Expected exit 0 for in-progress status {status}"
        assert status in result.output


# ---------------------------------------------------------------------------
# test_jobs_status_404_exits_1
# ---------------------------------------------------------------------------

class TestJobsStatus404:
    def test_jobs_status_404_exits_1(self):
        """404 → 'Job not found: {job_id}', exit 1."""
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = '{"detail": "Job not found"}'

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1
        assert "Job not found" in result.output
        assert "job-abc-123" in result.output

    def test_jobs_status_non_200_non_404_exits_1(self):
        """Non-200 and non-404 (e.g. 500) → error message, exit 1."""
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1
        assert "500" in result.output


# ---------------------------------------------------------------------------
# Additional: httpx error paths
# ---------------------------------------------------------------------------

class TestJobsStatusConnectionErrors:
    def test_jobs_status_connect_error_exits_1(self):
        """httpx.ConnectError → human-readable message, exit 1."""
        import httpx as _httpx

        runner = CliRunner()

        with patch("httpx.get", side_effect=_httpx.ConnectError("refused")):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1
        assert "not running" in result.output.lower() or "archon-search serve" in result.output

    def test_jobs_status_http_error_exits_1(self):
        """Generic httpx.HTTPError → error message, exit 1."""
        import httpx as _httpx

        runner = CliRunner()
        mock_request = MagicMock()

        with patch("httpx.get", side_effect=_httpx.HTTPStatusError("bad", request=mock_request, response=MagicMock())):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Additional: error field suppressed for DONE, --api-url, auth header
# ---------------------------------------------------------------------------

class TestJobsStatusEdgeCases:
    def test_done_job_stale_error_not_printed(self):
        """DONE job with a stale error string → error is NOT printed."""
        runner = CliRunner()
        job_data = _make_job_response(status="DONE", error="stale error text")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0
        assert "stale error text" not in result.output

    def test_custom_api_url_trailing_slash_stripped(self):
        """--api-url with trailing slash → httpx.get called with URL http://host:9000/jobs/job-abc-123."""
        runner = CliRunner()
        job_data = _make_job_response(status="DONE")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            result = runner.invoke(
                jobs,
                ["status", "job-abc-123", "--api-url", "http://host:9000/", "--api-key", "test-key"],
            )

        assert result.exit_code == 0
        called_url = mock_get.call_args[0][0]
        assert called_url.startswith("http://host:9000/jobs/")
        assert called_url == "http://host:9000/jobs/job-abc-123"

    def test_authorization_bearer_header_sent(self):
        """httpx.get is called with Authorization: Bearer <key> header."""
        runner = CliRunner()
        job_data = _make_job_response(status="DONE")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0
        called_headers = mock_get.call_args[1]["headers"]
        assert called_headers.get("Authorization") == "Bearer test-key"

    def test_env_var_api_key_fallback(self, monkeypatch):
        """ARCHON_SEARCH_API_KEY env var is used when --api-key is not provided."""
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "env-key-value")
        runner = CliRunner()
        job_data = _make_job_response(status="DONE")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            result = runner.invoke(jobs, ["status", "job-abc-123"])

        assert result.exit_code == 0
        called_headers = mock_get.call_args[1]["headers"]
        assert called_headers.get("Authorization") == "Bearer env-key-value"

    def test_jobs_status_done_called_once(self):
        """httpx.get is called exactly once (one-shot, no polling)."""
        runner = CliRunner()
        job_data = _make_job_response(status="DONE")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 0
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# Additional: FAILED with error=None
# ---------------------------------------------------------------------------

class TestJobsStatusFailedNoError:
    def test_failed_job_with_no_error_exits_1_and_omits_error_line(self):
        """FAILED job with error=None → exit 1, 'error:' not in output."""
        runner = CliRunner()
        job_data = _make_job_response(status="FAILED", error=None)
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(
                jobs, ["status", "job-abc-123", "--api-key", "test-key"]
            )

        assert result.exit_code == 1
        assert "error:" not in result.output
