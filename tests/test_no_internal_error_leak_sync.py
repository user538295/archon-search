"""Collection-sync errors never carry an internal exception message onto the wire.

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``detail`` or job
``error`` field for a caught exception whose message embeds internals. Both
``SearchCollectionSync._ingest_collection`` and ``_apply_collection_changes`` wrap a
catch-all ``except Exception`` around the pipeline / store / embedder calls, so the
caught message can be any LanceDB, ONNX or OSError text. They publish it on three
surfaces, all read here through a real app over ``TestClient``:

- ``SyncResult.errors`` -> the ``POST /sync`` job's ``result["errors"]`` (``GET /jobs/{id}``);
- ``CollectionProgress.error`` -> ``GET /status`` ``collections[].error`` and
  ``GET /indexing-state`` ``collections.<name>.error``.

Each test raises a path-bearing exception at the real raise point the sync wraps and
asserts the path stays off every surface, that each surface carries the fixed
``collection_sync_failed_detail`` text (so the absence check cannot pass vacuously),
and that the cause is still logged with its traceback.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import CollectionInfo
from archon_search.progress import (
    CollectionProgress,
    IndexingStateStore,
    IndexingStatus,
)
from archon_search.server.routes_jobs import _TERMINAL_STATUSES
from archon_search.sync import SearchCollectionSync, collection_sync_failed_detail
from archon_search.types import JobStatus
from tests._internal_error_leak import (
    LANCE_MSG,
    LEAK_NEEDLE,
    assert_logged_with_traceback,
)
from tests._maintenance_status_client import make_status_client
from tests.test_sync import make_mock_pipeline

pytestmark = pytest.mark.integration

_COLLECTION = "myproject"
_DONE_CHUNK_SIZE = 512
_JOB_POLL_DEADLINE_S = 10.0
_JOB_POLL_INTERVAL_S = 0.05
# The wire values of the statuses the jobs routes treat as terminal (one source of truth).
_TERMINAL_JOB_STATUSES = frozenset(status.value for status in _TERMINAL_STATUSES)

_Syncer = tuple[SearchCollectionSync, AsyncMock, Path]


def _pipeline(tmp_path: Path, *, existing: list[str], manifest: dict[str, str] | None) -> MagicMock:
    """``tests/test_sync.py``'s stand-in, plus the calls the incremental path also makes."""
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    pipeline = make_mock_pipeline(tmp_path, existing_collections=existing, manifest=manifest)
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)
    pipeline.store.supports_incremental_fts_delete = True
    pipeline.store.optimize_fts = AsyncMock()
    pipeline.delete_by_source_path = AsyncMock(return_value=1)
    pipeline.recompute_collection_meta = AsyncMock()
    return pipeline


def _source_dir(tmp_path: Path) -> Path:
    source = tmp_path / _COLLECTION
    source.mkdir()
    (source / "doc.md").write_text("content", encoding="utf-8")
    return source


def _initial_ingest_syncer(tmp_path: Path, state_store: IndexingStateStore) -> _Syncer:
    """A brand-new collection: ``sync()`` Step 6 -> ``_ingest_collection`` -> ``ingest_directory`` raises."""
    source = _source_dir(tmp_path)
    pipeline = _pipeline(tmp_path, existing=[], manifest=None)
    raiser = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    pipeline.ingest_directory = raiser
    return SearchCollectionSync(pipeline, state_store=state_store), raiser, source


def _incremental_syncer(tmp_path: Path, state_store: IndexingStateStore) -> _Syncer:
    """A DONE collection with a new file: ``_apply_collection_changes`` -> ``ingest_file`` raises."""
    source = _source_dir(tmp_path)
    pipeline = _pipeline(tmp_path, existing=[_COLLECTION], manifest={_COLLECTION: str(source.resolve())})
    raiser = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    pipeline.ingest_file = raiser
    state_store.update_collection(_COLLECTION, CollectionProgress(
        status=IndexingStatus.DONE, indexed_chunk_size=_DONE_CHUNK_SIZE, file_mtimes={},
    ))
    syncer = SearchCollectionSync(pipeline, state_store=state_store, chunk_size=_DONE_CHUNK_SIZE)
    return syncer, raiser, source


