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
        valid_key = "a" * 64  # must be a valid 64-char lowercase hex string
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", valid_key)
        runner = CliRunner()
        job_data = _make_job_response(status="DONE")
        mock_resp = _mock_httpx_get(job_data)

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            result = runner.invoke(jobs, ["status", "job-abc-123"])

        assert result.exit_code == 0
        called_headers = mock_get.call_args[1]["headers"]
        assert called_headers.get("Authorization") == f"Bearer {valid_key}"

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
# jobs list
# ---------------------------------------------------------------------------

class TestJobsList:
    def _make_list_response(self, items=None):
        if items is None:
            items = [
                {
                    "job_id": "abcdef12-0000-0000-0000-000000000001",
                    "job_type": "ingest",
                    "status": "DONE",
                    "collection": "my-col",
                    "created_at": "2026-07-15T10:00:00+00:00",
                    "updated_at": "2026-07-15T10:02:14+00:00",
                },
            ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"items": items, "next_cursor": None, "total": len(items)}
        mock_resp.text = ""
        return mock_resp

    def test_list_prints_job_id_truncated(self):
        """list output shows the first 8 chars of the job_id."""
        runner = CliRunner()
        mock_resp = self._make_list_response()
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 0, result.output
        assert "abcdef12" in result.output

    def test_list_prints_job_type_and_status(self):
        """list output includes job_type and status columns."""
        runner = CliRunner()
        mock_resp = self._make_list_response()
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert "ingest" in result.output
        assert "DONE" in result.output
        assert "my-col" in result.output

    def test_list_forwards_status_filter(self):
        """--status is forwarded as params to httpx.get."""
        runner = CliRunner()
        mock_resp = self._make_list_response(items=[])
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            runner.invoke(jobs, ["list", "--status", "running", "--api-key", "test-key"])
        call_kwargs = mock_get.call_args
        # params kwarg contains ("status", "running")
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params", [])
        assert any(k == "status" and v == "running" for k, v in params)

    def test_list_forwards_limit(self):
        """--limit is forwarded as params to httpx.get."""
        runner = CliRunner()
        mock_resp = self._make_list_response(items=[])
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            runner.invoke(jobs, ["list", "--limit", "10", "--api-key", "test-key"])
        call_kwargs = mock_get.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params", [])
        assert any(k == "limit" and str(v) == "10" for k, v in params)

    def test_list_connect_error_exits_1(self):
        """httpx.ConnectError → server-not-running message, exit 1."""
        import httpx as _httpx
        runner = CliRunner()
        with patch("httpx.get", side_effect=_httpx.ConnectError("refused")):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 1
        assert "not running" in result.output.lower() or "start it first" in result.output.lower()

    def test_list_empty_shows_no_rows(self):
        """Empty list prints no job rows."""
        runner = CliRunner()
        mock_resp = self._make_list_response(items=[])
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 0
        assert "abcdef12" not in result.output

    def test_list_server_error_exits_1(self):
        """Non-200 response (e.g. 500) → error message, exit 1."""
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 1
        assert "500" in result.output

    def test_list_multiple_status_filters_forwarded(self):
        """Multiple --status flags are all forwarded as repeated params."""
        runner = CliRunner()
        mock_resp = self._make_list_response(items=[])
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            runner.invoke(jobs, ["list", "--status", "running", "--status", "queued", "--api-key", "test-key"])
        call_kwargs = mock_get.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params", [])
        statuses = [v for k, v in params if k == "status"]
        assert "running" in statuses
        assert "queued" in statuses

    def test_list_renders_non_ingest_job_type(self):
        """job_type='migration' from response is rendered in the output row."""
        runner = CliRunner()
        job = {
            "job_id": "abcdef123456",
            "job_type": "migration",
            "status": "DONE",
            "collection": "mytest",
            "created_at": "2026-07-15T10:00:00Z",
            "updated_at": "2026-07-15T10:00:30Z",
        }
        mock_resp = self._make_list_response(items=[job])
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 0
        assert "migration" in result.output

    def test_list_limit_zero_rejected(self):
        """--limit 0 is rejected by click.IntRange(min=1)."""
        runner = CliRunner()
        result = runner.invoke(jobs, ["list", "--limit", "0", "--api-key", "test-key"])
        assert result.exit_code != 0

    def test_list_limit_over_max_rejected(self):
        """--limit 201 is rejected by click.IntRange(min=1, max=200)."""
        runner = CliRunner()
        result = runner.invoke(jobs, ["list", "--limit", "201", "--api-key", "test-key"])
        assert result.exit_code != 0

    def test_list_shows_truncation_footer_when_total_exceeds_items(self):
        """When total > len(items), a 'Showing N of total' footer is printed."""
        runner = CliRunner()
        job = {
            "job_id": "abcdef123456",
            "job_type": "ingest",
            "status": "DONE",
            "collection": "mytest",
            "created_at": "2026-07-15T10:00:00Z",
            "updated_at": "2026-07-15T10:00:30Z",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"items": [job], "total": 100, "next_cursor": "some-cursor"}
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 0
        assert "Showing 1 of 100" in result.output

    def test_list_no_footer_when_total_equals_items(self):
        """When total == len(items), no truncation footer is printed."""
        runner = CliRunner()
        job = {
            "job_id": "abcdef123456",
            "job_type": "ingest",
            "status": "DONE",
            "collection": "mytest",
            "created_at": "2026-07-15T10:00:00Z",
            "updated_at": "2026-07-15T10:00:30Z",
        }
        mock_resp = self._make_list_response(items=[job])
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--api-key", "test-key"])
        assert result.exit_code == 0
        assert "Showing" not in result.output


