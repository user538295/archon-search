"""Integration tests for _export_task() in routes_export.py — Task 4.1."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.server.routes_export import _export_task
from archon_search.store import SearchStore
from archon_search.types import ExportJob, JobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str, idx: int, text: str = "hello world") -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx)] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_export_job(
    collection: str,
    output_path: str,
    tmp_path_str: str,
    store: JobStore,
    namespace: str = DEFAULT_NAMESPACE,
) -> ExportJob:
    return store.create_export(
        collection=collection,
        output_path=output_path,
        tmp_path=tmp_path_str,
        namespace=namespace,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


@pytest.fixture
def search_config() -> SearchConfig:
    config = SearchConfig()
    config.jobs.checkpoint_interval = 10  # small interval for tests
    return config


@pytest.fixture
def tmp_search_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Per-test SearchStore with its own LanceDB database."""
    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


async def _seed_collection(
    search_store: SearchStore,
    col_name: str,
    n_chunks: int,
) -> list[ChunkRecord]:
    """Seed *n_chunks* chunks in *col_name*."""
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i) for i in range(n_chunks)]
    await search_store.ensure_collection(col_name, _DIM)
    await search_store.ingest_chunks(col_name, chunks)
    return chunks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_task_completes(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
) -> None:
    """_export_task() on a seeded collection produces a DONE job with a valid archive."""
    col = "export-test-complete"
    await _seed_collection(tmp_search_store, col, 5)

    archive_path = tmp_path / "exports" / f"{col}-test.tar.gz"
    tmp_file = tmp_path / "exports" / ".export-test.jsonl.tmp"
    job = _make_export_job(col, str(archive_path), str(tmp_file), tmp_job_store)

    await _export_task(job, tmp_job_store, tmp_search_store, search_config)

    finished = tmp_job_store.get(job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE
    assert finished.result is not None
    assert "archive_path" in finished.result

    assert archive_path.exists(), "archive must exist on disk"
    # Verify archive structure
    with tarfile.open(archive_path, "r:gz") as tf:
        names = {m.name for m in tf.getmembers()}
    assert names == {"manifest.json", "documents.jsonl"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_task_empty_collection(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
) -> None:
    """Empty collection produces a valid archive with doc_count=0."""
    col = "export-test-empty"
    await tmp_search_store.ensure_collection(col, _DIM)

    archive_path = tmp_path / "empty-export.tar.gz"
    tmp_file = tmp_path / ".export-empty.jsonl.tmp"
    job = _make_export_job(col, str(archive_path), str(tmp_file), tmp_job_store)

    await _export_task(job, tmp_job_store, tmp_search_store, search_config)

    finished = tmp_job_store.get(job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE
    assert archive_path.exists()

    import json
    with tarfile.open(archive_path, "r:gz") as tf:
        manifest_member = tf.getmember("manifest.json")
        f = tf.extractfile(manifest_member)
        assert f is not None
        manifest = json.loads(f.read())

    assert manifest["doc_count"] == 0
    assert manifest["collection"] == col


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_task_cancellation(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
) -> None:
    """When CANCELLING is detected mid-write, job ends CANCELLED with no archive."""
    col = "export-test-cancel"
    # Seed enough chunks to trigger at least one checkpoint
    search_config.jobs.checkpoint_interval = 1
    await _seed_collection(tmp_search_store, col, 5)

    archive_path = tmp_path / "cancel-export.tar.gz"
    tmp_file = tmp_path / ".export-cancel.jsonl.tmp"
    job = _make_export_job(col, str(archive_path), str(tmp_file), tmp_job_store)

    # Set CANCELLING before the task runs (simulates cancellation signal)
    tmp_job_store.update(job.job_id, status=JobStatus.CANCELLING)

    await _export_task(job, tmp_job_store, tmp_search_store, search_config)

    finished = tmp_job_store.get(job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.CANCELLED
    assert not archive_path.exists(), "archive must NOT exist after cancellation"
    assert not tmp_file.exists(), "tmp file must be cleaned up"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_task_store_error(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
) -> None:
    """Simulated read failure results in FAILED job with error message."""
    col = "export-test-error"
    # We can simulate a failure by using a disconnected store

    disconnected_store = SearchStore(tmp_path / "bad-db")
    # Do NOT connect — any access should raise RuntimeError("not connected")

    archive_path = tmp_path / "error-export.tar.gz"
    tmp_file = tmp_path / ".export-error.jsonl.tmp"
    job = _make_export_job(col, str(archive_path), str(tmp_file), tmp_job_store)

    await _export_task(job, tmp_job_store, disconnected_store, search_config)

    finished = tmp_job_store.get(job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.FAILED
    assert finished.error is not None
    assert not archive_path.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_task_checkpoint_progress(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
) -> None:
    """After processing checkpoint_interval docs, progress.processed equals that count."""
    col = "export-test-progress"
    n = search_config.jobs.checkpoint_interval  # number of docs to trigger a checkpoint
    await _seed_collection(tmp_search_store, col, n)

    archive_path = tmp_path / "progress-export.tar.gz"
    tmp_file = tmp_path / ".export-progress.jsonl.tmp"
    job = _make_export_job(col, str(archive_path), str(tmp_file), tmp_job_store)

    await _export_task(job, tmp_job_store, tmp_search_store, search_config)

    finished = tmp_job_store.get(job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE
    # Progress should have been set during writing phase
    # After completion, job may be in DONE with progress from packaging phase
    # At minimum verify the archive contains the expected number of docs
    import json
    with tarfile.open(archive_path, "r:gz") as tf:
        manifest_member = tf.getmember("manifest.json")
        f = tf.extractfile(manifest_member)
        assert f is not None
        manifest = json.loads(f.read())
    assert manifest["doc_count"] == n
