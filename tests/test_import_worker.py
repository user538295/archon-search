"""Integration tests for _import_task() in routes_export.py — Task 5.1."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import struct
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import ChunkRecord
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION, ExportArchiveWriter
from archon_search.jobs.store import JobStore
from archon_search.server.routes_export import _export_task, _import_task
from archon_search.store import SearchStore
from archon_search.types import ImportJob, JobStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 4
_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str, idx: int, text: str = "hello world") -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx % 4 + 0.1)] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )


def _encode_vector(floats: list[float]) -> str:
    packed = struct.pack(f"<{len(floats)}f", *floats)
    return base64.standard_b64encode(packed).decode("ascii")


def _make_archive(
    archive_path: Path,
    collection: str,
    chunks: list[ChunkRecord],
    schema_version: int = EXPORT_SCHEMA_VERSION,
    active_embedding_model: str = _DEFAULT_MODEL,
    corrupt_line: bool = False,
) -> None:
    """Build a minimal valid .tar.gz archive with the given chunks."""
    tmp_jsonl = archive_path.parent / ".tmp.jsonl"
    with tmp_jsonl.open("wb") as f:
        for i, chunk in enumerate(chunks):
            if corrupt_line and i == len(chunks) - 1:
                f.write(b"NOT_JSON\n")
            else:
                doc = {
                    "doc_id": chunk.doc_id,
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "vector": _encode_vector(chunk.vector),
                    "source_path": chunk.source_path,
                    "indexed_at": chunk.indexed_at,
                    "file_type": chunk.file_type,
                    "language": chunk.language,
                    "metadata": chunk.metadata,
                    "acl": chunk.acl,
                    "custom_score": chunk.custom_score,
                    "ingested_by": chunk.ingested_by,
                    "updated_at": chunk.updated_at,
                }
                f.write((json.dumps(doc) + "\n").encode())

    manifest = {
        "archon_search_version": "test",
        "schema_version": schema_version,
        "collection": collection,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "doc_count": len(chunks),
        "active_embedding_model": active_embedding_model,
        "description": "",
    }
    manifest_bytes = json.dumps(manifest).encode()

    with tarfile.open(archive_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))
        tf.add(str(tmp_jsonl), arcname="documents.jsonl")

    tmp_jsonl.unlink()


def _make_import_job(
    collection: str,
    archive_path: str,
    store: JobStore,
    force_overwrite: bool = False,
    ignore_schema_version: bool = False,
    on_error: str = "fail",
    namespace: str = DEFAULT_NAMESPACE,
) -> ImportJob:
    return store.create_import(
        collection=collection,
        archive_path=archive_path,
        force_overwrite=force_overwrite,
        ignore_schema_version=ignore_schema_version,
        on_error=on_error,
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
    config.jobs.checkpoint_interval = 10
    return config


@pytest.fixture
def tmp_search_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


@pytest.fixture
def mock_pipeline() -> MagicMock:
    """Stub pipeline with a no-op recompute_collection_meta."""
    pipeline = MagicMock()
    pipeline.recompute_collection_meta = AsyncMock()
    return pipeline


@pytest.fixture
def mock_embedder_cache() -> MagicMock:
    """Stub embedder cache that returns a fake embedder."""
    embedder = MagicMock()
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=embedder)
    return cache


async def _seed_collection(
    search_store: SearchStore,
    col_name: str,
    n_chunks: int,
) -> list[ChunkRecord]:
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i) for i in range(n_chunks)]
    await search_store.ensure_collection(col_name, _DIM)
    await search_store.ingest_chunks(col_name, chunks)
    return chunks


async def search_store_seed_partial(
    search_store: SearchStore,
    col_name: str,
    chunks: list[ChunkRecord],
) -> None:
    """Seed a collection with an arbitrary list of ChunkRecord objects."""
    await search_store.ensure_collection(col_name, _DIM)
    if chunks:
        await search_store.ingest_chunks(col_name, chunks)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_roundtrip(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Export a seeded collection, import into a new collection; job ends DONE."""
    src_col = "roundtrip-src"
    dst_col = "roundtrip-dst"
    # Use empty embedding model to match the archive (seeded collections have no meta row)
    search_config.embedding_model = ""
    chunks = await _seed_collection(tmp_search_store, src_col, 5)

    archive_path = tmp_path / "exports" / f"{src_col}.tar.gz"
    tmp_file = tmp_path / "exports" / ".export-rt.jsonl.tmp"
    export_job = tmp_job_store.create_export(
        collection=src_col,
        output_path=str(archive_path),
        tmp_path=str(tmp_file),
    )
    await _export_task(export_job, tmp_job_store, tmp_search_store, search_config)
    assert tmp_job_store.get(export_job.job_id).status == JobStatus.DONE

    # Now import into a new collection
    import_job = _make_import_job(dst_col, str(archive_path), tmp_job_store)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result is not None
    assert finished.result["imported"] == 5
    assert finished.result["skipped"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_force_overwrite(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Import with force_overwrite=True into an existing collection drops old data."""
    col = "force-overwrite-col"
    chunks = await _seed_collection(tmp_search_store, col, 3)

    archive_path = tmp_path / "overwrite.tar.gz"
    _make_archive(archive_path, col, chunks[:2])

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, force_overwrite=True)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result["imported"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_existing_collection_no_force_fails(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Import into existing collection without force_overwrite ends in FAILED."""
    col = "existing-no-force"
    chunks = await _seed_collection(tmp_search_store, col, 2)

    archive_path = tmp_path / "no-force.tar.gz"
    _make_archive(archive_path, col, chunks)

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, force_overwrite=False)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.FAILED
    assert finished.error is not None
    assert "already exists" in finished.error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_schema_version_mismatch_rejected(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Archive with wrong schema_version fails unless ignore_schema_version=True."""
    col = "schema-ver-col"
    chunks = [_chunk(_doc_id(), 0)]

    archive_path = tmp_path / "bad-schema.tar.gz"
    _make_archive(archive_path, col, chunks, schema_version=99)

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, ignore_schema_version=False)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )
    assert tmp_job_store.get(import_job.job_id).status == JobStatus.FAILED

    # With ignore_schema_version=True it should succeed
    import_job2 = _make_import_job(
        col + "-ignored", str(archive_path), tmp_job_store,
        ignore_schema_version=True,
    )
    await _import_task(
        import_job2, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )
    assert tmp_job_store.get(import_job2.job_id).status == JobStatus.DONE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_embedding_model_mismatch_always_rejected(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Embedding model mismatch always fails regardless of ignore_schema_version."""
    col = "model-mismatch-col"
    chunks = [_chunk(_doc_id(), 0)]

    archive_path = tmp_path / "bad-model.tar.gz"
    _make_archive(archive_path, col, chunks, active_embedding_model="some/other-model")

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, ignore_schema_version=True)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.FAILED
    assert finished.error is not None
    assert "mismatch" in finished.error.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_on_error_skip(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Archive with one corrupt line and on_error='skip' → DONE with skipped=1."""
    col = "onerr-skip-col"
    chunks = [_chunk(_doc_id(), i) for i in range(3)]

    archive_path = tmp_path / "corrupt-skip.tar.gz"
    _make_archive(archive_path, col, chunks, corrupt_line=True)

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, on_error="skip")
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result["skipped"] == 1
    assert finished.result["imported"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_on_error_fail(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Archive with one corrupt line and on_error='fail' → FAILED."""
    col = "onerr-fail-col"
    chunks = [_chunk(_doc_id(), i) for i in range(3)]

    archive_path = tmp_path / "corrupt-fail.tar.gz"
    _make_archive(archive_path, col, chunks, corrupt_line=True)

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, on_error="fail")
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.FAILED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_resume_from_checkpoint(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Job with progress checkpoint set skips already-processed docs on second run."""
    col = "resume-col"
    n = 200
    skip_count = 100
    search_config.jobs.checkpoint_interval = 100
    search_config.embedding_model = _DEFAULT_MODEL  # matches archive's active_embedding_model

    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i, text=f"doc {i}") for i in range(n)]

    archive_path = tmp_path / "resume.tar.gz"
    _make_archive(archive_path, col, chunks)

    # Create the import job and manually inject a checkpoint progress to simulate
    # a crash that happened after 100 docs were ingested.
    import_job = _make_import_job(col, str(archive_path), tmp_job_store)
    tmp_job_store.update(
        import_job.job_id,
        status=JobStatus.FAILED,
        progress={"processed": skip_count, "total": n, "phase": "ingesting"},
    )

    crashed_job = tmp_job_store.get(import_job.job_id)
    assert crashed_job is not None
    assert crashed_job.status == JobStatus.FAILED
    assert crashed_job.progress is not None
    assert crashed_job.progress["processed"] == skip_count

    # Transition back to QUEUED (as POST /jobs/{id}/resume would do)
    tmp_job_store.update(import_job.job_id, status=JobStatus.QUEUED)

    # Seed the partial collection state: pre-populate with the first skip_count docs
    # to simulate what was ingested before the crash.
    await search_store_seed_partial(tmp_search_store, col, chunks[:skip_count])

    # Track how many docs are ingested in the second run
    ingest_call_total = [0]
    original_ingest = tmp_search_store.ingest_chunks

    async def patched_ingest(collection, chunks_batch, **kwargs):  # type: ignore[no-untyped-def]
        ingest_call_total[0] += len(chunks_batch)
        return await original_ingest(collection, chunks_batch, **kwargs)

    tmp_search_store.ingest_chunks = patched_ingest  # type: ignore[method-assign]

    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    tmp_search_store.ingest_chunks = original_ingest  # type: ignore[method-assign]

    final_job = tmp_job_store.get(import_job.job_id)
    assert final_job is not None
    assert final_job.status == JobStatus.DONE, f"Expected DONE, got {final_job.status}: {final_job.error}"
    # Only the remaining n - skip_count docs should be ingested in the second run
    expected_ingested = n - skip_count
    assert ingest_call_total[0] == expected_ingested, (
        f"Expected {expected_ingested} docs in second run, got {ingest_call_total[0]}"
    )
