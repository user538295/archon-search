"""Job ``error`` fields and maintenance ``last_error`` never carry an internal exception message.

CLAUDE.md hard invariant: never put ``str(exc)`` in a job ``error`` field for a
caught exception whose message embeds internals. A job record is wire-facing via
``GET /jobs/{id}`` (``job_to_dict``); a maintenance ``last_error`` is wire-facing
via ``GET /status`` (``routes_status`` reads it from ``.maintenance-state.json``).
Each test raises an exception carrying an internal-looking path at the real raise
point the background task wraps and asserts the path never reaches that record,
with a presence anchor (FAILED status / non-empty error, raise point hit) so the
absence check cannot pass vacuously.
"""
from __future__ import annotations

import asyncio
import json
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._path_safety import validate_ingest_path
from archon_search._types import CollectionInfo
from archon_search.config import GraphConfig, MaintenanceConfig, SearchConfig
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderNotReadyError
from archon_search.jobs.maintenance_loop import (
    EXPIRED_CHUNK_PRUNING_FAILED_DETAIL,
    FTS_OPTIMIZE_FAILED_DETAIL,
    GRAPH_GC_FAILED_DETAIL,
    ORPHAN_CLEANUP_FAILED_DETAIL,
    MaintenanceLoop,
)
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.scheduler import DISPATCH_FAILED_DETAIL, JobScheduler
from archon_search.jobs.store import JobStore
from archon_search.server.routes_collections import (
    METADATA_REINDEX_FAILED_DETAIL,
    MIGRATION_FAILED_DETAIL,
    _migration_task,
    _reindex_metadata_task,
)
from archon_search.server.routes_graph import COMMUNITY_REBUILD_FAILED_DETAIL, _community_rebuild_task
from archon_search.server.routes_jobs import (
    INGEST_FAILED_DETAIL,
    INGEST_PATH_NOT_FOUND_PREFIX,
    REINDEX_FAILED_DETAIL,
    IngestRequest,
    _default_ingest_task,
    _default_ingest_task_with_lock,
    _reindex_task,
)
from archon_search.types import JobStatus, MigrationKind, MigrationSpec
from tests._internal_error_leak import LANCE_MSG, LEAK_NEEDLE, ONNX_MSG, assert_logged_with_traceback
from tests._maintenance_status_client import make_status_client

_COLLECTION = "col"
_NAMESPACE = "default"
_TARGET_MODEL = "BAAI/bge-small-en-v1.5"
_STATE_FILE = ".maintenance-state.json"


def _job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _assert_failed_without_leak(job_store: JobStore, job_id: str, detail: str) -> None:
    """The ``GET /jobs/{id}`` body must report FAILED with the sanitized, path-free error."""
    job = job_store.get(job_id)
    assert job is not None
    wire = job_to_dict(job)
    assert wire["status"] == JobStatus.FAILED.value, wire
    assert wire["error"] == detail, wire
    assert LEAK_NEEDLE not in json.dumps(wire, default=str), f"internal path leaked into job record: {wire['error']!r}"


def _embedder_cache(error: Exception | None = None) -> MagicMock:
    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=MagicMock())
    return cache


def _search_store() -> MagicMock:
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()
    return store


# ---------------------------------------------------------------------------
# F — routes_collections background tasks
# ---------------------------------------------------------------------------


