"""Task 6.3 — Health/status and observability integration tests.

Verifies the full wiring for:
- GET /status readiness.storage_connected (B2)
- Correlation-ID logged in JSONL when log_format='json' (B7)
- stage_timings_ms in POST /explain response body (B1); actual stage names: embed, vector, fuse, rerank, total
- Explicit single-collection /explain path (routes_explain.py:460-470)
- TelemetryWriter JSONL drain on lifespan shutdown (A3)
- No raw query string in telemetry JSONL (structural no-raw-query invariant)

Tests:
    test_get_status_storage_connected_with_real_store
    test_correlation_id_appears_in_log_jsonl
    test_explain_stage_timings_ms_in_response_body
    test_explain_explicit_single_collection_returns_200_with_context
    test_lifespan_telemetry_drains_on_app_shutdown_with_real_writer
    test_telemetry_entry_does_not_contain_raw_query_string

Run with:
    uv run pytest tests/integration/test_observability_integration.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test 1 — GET /status storage_connected with real store (B2)
# ---------------------------------------------------------------------------


def test_get_status_storage_connected_with_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status with a real SearchStore returns readiness.storage_connected == True.

    Verifies the B2 health contract: the storage ping actually runs against the
    real LanceDB connection, not a mock, and surfaces the result through the
    /status JSON response.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, (
            f"GET /status returned {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "readiness" in body, f"'readiness' key missing from /status response: {body}"
        assert body["readiness"]["storage_connected"] is True, (
            "Expected readiness.storage_connected=True with a real connected store, "
            f"got: {body['readiness']['storage_connected']!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Correlation-ID in JSON log (B7)
# ---------------------------------------------------------------------------


def test_correlation_id_appears_in_log_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-Request-Id header is assigned by middleware and appears in JSON log records.

    Sets config.log_format='json' and config.log_file to a tmp_path file before
    building the app. Makes a request and asserts that at least one JSONL record
    contains 'correlation_id' matching the X-Request-Id response header.
    """
    import secrets

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    log_file = tmp_path / "archon.log"

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0
    cfg.log_format = "json"
    cfg.log_file = str(log_file)

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    # Configure logging before building the app so the file handler is attached.
    # In production, run_server() calls configure_logging() before create_app().
    # In tests using TestClient directly, we must do this explicitly.
    from archon_search.logging_setup import configure_logging

    configure_logging(cfg)

    import logging as _logging

    app = create_app(cfg, job_store, scheduler=scheduler)
    search_id = "search-corr-id-xyz789"
    try:
        with TestClient(app) as client:
            # Use a deterministic request-id so we can correlate against the log
            request_id = "test-corr-id-abc123"
            resp = client.get(
                "/status",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Request-ID": request_id,
                },
            )
            assert resp.status_code == 200, (
                f"GET /status returned {resp.status_code}: {resp.text}"
            )
            # Middleware echoes the request-id back in the response header
            returned_id = resp.headers.get("x-request-id")
            assert returned_id == request_id, (
                f"Expected X-Request-Id response header to be {request_id!r}, "
                f"got {returned_id!r}"
            )

            # Ingest + search to produce log records that carry correlation_id.
            # The ingest job and search handler both emit archon_search log messages
            # that the CorrelationIdFilter will tag with the active correlation_id.
            doc_file = tmp_path / "corr_id_doc.txt"
            doc_file.write_text("Correlation ID test document.\n" * 8)
            col = "corr-id-test-col"
            ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

            search_resp = client.post(
                "/search",
                json={"collection": col, "query": "correlation test"},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Request-ID": search_id,
                },
            )
            assert search_resp.status_code == 200, (
                f"POST /search returned {search_resp.status_code}: {search_resp.text}"
            )
            returned_search_id = search_resp.headers.get("x-request-id")
            assert returned_search_id == search_id, (
                f"Expected X-Request-Id response header to be {search_id!r}, "
                f"got {returned_search_id!r}"
            )
    finally:
        # Always clean up the logging handler added by configure_logging() so it
        # does not leak to other tests running in the same xdist worker process.
        _as_logger = _logging.getLogger("archon_search")
        for h in _as_logger.handlers[:]:
            _as_logger.removeHandler(h)
            h.close()
        _as_logger.propagate = True

    # Verify the log file exists with at least one JSON record with correlation_id.
    assert log_file.exists(), (
        f"Log file {log_file} was not created. configure_logging() must attach a "
        "TimedRotatingFileHandler when log_file is set."
    )

    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, (
        f"Log file {log_file} is empty. Expected JSON log records to be written "
        "during the request lifecycle."
    )

    # Assert at least one record carries correlation_id matching the known search_id.
    # The request-context middleware sets correlation_id on every request;
    # any logger.* call within the archon_search hierarchy during request handling
    # will include it via CorrelationIdFilter.
    records_with_search_corr_id = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("correlation_id") == search_id:
            records_with_search_corr_id.append(record)

    assert records_with_search_corr_id, (
        f"Expected at least one JSONL record with correlation_id={search_id!r} in {log_file}. "
        f"Lines found: {lines[:5]!r}. "
        "Check that CorrelationIdFilter is attached to the file handler in configure_logging() "
        "and that the search request handler emits at least one log record."
    )


