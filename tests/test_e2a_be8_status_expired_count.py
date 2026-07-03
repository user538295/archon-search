"""TDD tests for BE-8: MaintenanceStatusDetail gains expired_chunk_count and last_expired_pruned_at.

Plan: Documentation/Backlog/e2a-ttl-scoping-team-plan.md Task BE-8.

Covers:
- Schema: expired_chunk_count is int (non-nullable, default 0); last_expired_pruned_at is str|null
- Route: _build_maintenance_status is async; expired_chunk_count populated via store.count_expired_chunks()
- Route: last_expired_pruned_at read from maintenance state file
- CLI: text output shows both new fields
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from archon_search._types import CollectionInfo
from archon_search.cli.maintenance_cmd import maintenance_cmd
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.server.schemas import MaintenanceStatusDetail


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_maintenance_status_detail_expired_chunk_count_default_zero() -> None:
    """BE-8 S1: expired_chunk_count defaults to 0 and is non-nullable (int)."""
    detail = MaintenanceStatusDetail(enabled=False, interval_hours=0)
    assert detail.expired_chunk_count == 0
    assert isinstance(detail.expired_chunk_count, int)


def test_maintenance_status_detail_expired_chunk_count_set() -> None:
    """BE-8 S2: expired_chunk_count can be set to a positive integer."""
    detail = MaintenanceStatusDetail(enabled=True, interval_hours=24, expired_chunk_count=7)
    assert detail.expired_chunk_count == 7


def test_maintenance_status_detail_last_expired_pruned_at_default_none() -> None:
    """BE-8 S3: last_expired_pruned_at defaults to None (null until first prune)."""
    detail = MaintenanceStatusDetail(enabled=False, interval_hours=0)
    assert detail.last_expired_pruned_at is None


def test_maintenance_status_detail_last_expired_pruned_at_set() -> None:
    """BE-8 S4: last_expired_pruned_at can be set to a non-null ISO string."""
    ts = "2026-07-03T10:00:00+00:00"
    detail = MaintenanceStatusDetail(
        enabled=True, interval_hours=24, last_expired_pruned_at=ts
    )
    assert detail.last_expired_pruned_at == ts


def test_maintenance_status_detail_serialises_expired_count() -> None:
    """BE-8: expired_chunk_count appears in JSON serialisation as integer."""
    detail = MaintenanceStatusDetail(enabled=True, interval_hours=12, expired_chunk_count=3)
    d = detail.model_dump()
    assert d["expired_chunk_count"] == 3
    assert d["last_expired_pruned_at"] is None


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


def _make_mock_search_store(
    collections: list[CollectionInfo] | None = None,
    expired_per_collection: int = 0,
) -> MagicMock:
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock._run_startup_migrations = AsyncMock()
    mock.ping = AsyncMock(return_value=True)
    mock.get_all_collections_meta = AsyncMock(
        return_value=[
            CollectionMeta(name=c.name, namespace=c.namespace)
            for c in (collections or [])
        ]
    )
    mock.get_collection_meta = AsyncMock(return_value=None)
    mock.list_collections = AsyncMock(return_value=collections or [])
    mock.count_untagged_language_chunks = AsyncMock(return_value=0)
    mock.pending_migrations = AsyncMock(return_value=[])
    mock.count_expired_chunks = AsyncMock(return_value=expired_per_collection)
    return mock


def _make_client(
    tmp_path: Path,
    auth_headers: dict[str, str],
    *,
    interval_hours: int = 0,
    collections: list[CollectionInfo] | None = None,
    expired_per_collection: int = 0,
) -> TestClient:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.maintenance.interval_hours = interval_hours
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)
    app.state.search_store = _make_mock_search_store(
        collections=collections,
        expired_per_collection=expired_per_collection,
    )
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# Route tests — expired_chunk_count
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_maintenance_expired_chunk_count_zero_no_collections(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S5: With no collections, expired_chunk_count is 0."""
    client = _make_client(tmp_path, auth_headers)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"] is not None
    assert body["maintenance"]["expired_chunk_count"] == 0


