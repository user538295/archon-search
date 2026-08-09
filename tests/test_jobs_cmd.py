"""Unit tests for archon_search.cli.jobs_cmd — FE-3 (CSP120 S24)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.jobs_cmd import _clean, jobs


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


def _data_rows(output: str) -> list[str]:
    """Physical job rows in `jobs list` output, minus header, rule and footer."""
    return [
        line
        for line in output.splitlines()
        if line.strip()
        and not line.startswith("ID ")
        and set(line.strip()) != {"-"}
        and not line.startswith("Showing ")
    ]


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
        assert "Showing 1 of 100 jobs — use --limit to see more (max: 200)." in result.output

    def test_list_one_row_per_job_when_fields_contain_control_chars(self):
        """S361: control characters in a job row must not split it across physical rows.

        ``collection`` is a free-form, user-controlled string (``POST /ingest``
        only rejects the empty string). Embedded control characters used to make a
        single job render as several physical lines, so the printed row count
        exceeded ``--limit`` and no longer matched the ``Showing N of M jobs``
        footer, which counts logical items.

        Four of the five displayed fields are poisoned here, so dropping
        ``_clean`` from any of them fails this test. The fifth, ``created_at``,
        is pinned by ``test_list_created_at_control_char_kept_on_one_row``.
        """
        runner = CliRunner()
        items = [
            {
                "job_id": "ab\ncd\ref12",
                "job_type": "ing\nest",
                "status": "PEN\nDING",
                "collection": "evil\nname\rrows",
                "created_at": "2026-07-15T10:00:00+00:00",
                "updated_at": "2026-07-15T10:00:00+00:00",
            },
            {
                "job_id": "beefcafe0000",
                "job_type": "ingest",
                "status": "PENDING",
                "collection": "normal",
                "created_at": "2026-07-15T10:00:00+00:00",
                "updated_at": "2026-07-15T10:00:00+00:00",
            },
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"items": items, "total": 100, "next_cursor": "c"}
        mock_resp.text = ""
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--limit", "2", "--api-key", "test-key"])

        assert result.exit_code == 0, result.output
        data_rows = _data_rows(result.output)
        assert len(data_rows) == 2, f"limit=2 but printed {len(data_rows)} physical rows: {data_rows}"
        assert "Showing 2 of 100" in result.output
        # The sanitised text is still shown, not dropped.
        assert "evil name rows" in result.output

    def test_list_created_at_control_char_kept_on_one_row(self):
        """S361: ``created_at`` gets an extra transform, so it is pinned separately."""
        runner = CliRunner()
        items = [
            {
                "job_id": "abcdef123456",
                "job_type": "ingest",
                "status": "PENDING",
                "collection": "mycol",
                "created_at": "2026-07-15\n10:00:00+00:00",
                "updated_at": "2026-07-15T10:00:00+00:00",
            },
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"items": items, "total": 100, "next_cursor": "c"}
        mock_resp.text = ""
        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(jobs, ["list", "--limit", "1", "--api-key", "test-key"])

        assert result.exit_code == 0, result.output
        data_rows = _data_rows(result.output)
        assert len(data_rows) == 1, f"printed {len(data_rows)} physical rows: {data_rows}"
        assert "Showing 1 of 100" in result.output

    def test_list_footer_omits_limit_hint_when_not_truncated(self):
        """S364: when all matching jobs were returned (N == M), the footer must not
        include the '— use --limit to see more' hint.

        The footer is still printed (S361: ``Showing N of M jobs.``) so that the
        row count can be cross-checked, but the misleading 'see more' advice is
        suppressed when nothing was withheld.
        """
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
            result = runner.invoke(jobs, ["list", "--limit", "200", "--api-key", "test-key"])
        assert result.exit_code == 0
        assert "Showing 1 of 1 jobs." in result.output
        assert "use --limit to see more" not in result.output
        assert len(_data_rows(result.output)) == 1


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
# control characters in the detail views
# ---------------------------------------------------------------------------

class TestDetailViewsSanitised:
    """S361 follow-up: `jobs show` / `jobs status` echo the same untrusted fields.

    Neither has a row budget, so no `--limit` invariant is at stake — but both
    would otherwise pass a terminal escape straight through to the operator.
    """

    @staticmethod
    def _job() -> dict:
        return {
            "job_id": "abcdef123456",
            "job_type": "ingest",
            "status": "FAILED",
            "collection": "evil\nname",
            "source": "user",
            "source_path": "/tmp/a\rb",
            "created_at": "2026-07-15T10:00:00Z",
            "updated_at": "2026-07-15T10:00:30Z",
            "error": "boom\x1b[31m\nsecond line",
        }

    def test_show_output_has_no_control_characters(self):
        """`jobs show` never emits a non-printable character."""
        runner = CliRunner()
        with patch("httpx.get", return_value=_mock_httpx_get(self._job())):
            result = runner.invoke(jobs, ["show", "abcdef123456", "--api-key", "test-key"])

        assert result.exit_code == 1, result.output  # FAILED job
        bad = [c for c in result.output if not c.isprintable() and c != "\n"]
        assert not bad, f"unsanitised characters in output: {bad!r}"
        assert "evil name" in result.output

    def test_status_output_has_no_control_characters(self):
        """`jobs status` never emits a non-printable character."""
        runner = CliRunner()
        with patch("httpx.get", return_value=_mock_httpx_get(self._job())):
            result = runner.invoke(jobs, ["status", "abcdef123456", "--api-key", "test-key"])

        assert result.exit_code == 1, result.output  # FAILED job
        bad = [c for c in result.output if not c.isprintable() and c != "\n"]
        assert not bad, f"unsanitised characters in output: {bad!r}"
        assert "evil name" in result.output


# ---------------------------------------------------------------------------
# _clean
# ---------------------------------------------------------------------------

class TestClean:
    # Every character str.splitlines() breaks on - these are what can turn one
    # job into several physical rows (S361). Written as \u escapes on purpose: a
    # literal separator here would split this very source line for any reader
    # that uses splitlines(), and an editor "tidying" it into a plain space
    # would silently make the case vacuous.
    @pytest.mark.parametrize(
        "char",
        ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    )
    def test_clean_neutralises_every_line_boundary(self, char: str):
        """No output of _clean may ever split into more than one line."""
        assert _clean(f"a{char}b") == "a b"
        assert len(_clean(f"a{char}b").splitlines()) == 1

    @pytest.mark.parametrize(
        "char", ["\x00", "\t", "\x1b", "\x7f", "\u200b", "\u202e"]
    )
    def test_clean_replaces_other_control_chars(self, char: str):
        """Terminal escapes and zero-width/bidi characters are also replaced."""
        assert _clean(f"a{char}b") == "a b"

    @pytest.mark.parametrize(
        "value", ["my-col_01", "hello world", "\u65e5\u672c\u8a9e", "caf\u00e9", "\U0001f389"]
    )
    def test_clean_preserves_legitimate_text(self, value: str):
        """Ordinary names, including spaces and non-ASCII, pass through unchanged."""
        assert _clean(value) == value

    def test_clean_is_length_preserving(self):
        """1:1 substitution keeps column widths correct when callers slice afterwards."""
        value = "evil\nname\rrows x"
        assert len(_clean(value)) == len(value)


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