# ---------------------------------------------------------------------------
# jobs show
# ---------------------------------------------------------------------------

class TestJobsShow:
    def test_show_done_prints_full_detail(self):
        """show prints job_id, job_type, status, collection, created_at."""
        runner = CliRunner()
        job_data = {
            **_make_job_response(job_id="full-uuid-here", status="DONE"),
            "job_type": "ingest",
            "updated_at": "2026-07-15T10:02:14Z",
            "source_path": "/data/file.txt",
            "source": "user",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = job_data
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["show", "full-uuid-here", "--api-key", "test-key"])
        assert result.exit_code == 0, result.output
        assert "full-uuid-here" in result.output
        assert "ingest" in result.output
        assert "DONE" in result.output

    def test_show_failed_exits_1(self):
        """show FAILED → exit 1."""
        runner = CliRunner()
        job_data = {**_make_job_response(status="FAILED", error="disk full"), "job_type": "ingest", "updated_at": "2026-07-15T10:00:01Z", "source_path": "", "source": "user"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = job_data
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["show", "job-abc", "--api-key", "test-key"])
        assert result.exit_code == 1
        assert "disk full" in result.output

    def test_show_404_exits_1_with_message(self):
        """404 → 'Job <id> not found.', exit 1."""
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["show", "missing-id", "--api-key", "test-key"])
        assert result.exit_code == 1
        assert "missing-id" in result.output

    def test_show_wait_delegates_to_poll_job(self):
        """--wait flag invokes _poll_job and returns job detail on DONE."""
        runner = CliRunner()
        done_job = {
            **_make_job_response(status="DONE"),
            "job_type": "ingest",
            "updated_at": "2026-07-15T10:02:14Z",
            "source_path": "",
            "source": "user",
        }
        with patch("archon_search.cli.jobs_cmd._poll_job", return_value=done_job) as mock_poll:
            result = runner.invoke(jobs, ["show", "job-abc-123", "--wait", "--api-key", "test-key"])
        assert result.exit_code == 0, result.output
        mock_poll.assert_called_once()

    def test_show_wait_no_job_returned_exits_0(self):
        """--wait with empty dict from _poll_job (interrupt or timeout) → exit 0, no detail printed."""
        runner = CliRunner()
        with patch("archon_search.cli.jobs_cmd._poll_job", return_value={}):
            result = runner.invoke(jobs, ["show", "job-abc-123", "--wait", "--api-key", "test-key"])
        assert result.exit_code == 0

    def test_show_wait_failed_exits_1(self):
        """--wait with a FAILED job (SystemExit(1) from _poll_job) → exit 1."""
        runner = CliRunner()
        with patch("archon_search.cli.jobs_cmd._poll_job", side_effect=SystemExit(1)):
            result = runner.invoke(jobs, ["show", "job-abc-123", "--wait", "--api-key", "test-key"])
        assert result.exit_code == 1

    def test_show_connect_error_exits_1(self):
        """jobs show non-wait ConnectError → friendly message + exit 1."""
        import httpx as _httpx
        runner = CliRunner()
        with patch("archon_search.cli.jobs_cmd.httpx.get", side_effect=_httpx.ConnectError("refused")):
            result = runner.invoke(jobs, ["show", "job-abc-123", "--api-key", "test-key"])
        assert result.exit_code == 1
        assert "not running" in result.output.lower() or "start it first" in result.output.lower()

    def test_show_wait_timeout_forwarded_to_poll_job(self):
        """--timeout is forwarded to _poll_job as timeout_seconds."""
        runner = CliRunner()
        done_job = {
            **_make_job_response(status="DONE"),
            "job_type": "ingest",
            "updated_at": "2026-07-15T10:02:14Z",
            "source_path": "",
            "source": "user",
        }
        with patch("archon_search.cli.jobs_cmd._poll_job", return_value=done_job) as mock_poll:
            runner.invoke(jobs, ["show", "job-abc-123", "--wait", "--timeout", "120", "--api-key", "test-key"])
        mock_poll.assert_called_once()
        _, kwargs = mock_poll.call_args
        assert kwargs.get("timeout_seconds") == 120


