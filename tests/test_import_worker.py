"""Integration tests for _import_task() in routes_export.py — Task 5.1."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import logging
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
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderNotReadyError
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION, quote_archive_value
from archon_search.jobs.store import JobStore
from archon_search.server import _job_warmup
from archon_search.server.routes_export import IMPORT_FAILED_DETAIL, _export_task, _import_task
from archon_search.server.routes_jobs import _ACTIVE_STATUSES
from archon_search.store import SearchStore
from archon_search.types import ImportJob, JobStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM = 4
_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
#: ``doc_overrides`` value that removes the field from the archived document.
_DROP = object()


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
    corrupt_index: int | None = None,
    doc_overrides: dict[int, dict[str, object]] | None = None,
    blank_line_before: int | None = None,
) -> None:
    """Build a minimal valid .tar.gz archive with the given chunks.

    *corrupt_line=True* corrupts the last line (backward-compat).
    *corrupt_index* corrupts a specific 0-based line index.
    *doc_overrides* replaces fields of the document at a 0-based index (``_DROP`` removes one).
    *blank_line_before* writes an empty line before the document at that 0-based index.
    """
    _corrupt_at: int | None = corrupt_index
    if _corrupt_at is None and corrupt_line:
        _corrupt_at = len(chunks) - 1

    tmp_jsonl = archive_path.parent / ".tmp.jsonl"
    with tmp_jsonl.open("wb") as f:
        for i, chunk in enumerate(chunks):
            if i == blank_line_before:
                f.write(b"\n")
            if _corrupt_at is not None and i == _corrupt_at:
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
                for field, value in (doc_overrides or {}).get(i, {}).items():
                    if value is _DROP:
                        doc.pop(field)
                    else:
                        doc[field] = value
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
    """Stub pipeline with no-op recompute_collection_meta and warmup_models."""
    pipeline = MagicMock()
    pipeline.recompute_collection_meta = AsyncMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
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
    await _seed_collection(tmp_search_store, src_col, 5)

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


_ACL_FIELDS = ("acl", "scopes", "acl_source", "acl_sidecar_path", "acl_warning")
#: Stands in for a key ``list_chunks_raw`` did not emit, so a missing field fails the equality, not a KeyError.
_ABSENT = "<absent>"
_PROV_SIDECAR_PATH = "secret.md.acl"
_PROV_WARNINGS = ["acl sidecar entry 'deny-all' is not a valid namespace; ignored"]


async def _acl_fields_by_chunk(search_store: SearchStore, collection: str) -> dict[str, dict[str, object]]:
    return {
        r["chunk_id"]: {k: r.get(k, _ABSENT) for k in _ACL_FIELDS}
        async for r in search_store.list_chunks_raw(collection, DEFAULT_NAMESPACE)
    }


def _archived_doc_keys(archive_path: Path) -> set[str]:
    """Union of the keys of every document in the archive's ``documents.jsonl``."""
    with tarfile.open(archive_path, "r:gz") as tf:
        member = tf.extractfile("documents.jsonl")
        assert member is not None
        return {key for line in member.read().decode().splitlines() if line for key in json.loads(line)}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_import_roundtrip_preserves_acl_provenance(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """An exported then imported chunk keeps its ACL, scopes and G15 ACL provenance."""
    src_col = "acl-prov-src"
    dst_col = "acl-prov-dst"
    search_config.embedding_model = ""
    doc_id = _doc_id()
    chunks = [
        dataclasses.replace(
            _chunk(doc_id, 0),
            acl=["default"],
            scopes=["user:alice"],
            acl_source="sidecar",
            acl_sidecar_path=_PROV_SIDECAR_PATH,
            acl_warning=list(_PROV_WARNINGS),
        ),
        _chunk(doc_id, 1),  # no ACL: provenance stays null / []
    ]
    await search_store_seed_partial(tmp_search_store, src_col, chunks)
    source_fields = await _acl_fields_by_chunk(tmp_search_store, src_col)
    # Absolute, so a source read path that drops provenance cannot pass by matching an equally empty import.
    assert source_fields == {
        chunks[0].chunk_id: {
            "acl": ["default"],
            "scopes": ["user:alice"],
            "acl_source": "sidecar",
            "acl_sidecar_path": _PROV_SIDECAR_PATH,
            "acl_warning": _PROV_WARNINGS,
        },
        chunks[1].chunk_id: {
            "acl": None, "scopes": None, "acl_source": None, "acl_sidecar_path": None, "acl_warning": [],
        },
    }

    archive_path = tmp_path / "exports" / f"{src_col}.tar.gz"
    export_job = tmp_job_store.create_export(
        collection=src_col, output_path=str(archive_path), tmp_path=str(tmp_path / "exports" / ".prov.jsonl.tmp"),
    )
    await _export_task(export_job, tmp_job_store, tmp_search_store, search_config)
    assert tmp_job_store.get(export_job.job_id).status == JobStatus.DONE

    import_job = _make_import_job(dst_col, str(archive_path), tmp_job_store)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )
    assert tmp_job_store.get(import_job.job_id).status == JobStatus.DONE

    assert await _acl_fields_by_chunk(tmp_search_store, dst_col) == source_fields


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_archive_without_acl_provenance_imports_null_provenance(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """An archive written before ACL provenance was exported still imports, with null provenance."""
    dst_col = "acl-prov-legacy"
    search_config.embedding_model = _DEFAULT_MODEL
    doc_id = _doc_id()
    archive_path = tmp_path / "legacy.tar.gz"
    _make_archive(archive_path, "legacy", [_chunk(doc_id, 0)])
    assert _archived_doc_keys(archive_path).isdisjoint(_ACL_FIELDS[1:]), "the archive must predate provenance"

    import_job = _make_import_job(dst_col, str(archive_path), tmp_job_store)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )
    assert tmp_job_store.get(import_job.job_id).status == JobStatus.DONE

    assert await _acl_fields_by_chunk(tmp_search_store, dst_col) == {
        f"{doc_id}-000000": {
            "acl": None, "scopes": None, "acl_source": None, "acl_sidecar_path": None, "acl_warning": [],
        },
    }


