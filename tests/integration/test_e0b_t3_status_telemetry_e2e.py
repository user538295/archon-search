"""E0b / T-3 — e2e: GET /status key availability, failed_expired_count, and
GET /telemetry/stats truncated_count.

Scenarios covered:
  S7:  HyDE enabled, ANTHROPIC_API_KEY absent → GET /status hyde.key_available=false
  S14: FAILED_EXPIRED job seeded → GET /status failed_expired_ingest_count == 1
  S16: Non-truncated telemetry entry written → GET /telemetry/stats truncated_count == 0
  S17: Truncated telemetry entry written → GET /telemetry/stats truncated_count == 1

Note: TestClient-based tests are integration-level (in-process ASGI). Labeled
#e2e_test in the plan because they exercise the full application stack with a
real SearchPipeline, real LanceDB store, and real ASGI middleware chain.
True process-isolated e2e is not required for E0b.

S6 (key_available=true when key set) and S13 (GET /jobs?status=FAILED_EXPIRED) are
covered at integration level in tests/test_routes_status_be8.py and
tests/test_routes_status_be10.py respectively. T-3 provides a complementary
full-stack verification using make_real_app (real pipeline) instead of the
mocked pipeline used by those BE tests.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.telemetry.entry import TelemetryEntry
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# S7: HyDE enabled, ANTHROPIC_API_KEY absent → hyde.key_available=false
# ---------------------------------------------------------------------------


def test_e2e_status_key_available_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app with hyde.enabled=True and no ANTHROPIC_API_KEY; assert hyde.key_available=false.

    Covers scenario S7: the status route must expose key availability as false when
    the feature is configured but the API key is absent from the environment.
    conftest.py clears ANTHROPIC_API_KEY for every test, so no extra monkeypatch action
    is needed.
    """
    # ANTHROPIC_API_KEY is cleared by the root conftest.py for every test.
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))

        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "hyde" in data, "GET /status must include 'hyde' key when hyde.enabled=True"
        hyde = data["hyde"]
        assert hyde is not None, (
            "GET /status hyde must not be null when hyde.enabled=True"
        )
        assert hyde["key_available"] is False, (
            f"expected hyde.key_available=false when ANTHROPIC_API_KEY is absent, "
            f"got: {hyde['key_available']!r}"
        )


# ---------------------------------------------------------------------------
# S14: FAILED_EXPIRED job seeded → failed_expired_ingest_count == 1
# ---------------------------------------------------------------------------


def test_e2e_status_failed_expired_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app; seed one FAILED_EXPIRED IngestJob before startup; assert
    failed_expired_ingest_count == 1 in GET /status.

    Covers scenario S14: the status route must count FAILED_EXPIRED IngestJob
    instances in the caller's namespace and surface the count.

    Jobs are pre-seeded by writing to the same jobs.json path that make_real_app
    uses for its internal JobStore.  The app's JobStore reads from that file on
    init, so the pre-seeded job is visible immediately at startup.
    """
    jobs_path = tmp_path / "jobs.json"

    # Pre-seed: create a FAILED_EXPIRED job in DEFAULT_NAMESPACE before the app starts.
    pre_store = JobStore(path=jobs_path)
    job = pre_store.create(namespace=DEFAULT_NAMESPACE)
    pre_store.update(job.job_id, status=JobStatus.FAILED)
    pre_store.update(job.job_id, status=JobStatus.FAILED_EXPIRED)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.get("/status", headers=_auth(api_key))

        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "failed_expired_ingest_count" in data, (
            "GET /status must include 'failed_expired_ingest_count' field"
        )
        count = data["failed_expired_ingest_count"]
        assert count == 1, (
            f"expected failed_expired_ingest_count == 1 after seeding exactly one FAILED_EXPIRED job, "
            f"got: {count}"
        )


# ---------------------------------------------------------------------------
# S17: Truncated telemetry entry written → truncated_count == 1
# ---------------------------------------------------------------------------


def test_e2e_telemetry_stats_truncated_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app with telemetry enabled; write a truncated JSONL entry directly to
    the telemetry log dir; assert GET /telemetry/stats truncated_count == 1.

    Covers scenario S17: a telemetry entry with truncated=True must be counted in
    the stats response.

    The TelemetryReader reads from the JSONL files in log_dir on each request.
    Writing a truncated entry directly to the daily file (outside the writer queue)
    is the standard approach used by BE-9 integration tests and avoids the
    complexity of generating a result set large enough to trigger truncation.
    """
    with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Write a truncated telemetry entry to today's JSONL file in the log dir.
        log_dir = Path(cfg.telemetry.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        today_iso = datetime.now(UTC).date().isoformat()
        jsonl_file = log_dir / f"{today_iso}.jsonl"

        entry = TelemetryEntry(
            query_id="e0b-t3-truncated-entry",
            timestamp=datetime.now(UTC).isoformat(),
            endpoint="search",
            latency_ms=10.0,
            status="ok",
            collection="e0b-t3-col",
            result_count=5,
            truncated=True,
        )
        with jsonl_file.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

        resp = client.get("/telemetry/stats", headers=_auth(api_key))

        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "truncated_count" in data, (
            "GET /telemetry/stats must include 'truncated_count' field"
        )
        truncated_count = data["truncated_count"]
        assert truncated_count == 1, (
            f"expected truncated_count == 1 after writing exactly one truncated telemetry entry, "
            f"got: {truncated_count}"
        )


# ---------------------------------------------------------------------------
# S16: Non-truncated telemetry entry written → truncated_count == 0
# ---------------------------------------------------------------------------


def test_e2e_telemetry_stats_no_truncation_for_small_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app with telemetry enabled; write a non-truncated JSONL entry directly
    to the telemetry log dir; assert GET /telemetry/stats truncated_count == 0.

    Covers scenario S16: a telemetry entry with truncated=None (the default, omitted)
    must NOT be counted in the truncated_count statistic. A result set that fits
    within the configured limit does not set truncated=True.

    This test verifies the `None` (omitted) path. The reader counts entries where
    `entry.truncated is True` (identity check), so both `None` and `False` are
    correctly excluded. The explicit `truncated=False` exclusion is covered at unit
    level by `tests/telemetry/test_reader.py::test_compute_stats_truncated_count_excludes_false_entries`.

    The TelemetryReader reads from the JSONL files in log_dir on each request.
    Writing the entry directly to the daily file (outside the writer queue) is the
    standard approach and avoids the complexity of running a full search cycle.
    """
    with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Write a non-truncated telemetry entry to today's JSONL file in the log dir.
        log_dir = Path(cfg.telemetry.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        today_iso = datetime.now(UTC).date().isoformat()
        jsonl_file = log_dir / f"{today_iso}.jsonl"

        entry = TelemetryEntry(
            query_id="e0b-t3-normal-entry",
            timestamp=datetime.now(UTC).isoformat(),
            endpoint="search",
            latency_ms=10.0,
            status="ok",
            collection="e0b-t3-col",
            result_count=2,
        )
        with jsonl_file.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

        resp = client.get("/telemetry/stats", headers=_auth(api_key))

        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "truncated_count" in data, (
            "GET /telemetry/stats must include 'truncated_count' field"
        )
        truncated_count = data["truncated_count"]
        assert truncated_count == 0, (
            f"expected truncated_count == 0 for a non-truncated telemetry entry, "
            f"got: {truncated_count}"
        )
