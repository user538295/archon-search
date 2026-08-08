"""Task 1.4 — Per-collection embedding model HTTP lifecycle.

Exercises PATCH /collections/{name} → POST /{name}/reindex → GET /{name}
round-trip, the failure-safety invariant (active model not promoted on error),
and search availability during a slow reindex.

Run with:
    uv run pytest tests/integration/test_http_per_collection_model.py -v
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archon_search.server.routes_collections as _routes_collections
from archon_search.sync import path_to_collection_name
from archon_search.types import JobStatus
from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = pytest.mark.integration

# Model names are arbitrary strings — the stubs always produce 384-dim vectors.
# validate_embedding_model is patched so it does not attempt a real model probe.
_MODEL_A = "BAAI/bge-small-en-v1.5"  # matches SearchConfig.embedding_model default
_MODEL_B = "BAAI/bge-base-en-v1.5"
_STUB_DIM = 384


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_until_terminal(
    client,
    job_id: str,
    api_key: str,
    *,
    timeout_s: float = 15.0,
) -> dict:
    """Poll GET /jobs/{job_id} until a terminal status is reached.

    Returns the final job dict. Calls pytest.fail on timeout.
    Accepts DONE, FAILED, and CANCELLED as terminal states.
    """
    deadline = time.monotonic() + timeout_s
    terminal = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=_auth(api_key))
        assert r.status_code == 200, f"GET /jobs/{job_id} returned {r.status_code}: {r.text}"
        data = r.json()
        if data["status"] in terminal:
            return data
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout_s}s")


# ---------------------------------------------------------------------------
# Test 1 — Full lifecycle: PATCH → reindex → GET confirms model promoted
# ---------------------------------------------------------------------------


def test_full_lifecycle_patch_reindex_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /collections/{name} sets pending model → POST reindex → GET shows active model.

    Flow:
    1. Ingest a document into a real collection (creates chunks > 0).
    2. PATCH with a new embedding_model.  Assert 200 + needs_reindex=True + pending set.
    3. POST /{name}/reindex.  Assert 202 + job_id.
    4. Poll until job DONE.
    5. GET /{name}.  Assert active_embedding_model == new model, needs_reindex=False,
       pending_embedding_model is None, reindex_job_id is None.
    """
    col_dir = tmp_path / "col-lifecycle"
    col_dir.mkdir()
    doc = col_dir / "sample.md"
    doc.write_text("# Lifecycle test\n\nContent for per-collection model lifecycle.\n" * 6)

    col_name = path_to_collection_name(str(col_dir))

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Register the collection path so PATCH/GET /collections/{name} can find it.
        cfg.collections.append(str(col_dir))

        # Ingest document so chunk_count > 0 (triggers needs_reindex path in PATCH).
        ingest_file_via_path(client, col_name, str(doc), api_key=api_key)

        # PATCH: set a new embedding model.
        # validate_embedding_model is patched so it does not attempt a real model probe.
        with patch.object(
            _routes_collections,
            "validate_embedding_model",
            return_value=_STUB_DIM,
        ):
            patch_resp = client.patch(
                f"/collections/{col_name}",
                json={"embedding_model": _MODEL_B},
                headers=_auth(api_key),
            )

        assert patch_resp.status_code == 200, (
            f"PATCH expected 200, got {patch_resp.status_code}: {patch_resp.text}"
        )
        patch_data = patch_resp.json()
        assert patch_data["needs_reindex"] is True, (
            f"expected needs_reindex=True after PATCH, got: {patch_data['needs_reindex']}"
        )
        assert patch_data["pending_embedding_model"] == _MODEL_B, (
            f"expected pending_embedding_model={_MODEL_B!r} in PATCH response, "
            f"got: {patch_data['pending_embedding_model']!r}"
        )

        # POST reindex job.
        reindex_resp = client.post(
            f"/collections/{col_name}/reindex",
            headers=_auth(api_key),
        )
        assert reindex_resp.status_code == 202, (
            f"POST reindex expected 202, got {reindex_resp.status_code}: {reindex_resp.text}"
        )
        job_id = reindex_resp.json()["job_id"]

        # Poll until terminal state.
        final = _poll_job_until_terminal(client, job_id, api_key)
        assert final["status"] == "DONE", (
            f"reindex job ended with {final['status']}: {final}"
        )

        # GET /collections/{name} — assert model promoted and all reindex state cleared.
        get_resp = client.get(
            f"/collections/{col_name}",
            headers=_auth(api_key),
        )
        assert get_resp.status_code == 200, (
            f"GET /collections/{col_name} expected 200, got {get_resp.status_code}: {get_resp.text}"
        )
        get_data = get_resp.json()
        assert get_data["active_embedding_model"] == _MODEL_B, (
            f"expected active_embedding_model={_MODEL_B!r} after DONE, "
            f"got: {get_data['active_embedding_model']!r}"
        )
        assert get_data["needs_reindex"] is False, (
            f"expected needs_reindex=False after DONE, got: {get_data['needs_reindex']}"
        )
        assert get_data["pending_embedding_model"] is None, (
            f"expected pending_embedding_model=None after DONE, "
            f"got: {get_data['pending_embedding_model']!r}"
        )
        assert get_data["reindex_job_id"] is None, (
            f"expected reindex_job_id=None after DONE, got: {get_data['reindex_job_id']!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Failure path: active model not promoted when reindex raises midway
# ---------------------------------------------------------------------------


def test_reindex_task_vectors_intact_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pipeline.ingest_directory raises, active_embedding_model stays at model-A.

    Uses the REAL _reindex_task so its failure-handling code (lines 269-288 in
    routes_jobs.py) actually runs: catches the exception, marks job FAILED, and
    does NOT touch active_embedding_model.

    Flow:
    1. Ingest with the default model (model-A).
    2. PATCH to set model-B as pending.
    3. Monkeypatch pipeline.ingest_directory to raise RuntimeError.
    4. POST reindex.  Poll until job FAILED.
    5. Assert active_embedding_model is still model-A.
    6. Assert documents are still searchable.
    """
    col_dir = tmp_path / "col-failure"
    col_dir.mkdir()
    doc = col_dir / "content.md"
    doc.write_text(
        "# Failure resilience\n\nThis document must remain searchable after reindex failure.\n" * 6
    )

    col_name = path_to_collection_name(str(col_dir))

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        cfg.collections.append(str(col_dir))

        # Ingest with model-A (the default embedder — always 384-dim stubs).
        ingest_file_via_path(client, col_name, str(doc), api_key=api_key)

        # Verify docs are searchable before we touch the model config.
        results_before = search(client, col_name, "resilience content", api_key=api_key)
        assert results_before, "expected search results after initial ingest"

        # PATCH to set model-B as pending (chunk_count > 0 → needs_reindex path).
        with patch.object(
            _routes_collections,
            "validate_embedding_model",
            return_value=_STUB_DIM,
        ):
            patch_resp = client.patch(
                f"/collections/{col_name}",
                json={"embedding_model": _MODEL_B},
                headers=_auth(api_key),
            )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["needs_reindex"] is True

        # Patch pipeline.ingest_directory to raise — the REAL _reindex_task runs and
        # its exception handler marks the job FAILED without touching active_embedding_model.
        # monkeypatch.setattr is used instead of a `with` context so the mock persists
        # until test teardown, guaranteeing it is in effect when the background asyncio
        # task eventually calls ingest_directory (which runs after the HTTP response is sent).
        pipeline = client.app.state.pipeline
        failing_ingest = AsyncMock(side_effect=RuntimeError("simulated ingest failure"))
        monkeypatch.setattr(pipeline, "ingest_directory", failing_ingest)
        reindex_resp = client.post(
            f"/collections/{col_name}/reindex",
            headers=_auth(api_key),
        )
        assert reindex_resp.status_code == 202
        job_id = reindex_resp.json()["job_id"]

        # Poll until terminal — expect FAILED (real _reindex_task marks FAILED on exception).
        final = _poll_job_until_terminal(client, job_id, api_key)
        assert final["status"] == "FAILED", (
            f"expected job status FAILED after ingest_directory raised, got: {final['status']}"
        )

        # active_embedding_model must still be model-A (not promoted to model-B).
        get_resp = client.get(f"/collections/{col_name}", headers=_auth(api_key))
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        active = get_data["active_embedding_model"]
        assert active == _MODEL_A, (
            f"active_embedding_model must remain {_MODEL_A!r} after failure; "
            f"got: {active!r}"
        )
        # needs_reindex and pending_embedding_model must stay set — the operator
        # must still be able to retry.  reindex_job_id is cleared so a new POST
        # /reindex is not blocked by a stale FAILED job reference.
        assert get_data["needs_reindex"] is True, (
            "needs_reindex must remain True after a failed reindex (retry is required)"
        )
        assert get_data["pending_embedding_model"] == _MODEL_B, (
            f"pending_embedding_model must remain {_MODEL_B!r} after failure; "
            f"got: {get_data['pending_embedding_model']!r}"
        )
        assert get_data["reindex_job_id"] is None, (
            f"reindex_job_id must be cleared after a FAILED job; "
            f"got: {get_data['reindex_job_id']!r}"
        )

        # Documents must still be searchable.
        results_after = search(client, col_name, "resilience content", api_key=api_key)
        assert results_after, (
            "documents must remain searchable after a failed reindex attempt"
        )


# ---------------------------------------------------------------------------
# Test 3 — Search available during reindex; model updated after DONE
# ---------------------------------------------------------------------------


def test_search_remains_available_during_reindex_and_model_updates_after_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search proceeds while a slow reindex is running; model is promoted after DONE.

    Uses two threading.Events (NOT asyncio.Event — asyncio.Event is bound to a
    single event loop and cannot be signaled from a test thread):
    - started_event: set by the slow task when it begins executing inside a thread.
    - release_event: set by the test thread after verifying that search works.

    The slow task calls ``await asyncio.to_thread(release_event.wait, timeout=30.0)``
    which blocks in a thread-pool worker WITHOUT blocking the event loop — so
    concurrent HTTP requests (including the search POST) can proceed normally.
    The timeout prevents the thread from hanging indefinitely if the test fails
    before setting release_event.

    Flow:
    1. Ingest documents.  Assert 200 search.
    2. PATCH to set model-B as pending.
    3. Replace _reindex_task with a slow version that signals started_event and
       then waits for release_event inside a thread (non-blocking for event loop).
    4. POST reindex.
    5. Wait for started_event (task has begun).
    6. POST /search — assert 200 with results (event loop is not blocked).
    7. Set release_event to unblock the task.
    8. Poll until job DONE.
    9. GET /collections/{name} — assert active_embedding_model updated and
       pending_embedding_model is None.
    """
    col_dir = tmp_path / "col-concurrent"
    col_dir.mkdir()
    doc = col_dir / "concurrent.md"
    doc.write_text(
        "# Concurrent availability\n\nSearch must work during slow model reindex.\n" * 8
    )

    col_name = path_to_collection_name(str(col_dir))

    started_event = threading.Event()
    release_event = threading.Event()

    async def _slow_reindex_task(**kwargs):
        """Slow reindex: signals started, waits for release (in thread pool), marks DONE."""
        job_id = kwargs["job_id"]
        job_store = kwargs["job_store"]
        store = kwargs["store"]
        collection = kwargs["collection"]
        namespace = kwargs.get("namespace", "default")

        job_store.update(job_id, status=JobStatus.RUNNING)
        started_event.set()

        # Block in a thread pool — does NOT block the event loop, so concurrent
        # HTTP requests (search, status) can proceed normally.
        # Timeout of 30s prevents the thread from hanging if the test fails early.
        await asyncio.to_thread(release_event.wait, 30.0)

        # Success path: promote model-B to active.
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            meta.active_embedding_model = _MODEL_B
            meta.pending_embedding_model = None
            meta.needs_reindex = False
            meta.reindex_job_id = None
            await store.update_collection_meta(meta)

        job_store.update(job_id, status=JobStatus.DONE)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        cfg.collections.append(str(col_dir))

        ingest_file_via_path(client, col_name, str(doc), api_key=api_key)

        # Verify initial search works.
        results_initial = search(client, col_name, "concurrent availability", api_key=api_key)
        assert results_initial, "expected results from initial ingest"

        # PATCH to set model-B as pending.
        with patch.object(
            _routes_collections,
            "validate_embedding_model",
            return_value=_STUB_DIM,
        ):
            patch_resp = client.patch(
                f"/collections/{col_name}",
                json={"embedding_model": _MODEL_B},
                headers=_auth(api_key),
            )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["needs_reindex"] is True

        # Install slow _reindex_task and POST reindex.
        with patch.object(
            _routes_collections,
            "_reindex_task",
            side_effect=_slow_reindex_task,
        ):
            reindex_resp = client.post(
                f"/collections/{col_name}/reindex",
                headers=_auth(api_key),
            )
        assert reindex_resp.status_code == 202
        job_id = reindex_resp.json()["job_id"]

        # Wait until the slow task has started (is now blocking in the thread pool).
        started = started_event.wait(timeout=10.0)
        assert started, "slow reindex task did not start within 10s"

        # POST /search while the task is blocked — event loop must not be frozen.
        concurrent_results = search(
            client, col_name, "concurrent availability", api_key=api_key
        )
        assert concurrent_results, (
            "search must return results while reindex is blocked waiting for release_event"
        )

        # Unblock the slow task.
        release_event.set()

        # Poll until DONE.
        final = _poll_job_until_terminal(client, job_id, api_key)
        assert final["status"] == "DONE", (
            f"slow reindex job ended with unexpected status: {final['status']}"
        )

        # GET /collections/{name} — model must be promoted, all reindex state cleared.
        get_resp = client.get(
            f"/collections/{col_name}",
            headers=_auth(api_key),
        )
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["active_embedding_model"] == _MODEL_B, (
            f"expected active_embedding_model={_MODEL_B!r} after slow reindex DONE, "
            f"got: {get_data['active_embedding_model']!r}"
        )
        assert get_data["needs_reindex"] is False, (
            f"expected needs_reindex=False after DONE, got: {get_data['needs_reindex']}"
        )
        assert get_data["pending_embedding_model"] is None, (
            f"expected pending_embedding_model=None after DONE, "
            f"got: {get_data['pending_embedding_model']!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — 409 guard: second POST /reindex while first is in-progress
# ---------------------------------------------------------------------------


def test_reindex_409_when_already_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /reindex returns 409 while a reindex job is already RUNNING.

    Uses a slow _reindex_task (same pattern as Test 3) to hold the job in
    RUNNING state, then issues a second POST and asserts 409.
    """
    col_dir = tmp_path / "col-409"
    col_dir.mkdir()
    doc = col_dir / "guard.md"
    doc.write_text("# 409 guard\n\nContent to ensure chunk_count > 0.\n" * 6)

    col_name = path_to_collection_name(str(col_dir))

    started_event = threading.Event()
    release_event = threading.Event()

    async def _blocking_reindex_task(**kwargs):
        job_id = kwargs["job_id"]
        job_store = kwargs["job_store"]
        store = kwargs["store"]
        collection = kwargs["collection"]
        namespace = kwargs.get("namespace", "default")

        job_store.update(job_id, status=JobStatus.RUNNING)
        started_event.set()
        await asyncio.to_thread(release_event.wait, 30.0)

        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            meta.active_embedding_model = _MODEL_B
            meta.pending_embedding_model = None
            meta.needs_reindex = False
            meta.reindex_job_id = None
            await store.update_collection_meta(meta)
        job_store.update(job_id, status=JobStatus.DONE)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        cfg.collections.append(str(col_dir))

        ingest_file_via_path(client, col_name, str(doc), api_key=api_key)

        with patch.object(
            _routes_collections,
            "validate_embedding_model",
            return_value=_STUB_DIM,
        ):
            client.patch(
                f"/collections/{col_name}",
                json={"embedding_model": _MODEL_B},
                headers=_auth(api_key),
            )

        # Start first reindex (slow).
        with patch.object(
            _routes_collections,
            "_reindex_task",
            side_effect=_blocking_reindex_task,
        ):
            first_resp = client.post(
                f"/collections/{col_name}/reindex",
                headers=_auth(api_key),
            )
        assert first_resp.status_code == 202

        # Wait until the task is actually RUNNING before issuing the second POST.
        started = started_event.wait(timeout=10.0)
        assert started, "blocking reindex task did not start within 10s"

        # Second POST while first is RUNNING — must return 409.
        second_resp = client.post(
            f"/collections/{col_name}/reindex",
            headers=_auth(api_key),
        )
        assert second_resp.status_code == 409, (
            f"expected 409 when reindex already in progress, "
            f"got {second_resp.status_code}: {second_resp.text}"
        )

        # Unblock the slow task so the app can shut down cleanly.
        release_event.set()
        _poll_job_until_terminal(client, first_resp.json()["job_id"], api_key)


# ---------------------------------------------------------------------------
# Test 5 — Empty collection: PATCH promotes model immediately (no reindex)
# ---------------------------------------------------------------------------


def test_patch_empty_collection_promotes_model_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH on a collection with zero chunks promotes the model immediately.

    No reindex job is needed — chunk_count == 0 means there is nothing to
    re-embed.  The response must show needs_reindex=False and
    active_embedding_model == new model.

    Strategy: ingest a document so that collection meta exists (PATCH returns 404
    without meta), then monkeypatch SearchStore.count_chunks to return 0 on the
    PATCH call — simulating a collection whose documents were all deleted.
    """
    col_dir = tmp_path / "col-empty"
    col_dir.mkdir()
    doc = col_dir / "empty-test.md"
    doc.write_text("# Empty test\n\nSeed document — will be simulated-deleted.\n" * 4)

    col_name = path_to_collection_name(str(col_dir))

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        cfg.collections.append(str(col_dir))

        # Ingest to create collection meta (PATCH requires meta to exist).
        ingest_file_via_path(client, col_name, str(doc), api_key=api_key)

        # Simulate all documents deleted: count_chunks returns 0 so the
        # PATCH handler takes the immediate-promotion branch.
        search_store = client.app.state.search_store
        with (
            patch.object(search_store, "count_chunks", new=AsyncMock(return_value=0)),
            patch.object(
                _routes_collections,
                "validate_embedding_model",
                return_value=_STUB_DIM,
            ),
        ):
            patch_resp = client.patch(
                f"/collections/{col_name}",
                json={"embedding_model": _MODEL_B},
                headers=_auth(api_key),
            )

        assert patch_resp.status_code == 200, (
            f"PATCH expected 200, got {patch_resp.status_code}: {patch_resp.text}"
        )
        data = patch_resp.json()
        assert data["needs_reindex"] is False, (
            f"empty collection must not need reindex; got needs_reindex={data['needs_reindex']}"
        )
        assert data["active_embedding_model"] == _MODEL_B, (
            f"active_embedding_model must be immediately promoted for empty collection; "
            f"got: {data['active_embedding_model']!r}"
        )
        assert data["pending_embedding_model"] is None, (
            f"pending_embedding_model must be None after immediate promotion; "
            f"got: {data['pending_embedding_model']!r}"
        )