_BAD_FIELD_INDEX = 1  # the middle of three docs: archive line 2
#: Archive values of the wrong type for a list[str] / str field. JSON-legal, so only the importer can reject them.
_MALFORMED_FIELD_VALUES = [
    pytest.param("acl_warning", "oops", id="acl_warning-str"),
    pytest.param("acl_warning", [1], id="acl_warning-non-str-item"),
    pytest.param("acl", "default", id="acl-str"),
    pytest.param("acl", {"ns": "default"}, id="acl-object"),
    pytest.param("scopes", "user:alice", id="scopes-str"),
    pytest.param("scopes", [None], id="scopes-null-item"),
    pytest.param("acl_source", 7, id="acl_source-int"),
    pytest.param("acl_sidecar_path", ["a.acl"], id="acl_sidecar_path-list"),
    pytest.param("ingested_by", 7, id="ingested_by-int"),
    pytest.param("ingested_by", ["http"], id="ingested_by-list"),
]
#: Two representative shapes (a list field holding a scalar, a str field holding a list): fail mode shares
#: the skip-mode code path, which covers every case above.
_MALFORMED_FIELD_VALUES_FAIL_MODE = [
    pytest.param("acl", "default", id="acl-str"),
    pytest.param("ingested_by", ["http"], id="ingested_by-list"),
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _MALFORMED_FIELD_VALUES)
async def test_import_skip_mode_skips_doc_with_wrongly_typed_field(
    field: str,
    value: object,
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A wrongly typed ACL or ingested_by field is rejected per doc, so on_error='skip' drops that doc only."""
    col = "bad-acl-skip"
    chunks = [_chunk(_doc_id(), i) for i in range(3)]
    archive_path = tmp_path / "bad-acl-skip.tar.gz"
    _make_archive(archive_path, col, chunks, doc_overrides={_BAD_FIELD_INDEX: {field: value}})

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, on_error="skip")
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert (finished.result["imported"], finished.result["skipped"]) == (2, 1)
    imported = await _acl_fields_by_chunk(tmp_search_store, col)
    assert set(imported) == {c.chunk_id for i, c in enumerate(chunks) if i != _BAD_FIELD_INDEX}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _MALFORMED_FIELD_VALUES_FAIL_MODE)
async def test_import_fail_mode_rejects_doc_with_wrongly_typed_field(
    field: str,
    value: object,
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """With on_error='fail' the job fails naming the archive line, never with the type error's text."""
    finished = await _run_failing_import(
        tmp_path, "bad-acl-fail", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_BAD_FIELD_INDEX: {field: value}},
    )

    assert finished.error == (
        f"malformed doc at line {_BAD_FIELD_INDEX + 1}: a required field is missing or has the wrong type"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archived", "stored"),
    [
        pytest.param(_DROP, "cli", id="absent"),
        pytest.param(None, "cli", id="null"),
        pytest.param("", "cli", id="empty"),  # what an export of a null ingested_by column used to write
        pytest.param("archon-search-cli", "cli", id="legacy"),
        pytest.param("not-a-source", "cli", id="unknown"),
        pytest.param("watcher", "watcher", id="canonical"),
    ],
)
async def test_import_normalizes_ingested_by(
    archived: object,
    stored: str,
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """An archived ingested_by is normalized like every stored-row read: only valid IngestedBy values are stored."""
    col = "import-ingested-by"
    archive_path = tmp_path / "import-ingested-by.tar.gz"
    _make_archive(archive_path, col, [_chunk(_doc_id(), 0)], doc_overrides={0: {"ingested_by": archived}})

    import_job = _make_import_job(col, str(archive_path), tmp_job_store)
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )
    assert tmp_job_store.get(import_job.job_id).status == JobStatus.DONE

    # The raw table, not list_chunks_raw: that read path normalizes too, which would mask what was stored.
    table = await tmp_search_store._require_connected().open_table(col)
    [row] = await table.query().to_list()
    assert row["ingested_by"] == stored


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
    rejected = tmp_job_store.get(import_job.job_id)
    assert rejected.status == JobStatus.FAILED
    assert rejected.error == (
        f"schema_version mismatch: archive has 99, server expects {EXPORT_SCHEMA_VERSION!r}; "
        "use ignore_schema_version=true to bypass"
    )

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
    """Archive with one corrupt line and on_error='fail' → FAILED, naming the line."""
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
    # A rejection the import itself diagnoses keeps its message, unlike an unexpected failure;
    # the message is fixed text plus the line number, never the JSON parser's own wording.
    assert finished.error == "Corrupt line 3: not valid UTF-8 JSON"


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