# ---------------------------------------------------------------------------
# _fmt_elapsed
# ---------------------------------------------------------------------------

class TestFmtElapsed:
    def test_elapsed_terminal_job_seconds_only(self):
        """Terminal job within 1 minute shows elapsed as Ns."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        result = _fmt_elapsed("2026-07-15T10:00:00+00:00", "2026-07-15T10:00:45+00:00", "DONE")
        assert result == "45s"

    def test_elapsed_terminal_job_minutes_and_seconds(self):
        """Terminal job over 1 minute shows as Nm Ns."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        result = _fmt_elapsed("2026-07-15T10:00:00+00:00", "2026-07-15T10:02:14+00:00", "DONE")
        assert result == "2m 14s"

    def test_elapsed_failed_job_uses_updated_at(self):
        """FAILED job uses updated_at as end time."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        result = _fmt_elapsed("2026-07-15T10:00:00+00:00", "2026-07-15T10:00:30+00:00", "FAILED")
        assert result == "30s"

    def test_elapsed_invalid_timestamp_returns_dash(self):
        """Invalid timestamp returns '-'."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        result = _fmt_elapsed("not-a-date", "also-not-a-date", "DONE")
        assert result == "-"

    def test_elapsed_empty_timestamps_returns_dash(self):
        """Empty timestamps return '-'."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        result = _fmt_elapsed("", "", "DONE")
        assert result == "-"

    def test_elapsed_zero_seconds(self):
        """Same created and updated timestamps → 0s."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        result = _fmt_elapsed("2026-07-15T10:00:00+00:00", "2026-07-15T10:00:00+00:00", "DONE")
        assert result == "0s"

    def test_elapsed_running_job_naive_start_returns_dash(self):
        """RUNNING job with naive (no tz) created_at → TypeError caught → '-'."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        # Naive datetime + aware now() → TypeError in subtraction → "-"
        result = _fmt_elapsed("2026-07-15T10:00:00", "", "RUNNING")
        assert result == "-"

    def test_elapsed_running_job_aware_start_returns_string(self):
        """RUNNING job with aware created_at far in the past → valid elapsed string."""
        from archon_search.cli.jobs_cmd import _fmt_elapsed
        # Use a past date so elapsed is always positive and non-zero
        result = _fmt_elapsed("2020-01-01T00:00:00+00:00", "", "RUNNING")
        # Result should be a valid duration string (minutes-based for a multi-year span)
        assert result != "-"
        assert "m" in result or result.endswith("s")


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