async def test_migration_task_failure_does_not_echo_store_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    queued = job_store.create_migration(collection=_COLLECTION, kind=MigrationKind.REWRITE, backup_confirmed=True)
    running = job_store.transition(queued.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running is not None
    spec = MigrationSpec(name="rebuild", kind=MigrationKind.REWRITE, description="test", introduced_at=1)
    store = _search_store()
    store.apply_rewrite_migration = AsyncMock(side_effect=RuntimeError(LANCE_MSG))

    with caplog.at_level(logging.ERROR):
        await _migration_task(job=running, job_store=job_store, search_store=store, spec=spec)

    store.apply_rewrite_migration.assert_awaited_once()
    _assert_failed_without_leak(job_store, queued.job_id, MIGRATION_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


async def test_reindex_metadata_task_failure_does_not_echo_store_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    # The route creates these jobs RUNNING (no QUEUED -> RUNNING claim).
    running = queued = job_store.create_metadata_reindex(collection=_COLLECTION)
    assert running.status == JobStatus.RUNNING
    store = _search_store()
    store.reindex_metadata = AsyncMock(side_effect=OSError(LANCE_MSG))

    with caplog.at_level(logging.ERROR):
        await _reindex_metadata_task(
            job=running, job_store=job_store, search_store=store, dry_run=False, normalize_timestamps=False
        )

    store.reindex_metadata.assert_awaited_once()
    _assert_failed_without_leak(job_store, queued.job_id, METADATA_REINDEX_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


# ---------------------------------------------------------------------------
# F — routes_jobs ingest / reindex background tasks
# ---------------------------------------------------------------------------


async def test_default_ingest_task_failure_does_not_echo_embedder_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    job = job_store.create(collection=_COLLECTION, path=str(tmp_path))
    cache = _embedder_cache(RuntimeError(ONNX_MSG))

    with caplog.at_level(logging.ERROR):
        await _default_ingest_task(
            job.job_id, job_store, IngestRequest(collection=_COLLECTION, path=str(tmp_path)),
            search_store=_search_store(), embedder_cache=cache, pipeline=MagicMock(), config=SearchConfig(),
        )

    cache.get_or_load.assert_awaited_once()
    _assert_failed_without_leak(job_store, job.job_id, INGEST_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, ONNX_MSG)


async def test_default_ingest_task_with_lock_failure_does_not_echo_embedder_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    job = job_store.create(collection=_COLLECTION, path=str(tmp_path))
    cache = _embedder_cache(RuntimeError(ONNX_MSG))
    held_lock = asyncio.Lock()
    await held_lock.acquire()

    with caplog.at_level(logging.ERROR):
        await _default_ingest_task_with_lock(
            job.job_id, job_store, IngestRequest(collection=_COLLECTION, path=str(tmp_path)),
            held_lock=held_lock,
            search_store=_search_store(), embedder_cache=cache, pipeline=MagicMock(), config=SearchConfig(),
        )

    cache.get_or_load.assert_awaited_once()
    assert not held_lock.locked()
    _assert_failed_without_leak(job_store, job.job_id, INGEST_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, ONNX_MSG)


async def _run_ingest_task(task: str, job_store: JobStore, job_id: str, body: IngestRequest, pipeline: Any) -> None:
    """Drive one of the two ingest lifecycle wrappers through the real ``_dispatch_ingest``."""
    deps: dict[str, Any] = {
        "search_store": _search_store(), "embedder_cache": _embedder_cache(), "pipeline": pipeline, "config": SearchConfig(),
    }
    if task == "plain":
        await _default_ingest_task(job_id, job_store, body, **deps)
        return
    held_lock = asyncio.Lock()
    await held_lock.acquire()
    await _default_ingest_task_with_lock(job_id, job_store, body, held_lock=held_lock, **deps)
    assert not held_lock.locked()


_INGEST_TASKS = ["plain", "with_lock"]


@pytest.mark.parametrize("task", _INGEST_TASKS)
async def test_keep_ingest_path_not_found_message_reaches_job_error(
    tmp_path: Path, task: str, caplog: pytest.LogCaptureFixture
) -> None:
    """S284: the curated not-found text echoes only the caller's own path, so it stays on the wire."""
    missing = str(tmp_path / "missing" / "s284")
    job_store = _job_store(tmp_path)
    job = job_store.create(collection=_COLLECTION, path=missing)

    with caplog.at_level(logging.WARNING):
        await _run_ingest_task(task, job_store, job.job_id, IngestRequest(collection=_COLLECTION, path=missing), MagicMock())

    failed = job_store.get(job.job_id)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert failed.error == f"{INGEST_PATH_NOT_FOUND_PREFIX}: {missing}"
    assert missing in caplog.text


def _tilde_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A ``~`` path: the server expands it to its own home directory."""
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return "~/missing/s284", home


def _symlink_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A path through a symlink: the server resolves it to the link's target."""
    target = tmp_path / "server-internal-target"
    target.mkdir()
    link = tmp_path / "caller-link"
    link.symlink_to(target, target_is_directory=True)
    return str(link / "missing" / "s284"), target


@pytest.mark.parametrize("task", _INGEST_TASKS)
@pytest.mark.parametrize("submitted_path", [_tilde_path, _symlink_path], ids=["tilde", "symlink"])
async def test_ingest_path_not_found_echoes_submitted_path_not_resolved_one(
    tmp_path: Path, task: str, submitted_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2-I-103: the not-found text echoes the caller's raw ``path``, never the server's expansion of it."""
    submitted, server_dir = submitted_path(tmp_path, monkeypatch)
    body = IngestRequest(collection=_COLLECTION, path=submitted).with_resolved_path(
        str(validate_ingest_path(submitted))
    )
    # Presence anchor: the server really did rewrite the path to its own directory.
    assert body.path is not None and body.path.startswith(str(server_dir.resolve()))
    job_store = _job_store(tmp_path)
    job = job_store.create(collection=_COLLECTION, path=body.path)

    await _run_ingest_task(task, job_store, job.job_id, body, MagicMock())

    failed = job_store.get(job.job_id)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert failed.error == f"{INGEST_PATH_NOT_FOUND_PREFIX}: {submitted}"
    assert str(server_dir.resolve()) not in failed.error


@pytest.mark.parametrize("task", _INGEST_TASKS)
async def test_ingest_task_pipeline_file_not_found_does_not_echo_internal_path(
    tmp_path: Path, task: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the curated subclass keeps its text: a plain FileNotFoundError from the pipeline is sanitized."""
    source = tmp_path / "doc.md"
    source.write_text("hello", encoding="utf-8")
    job_store = _job_store(tmp_path)
    job = job_store.create(collection=_COLLECTION, path=str(source))
    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(side_effect=FileNotFoundError(LANCE_MSG))

    with caplog.at_level(logging.ERROR):
        await _run_ingest_task(
            task, job_store, job.job_id, IngestRequest(collection=_COLLECTION, path=str(source)), pipeline
        )

    pipeline.ingest_file.assert_awaited_once()
    _assert_failed_without_leak(job_store, job.job_id, INGEST_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


async def test_reindex_task_meta_lookup_failure_still_marks_job_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The meta lookup inside embedder resolution fails: the handler's own meta cleanup fails the same way."""
    job_store = _job_store(tmp_path)
    job = job_store.create_reindex(_COLLECTION, namespace=_NAMESPACE, target_embedding_model=None)
    store = _search_store()
    store.get_collection_meta = AsyncMock(side_effect=RuntimeError(LANCE_MSG))

    with caplog.at_level(logging.ERROR):
        await _reindex_task(
            job.job_id, store, job_store, _embedder_cache(), MagicMock(), _COLLECTION, _NAMESPACE, tmp_path, SearchConfig(),
        )

    assert store.get_collection_meta.await_count == 2, "resolution lookup and cleanup lookup must both run"
    _assert_failed_without_leak(job_store, job.job_id, REINDEX_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


async def test_reindex_task_embedder_not_ready_meta_cleanup_failure_still_marks_job_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    job = job_store.create_reindex(_COLLECTION, namespace=_NAMESPACE, target_embedding_model=_TARGET_MODEL)
    store = _search_store()
    store.get_collection_meta = AsyncMock(side_effect=RuntimeError(LANCE_MSG))

    with caplog.at_level(logging.ERROR):
        await _reindex_task(
            job.job_id, store, job_store, _embedder_cache(EmbedderNotReadyError("still loading")), MagicMock(),
            _COLLECTION, _NAMESPACE, tmp_path, SearchConfig(),
        )

    store.get_collection_meta.assert_awaited_once_with(_COLLECTION, _NAMESPACE)
    _assert_failed_without_leak(job_store, job.job_id, EMBEDDER_NOT_READY_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


async def test_reindex_task_embedder_failure_does_not_echo_embedder_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    job = job_store.create_reindex(_COLLECTION, namespace=_NAMESPACE, target_embedding_model=_TARGET_MODEL)
    cache = _embedder_cache(RuntimeError(ONNX_MSG))

    with caplog.at_level(logging.ERROR):
        await _reindex_task(
            job.job_id, _search_store(), job_store, cache, MagicMock(),
            _COLLECTION, _NAMESPACE, tmp_path, SearchConfig(),
        )

    cache.get_or_load.assert_awaited_once_with(_TARGET_MODEL)
    _assert_failed_without_leak(job_store, job.job_id, REINDEX_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, ONNX_MSG)


async def test_reindex_task_ingest_failure_does_not_echo_pipeline_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """routes_jobs.py ``error=str(ingest_error)`` — the same leak, not listed in the audit's group F."""
    job_store = _job_store(tmp_path)
    job = job_store.create_reindex(_COLLECTION, namespace=_NAMESPACE, target_embedding_model=_TARGET_MODEL)
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=RuntimeError(LANCE_MSG))

    with caplog.at_level(logging.ERROR):
        await _reindex_task(
            job.job_id, _search_store(), job_store, _embedder_cache(), pipeline,
            _COLLECTION, _NAMESPACE, tmp_path, SearchConfig(),
        )

    pipeline.ingest_directory.assert_awaited_once()
    _assert_failed_without_leak(job_store, job.job_id, REINDEX_FAILED_DETAIL)
    # The ingest error is logged outside its except block: exc_info must carry it explicitly.
    assert_logged_with_traceback(caplog, LANCE_MSG)


# ---------------------------------------------------------------------------
# routes_graph community-rebuild background task
# ---------------------------------------------------------------------------


async def _run_community_rebuild(tmp_path: Path, get_all_nodes: AsyncMock) -> tuple[JobStore, str]:
    """Drive ``_community_rebuild_task`` with a real CommunityBuilder over a failing graph store."""
    job_store = _job_store(tmp_path)
    # The route creates these jobs RUNNING (no QUEUED -> RUNNING claim).
    running = queued = job_store.create_community_rebuild(collection=_COLLECTION, namespace=_NAMESPACE)
    assert running.status == JobStatus.RUNNING
    graph_store = MagicMock()
    graph_store.get_all_nodes = get_all_nodes

    await _community_rebuild_task(
        job=running, job_store=job_store, graph_store=graph_store, graph_config=GraphConfig(),
        search_store=_search_store(),
    )

    get_all_nodes.assert_awaited_once()
    return job_store, queued.job_id


@pytest.mark.parametrize(
    "error",
    # ImportError: a plain one (not CommunityDependencyError) is not the curated install hint.
    # OSError: outside the builder's documented ValueError/ImportError/RuntimeError — must still
    # reach FAILED instead of leaving the job RUNNING.
    [RuntimeError(LANCE_MSG), ValueError(LANCE_MSG), ImportError(LANCE_MSG), OSError(LANCE_MSG)],
    ids=["runtime", "value", "import", "os"],
)
async def test_community_rebuild_task_failure_does_not_echo_graph_store_error(
    tmp_path: Path, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        job_store, job_id = await _run_community_rebuild(tmp_path, AsyncMock(side_effect=error))

    _assert_failed_without_leak(job_store, job_id, COMMUNITY_REBUILD_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


async def test_keep_community_rebuild_no_nodes_message_reaches_job_error(tmp_path: Path) -> None:
    job_store, job_id = await _run_community_rebuild(tmp_path, AsyncMock(return_value=[]))

    job = job_store.get(job_id)
    assert job is not None and job.status == JobStatus.FAILED
    assert job.error is not None and f"No entity graph nodes found for collection {_COLLECTION!r}" in job.error


# ---------------------------------------------------------------------------
# F — JobScheduler dispatch failure
# ---------------------------------------------------------------------------


def test_scheduler_dispatch_failure_does_not_echo_dispatch_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = _job_store(tmp_path)
    job = job_store.create_export(
        collection=_COLLECTION, output_path=str(tmp_path / "out.tar.gz"), tmp_path=str(tmp_path / "out.jsonl.tmp")
    )
    dispatch = MagicMock(side_effect=RuntimeError(LANCE_MSG))
    scheduler = JobScheduler(store=job_store, max_concurrent=1, dispatch_fn=dispatch)

    with caplog.at_level(logging.ERROR):
        scheduler._tick()

    dispatch.assert_called_once()
    _assert_failed_without_leak(job_store, job.job_id, DISPATCH_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


# ---------------------------------------------------------------------------
# G — MaintenanceLoop per-policy ``last_error``
# ---------------------------------------------------------------------------


async def _no_chunks(*args: Any, **kwargs: Any):
    return
    yield


# Each per-policy method's name and the ``last_error`` its failure records. Names are
# read off the methods themselves, so renaming one fails this module at import
# instead of patching an attribute the pass never calls.
_POLICY_DETAILS: dict[str, str] = {
    MaintenanceLoop._run_fts_optimize.__name__: FTS_OPTIMIZE_FAILED_DETAIL,
    MaintenanceLoop._run_orphan_cleanup.__name__: ORPHAN_CLEANUP_FAILED_DETAIL,
    MaintenanceLoop._run_expired_chunk_pruning.__name__: EXPIRED_CHUNK_PRUNING_FAILED_DETAIL,
    MaintenanceLoop._run_collection_graph_gc.__name__: GRAPH_GC_FAILED_DETAIL,
}
_POLICIES = list(_POLICY_DETAILS)


def _failing_maintenance_loop(tmp_path: Path, failing_policy: str) -> tuple[MaintenanceLoop, AsyncMock]:
    """A MaintenanceLoop over one collection whose ``failing_policy`` raises a LanceDB-path error."""
    store = AsyncMock()
    store.list_collections = AsyncMock(return_value=[CollectionInfo(name="docs", doc_count=1, chunk_count=5, namespace=_NAMESPACE)])
    store.get_collection_meta = AsyncMock(return_value=None)
    store.get_all_collections_meta = AsyncMock(return_value=[])
    store.list_chunks_raw = _no_chunks
    graph_store = MagicMock()
    graph_store.count_stale_mentions = AsyncMock(return_value=0)
    graph_store.prune_stale_mentions = AsyncMock(return_value=0)
    graph_store.delete_orphan_nodes_and_edges = AsyncMock(return_value=MagicMock(communities_invalidated=False))
    loop = MaintenanceLoop(
        dispatch_retry=lambda _job: None,
        job_store=MagicMock(), search_store=store,
        config=MaintenanceConfig(interval_hours=0, graph_gc=True), data_dir=tmp_path, graph_store=graph_store,
    )
    for policy in _POLICIES:
        setattr(loop, policy, AsyncMock(return_value=0))
    failing = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    setattr(loop, failing_policy, failing)
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]
    return loop, failing


@pytest.mark.parametrize("failing_policy", _POLICIES)
async def test_maintenance_last_error_does_not_echo_policy_exception(
    tmp_path: Path, failing_policy: str, caplog: pytest.LogCaptureFixture
) -> None:
    loop, failing = _failing_maintenance_loop(tmp_path, failing_policy)

    with caplog.at_level(logging.ERROR):
        await loop._run_one_pass()

    failing.assert_awaited_once()
    health = json.loads((tmp_path / _STATE_FILE).read_text(encoding="utf-8"))["collection_health"][f"{_NAMESPACE}/docs"]
    assert health["last_error"] == _POLICY_DETAILS[failing_policy], health
    assert_logged_with_traceback(caplog, LANCE_MSG)


@pytest.mark.integration
def test_maintenance_last_error_reaches_get_status_without_policy_exception(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """End to end: the sanitized ``last_error`` is what ``GET /status`` serves (routes_status)."""
    failing_policy = MaintenanceLoop._run_fts_optimize.__name__
    loop_dir = tmp_path / "maintenance"  # kept apart from the app's own data dir
    loop_dir.mkdir()
    loop, failing = _failing_maintenance_loop(loop_dir, failing_policy)
    asyncio.run(loop._run_one_pass())
    failing.assert_awaited_once()

    client = make_status_client(tmp_path, auth_headers)
    with client:
        client.app.state.maintenance_loop = loop
        response = client.get("/status")

    assert response.status_code == HTTPStatus.OK, response.text
    health = {e["collection"]: e for e in response.json()["maintenance"]["collection_health"]}
    assert health["docs"]["last_error"] == _POLICY_DETAILS[failing_policy], health
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into GET /status: {response.text!r}"
