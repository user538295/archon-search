"""Unit + integration tests for BE-3 — the async community-rebuild task.

Mirrors ``_migration_task`` (``routes_collections.py``): constructs a fresh
``CommunityBuilder``, awaits ``build(collection, ns)``, and maps the outcome
to the job:

- success -> DONE with result={"communities_built": N}
- NoGraphNodesError -> FAILED with the builder's curated message
- CommunityDependencyError -> FAILED with COMMUNITY_DEPENDENCY_DETAIL (its message embeds the install command)
- any other ValueError / ImportError / RuntimeError -> FAILED with COMMUNITY_REBUILD_FAILED_DETAIL

Covers:
- #unit_test test_rebuild_task_success_sets_done_with_count
- #unit_test test_rebuild_task_zero_nodes_sets_failed
- #unit_test test_rebuild_task_missing_leidenalg_sets_failed_with_fixed_detail
- #integration_test test_rebuild_job_reaches_done_visible_in_jobs
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from archon_search.community_builder import COMMUNITY_DEPENDENCY_DETAIL, NoGraphNodesError, _run_leiden_partition_sync
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.types import JobStatus
from tests._install_hint_leak import assert_install_hint_logged_not_sent, patch_install_hint

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Unit tests — CommunityBuilder.build is mocked; only the task's outcome
# mapping is under test.
# ---------------------------------------------------------------------------


async def test_rebuild_task_success_sets_done_with_count(tmp_path: Path) -> None:
    """Success transitions the job to DONE with {"communities_built": N} (N=0 and N=1 valid, IMod-2)."""
    from archon_search.server.routes_graph import _community_rebuild_task

    for n in (0, 1):
        jobs_path = tmp_path / f"jobs-{n}.json"
        job_store = JobStore(path=jobs_path)
        job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")
        job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

        fake_communities = [object() for _ in range(n)]
        with patch(
            "archon_search.server.routes_graph.CommunityBuilder"
        ) as mock_builder_cls:
            mock_builder_cls.return_value.build = AsyncMock(return_value=fake_communities)

            await _community_rebuild_task(
                job=job_store.get(job.job_id),
                job_store=job_store,
                graph_store=object(),
                graph_config=object(),
                search_store=object(),
            )

        updated = job_store.get(job.job_id)
        assert updated is not None
        assert updated.status == JobStatus.DONE
        assert updated.result == {"communities_built": n}


async def test_rebuild_task_zero_nodes_sets_failed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A zero-node collection (NoGraphNodesError) -> FAILED with the builder's message (S8).

    The curated, expected failure is logged as a warning, without a traceback.
    """
    exc_msg = "No entity graph nodes found for collection 'col-a'."
    from archon_search.server.routes_graph import _community_rebuild_task

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")
    job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls, \
            caplog.at_level(logging.WARNING, logger="archon_search.server.routes_graph"):
        mock_builder_cls.return_value.build = AsyncMock(side_effect=NoGraphNodesError(exc_msg))

        await _community_rebuild_task(
            job=job_store.get(job.job_id),
            job_store=job_store,
            graph_store=object(),
            graph_config=object(),
            search_store=object(),
        )

    updated = job_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error == exc_msg
    failure_logs = [r for r in caplog.records if exc_msg in r.getMessage()]
    assert [r.levelno for r in failure_logs] == [logging.WARNING], caplog.text
    assert failure_logs[0].exc_info is None
    assert "Traceback" not in caplog.text


async def test_rebuild_task_missing_leidenalg_sets_failed_with_fixed_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A CommunityDependencyError (missing leidenalg) -> FAILED with COMMUNITY_DEPENDENCY_DETAIL, no crash (Q6, S8).

    The exception's own message embeds the receipt-derived install command (possibly index
    credentials), so it is logged as a warning and never reaches the job record.
    """
    from archon_search.server.routes_graph import _community_rebuild_task

    patch_install_hint(monkeypatch, "archon_search.community_builder")
    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")
    job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

    def _partition_without_leidenalg(*args: object, **kwargs: object) -> None:
        with patch.dict(sys.modules, {"igraph": None, "leidenalg": None}):
            _run_leiden_partition_sync([], [], resolution=1.0)

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls, \
            caplog.at_level(logging.WARNING):
        mock_builder_cls.return_value.build = AsyncMock(side_effect=_partition_without_leidenalg)

        await _community_rebuild_task(
            job=job_store.get(job.job_id),
            job_store=job_store,
            graph_store=object(),
            graph_config=object(),
            search_store=object(),
        )

    mock_builder_cls.return_value.build.assert_awaited_once()
    updated = job_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error == COMMUNITY_DEPENDENCY_DETAIL
    assert_install_hint_logged_not_sent(caplog, json.dumps(job_to_dict(updated), default=str))


async def test_rebuild_task_runtime_error_sets_failed(tmp_path: Path) -> None:
    """A RuntimeError from the builder (e.g. GraphStore I/O) -> FAILED with the sanitized text, never its message (C4)."""
    from archon_search.server.routes_graph import COMMUNITY_REBUILD_FAILED_DETAIL, _community_rebuild_task

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")
    job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls:
        mock_builder_cls.return_value.build = AsyncMock(side_effect=RuntimeError("graph store I/O failed"))

        await _community_rebuild_task(
            job=job_store.get(job.job_id),
            job_store=job_store,
            graph_store=object(),
            graph_config=object(),
            search_store=object(),
        )

    updated = job_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error == COMMUNITY_REBUILD_FAILED_DETAIL


# ---------------------------------------------------------------------------
# Integration test — real CommunityBuilder + real GraphStore drive a real
# build to DONE, verified via job_to_dict (the GET /jobs/{id} serialisation
# path).
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_rebuild_job_reaches_done_visible_in_jobs(tmp_path: Path) -> None:
    """The spawned task drives a real build to DONE, visible with its count in GET /jobs/{id} (S2)."""
    pytest.importorskip("leidenalg", reason="leidenalg not installed; skipping BE-3 integration test")

    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphNode,
    )
    from archon_search.server.routes_graph import _community_rebuild_task

    col = "test-col"
    ns = "default"

    node = GraphNode(
        id="concept:alpha",
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name=col,
    )

    graph_store = GraphStore(tmp_path / "graph_db")
    await graph_store.connect()
    await graph_store.ensure_graph_tables(col, ns=ns)
    await graph_store.write_graph(col, [node], [], ns=ns)

    graph_config = GraphConfig(enabled=True, leiden_resolution=1.0, max_community_size=10)

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection=col, namespace=ns)
    job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

    await _community_rebuild_task(
        job=job_store.get(job.job_id),
        job_store=job_store,
        graph_store=graph_store,
        graph_config=graph_config,
        search_store=None,
    )

    await graph_store.disconnect()

    updated = job_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.DONE
    assert updated.result == {"communities_built": 1}

    serialised = job_to_dict(updated)
    assert serialised["status"] == "DONE"
    assert serialised["result"] == {"communities_built": 1}