@pytest.mark.integration
def test_status_maintenance_expired_chunk_count_summed_across_collections(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S6: expired_chunk_count is sum of store.count_expired_chunks() per collection."""
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="col2", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    # Each collection returns 3 expired chunks → total = 6
    client = _make_client(
        tmp_path, auth_headers, collections=collections, expired_per_collection=3
    )
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"]["expired_chunk_count"] == 6


@pytest.mark.integration
def test_status_maintenance_count_expired_chunks_called_per_collection(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S7: store.count_expired_chunks is called once per collection in the namespace."""
    collections = [
        CollectionInfo(name="a", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="b", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    store_mock = client.app.state.search_store
    # count_expired_chunks should have been called twice (once per collection)
    assert store_mock.count_expired_chunks.await_count == 2


# ---------------------------------------------------------------------------
# Route tests — last_expired_pruned_at
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_maintenance_last_expired_pruned_at_null_by_default(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S8: last_expired_pruned_at is null when not yet written to state file."""
    client = _make_client(tmp_path, auth_headers)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"]["last_expired_pruned_at"] is None


@pytest.mark.integration
def test_status_maintenance_last_expired_pruned_at_from_state_file(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S9: last_expired_pruned_at is read from maintenance state file."""
    expected_ts = "2026-07-03T08:00:00+00:00"
    client = _make_client(tmp_path, auth_headers)
    with client:
        loop: MaintenanceLoop = client.app.state.maintenance_loop
        loop._save_state({
            "last_run_at": "2026-07-03T08:00:00+00:00",
            "next_run_at": None,
            "last_expired_pruned_at": expected_ts,
            "collection_health": {},
            "retry_counts": {},
        })
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"]["last_expired_pruned_at"] == expected_ts


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _mock_server_payload_with_expired(
    expired_chunk_count: int = 0,
    last_expired_pruned_at: str | None = None,
) -> dict:
    return {
        "maintenance": {
            "enabled": True,
            "interval_hours": 24,
            "last_run_at": "2026-07-03T08:00:00+00:00",
            "next_run_at": "2026-07-04T08:00:00+00:00",
            "expired_chunk_count": expired_chunk_count,
            "last_expired_pruned_at": last_expired_pruned_at,
            "collection_health": [],
        }
    }


def _mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    return resp


def test_maintenance_cli_shows_expired_chunk_count_nonzero(tmp_path: Path) -> None:
    """BE-8 S10: CLI status text shows expired_chunk_count when > 0."""
    runner = CliRunner()
    server_payload = _mock_server_payload_with_expired(expired_chunk_count=5)
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "expired" in result.output.lower()
    assert "5" in result.output


def test_maintenance_cli_shows_expired_chunk_count_zero(tmp_path: Path) -> None:
    """BE-8 S11: CLI status text shows expired_chunk_count=0."""
    runner = CliRunner()
    server_payload = _mock_server_payload_with_expired(expired_chunk_count=0)
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "Expired chunks (live): 0" in result.output


def test_maintenance_cli_shows_last_expired_pruned_at_when_set(tmp_path: Path) -> None:
    """BE-8 S12: CLI shows last_expired_pruned_at when non-null."""
    ts = "2026-07-03T10:00:00+00:00"
    runner = CliRunner()
    server_payload = _mock_server_payload_with_expired(
        expired_chunk_count=2,
        last_expired_pruned_at=ts,
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "Last expired pruned:   2026-07-03" in result.output


def test_maintenance_cli_shows_last_expired_pruned_at_never_when_null(tmp_path: Path) -> None:
    """BE-8 S13: CLI shows 'never' (or similar) when last_expired_pruned_at is null."""
    runner = CliRunner()
    server_payload = _mock_server_payload_with_expired(
        expired_chunk_count=0,
        last_expired_pruned_at=None,
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    # Should show 'never' when timestamp is null
    assert "never" in result.output.lower()


def test_maintenance_cli_json_includes_expired_fields(tmp_path: Path) -> None:
    """BE-8 S14: --json output includes expired_chunk_count and last_expired_pruned_at."""
    ts = "2026-07-03T10:00:00+00:00"
    runner = CliRunner()
    server_payload = _mock_server_payload_with_expired(
        expired_chunk_count=3,
        last_expired_pruned_at=ts,
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(
            maintenance_cmd, ["status", "--json", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["expired_chunk_count"] == 3
    assert payload["last_expired_pruned_at"] == ts


def test_maintenance_cli_json_offline_includes_expired_fields_as_defaults(tmp_path: Path) -> None:
    """BE-8 S15: --json output offline includes expired_chunk_count=0 and last_expired_pruned_at=null."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(
            maintenance_cmd, ["status", "--json", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "expired_chunk_count" in payload
    assert payload["expired_chunk_count"] == 0
    assert "last_expired_pruned_at" in payload
    assert payload["last_expired_pruned_at"] is None


@pytest.mark.integration
def test_status_maintenance_count_expired_only_for_caller_namespace(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S16: store.count_expired_chunks is NOT called for collections in other namespaces."""
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="col2", doc_count=0, chunk_count=0, namespace="tenant-b"),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    store_mock = client.app.state.search_store
    # Only col1 belongs to DEFAULT_NAMESPACE — count_expired_chunks called once only
    assert store_mock.count_expired_chunks.await_count == 1
    # Verify it was called with the right collection name AND namespace
    called_cols = [call.args[0] for call in store_mock.count_expired_chunks.call_args_list]
    assert called_cols == ["col1"]
    called_ns = [call.args[1] for call in store_mock.count_expired_chunks.call_args_list]
    assert called_ns == [DEFAULT_NAMESPACE]


# ---------------------------------------------------------------------------
# R2–R5: additional tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_expired_chunk_count_is_point_in_time(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S17: expired_chunk_count reflects live store state — not a cached value.

    Two consecutive GETs must return different counts when the mock changes between calls,
    proving the route queries the store live on each request.
    """
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections, expired_per_collection=3)
    with client:
        # First GET: mock returns 3
        response1 = client.get("/status")
        assert response1.status_code == 200
        assert response1.json()["maintenance"]["expired_chunk_count"] == 3

        # Simulate a new expired chunk seeded after the first call
        client.app.state.search_store.count_expired_chunks = AsyncMock(return_value=7)

        # Second GET: must return updated count (proves no caching)
        response2 = client.get("/status")
        assert response2.status_code == 200
        assert response2.json()["maintenance"]["expired_chunk_count"] == 7


@pytest.mark.integration
def test_get_status_maintenance_fields_after_prune(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8 S7: after a prune run, last_expired_pruned_at is set; expired_chunk_count
    reflects live state — seeding a new expired chunk after the prune increases the count.
    """
    pruned_at = "2026-07-03T08:00:00+00:00"
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections, expired_per_collection=0)
    with client:
        # Write maintenance state simulating a completed prune pass
        loop: MaintenanceLoop = client.app.state.maintenance_loop
        loop._save_state({
            "last_run_at": pruned_at,
            "next_run_at": None,
            "last_expired_pruned_at": pruned_at,
            "collection_health": {},
            "retry_counts": {},
        })

        # First GET: 0 expired chunks (just pruned), last_expired_pruned_at is set
        response1 = client.get("/status")
        assert response1.status_code == 200
        body1 = response1.json()
        assert body1["maintenance"]["last_expired_pruned_at"] == pruned_at
        assert body1["maintenance"]["expired_chunk_count"] == 0

        # Seed a new expired chunk (simulated by changing mock return value)
        client.app.state.search_store.count_expired_chunks = AsyncMock(return_value=1)

        # Second GET: count must increase (proves live point-in-time, not cached prune delta)
        response2 = client.get("/status")
        assert response2.status_code == 200
        body2 = response2.json()
        assert body2["maintenance"]["expired_chunk_count"] == 1
        # last_expired_pruned_at unchanged
        assert body2["maintenance"]["last_expired_pruned_at"] == pruned_at


@pytest.mark.integration
def test_status_maintenance_expired_chunk_count_on_error(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8: when store.count_expired_chunks raises, the route still returns 200
    and expired_chunk_count reflects any successfully counted collections.
    With per-collection error handling, a single-collection failure results in 0.
    """
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections)
    with client:
        client.app.state.search_store.count_expired_chunks = AsyncMock(
            side_effect=RuntimeError("store unavailable")
        )
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"] is not None
    # Single collection raised — per-collection error handling leaves count at 0
    assert body["maintenance"]["expired_chunk_count"] == 0


@pytest.mark.integration
def test_status_maintenance_partial_collection_error(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8: per-collection error handling — if one collection's count raises,
    the count from successfully counted collections is preserved in the sum.
    """
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="col2", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections)
    with client:
        # col1 returns 5; col2 raises — partial sum should be 5
        client.app.state.search_store.count_expired_chunks = AsyncMock(
            side_effect=[5, RuntimeError("col2 unavailable")]
        )
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"] is not None
    # col1 contributed 5; col2 failed — per-collection handling preserves partial sum
    assert body["maintenance"]["expired_chunk_count"] == 5


import asyncio
import inspect


@pytest.mark.integration
def test_build_maintenance_status_receives_store_parameter(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """BE-8: _build_maintenance_status signature is async and accepts explicit store param."""
    from archon_search.server.routes_status import _build_maintenance_status

    # Verify it is a coroutine function (async def)
    assert inspect.iscoroutinefunction(_build_maintenance_status), (
        "_build_maintenance_status must be async def"
    )

    # Verify it accepts a store parameter and returns correct expired_chunk_count
    collections = [
        CollectionInfo(name="col1", doc_count=0, chunk_count=0, namespace=DEFAULT_NAMESPACE),
    ]
    client = _make_client(tmp_path, auth_headers, collections=collections, expired_per_collection=5)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["maintenance"]["expired_chunk_count"] == 5
