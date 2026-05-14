"""Tests for GET /telemetry/stats route handler — FEAT-039c Task 3.2."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig, TelemetryConfig
from archon_search.server.schemas_telemetry import DisabledResponse, StatsResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    enabled: bool = True,
    log_dir: str = "/tmp/unused-telemetry",
    retention_days: int = 30,
) -> SearchConfig:
    config = SearchConfig()
    config.telemetry = TelemetryConfig(
        enabled=enabled,
        log_dir=log_dir,
        retention_days=retention_days,
    )
    return config


def _make_test_app(config: SearchConfig) -> FastAPI:
    """Create a minimal FastAPI app with only the telemetry router."""
    from archon_search.server.routes_telemetry import router

    app = FastAPI()
    app.state.config = config
    app.include_router(router)
    return app


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _ok_entry(latency_ms: float = 10.0) -> dict:
    return {
        "query_id": "aabbcc",
        "timestamp": "2026-05-15T00:00:00Z",
        "endpoint": "route",
        "latency_ms": latency_ms,
        "status": "ok",
        "collections": ["col_a"],
        "decomposer_invoked": False,
    }


def _timeout_entry(latency_ms: float = 500.0) -> dict:
    return {
        "query_id": "ddeeff",
        "timestamp": "2026-05-15T00:00:00Z",
        "endpoint": "route",
        "latency_ms": latency_ms,
        "status": "timeout",
        "error_kind": "timeout",
    }


# ---------------------------------------------------------------------------
# Stats tests
# ---------------------------------------------------------------------------


def test_stats_disabled_returns_enabled_false() -> None:
    config = _make_config(enabled=False)
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False


def test_stats_no_files_returns_zeros(tmp_path: Path) -> None:
    log_dir = tmp_path / "empty"
    log_dir.mkdir()
    config = _make_config(enabled=True, log_dir=str(log_dir))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["total_queries"] == 0


def test_stats_returns_correct_values(tmp_path: Path) -> None:
    """2 ok + 1 timeout across two files → assert aggregates."""
    today_utc = datetime.now(UTC).date()
    yesterday_utc = today_utc - timedelta(days=1)

    file_today = tmp_path / f"{today_utc.isoformat()}.jsonl"
    file_yesterday = tmp_path / f"{yesterday_utc.isoformat()}.jsonl"

    # File 1: two ok entries with latencies 10ms and 20ms
    _write_jsonl(file_today, [_ok_entry(10.0), _ok_entry(20.0)])
    # File 2: one timeout entry with latency 500ms
    _write_jsonl(file_yesterday, [_timeout_entry(500.0)])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats")
    assert response.status_code == 200
    body = response.json()

    assert body["total_queries"] == 3
    assert body["enabled"] is True
    # 2/3 success
    assert abs(body["success_rate"] - 2 / 3) < 1e-9
    # sorted latencies: [10, 20, 500]; p50 = ceil(50/100*3)-1 = 1 → 20ms
    assert body["latency_ms"]["p50"] == pytest.approx(20.0)
    # by_endpoint.route.total == 3
    assert body["by_endpoint"]["route"]["total"] == 3
    assert body["by_endpoint"]["route"]["ok"] == 2
    assert body["by_endpoint"]["route"]["error"] == 1
    # error_breakdown.timeout == 1
    assert body["error_breakdown"]["timeout"] == 1


def test_stats_since_after_until_returns_400() -> None:
    config = _make_config(enabled=True)
    client = TestClient(_make_test_app(config))
    # since=2026-05-15, until=2026-05-14 → since > until
    response = client.get("/telemetry/stats?since=2026-05-15&until=2026-05-14")
    assert response.status_code == 400


def test_stats_single_future_since_returns_400() -> None:
    """since=2099-01-01 with no until → until defaults to today → since > until."""
    config = _make_config(enabled=True)
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats?since=2099-01-01")
    assert response.status_code == 400


def test_stats_date_range_selects_files(tmp_path: Path) -> None:
    """Only file within range should be included."""
    in_range = date(2026, 5, 14)
    out_of_range = date(2026, 5, 1)

    _write_jsonl(tmp_path / f"{in_range.isoformat()}.jsonl", [_ok_entry(10.0)])
    _write_jsonl(tmp_path / f"{out_of_range.isoformat()}.jsonl", [_ok_entry(99.0)])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats?since=2026-05-14&until=2026-05-14")
    assert response.status_code == 200
    body = response.json()
    assert body["total_queries"] == 1


def test_stats_skipped_lines_counted(tmp_path: Path) -> None:
    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    # One valid entry + one bad JSON line
    with file_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_ok_entry()) + "\n")
        f.write("{bad json\n")

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["skipped_lines"] == 1
    assert body["total_queries"] == 1


def test_stats_schema_version_is_1(tmp_path: Path) -> None:
    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/stats")
    assert response.status_code == 200
    assert response.json()["schema_version"] == 1


def test_stats_uses_asyncio_to_thread(tmp_path: Path) -> None:
    """asyncio.to_thread must be called with reader.read_entries as the first arg."""
    config = _make_config(enabled=True, log_dir=str(tmp_path))

    real_to_thread = asyncio.to_thread

    captured: list = []

    async def _spy_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(func)
        return await real_to_thread(func, *args, **kwargs)

    app = _make_test_app(config)
    with patch("archon_search.server.routes_telemetry.asyncio.to_thread", side_effect=_spy_to_thread):
        client = TestClient(app)
        response = client.get("/telemetry/stats")

    assert response.status_code == 200
    assert len(captured) == 1
    # The function passed to to_thread should be a bound method named read_entries
    assert captured[0].__name__ == "read_entries"


# ---------------------------------------------------------------------------
# Entries helpers
# ---------------------------------------------------------------------------


def _search_entry(
    *,
    status: str = "ok",
    collection: str = "col_a",
    endpoint: str = "search",
    error_kind: str | None = None,
) -> dict:
    entry: dict = {
        "query_id": "aabbcc112233",
        "timestamp": "2026-05-15T00:00:00Z",
        "endpoint": endpoint,
        "latency_ms": 12.5,
        "status": status,
        "collection": collection,
        "result_count": 3,
        "result_doc_ids": ["d1", "d2", "d3"],
    }
    if error_kind is not None:
        entry["error_kind"] = error_kind
    return entry


def _route_entry(*, collection: str = "col_b") -> dict:
    return {
        "query_id": "bbccdd223344",
        "timestamp": "2026-05-15T00:00:00Z",
        "endpoint": "route",
        "latency_ms": 25.0,
        "status": "ok",
        "collections": [collection],
        "decomposer_invoked": False,
    }


# ---------------------------------------------------------------------------
# Entries tests
# ---------------------------------------------------------------------------


def test_entries_disabled_returns_enabled_false() -> None:
    config = _make_config(enabled=False)
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries")
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_entries_pagination_offset_limit(tmp_path: Path) -> None:
    """5 entries in JSONL, limit=2 → next_offset=2, total_in_window=5."""
    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    _write_jsonl(file_path, [_search_entry() for _ in range(5)])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?limit=2")
    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 2
    assert body["next_offset"] == 2
    assert body["total_in_window"] == 5


def test_entries_filter_by_status(tmp_path: Path) -> None:
    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    ok_entry = _search_entry(status="ok")
    err_entry = _search_entry(status="timeout", error_kind="timeout")
    _write_jsonl(file_path, [ok_entry, err_entry])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?status=ok")
    assert response.status_code == 200
    body = response.json()
    assert body["total_in_window"] == 1
    assert all(e["status"] == "ok" for e in body["entries"])


def test_entries_filter_by_endpoint(tmp_path: Path) -> None:
    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    _write_jsonl(file_path, [_search_entry(endpoint="search"), _route_entry()])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?endpoint=search")
    assert response.status_code == 200
    body = response.json()
    assert body["total_in_window"] == 1
    assert body["entries"][0]["endpoint"] == "search"


def test_entries_filter_by_error_kind(tmp_path: Path) -> None:
    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    timeout_entry = _search_entry(status="timeout", error_kind="timeout")
    internal_entry = _search_entry(status="internal_error", error_kind="internal_error")
    _write_jsonl(file_path, [_search_entry(), timeout_entry, internal_entry])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?error_kind=timeout")
    assert response.status_code == 200
    body = response.json()
    assert body["total_in_window"] == 1
    assert body["entries"][0]["error_kind"] == "timeout"


def test_entries_filter_by_collection(tmp_path: Path) -> None:
    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    _write_jsonl(
        file_path,
        [_search_entry(collection="col_a"), _search_entry(collection="col_b")],
    )

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?collection=col_a")
    assert response.status_code == 200
    body = response.json()
    assert body["total_in_window"] == 1
    assert body["entries"][0]["collection"] == "col_a"


def test_entries_invalid_status_returns_422() -> None:
    config = _make_config(enabled=True)
    client = TestClient(_make_test_app(config))
    # "error" is not a valid Status value
    response = client.get("/telemetry/entries?status=error")
    assert response.status_code == 422


def test_entries_invalid_endpoint_returns_422() -> None:
    config = _make_config(enabled=True)
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?endpoint=bogus")
    assert response.status_code == 422


def test_entries_invalid_error_kind_returns_422() -> None:
    config = _make_config(enabled=True)
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?error_kind=bad_kind")
    assert response.status_code == 422


def test_entries_since_after_until_returns_400() -> None:
    config = _make_config(enabled=True)
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries?since=2026-05-15&until=2026-05-14")
    assert response.status_code == 400


def test_entries_schema_version_is_1(tmp_path: Path) -> None:
    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries")
    assert response.status_code == 200
    assert response.json()["schema_version"] == 1


def test_entries_only_documented_fields(tmp_path: Path) -> None:
    """Each returned entry contains required fields and no undocumented keys."""
    from archon_search.telemetry.entry import DOCUMENTED_SCHEMA_FIELDS

    today_utc = datetime.now(UTC).date()
    file_path = tmp_path / f"{today_utc.isoformat()}.jsonl"
    _write_jsonl(file_path, [_search_entry()])

    config = _make_config(enabled=True, log_dir=str(tmp_path))
    client = TestClient(_make_test_app(config))
    response = client.get("/telemetry/entries")
    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]

    required_fields = {"query_id", "timestamp", "endpoint", "latency_ms", "status"}
    for field in required_fields:
        assert field in entry, f"missing required field: {field}"

    # No keys outside documented schema
    extra_keys = set(k for k, v in entry.items() if v is not None) - DOCUMENTED_SCHEMA_FIELDS
    assert not extra_keys, f"undocumented fields present: {extra_keys}"