class _CrashOnIngestCall:
    """Wraps ``SearchStore.ingest_chunks`` and raises on the *crash_at*-th call (1-based), as a crash would."""

    def __init__(self, ingest_chunks: object, crash_at: int) -> None:
        self._ingest_chunks = ingest_chunks
        self._crash_at = crash_at
        self._calls = 0

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self._calls += 1
        if self._calls == self._crash_at:
            raise RuntimeError("simulated crash mid-import")
        return await self._ingest_chunks(*args, **kwargs)  # type: ignore[operator]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_after_skipped_lines_does_not_duplicate_chunks(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """Resume continues after the last archive line consumed, not after ``processed`` lines.

    A corrupt line and a malformed doc are skipped before the first checkpoint, so the checkpoint's
    ``processed`` (imported docs) trails the archive lines already consumed. Resuming from ``processed``
    would re-read an already-imported line and append its chunk a second time.
    """
    col = "resume-after-skip"
    search_config.jobs.checkpoint_interval = 2
    chunks = [_chunk(_doc_id(), i) for i in range(7)]
    archive_path = tmp_path / "resume-after-skip.tar.gz"
    # Line 2 is corrupt JSON (skipped by the reader), line 3 a malformed doc (skipped by the importer):
    # the first checkpoint lands after line 4 with processed=2.
    _make_archive(archive_path, col, chunks, corrupt_index=1, doc_overrides={2: {"acl": "default"}})
    expected_chunk_ids = sorted(c.chunk_id for i, c in enumerate(chunks) if i not in (1, 2))

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, on_error="skip")
    original_ingest = tmp_search_store.ingest_chunks
    tmp_search_store.ingest_chunks = _CrashOnIngestCall(original_ingest, crash_at=2)  # type: ignore[method-assign]
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )
    crashed = tmp_job_store.get(import_job.job_id)
    assert (crashed.status, crashed.progress["processed"]) == (JobStatus.FAILED, 2)

    tmp_search_store.ingest_chunks = original_ingest  # type: ignore[method-assign]
    tmp_job_store.update(import_job.job_id, status=JobStatus.QUEUED)  # as POST /jobs/{id}/resume does
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    stored_chunk_ids = sorted([r["chunk_id"] async for r in tmp_search_store.list_chunks_raw(col, DEFAULT_NAMESPACE)])
    assert stored_chunk_ids == expected_chunk_ids
    # The two lines skipped before the checkpoint still count, although the resumed run never re-reads them.
    assert (finished.result["imported"], finished.result["skipped"]) == (5, 2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_on_error_skip_corrupt_first_line(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """S355: corrupt FIRST line with on_error='skip' must not abort the job."""
    col = "onerr-skip-first"
    chunks = [_chunk(_doc_id(), i) for i in range(3)]

    archive_path = tmp_path / "corrupt-first.tar.gz"
    _make_archive(archive_path, col, chunks, corrupt_index=0)

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, on_error="skip")
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result["skipped"] == 1
    assert finished.result["imported"] == 2
    assert finished.result["total_in_archive"] == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_on_error_skip_corrupt_middle_line(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """S355: valid lines after a corrupt middle line must all be imported."""
    col = "onerr-skip-middle"
    chunks = [_chunk(_doc_id(), i) for i in range(3)]

    archive_path = tmp_path / "corrupt-middle.tar.gz"
    _make_archive(archive_path, col, chunks, corrupt_index=1)

    import_job = _make_import_job(col, str(archive_path), tmp_job_store, on_error="skip")
    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result["skipped"] == 1
    assert finished.result["imported"] == 2
    assert finished.result["total_in_archive"] == 3


# ---------------------------------------------------------------------------
# S288 — the import warms the imported collection's embedder before DONE
# ---------------------------------------------------------------------------

# Rebound warm-up ceiling for the timeout test, and how long its never-finishing
# warm-up would otherwise block — far beyond the rebound ceiling.
_SHORT_WARMUP_WAIT_SECONDS = 0.1
_WEDGED_WARMUP_SECONDS = 30.0
# Generous for a loaded CI host, yet below the wedged warm-up: proves the ceiling bit.
_TIMEOUT_TEST_MAX_ELAPSED_SECONDS = 20.0
_S288_DOC_COUNT = 3


def _running_import_job(
    tmp_path: Path, col: str, job_store: JobStore, doc_count: int = _S288_DOC_COUNT
) -> ImportJob:
    """Archive *doc_count* chunks and queue their import, promoted to RUNNING as ``JobScheduler._tick`` does."""
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, [_chunk(_doc_id(), i) for i in range(doc_count)])
    job = _make_import_job(col, str(archive_path), job_store)
    promoted = job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert isinstance(promoted, ImportJob)
    return promoted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_warms_collection_embedder_before_done(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """S288: DONE must mean the embedder serving the imported collection is built.

    ``get_or_load`` returns a lazy embedder, so without a warm-up the first search
    after ``import --wait`` finds it cold and silently runs FTS-only.
    """
    collection_embedder = mock_embedder_cache.get_or_load.return_value
    import_job = _running_import_job(tmp_path, "warm-col", tmp_job_store)
    calls: list[str] = []
    status_at_warmup: list[JobStatus] = []

    async def _record_recompute(*_args: object, **_kwargs: object) -> None:
        calls.append("recompute")

    async def _record_warmup(*_args: object, **_kwargs: object) -> bool:
        calls.append("warmup")
        status_at_warmup.append(tmp_job_store.get(import_job.job_id).status)
        return True

    mock_pipeline.recompute_collection_meta.side_effect = _record_recompute
    mock_pipeline.warmup_models.side_effect = _record_warmup

    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    mock_pipeline.warmup_models.assert_awaited_once_with(collection_embedder)
    assert calls == ["recompute", "warmup"]
    assert status_at_warmup == [JobStatus.RUNNING]
    assert tmp_job_store.get(import_job.job_id).status == JobStatus.DONE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_warmup_failure_does_not_fail_import(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warm-up error is auxiliary: the content is imported, so the job is DONE."""
    mock_pipeline.warmup_models.side_effect = RuntimeError("onnx build exploded")
    import_job = _running_import_job(tmp_path, "warm-fail-col", tmp_job_store)

    with caplog.at_level(logging.WARNING, logger=_job_warmup.logger.name):
        await _import_task(
            import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
        )

    mock_pipeline.warmup_models.assert_awaited_once()
    warnings = [r for r in caplog.records if r.name == _job_warmup.logger.name]
    assert len(warnings) == 1
    # A failure is not reported as a timeout, and keeps its traceback for diagnosis.
    assert "failed" in warnings[0].getMessage()
    assert "did not finish within" not in warnings[0].getMessage()
    assert warnings[0].exc_info is not None
    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result["imported"] == _S288_DOC_COUNT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_warmup_timeout_does_not_fail_import(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wedged warm-up is bounded like ingest's (S281), logged as a timeout, and never fails the import."""
    monkeypatch.setattr(_job_warmup, "JOB_WARMUP_WAIT_SECONDS", _SHORT_WARMUP_WAIT_SECONDS)

    async def _wedged(*_args: object, **_kwargs: object) -> bool:
        await asyncio.sleep(_WEDGED_WARMUP_SECONDS)
        return True

    mock_pipeline.warmup_models.side_effect = _wedged
    import_job = _running_import_job(tmp_path, "warm-timeout-col", tmp_job_store)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with caplog.at_level(logging.WARNING, logger=_job_warmup.logger.name):
        await _import_task(
            import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
        )

    mock_pipeline.warmup_models.assert_awaited_once()
    assert loop.time() - started < _TIMEOUT_TEST_MAX_ELAPSED_SECONDS
    warnings = [r.getMessage() for r in caplog.records if r.name == _job_warmup.logger.name]
    assert len(warnings) == 1
    assert "did not finish within" in warnings[0]
    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result["imported"] == _S288_DOC_COUNT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_cancel_during_warmup_ends_cancelled(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A ``DELETE /jobs/{id}`` landing during the warm-up is honoured, not overwritten by DONE."""
    import_job = _running_import_job(tmp_path, "warm-cancel-col", tmp_job_store)

    async def _cancel_like_delete_job(*_args: object, **_kwargs: object) -> bool:
        # The same transition routes_jobs.delete_job applies to an active job.
        cancelling = tmp_job_store.transition(import_job.job_id, _ACTIVE_STATUSES, JobStatus.CANCELLING)
        assert cancelling is not None
        return True

    mock_pipeline.warmup_models.side_effect = _cancel_like_delete_job

    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    mock_pipeline.warmup_models.assert_awaited_once()
    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.CANCELLED
    assert finished.result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_cancelled_mid_warmup_stays_running_for_crash_recovery(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A task cancelled mid-warm-up (only shutdown does that) propagates and leaves the job RUNNING.

    ``DELETE /jobs/{id}`` never cancels the task — it sets CANCELLING. The only
    ``task.cancel()`` is the lifespan shutdown, and crash recovery turns a RUNNING job
    into ``FAILED``/``process_restart``, which ``POST /jobs/{id}/resume`` accepts. A
    CANCELLED job would not be resumable.
    """
    import_job = _running_import_job(tmp_path, "warm-taskcancel-col", tmp_job_store)
    warmup_entered = asyncio.Event()

    async def _block_until_cancelled(*_args: object, **_kwargs: object) -> bool:
        warmup_entered.set()
        await asyncio.Event().wait()
        return True

    mock_pipeline.warmup_models.side_effect = _block_until_cancelled
    task = asyncio.create_task(
        _import_task(
            import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
        )
    )
    await warmup_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert tmp_job_store.get(import_job.job_id).status == JobStatus.RUNNING
    # What the next process start makes of it: a FAILED job that resume accepts.
    recovered = JobStore(path=tmp_path / "jobs.json").get(import_job.job_id)
    assert recovered.status == JobStatus.FAILED
    assert recovered.error == "process_restart"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_empty_archive_skips_warmup(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """An empty archive creates no table, so there is nothing to search and nothing to warm."""
    import_job = _running_import_job(tmp_path, "warm-empty-col", tmp_job_store, doc_count=0)

    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result == {"imported": 0, "skipped": 0, "total_in_archive": 0}
    mock_pipeline.warmup_models.assert_not_awaited()


# ---------------------------------------------------------------------------
# The job ``error`` field never carries an unexpected exception's text
# ---------------------------------------------------------------------------

_INTERNAL_DETAIL = "lance error: /var/lib/secret/db/table.lance io failure"
_ROUTES_EXPORT_LOGGER = "archon_search.server.routes_export"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_unexpected_failure_records_sanitized_error(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cause = RuntimeError(_INTERNAL_DETAIL)
    mock_pipeline.recompute_collection_meta.side_effect = cause
    import_job = _running_import_job(tmp_path, "unexpected-fail-col", tmp_job_store)

    with caplog.at_level(logging.ERROR, logger=_ROUTES_EXPORT_LOGGER):
        await _import_task(
            import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
        )

    mock_pipeline.recompute_collection_meta.assert_awaited_once()
    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.FAILED
    assert finished.error == IMPORT_FAILED_DETAIL
    assert "secret" not in finished.error
    # The withheld cause is still logged server-side, traceback included.
    [record] = [r for r in caplog.records if r.name == _ROUTES_EXPORT_LOGGER]
    assert import_job.job_id in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is cause


# ---------------------------------------------------------------------------
# Archive refusals carry fixed text built from our own data, never library text
# ---------------------------------------------------------------------------

_REJECTED_LINE = 2
_REJECTED_INDEX = _REJECTED_LINE - 1


async def _run_failing_import(
    tmp_path: Path,
    col: str,
    job_store: JobStore,
    search_store: SearchStore,
    pipeline: MagicMock,
    embedder_cache: MagicMock,
    config: SearchConfig,
    **archive_kwargs: object,
) -> ImportJob:
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, [_chunk(_doc_id(), i) for i in range(3)], **archive_kwargs)  # type: ignore[arg-type]
    job = _make_import_job(col, str(archive_path), job_store, on_error="fail")
    await _import_task(job, job_store, search_store, pipeline, embedder_cache, config)
    finished = job_store.get(job.job_id)
    assert finished.status == JobStatus.FAILED
    return finished


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_corrupt_vector_names_the_line_not_the_decoder_error(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_ROUTES_EXPORT_LOGGER):
        finished = await _run_failing_import(
            tmp_path, "bad-vector-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
            search_config, doc_overrides={_REJECTED_INDEX: {"vector": "!!!not-base64"}},
        )

    assert finished.error == (
        f"corrupt doc (vector) at line {_REJECTED_LINE}: missing or not base64-encoded float32"
    )
    # Logged once, by the refusal handler, with the decoder's error chained as the cause.
    [record] = [r for r in caplog.records if r.name == _ROUTES_EXPORT_LOGGER]
    assert record.exc_info is not None
    assert record.exc_info[1].__cause__ is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_malformed_doc_names_the_line_not_the_exception(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    finished = await _run_failing_import(
        tmp_path, "malformed-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_REJECTED_INDEX: {"text": _DROP}},
    )

    assert finished.error == (
        f"malformed doc at line {_REJECTED_LINE}: a required field is missing or has the wrong type"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_names_the_physical_line_past_a_blank_line(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A blank line still counts: the error names the line an operator finds in the file."""
    finished = await _run_failing_import(
        tmp_path, "blank-line-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_REJECTED_INDEX: {"text": _DROP}}, blank_line_before=_REJECTED_INDEX,
    )

    assert finished.error == (
        f"malformed doc at line {_REJECTED_LINE + 1}: a required field is missing or has the wrong type"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_truncates_archive_supplied_model_name(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A crafted manifest value cannot inflate the job ``error``."""
    crafted_model = "m" * 10_000
    finished = await _run_failing_import(
        tmp_path, "long-model-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, active_embedding_model=crafted_model,
    )

    assert finished.error == (
        f"embedding model mismatch: archive has {quote_archive_value(crafted_model)}, "
        f"server is configured with {search_config.embedding_model!r}; re-export with a matching model"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_unsafe_archive_reports_reason_code(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A zip-slip member surfaces as the PathUnsafeError reason code."""
    archive_path = tmp_path / "unsafe.tar.gz"
    _make_archive(archive_path, "unsafe-col", [_chunk(_doc_id(), 0)])
    with tarfile.open(archive_path, "r:gz") as src:
        members = [(m, src.extractfile(m).read()) for m in src.getmembers()]
    with tarfile.open(archive_path, "w:gz") as tf:
        for member, payload in members:
            tf.addfile(member, io.BytesIO(payload))
        evil = tarfile.TarInfo(name="../../etc/passwd")
        evil.size = 0
        tf.addfile(evil, io.BytesIO(b""))
    job = _make_import_job("unsafe-col", str(archive_path), tmp_job_store)

    await _import_task(job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config)

    finished = tmp_job_store.get(job.job_id)
    assert finished.status == JobStatus.FAILED
    assert finished.error == "unsafe_tar_member"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_embedder_not_ready_reports_sanitized_detail(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A still-loading embedder is retryable: FAILED with the wire-safe detail, so resume applies."""
    mock_embedder_cache.get_or_load.side_effect = EmbedderNotReadyError("internal: model x after 30s")
    import_job = _running_import_job(tmp_path, "not-ready-col", tmp_job_store)

    await _import_task(
        import_job, tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config
    )

    finished = tmp_job_store.get(import_job.job_id)
    assert finished.status == JobStatus.FAILED
    assert finished.error == EMBEDDER_NOT_READY_DETAIL