# ---------------------------------------------------------------------------
# Test 3 — stage_timings_ms in /explain response body (B1)
# ---------------------------------------------------------------------------


def test_explain_stage_timings_ms_in_response_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with stage_timings_enabled=True returns stage_timings_ms dict.

    Explicitly sets config.observability.stage_timings_enabled = True before
    building the app to guarantee timings are recorded. Asserts the response body
    contains 'stage_timings_ms' with at least the 'total' key (the recorder always
    records 'total'; 'embed'/'vector'/'fuse'/'rerank' are recorded by pipeline stages
    via record_stage() calls in embedder.py, store.py, and reranker.py). Verifies B1
    stage timing wiring end-to-end.
    """
    import secrets

    from archon_search.config import ObservabilityConfig, SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0
    cfg.observability = ObservabilityConfig(stage_timings_enabled=True)

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        # Ingest a document so /explain has data to work with
        col = "timings-test-col"
        doc_file = tmp_path / "timings_doc.txt"
        doc_file.write_text("Stage timings test document with enough content to index.\n" * 8)
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        resp = client.post(
            "/explain",
            json={"collection": col, "query": "stage timings test"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, (
            f"POST /explain returned {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "stage_timings_ms" in body, (
            f"Expected 'stage_timings_ms' key in /explain response, got keys: {list(body.keys())}"
        )
        timings = body["stage_timings_ms"]
        assert timings is not None, (
            "stage_timings_ms is None; expected a dict with timing entries. "
            "Check that config.observability.stage_timings_enabled=True is wired through."
        )
        assert isinstance(timings, dict), (
            f"Expected stage_timings_ms to be a dict, got {type(timings).__name__!r}"
        )
        assert len(timings) > 0, (
            f"stage_timings_ms is an empty dict; expected at least 'total'. Got: {timings!r}"
        )
        # 'total' is always recorded by the explain handler itself
        assert "total" in timings, (
            f"Expected 'total' key in stage_timings_ms, got keys: {list(timings.keys())}"
        )
        for key, value in timings.items():
            assert isinstance(value, (int, float)), (
                f"stage_timings_ms[{key!r}] should be a number, got {type(value).__name__!r}"
            )
            assert value >= 0, (
                f"stage_timings_ms[{key!r}]={value!r} should be non-negative"
            )


# ---------------------------------------------------------------------------
# Test 3b — S345: rerank stage key present in stage_timings_ms (B1)
# ---------------------------------------------------------------------------


def test_explain_stage_timings_rerank_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with rerank=True includes all six documented stage keys.

    The docs at 20_monitoring_and_alerts.md:82 list six query-path stages:
    embed, vector, fts, fuse, rerank, total.  When rerank=True (default) and
    a reranker is configured, every key must be present and >= 0.
    Regression guard for S345.
    """
    import secrets

    from archon_search.config import ObservabilityConfig, SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0
    cfg.observability = ObservabilityConfig(stage_timings_enabled=True)

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        col = "s345-rerank-timings"
        doc_file = tmp_path / "rerank_doc.txt"
        doc_file.write_text("Programming language design and compiler theory.\n" * 8)
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        resp = client.post(
            "/explain",
            json={"collection": col, "query": "programming language", "rerank": True},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, f"POST /explain returned {resp.status_code}: {resp.text}"
        body = resp.json()
        timings = body.get("stage_timings_ms")
        assert timings is not None, "stage_timings_ms is None"

        documented_stages = {"embed", "vector", "fts", "fuse", "rerank", "total"}
        missing = documented_stages - set(timings.keys())
        assert not missing, (
            f"stage_timings_ms is missing documented stage(s) {sorted(missing)} "
            f"(20_monitoring_and_alerts.md:82); present keys={sorted(timings.keys())}"
        )
        for key in documented_stages:
            assert isinstance(timings[key], (int, float)), (
                f"stage_timings_ms[{key!r}] should be a number"
            )
            assert timings[key] >= 0, (
                f"stage_timings_ms[{key!r}]={timings[key]!r} should be non-negative"
            )


# ---------------------------------------------------------------------------
# Test 4 — Explicit single-collection /explain path (routes_explain.py:460-470)
# ---------------------------------------------------------------------------


def test_explain_explicit_single_collection_returns_200_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with explicit collection name returns 200 and non-empty results.

    Uses the 'collection' (singular) field, exercising the explicit single-collection
    path at routes_explain.py:460-470. Verifies the response body has 'results'
    and 'stage_timings_ms' (non-null when stage_timings_enabled=True).
    """
    import secrets

    from archon_search.config import ObservabilityConfig, SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0
    cfg.observability = ObservabilityConfig(stage_timings_enabled=True)

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    col = "explicit-col-explain"
    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        doc_file = tmp_path / "explicit_col_doc.txt"
        doc_file.write_text(
            "Explicit collection explain test document content here.\n" * 8
        )
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        # Use 'collection' (singular) — exercises the explicit single-collection path
        resp = client.post(
            "/explain",
            json={"collection": col, "query": "explain test document"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, (
            f"POST /explain returned {resp.status_code}: {resp.text}"
        )
        body = resp.json()

        # The explicit single-collection path must return results
        assert "results" in body, (
            f"'results' key missing from /explain response. Keys: {list(body.keys())}"
        )
        assert isinstance(body["results"], list), (
            f"Expected 'results' to be a list, got {type(body['results']).__name__!r}"
        )
        assert len(body["results"]) > 0, (
            "Expected non-empty results from /explain with a real ingested document. "
            "The explicit single-collection path (routes_explain.py:460-470) must run "
            "the explain pipeline and return scored candidates."
        )

        # stage_timings_ms must be non-null when stage_timings_enabled=True
        assert "stage_timings_ms" in body, (
            f"'stage_timings_ms' key missing from /explain response. Keys: {list(body.keys())}"
        )
        assert body["stage_timings_ms"] is not None, (
            "stage_timings_ms is None in explicit single-collection path. "
            "Expected a dict since stage_timings_enabled=True."
        )


# ---------------------------------------------------------------------------
# Test 5 — TelemetryWriter JSONL drain on app shutdown (A3)
# ---------------------------------------------------------------------------


def test_lifespan_telemetry_drains_on_app_shutdown_with_real_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TelemetryWriter flushes pending entries to JSONL on app lifespan shutdown.

    Creates a real app with telemetry enabled (real TelemetryWriter, real log_dir
    under tmp_path). Triggers a search to produce a telemetry entry. Exits the
    TestClient context (triggering lifespan shutdown). Asserts the JSONL file
    contains at least one record. Verifies the A3 lifespan drain contract.
    """
    import secrets

    from archon_search.config import SearchConfig, TelemetryConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    telemetry_log_dir = tmp_path / "search-logs"

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0
    cfg.telemetry = TelemetryConfig(
        enabled=True,
        log_dir=str(telemetry_log_dir),
    )

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    col = "telemetry-drain-col"
    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        doc_file = tmp_path / "drain_doc.txt"
        doc_file.write_text("Telemetry drain test document content here.\n" * 8)
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        # Trigger a search to enqueue a telemetry entry
        resp = client.post(
            "/search",
            json={"collection": col, "query": "telemetry drain"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, (
            f"POST /search returned {resp.status_code}: {resp.text}"
        )
    # TestClient context exit triggers lifespan shutdown — TelemetryWriter.drain_and_stop() runs.

    # Locate any JSONL file written under the telemetry log dir
    jsonl_files = list(telemetry_log_dir.glob("*.jsonl"))
    assert jsonl_files, (
        f"Expected at least one .jsonl file under {telemetry_log_dir}, "
        "but none found after lifespan shutdown. "
        "TelemetryWriter.drain_and_stop() must flush pending entries before the "
        "lifespan context exits (A3 contract)."
    )

    total_records = 0
    for jsonl_file in jsonl_files:
        lines = [
            ln for ln in jsonl_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        total_records += len(lines)
        for line in lines:
            record = json.loads(line)
            assert "endpoint" in record, (
                f"Telemetry record missing 'endpoint' field: {record!r}"
            )
            assert "latency_ms" in record, (
                f"Telemetry record missing 'latency_ms' field: {record!r}"
            )

    assert total_records > 0, (
        f"JSONL file(s) found under {telemetry_log_dir} but all are empty. "
        "At least one search record must be present after drain."
    )


# ---------------------------------------------------------------------------
# Test 6 — No raw query string in telemetry JSONL (structural privacy invariant)
# ---------------------------------------------------------------------------


def test_telemetry_entry_does_not_contain_raw_query_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry JSONL must not contain the raw query string from POST /search.

    Enables telemetry with log_dir under tmp_path (never the real user path).
    POSTs /search with a unique sentinel query string. Verifies:
      1. At least one JSONL file exists and is non-empty (guards against vacuous pass).
      2. No line in the file contains the literal sentinel string.
    Verifies the structural no-raw-query invariant at the HTTP wiring level.
    """
    import secrets

    from archon_search.config import SearchConfig, TelemetryConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    telemetry_log_dir = tmp_path / "search-logs"
    sentinel_query = "secret_test_query_string_xyz987_must_not_appear_in_telemetry"

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0
    cfg.telemetry = TelemetryConfig(
        enabled=True,
        log_dir=str(telemetry_log_dir),
    )

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    col = "no-query-telemetry-col"
    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        doc_file = tmp_path / "no_query_doc.txt"
        doc_file.write_text("Privacy invariant test document content here.\n" * 8)
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        # POST /search with the sentinel query string
        resp = client.post(
            "/search",
            json={"collection": col, "query": sentinel_query},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, (
            f"POST /search returned {resp.status_code}: {resp.text}"
        )
    # TestClient exit triggers drain_and_stop() — entries flushed to disk.

    jsonl_files = list(telemetry_log_dir.glob("*.jsonl"))
    assert jsonl_files, (
        f"Expected at least one .jsonl file under {telemetry_log_dir} after telemetry drain. "
        "Without any file the test passes vacuously — ensure telemetry is enabled and working."
    )

    all_lines: list[str] = []
    for jsonl_file in jsonl_files:
        all_lines.extend(
            ln for ln in jsonl_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        )

    assert all_lines, (
        f"JSONL file(s) found under {telemetry_log_dir} but all are empty. "
        "At least one search record must be present (prevents vacuous pass)."
    )

    # The sentinel query must not appear anywhere in the telemetry output
    for line in all_lines:
        assert sentinel_query not in line, (
            f"Raw query string {sentinel_query!r} found in telemetry JSONL line:\n{line}\n"
            "The structural no-raw-query invariant is violated: TelemetryEntry factory "
            "methods must never accept or log raw query strings."
        )