_SCENARIOS: dict[str, Callable[[Path, IndexingStateStore], _Syncer]] = {
    "ingest_collection": _initial_ingest_syncer,
    "apply_collection_changes": _incremental_syncer,
}


def _client(tmp_path: Path, auth_headers: dict[str, str]) -> TestClient:
    """A real app whose namespace owns ``_COLLECTION`` (``GET /indexing-state`` filters on it)."""
    return make_status_client(
        tmp_path, auth_headers, collections=[CollectionInfo(name=_COLLECTION, doc_count=0, chunk_count=0)]
    )


def _install_syncer(client: TestClient, tmp_path: Path, scenario: str) -> _Syncer:
    """Swap the app's ``collection_sync`` for one over the app's own indexing-state store."""
    syncer_dir = tmp_path / "syncer"
    syncer_dir.mkdir()
    syncer, raiser, source = _SCENARIOS[scenario](syncer_dir, client.app.state.state_store)
    client.app.state.collection_sync = syncer
    return syncer, raiser, source


def _poll_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _JOB_POLL_DEADLINE_S
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == HTTPStatus.OK, response.text
        if response.json()["status"] in _TERMINAL_JOB_STATUSES:
            return response.json()
        time.sleep(_JOB_POLL_INTERVAL_S)
    pytest.fail(f"sync job {job_id} did not finish within {_JOB_POLL_DEADLINE_S}s")


def _assert_state_surfaces_sanitized(client: TestClient) -> None:
    """``GET /status`` and ``GET /indexing-state`` serve ``CollectionProgress.error`` verbatim."""
    expected = collection_sync_failed_detail(_COLLECTION)

    status = client.get("/status")
    assert status.status_code == HTTPStatus.OK, status.text
    entry = {e["name"]: e for e in status.json()["collections"]}[_COLLECTION]
    assert (entry["status"], entry["error"]) == (IndexingStatus.FAILED.value, expected), entry
    assert LEAK_NEEDLE not in status.text, f"internal path leaked into GET /status: {entry!r}"

    state = client.get("/indexing-state")
    assert state.status_code == HTTPStatus.OK, state.text
    entry = state.json()["collections"][_COLLECTION]
    assert (entry["status"], entry["error"]) == (IndexingStatus.FAILED.value, expected), entry
    assert LEAK_NEEDLE not in state.text, f"internal path leaked into GET /indexing-state: {entry!r}"


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_post_sync_failure_reaches_the_wire_without_ingest_exception(
    tmp_path: Path, scenario: str, auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """``POST /sync`` -> ``GET /jobs/{id}`` ``result["errors"]``, then both indexing-state reads."""
    with _client(tmp_path, auth_headers) as client, caplog.at_level(logging.WARNING):
        _, raiser, source = _install_syncer(client, tmp_path, scenario)
        client.app.state.config.collections = [str(source)]

        accepted = client.post("/sync")
        assert accepted.status_code == HTTPStatus.ACCEPTED, accepted.text
        job = _poll_job(client, accepted.json()["job_id"])

        raiser.assert_awaited()
        assert job["status"] == JobStatus.DONE.value, job
        assert job["result"]["errors"] == [collection_sync_failed_detail(_COLLECTION)], job
        assert LEAK_NEEDLE not in json.dumps(job), f"internal path leaked into GET /jobs: {job!r}"
        _assert_state_surfaces_sanitized(client)

    assert_logged_with_traceback(caplog, LANCE_MSG)


def test_sync_collection_failure_reaches_the_wire_without_ingest_exception(
    tmp_path: Path, auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """``sync_collection`` (the watcher callback's entry point, server/app.py) called directly
    on the app's own event loop: ``_apply_collection_changes`` runs with no job, so the
    failure is published only through the indexing state."""
    with _client(tmp_path, auth_headers) as client, caplog.at_level(logging.WARNING):
        syncer, raiser, source = _install_syncer(client, tmp_path, "apply_collection_changes")
        client.portal.call(syncer.sync_collection, _COLLECTION, source)

        raiser.assert_awaited()
        _assert_state_surfaces_sanitized(client)

    assert_logged_with_traceback(caplog, LANCE_MSG)
