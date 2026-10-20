"""Unit + integration tests for BE-3 — the async community-rebuild task.

Mirrors ``_migration_task`` (``routes_collections.py``): constructs a fresh
``CommunityBuilder``, awaits ``build(collection, ns)``, and maps the outcome
to the job:

- success -> DONE with result={"communities_built": N}
- ValueError / ImportError / RuntimeError -> FAILED with the error string
- any other Exception -> FAILED with the fixed ``COMMUNITY_REBUILD_FAILED_DETAIL``

Covers:
- #unit_test test_rebuild_task_success_sets_done_with_count
- #unit_test test_rebuild_task_zero_nodes_sets_failed
- #unit_test test_rebuild_task_missing_leidenalg_sets_failed
- #integration_test test_rebuild_job_reaches_done_visible_in_jobs
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.types import JobStatus

pytestmark = pytest.mark.asyncio

_ROUTES_GRAPH_LOGGER = "archon_search.server.routes_graph"
_ROUTES_JOBS_LOGGER = "archon_search.server.routes_jobs"
_SECRET_KEY_TEXT = "/secret/internal/key"


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


@pytest.mark.parametrize(
    ("exc_cls", "exc_msg"),
    [
        (ValueError, "No entity graph nodes found for collection 'col-a'."),
    ],
)
async def test_rebuild_task_zero_nodes_sets_failed(tmp_path: Path, exc_cls, exc_msg) -> None:
    """A zero-node collection (ValueError) -> FAILED with the builder's message (S8)."""
    from archon_search.server.routes_graph import _community_rebuild_task

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls:
        mock_builder_cls.return_value.build = AsyncMock(side_effect=exc_cls(exc_msg))

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


async def test_rebuild_task_import_error_sets_failed_with_its_message(tmp_path: Path) -> None:
    """An ImportError from the builder (e.g. missing leidenalg) -> FAILED carrying its message, no crash (Q6, S8)."""
    import_error_message = "optional dependency missing"
    from archon_search.server.routes_graph import _community_rebuild_task

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls:
        mock_builder_cls.return_value.build = AsyncMock(
            side_effect=ImportError(import_error_message)
        )

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
    assert updated.error == import_error_message


async def test_rebuild_task_runtime_error_sets_failed(tmp_path: Path) -> None:
    """A RuntimeError from the builder (e.g. GraphStore I/O) -> FAILED with the error string (C4)."""
    from archon_search.server.routes_graph import _community_rebuild_task

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")

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
    assert updated.error == "graph store I/O failed"


async def test_rebuild_task_unexpected_error_sets_failed_with_sanitized_detail_and_clears_meta_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception outside (ValueError, ImportError, RuntimeError) — e.g. a KeyError — must not leave the job
    RUNNING with ``community_rebuild_job_id`` still set (every later rebuild would 409 until a restart): the job
    goes FAILED with the fixed sanitized detail (never the exception text, which is logged with its traceback
    instead) and the meta id is cleared."""
    import logging
    from types import SimpleNamespace

    from tests._job_store_faults import assert_error_logged_with_traceback

    from archon_search.server.routes_graph import COMMUNITY_REBUILD_FAILED_DETAIL, _community_rebuild_task

    job_store = JobStore(path=tmp_path / "jobs.json")
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")
    meta = SimpleNamespace(community_rebuild_job_id=job.job_id)
    search_store = SimpleNamespace(
        get_collection_meta=AsyncMock(return_value=meta),
        update_collection_meta=AsyncMock(),
    )

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls:
        mock_builder_cls.return_value.build = AsyncMock(side_effect=KeyError(_SECRET_KEY_TEXT))
        caplog.set_level(logging.ERROR, logger=_ROUTES_GRAPH_LOGGER)

        await _community_rebuild_task(
            job=job_store.get(job.job_id),
            job_store=job_store,
            graph_store=object(),
            graph_config=object(),
            search_store=search_store,
        )

    updated = job_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error == COMMUNITY_REBUILD_FAILED_DETAIL
    assert _SECRET_KEY_TEXT not in (updated.error or "")
    assert_error_logged_with_traceback(caplog, _ROUTES_GRAPH_LOGGER)
    assert meta.community_rebuild_job_id is None
    search_store.update_collection_meta.assert_awaited_once_with(meta)


async def test_rebuild_task_done_write_failure_does_not_escape(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A failed persist of the terminal DONE status (any exception, here a serialization TypeError) is logged with
    its traceback and suppressed: it must not escape the fire-and-forget task as an unretrieved exception."""
    import logging

    from archon_search.server.routes_graph import _community_rebuild_task
    from tests._job_store_faults import assert_error_logged_with_traceback, failing_job_writes

    job_store = JobStore(path=tmp_path / "jobs.json")
    job = job_store.create_community_rebuild(collection="col-a", namespace="ns1")
    caplog.set_level(logging.ERROR, logger=_ROUTES_JOBS_LOGGER)

    with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_builder_cls, \
         failing_job_writes(exc=TypeError("simulated serialization failure")) as writes:
        mock_builder_cls.return_value.build = AsyncMock(return_value=[])

        await _community_rebuild_task(  # must not raise
            job=job_store.get(job.job_id),
            job_store=job_store,
            graph_store=object(),
            graph_config=object(),
        )

    assert writes.statuses_of(job.job_id)[0] == JobStatus.DONE.value  # the DONE write was attempted (and failed)
    assert_error_logged_with_traceback(caplog, _ROUTES_JOBS_LOGGER)  # _persist_job_status logs it


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
