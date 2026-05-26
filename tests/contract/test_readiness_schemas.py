"""Contract tests for readiness schemas — Task 4.1 (B2)."""
from __future__ import annotations

import json


def test_check_status_values() -> None:
    from archon_search.server.schemas import CheckStatus

    assert CheckStatus.OK.value == "ok"
    assert CheckStatus.FAIL.value == "fail"


def test_readiness_response_ok_shape() -> None:
    from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

    obj = ReadinessResponse(ready=True, checks=ReadinessChecks(storage=CheckStatus.OK))
    assert obj.model_dump() == {"ready": True, "checks": {"storage": "ok"}}


def test_readiness_response_fail_shape() -> None:
    from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

    obj = ReadinessResponse(ready=False, checks=ReadinessChecks(storage=CheckStatus.FAIL))
    assert obj.model_dump() == {"ready": False, "checks": {"storage": "fail"}}


def test_watcher_report_empty_by_default() -> None:
    from archon_search.server.schemas import WatcherReport

    report = WatcherReport(running=False)
    assert report.watching == []


def test_readiness_detail_shape() -> None:
    from archon_search.server.schemas import JobCounts, ReadinessDetail, WatcherReport

    detail = ReadinessDetail(
        storage_connected=True,
        embedder_warm=True,
        reranker_warm=False,
        jobs=JobCounts(pending=2, running=1),
        collections_indexing=1,
        collections_failed=0,
        watcher=WatcherReport(running=True, watching=["col1", "col2"]),
    )
    dumped = detail.model_dump()
    assert dumped["storage_connected"] is True
    assert dumped["embedder_warm"] is True
    assert dumped["reranker_warm"] is False
    assert dumped["jobs"] == {"pending": 2, "running": 1}
    assert dumped["collections_indexing"] == 1
    assert dumped["collections_failed"] == 0
    assert dumped["watcher"] == {"running": True, "watching": ["col1", "col2"]}


def test_status_response_has_readiness_field() -> None:
    from archon_search.server.schemas import (
        JobCounts,
        ReadinessDetail,
        StatusResponse,
        WatcherReport,
    )

    detail = ReadinessDetail(
        storage_connected=True,
        embedder_warm=False,
        reranker_warm=False,
        jobs=JobCounts(pending=0, running=0),
        collections_indexing=0,
        collections_failed=0,
        watcher=WatcherReport(running=False),
    )
    resp = StatusResponse(running=True, pid=1, version="0.1", collections=[], readiness=detail)
    dumped = resp.model_dump()
    assert "readiness" in dumped
    assert dumped["readiness"]["storage_connected"] is True


def test_status_response_readiness_defaults_to_none() -> None:
    from archon_search.server.schemas import StatusResponse

    resp = StatusResponse(running=True, pid=1, version="0.1", collections=[])
    assert resp.readiness is None


# ---------------------------------------------------------------------------
# Snapshot tests — pin exact JSON serialisation
# ---------------------------------------------------------------------------


def test_readiness_response_ok_snapshot() -> None:
    from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

    obj = ReadinessResponse(ready=True, checks=ReadinessChecks(storage=CheckStatus.OK))
    assert obj.model_dump(mode="json") == {"ready": True, "checks": {"storage": "ok"}}
    assert json.dumps(obj.model_dump(mode="json"), sort_keys=True) == (
        '{"checks": {"storage": "ok"}, "ready": true}'
    )


def test_readiness_response_fail_snapshot() -> None:
    from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

    obj = ReadinessResponse(ready=False, checks=ReadinessChecks(storage=CheckStatus.FAIL))
    assert json.dumps(obj.model_dump(mode="json"), sort_keys=True) == (
        '{"checks": {"storage": "fail"}, "ready": false}'
    )


def test_readiness_detail_zeros_snapshot() -> None:
    from archon_search.server.schemas import JobCounts, ReadinessDetail, WatcherReport

    detail = ReadinessDetail(
        storage_connected=False,
        embedder_warm=False,
        reranker_warm=False,
        jobs=JobCounts(pending=0, running=0),
        collections_indexing=0,
        collections_failed=0,
        watcher=WatcherReport(running=False),
    )
    assert json.dumps(detail.model_dump(mode="json"), sort_keys=True) == (
        '{"collections_failed": 0, "collections_indexing": 0, "embedder_warm": false, '
        '"jobs": {"pending": 0, "running": 0}, "reranker_warm": false, '
        '"storage_connected": false, "watcher": {"running": false, "watching": []}}'
    )


def test_readiness_detail_typical_snapshot() -> None:
    from archon_search.server.schemas import JobCounts, ReadinessDetail, WatcherReport

    detail = ReadinessDetail(
        storage_connected=True,
        embedder_warm=True,
        reranker_warm=True,
        jobs=JobCounts(pending=1, running=2),
        collections_indexing=3,
        collections_failed=1,
        watcher=WatcherReport(running=True, watching=["a", "b"]),
    )
    assert json.dumps(detail.model_dump(mode="json"), sort_keys=True) == (
        '{"collections_failed": 1, "collections_indexing": 3, "embedder_warm": true, '
        '"jobs": {"pending": 1, "running": 2}, "reranker_warm": true, '
        '"storage_connected": true, "watcher": {"running": true, "watching": ["a", "b"]}}'
    )
