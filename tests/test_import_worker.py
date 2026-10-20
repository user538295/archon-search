"""Integration tests for _import_task() in routes_export.py — Task 5.1."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import io
import json
import logging
import math
import struct
import tarfile
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderNotReadyError
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION, quote_archive_value
from archon_search.jobs.store import JobStore
from archon_search.server import _job_warmup
from archon_search.server.routes_export import IMPORT_FAILED_DETAIL, _export_task, _import_task
from archon_search.server.routes_jobs import _ACTIVE_STATUSES
from archon_search.store import _META_MAX_VAL_LEN, SearchStore
from archon_search.types import ImportJob, JobStatus
from tests.test_store import _EXPIRES_AT_BEFORE_UTC_YEAR_1, _FLOAT_OVERFLOW_DIGITS

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
    """Export a seeded collection, import into a new collection: DONE, every row stored as it was exported.

    The seeded rows carry metadata, expires_at and custom_score, which the archive encodes differently from the
    row (a JSON string, a timestamp string, a float32 widened to a float) and the import must store back as-is.
    """
    src_col = "roundtrip-src"
    dst_col = "roundtrip-dst"
    # Use empty embedding model to match the archive (seeded collections have no meta row)
    search_config.embedding_model = ""
    doc_id = _doc_id()
    seeded = [
        dataclasses.replace(
            _chunk(doc_id, i),
            metadata={"team": f"t{i}"},
            expires_at="2030-01-02T03:04:05.000006Z",
            custom_score=_ROUNDTRIP_CUSTOM_SCORE,
        )
        for i in range(5)
    ]
    await search_store_seed_partial(tmp_search_store, src_col, seeded)

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
    assert finished.result["imported"] == len(seeded)
    assert finished.result["skipped"] == 0
    exported_rows = await _rows_by_chunk(tmp_search_store, src_col)
    assert {row["metadata"] for row in exported_rows.values()} == {json.dumps(c.metadata) for c in seeded}, (
        "the source rows must carry the seeded metadata, or the comparison below proves nothing"
    )
    assert await _rows_by_chunk(tmp_search_store, dst_col) == exported_rows


#: A custom_score that float32 stores exactly, so the roundtrip compares it exactly.
_ROUNDTRIP_CUSTOM_SCORE = 0.75


async def _rows_by_chunk(search_store: SearchStore, collection: str) -> dict[str, dict[str, object]]:
    """Every stored row of *collection* by chunk_id, as ``list_chunks_raw`` (the export's read) returns it."""
    return {r["chunk_id"]: dict(r) async for r in search_store.list_chunks_raw(collection, DEFAULT_NAMESPACE)}


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
#: Array nesting deep enough that ``json.loads`` raises ``RecursionError``.
_JSON_NESTING_PAST_RECURSION_LIMIT = 100_000
#: A str no UTF-8 encoder accepts: JSON-legal as "\ud800", but pyarrow fails a whole batch holding it in any string column.
_LONE_SURROGATE = "a\ud800b"
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
    pytest.param("metadata", "[" * _JSON_NESTING_PAST_RECURSION_LIMIT, id="metadata-json-too-deep"),
    pytest.param("metadata", "[1]", id="metadata-json-non-object"),
    pytest.param("metadata", ["k"], id="metadata-list"),
    pytest.param("metadata", {"k": 1}, id="metadata-non-str-value"),
    pytest.param("metadata", {"k": "v" * (_META_MAX_VAL_LEN + 1)}, id="metadata-value-too-long"),
    # ingest_chunks rejects a chunk_id that is not "<64 hex>-<6 digits>" for its whole batch.
    pytest.param("chunk_id", "not-a-chunk-id", id="chunk_id-malformed"),
    pytest.param("chunk_id", 7, id="chunk_id-int"),
    # String columns: ingest_chunks fails its whole batch on a value pyarrow cannot store as a string. A dict fails
    # any batch; a non-str scalar or a list fails a batch it shares with str values (the bad doc sits between two
    # valid ones here), although pyarrow stringifies it when the whole batch holds one.
    pytest.param("doc_id", ["d"], id="doc_id-list"),
    pytest.param("doc_id", 7, id="doc_id-int"),
    pytest.param("text", 7, id="text-int"),
    pytest.param("text", 1.5, id="text-float"),
    pytest.param("text", True, id="text-bool"),
    pytest.param("source_path", 7, id="source_path-int"),
    pytest.param("indexed_at", 7, id="indexed_at-int"),
    pytest.param("updated_at", ["u"], id="updated_at-list"),
    pytest.param("file_type", 7, id="file_type-int"),
    pytest.param("language", 7, id="language-int"),
    pytest.param("text", {"k": "v"}, id="text-object"),
    pytest.param("source_path", {"k": "v"}, id="source_path-object"),
    pytest.param("indexed_at", {"k": "v"}, id="indexed_at-object"),
    pytest.param("file_type", {"k": "v"}, id="file_type-object"),
    pytest.param("language", {"k": "v"}, id="language-object"),
    pytest.param("updated_at", {"k": "v"}, id="updated_at-object"),
    pytest.param("text", _LONE_SURROGATE, id="text-lone-surrogate"),
    pytest.param("acl_source", _LONE_SURROGATE, id="acl_source-lone-surrogate"),
    pytest.param("acl", [_LONE_SURROGATE], id="acl-lone-surrogate-item"),
    pytest.param("doc_id", _LONE_SURROGATE, id="doc_id-lone-surrogate"),
    pytest.param("source_path", _LONE_SURROGATE, id="source_path-lone-surrogate"),
    pytest.param("indexed_at", _LONE_SURROGATE, id="indexed_at-lone-surrogate"),
    pytest.param("file_type", _LONE_SURROGATE, id="file_type-lone-surrogate"),
    pytest.param("language", _LONE_SURROGATE, id="language-lone-surrogate"),
    pytest.param("updated_at", _LONE_SURROGATE, id="updated_at-lone-surrogate"),
    pytest.param("acl_sidecar_path", _LONE_SURROGATE, id="acl_sidecar_path-lone-surrogate"),
    pytest.param("scopes", [_LONE_SURROGATE], id="scopes-lone-surrogate-item"),
    pytest.param("acl_warning", [_LONE_SURROGATE], id="acl_warning-lone-surrogate-item"),
    # ingest_chunks converts custom_score with float() and normalizes expires_at as an ISO-8601 timestamp.
    pytest.param("custom_score", "high", id="custom_score-non-numeric-str"),
    pytest.param("custom_score", [1.0], id="custom_score-list"),
    pytest.param("custom_score", 10**_FLOAT_OVERFLOW_DIGITS, id="custom_score-float-overflow"),
    pytest.param("expires_at", "next week", id="expires_at-not-iso"),
    pytest.param("expires_at", "", id="expires_at-empty"),
    pytest.param("expires_at", 7, id="expires_at-int"),
    pytest.param("expires_at", _EXPIRES_AT_BEFORE_UTC_YEAR_1, id="expires_at-before-utc-year-1"),
]
#: Representative shapes (a list field holding a scalar, a str field holding a list, a malformed chunk_id, values
#: ingest_chunks cannot convert): fail mode shares the skip-mode code path, which covers every case above.
_MALFORMED_FIELD_VALUES_FAIL_MODE = [
    pytest.param("acl", "default", id="acl-str"),
    pytest.param("ingested_by", ["http"], id="ingested_by-list"),
    pytest.param("chunk_id", "not-a-chunk-id", id="chunk_id-malformed"),
    pytest.param("text", 7, id="text-int"),
    pytest.param("text", _LONE_SURROGATE, id="text-lone-surrogate"),
    pytest.param("custom_score", "high", id="custom_score-non-numeric-str"),
    pytest.param("expires_at", "next week", id="expires_at-not-iso"),
]
#: Archived values ingest_chunks stores, so the import must too (field, archived value, stored column value).
_ACCEPTED_FIELD_VALUES = [
    pytest.param("metadata", {"team": "a"}, '{"team": "a"}', id="metadata-object"),
    pytest.param("metadata", '{"team": "a"}', '{"team": "a"}', id="metadata-json-string"),
    pytest.param("expires_at", "2030-01-02T03:04:05Z", "2030-01-02T03:04:05.000000Z", id="expires_at-iso"),
    pytest.param("custom_score", 0.25, 0.25, id="custom_score-float"),
    pytest.param("custom_score", "0.25", 0.25, id="custom_score-numeric-str"),
    # A null string field was stored as null before the importer checked types, and still is.
    pytest.param("text", None, None, id="text-null"),
    # json.dumps escapes a lone surrogate, so unlike a string column the metadata column stores it.
    pytest.param("metadata", {"k": _LONE_SURROGATE}, '{"k": "a\\ud800b"}', id="metadata-lone-surrogate-value"),
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
    """A wrongly typed or malformed field is rejected per doc, so on_error='skip' drops that doc only."""
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
@pytest.mark.parametrize(("field", "archived", "stored"), _ACCEPTED_FIELD_VALUES)
async def test_import_stores_valid_optional_field_values(
    field: str,
    archived: object,
    stored: object,
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A non-empty metadata, a non-null expires_at or custom_score imports and is stored as ingest_chunks stores it."""
    col = "import-accepted-field"
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, [_chunk(_doc_id(), 0)], doc_overrides={0: {field: archived}})

    finished = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store))

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    table = await import_runner.search_store._require_connected().open_table(col)
    [row] = await table.query().to_list()
    assert row[field] == stored


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
        # Normalized away before pyarrow sees it, so unlike the other string fields it is not refused.
        pytest.param(_LONE_SURROGATE, "cli", id="unknown-lone-surrogate"),
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
async def test_import_reads_empty_string_metadata_as_no_metadata(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """An archived metadata of "" (not JSON) imports as empty metadata instead of a malformed doc."""
    col = "import-empty-metadata"
    archive_path = tmp_path / "import-empty-metadata.tar.gz"
    _make_archive(archive_path, col, [_chunk(_doc_id(), 0)], doc_overrides={0: {"metadata": ""}})

    finished = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store))

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    [row] = [r async for r in import_runner.search_store.list_chunks_raw(col, DEFAULT_NAMESPACE)]
    assert row["metadata"] == "{}"


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
    # A refused import must not claim the foreign collection, or a resume would write into it.
    assert isinstance(finished, ImportJob)
    assert finished.collection_claimed is False


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


class _SimulatedCrash(Exception):
    """Raised by ``_CrashOnCall``: an unexpected failure the import worker cannot tell from a real one."""


class _CrashOnCall:
    """Wraps a ``SearchStore`` method and raises on its *crash_at*-th call (1-based), as a crash would.

    With *after_commit* the wrapped call completes first, so its write is stored when the crash hits —
    the window between an ingest commit and the checkpoint that records it.
    """

    def __init__(
        self, method: Callable[..., Awaitable[object]], crash_at: int, *, after_commit: bool = False,
    ) -> None:
        self._method = method
        self._crash_at = crash_at
        self._after_commit = after_commit
        self._calls = 0

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self._calls += 1
        if self._calls == self._crash_at and not self._after_commit:
            raise _SimulatedCrash("simulated crash mid-import")
        result = await self._method(*args, **kwargs)
        if self._calls == self._crash_at:
            raise _SimulatedCrash("simulated crash mid-import")
        return result


#: The archive and batch sizes of ``_ImportRunner.crash_after_first_checkpoint``.
_CRASH_ARCHIVE_DOCS = 6
_CRASH_CHECKPOINT_INTERVAL = 2
#: Ingest calls ``crash_after_first_checkpoint``'s run 1 completes and checkpoints before the crashing one.
_CRASH_BATCHES_CHECKPOINTED = 1
#: Docs (one archive line each) run 1 stores and checkpoints at the default interval.
_CRASH_CHECKPOINTED_DOCS = _CRASH_CHECKPOINT_INTERVAL * _CRASH_BATCHES_CHECKPOINTED
#: Rows run 1 leaves stored when its crashing batch is committed first (``after_commit``).
_CRASH_COMMITTED_DOCS = _CRASH_CHECKPOINTED_DOCS + _CRASH_CHECKPOINT_INTERVAL


@dataclasses.dataclass
class _ImportRunner:
    """Runs ``_import_task`` against the test stores; *store_overrides* replace store methods for that run only."""

    job_store: JobStore
    search_store: SearchStore
    pipeline: MagicMock
    embedder_cache: MagicMock
    config: SearchConfig
    monkeypatch: pytest.MonkeyPatch

    async def run(self, job: ImportJob, **store_overrides: object) -> ImportJob:
        with self.monkeypatch.context() as patch:
            for name, replacement in store_overrides.items():
                patch.setattr(self.search_store, name, replacement)
            await _import_task(job, self.job_store, self.search_store, self.pipeline, self.embedder_cache, self.config)
        finished = self.job_store.get(job.job_id)
        assert isinstance(finished, ImportJob)
        return finished

    async def resume(self, job: ImportJob, **store_overrides: object) -> ImportJob:
        """Re-run a FAILED job the way ``POST /jobs/{id}/resume`` re-enqueues it."""
        requeued = self.job_store.transition(job.job_id, {JobStatus.FAILED}, JobStatus.QUEUED)
        assert requeued is not None, "only a FAILED import can be resumed"
        return await self.run(job, **store_overrides)

    async def all_stored_chunk_ids(self, collection: str) -> list[str]:
        """Every stored chunk_id, sorted, duplicates kept — a re-appended chunk shows up twice."""
        return sorted([r["chunk_id"] async for r in self.search_store.list_chunks_raw(collection, DEFAULT_NAMESPACE)])

    async def crash_after_first_checkpoint(
        self,
        tmp_path: Path,
        collection: str,
        *,
        n_docs: int = _CRASH_ARCHIVE_DOCS,
        checkpoint_interval: int = _CRASH_CHECKPOINT_INTERVAL,
        after_commit: bool = False,
        force_overwrite: bool = False,
        on_error: str = "fail",
        namespace: str = DEFAULT_NAMESPACE,
    ) -> tuple[ImportJob, list[ChunkRecord], Path]:
        """Run 1 of an *n_docs* archive: its first batch is stored and checkpointed, the next ingest call crashes.

        Sets the runner's ``checkpoint_interval`` to *checkpoint_interval* for this run and every later one, so
        the resumes under test batch the archive like run 1 did. With *after_commit* the crashing batch is
        committed before the crash, ahead of its checkpoint. Returns the job, the archived chunks (one doc
        each) and the archive path.
        """
        self.config.jobs.checkpoint_interval = checkpoint_interval
        chunks = [_chunk(_doc_id(), i) for i in range(n_docs)]
        archive_path = tmp_path / f"{collection}.tar.gz"
        _make_archive(archive_path, collection, chunks)
        job = _make_import_job(
            collection, str(archive_path), self.job_store,
            force_overwrite=force_overwrite, on_error=on_error, namespace=namespace,
        )
        crash = _CrashOnCall(
            self.search_store.ingest_chunks, crash_at=_CRASH_BATCHES_CHECKPOINTED + 1, after_commit=after_commit,
        )
        crashed = await self.run(job, ingest_chunks=crash)
        checkpointed = checkpoint_interval * _CRASH_BATCHES_CHECKPOINTED
        assert (crashed.status, crashed.progress["processed"], crashed.lines_consumed) == (
            JobStatus.FAILED, checkpointed, checkpointed,
        ), f"run 1 must fail after its first checkpoint: {crashed.status}, {crashed.progress}"
        return job, chunks, archive_path

    async def assert_imported_each_chunk_once(
        self, finished: ImportJob, collection: str, chunks: list[ChunkRecord], message: str = "each chunk stored once",
    ) -> None:
        """*finished* is DONE, stored each of *chunks* exactly once, and counts them all as imported, none skipped."""
        assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
        assert await self.all_stored_chunk_ids(collection) == sorted(c.chunk_id for c in chunks), message
        assert (finished.result["imported"], finished.result["skipped"]) == (len(chunks), 0), (
            f"unexpected counts: {finished.result}"
        )


@pytest.fixture
def import_runner(
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> _ImportRunner:
    return _ImportRunner(
        tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache, search_config, monkeypatch
    )


def _lost_ownership_error(collection: str) -> str:
    """The refusal of a resume that cannot verify it wrote the existing *collection*."""
    return (
        f"collection {collection!r} already exists and was not written by this import; "
        "start a new import with force_overwrite=true"
    )


def _stored_import_job(job_store: JobStore, job: ImportJob) -> ImportJob:
    stored = job_store.get(job.job_id)
    assert isinstance(stored, ImportJob)
    return stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_resume_from_checkpoint(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    import_runner: _ImportRunner,
) -> None:
    """A resume from a checkpoint without ``lines_consumed`` continues after its ``processed`` docs.

    The checkpoint predates ``lines_consumed``, so ``processed`` is the skip position. Dedup alone would hide a
    wrong skip position from the ingest count, so the result's imported/skipped counts pin it too.
    """
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

    # Seed the partial collection state: pre-populate with the first skip_count docs
    # to simulate what was ingested before the crash.
    await search_store_seed_partial(tmp_search_store, col, chunks[:skip_count])

    # Track how many docs are ingested in the second run
    ingest_call_total = [0]
    original_ingest = tmp_search_store.ingest_chunks

    async def patched_ingest(collection, chunks_batch, **kwargs):  # type: ignore[no-untyped-def]
        ingest_call_total[0] += len(chunks_batch)
        return await original_ingest(collection, chunks_batch, **kwargs)

    final_job = await import_runner.resume(import_job, ingest_chunks=patched_ingest)

    assert final_job.status == JobStatus.DONE, f"Expected DONE, got {final_job.status}: {final_job.error}"
    # Only the remaining n - skip_count docs should be ingested in the second run
    expected_ingested = n - skip_count
    assert ingest_call_total[0] == expected_ingested, (
        f"Expected {expected_ingested} docs in second run, got {ingest_call_total[0]}"
    )
    assert (final_job.result["imported"], final_job.result["skipped"]) == (n, 0), (
        f"the resume must count the checkpointed docs as imported and skip nothing: {final_job.result}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_after_skipped_lines_does_not_duplicate_chunks(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    import_runner: _ImportRunner,
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
    crashed = await import_runner.run(import_job, ingest_chunks=_CrashOnCall(original_ingest, crash_at=2))
    assert (crashed.status, crashed.progress["processed"]) == (JobStatus.FAILED, 2)

    finished = await import_runner.resume(import_job)

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert await import_runner.all_stored_chunk_ids(col) == expected_chunk_ids
    # The two lines skipped before the checkpoint still count, although the resumed run never re-reads them.
    assert (finished.result["imported"], finished.result["skipped"]) == (5, 2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_after_indexing_failure_does_not_duplicate_chunks(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A resume after an indexing-phase failure skips every archive line the failed run consumed.

    The corrupt line 5 is skipped after the last checkpoint and the final, smaller batch is flushed without
    one, so only the ``indexing`` write can record those lines. Without it the resume continues from the
    ``processed`` count: it re-reads the flushed doc — the overlap check keeps its row single, but it is
    counted as imported a second time — and loses the skipped line from the ``skipped`` count.
    """
    col = "resume-after-indexing"
    import_runner.config.jobs.checkpoint_interval = _CRASH_CHECKPOINT_INTERVAL
    chunks = [_chunk(_doc_id(), i) for i in range(_CRASH_ARCHIVE_DOCS)]
    corrupt_index = 2 * _CRASH_CHECKPOINT_INTERVAL  # the first line after the second (last) checkpoint
    archive_path = tmp_path / "resume-after-indexing.tar.gz"
    _make_archive(archive_path, col, chunks, corrupt_index=corrupt_index)
    expected_chunk_ids = sorted(c.chunk_id for i, c in enumerate(chunks) if i != corrupt_index)
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store, on_error="skip")

    # Run 1: every doc is stored, then the collection embedder is still loading — the resumable failure.
    import_runner.embedder_cache.get_or_load.side_effect = [EmbedderNotReadyError("loading"), MagicMock()]
    failed = await import_runner.run(import_job)
    assert (failed.status, failed.error) == (JobStatus.FAILED, EMBEDDER_NOT_READY_DETAIL)
    assert failed.lines_consumed == len(chunks), "the indexing write must record every consumed archive line"

    finished = await import_runner.resume(import_job)

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert await import_runner.all_stored_chunk_ids(col) == expected_chunk_ids, "the flushed doc must be stored once"
    assert (finished.result["imported"], finished.result["skipped"]) == (len(expected_chunk_ids), 1), (
        f"the corrupt line skipped after the last checkpoint must still count: {finished.result}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_after_crash_before_first_checkpoint_completes(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Rows committed before the first checkpoint still mark the collection as this job's own.

    The archive is smaller than ``checkpoint_interval``, so its only batch is flushed without a checkpoint;
    the crash lands right after that commit, leaving ``processed == 0`` beside a non-empty collection.
    The resume must continue into that collection, not refuse it as "already exists".
    """
    col = "resume-before-first-checkpoint"
    one_doc_short_of_a_checkpoint = import_runner.config.jobs.checkpoint_interval - 1
    chunks = [_chunk(_doc_id(), i) for i in range(one_doc_short_of_a_checkpoint)]
    archive_path = tmp_path / "resume-before-first-checkpoint.tar.gz"
    _make_archive(archive_path, col, chunks)
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store)

    crash = _CrashOnCall(import_runner.search_store.ingest_chunks, crash_at=1, after_commit=True)
    crashed = await import_runner.run(import_job, ingest_chunks=crash)
    assert (crashed.status, crashed.progress["processed"]) == (JobStatus.FAILED, 0)
    assert await import_runner.search_store.count_chunks(col, DEFAULT_NAMESPACE) == len(chunks)

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_skips_batch_committed_before_its_checkpoint(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A batch stored just before the crash, but not yet checkpointed, is not appended a second time."""
    col = "resume-uncheckpointed-batch"
    # Batch 2 is committed, then the crash hits before its checkpoint: more rows stored than checkpointed.
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col, after_commit=True)
    assert await import_runner.search_store.count_chunks(col, DEFAULT_NAMESPACE) == _CRASH_COMMITTED_DOCS

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_restarts_when_partial_collection_is_gone(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A resume whose partial collection was deleted imports the whole archive, not only the tail."""
    col = "resume-collection-gone"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    await import_runner.search_store.drop_collection(col)
    assert await import_runner.search_store.count_chunks(col, DEFAULT_NAMESPACE) == 0

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@contextlib.contextmanager
def _embedding_model_changed(config: SearchConfig) -> Iterator[None]:
    """The server's embedding model no longer matches the archive's (rejected after the first validating write)."""
    original = config.embedding_model
    config.embedding_model = "some/other-model"
    try:
        yield
    finally:
        config.embedding_model = original


@contextlib.contextmanager
def _archive_schema_changed(archive_path: Path, collection: str, chunks: list[ChunkRecord]) -> Iterator[None]:
    """The archive at the job's path has an unsupported schema_version (rejected before its doc_count is read)."""
    original = archive_path.read_bytes()
    _make_archive(archive_path, collection, chunks, schema_version=EXPORT_SCHEMA_VERSION + 1)
    try:
        yield
    finally:
        archive_path.write_bytes(original)


#: Builds a rejection context manager from (config, archive_path, collection, chunks).
_Rejection = Callable[[SearchConfig, Path, str, list[ChunkRecord]], contextlib.AbstractContextManager[None]]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rejection", "expected_error"),
    [
        pytest.param(
            lambda config, archive, col, chunks: _embedding_model_changed(config),
            "embedding model mismatch",
            id="embedding-model",
        ),
        pytest.param(
            lambda config, archive, col, chunks: _archive_schema_changed(archive, col, chunks),
            "schema_version mismatch",
            id="schema-version",
        ),
    ],
)
async def test_import_resume_rejected_by_validation_keeps_checkpoint_for_next_resume(
    tmp_path: Path,
    import_runner: _ImportRunner,
    rejection: _Rejection,
    expected_error: str,
) -> None:
    """A resume that fails archive validation must not wipe the checkpoint the next resume continues from.

    Run 1 crashes after one checkpoint, leaving a partial collection. Resume 1 is rejected by validation
    (the server config or the archive changed). Once that is fixed, resume 2 must continue from the
    checkpoint instead of hitting the "collection already exists" guard.
    """
    col = "resume-after-rejected-resume"
    import_job, chunks, archive_path = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    stored_after_run1 = await import_runner.search_store.count_chunks(col, DEFAULT_NAMESPACE)
    assert stored_after_run1 == _CRASH_CHECKPOINTED_DOCS, "run 1 stores only its first batch"

    with rejection(import_runner.config, archive_path, col, chunks):
        rejected = await import_runner.resume(import_job)
    assert rejected.status == JobStatus.FAILED, f"resume 1 must be rejected, got {rejected.status}"
    assert expected_error in rejected.error
    # The rejected resume ingested nothing, so the checkpoint it inherited must still be there.
    assert rejected.progress == {"processed": _CRASH_CHECKPOINTED_DOCS, "total": len(chunks), "phase": "validating"}, (
        f"the rejected resume must keep the checkpoint's processed/total: {rejected.progress}"
    )
    assert rejected.lines_consumed == _CRASH_CHECKPOINTED_DOCS, "the rejected resume must keep the checkpoint's skip position"

    finished = await import_runner.resume(import_job)
    await import_runner.assert_imported_each_chunk_once(
        finished, col, chunks, "resume 2 must store each archived chunk exactly once",
    )


#: Rows of the collection created under a rejected import's name: differs from the count that import's archive holds.
_SAME_NAME_COLLECTION_DOCS = 5
#: Docs in the rejected import's archive.
_NEVER_CLAIMED_ARCHIVE_DOCS = 3


async def _reject_at_validation_then_create_same_name(
    runner: _ImportRunner, tmp_path: Path, collection: str, *, force_overwrite: bool,
) -> tuple[ImportJob, list[ChunkRecord]]:
    """An import rejected at ``validating`` (it never claimed *collection*, no checkpoint); then another collection
    of the same name is created. Returns the rejected job and its archived chunks."""
    chunks = [_chunk(_doc_id(), i) for i in range(_NEVER_CLAIMED_ARCHIVE_DOCS)]
    archive_path = tmp_path / f"{collection}.tar.gz"
    _make_archive(archive_path, collection, chunks)
    job = _make_import_job(collection, str(archive_path), runner.job_store, force_overwrite=force_overwrite)
    with _embedding_model_changed(runner.config):
        rejected = await runner.run(job)
    assert (rejected.status, rejected.collection_claimed, rejected.progress["processed"]) == (
        JobStatus.FAILED, False, 0,
    ), f"the run must be rejected unclaimed, without a checkpoint: {rejected.status}, {rejected.progress}"
    assert rejected.progress["phase"] == "validating", "the rejected run must leave its validating progress behind"
    await _seed_collection(runner.search_store, collection, _SAME_NAME_COLLECTION_DOCS)
    return job, chunks


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_never_claimed_force_overwrite_resume_overwrites_same_name_collection(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A resume of a force_overwrite job that never claimed the collection runs as a fresh import: it drops the
    collection created since and imports the whole archive from line 0."""
    col = "never-claimed-force"
    import_job, chunks = await _reject_at_validation_then_create_same_name(
        import_runner, tmp_path, col, force_overwrite=True,
    )

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(
        finished, col, chunks, "the existing collection must be replaced by the whole archive",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_never_claimed_resume_refuses_same_name_collection_as_fresh_import(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Without force_overwrite such a resume is refused with the fresh import's "already exists" error, not the
    lost-ownership one: it never wrote anything it could have lost."""
    col = "never-claimed-no-force"
    import_job, _ = await _reject_at_validation_then_create_same_name(
        import_runner, tmp_path, col, force_overwrite=False,
    )

    refused = await import_runner.resume(import_job)

    assert (refused.status, refused.error) == (
        JobStatus.FAILED, f"collection {col!r} already exists; use force_overwrite=true to overwrite",
    )
    assert await import_runner.search_store.count_chunks(col, DEFAULT_NAMESPACE) == _SAME_NAME_COLLECTION_DOCS, (
        "the refused resume must leave the existing collection untouched"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_method",
    [
        # Fails in the ownership check (is the archive's first doc stored, as archived?), before the collection claim.
        pytest.param("get_chunks_by_ids", id="ownership-check-fails"),
        # Fails creating the table, after the collection claim, before the first new checkpoint.
        pytest.param("ensure_collection", id="table-creation-fails"),
    ],
)
async def test_import_resume_failing_before_new_checkpoint_keeps_checkpoint(
    tmp_path: Path,
    import_runner: _ImportRunner,
    failing_method: str,
) -> None:
    """A resume that fails after validation but before its first new checkpoint leaves the old one intact."""
    col = f"resume-fails-{failing_method.replace('_', '-')}"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)

    failing = _CrashOnCall(getattr(import_runner.search_store, failing_method), crash_at=1)
    failed = await import_runner.resume(import_job, **{failing_method: failing})
    assert (failed.status, failed.error) == (JobStatus.FAILED, IMPORT_FAILED_DETAIL)
    assert (failed.progress["processed"], failed.progress["total"], failed.lines_consumed) == (
        _CRASH_CHECKPOINTED_DOCS, len(chunks), _CRASH_CHECKPOINTED_DOCS,
    ), (
        f"the failed resume must keep the checkpoint: {failed.progress}, lines_consumed={failed.lines_consumed}"
    )

    finished = await import_runner.resume(import_job)
    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_looks_up_stored_chunks_only_until_first_clean_batch(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A resume stops checking batches for already-stored chunks once a batch comes back with none.

    Commits follow archive order, so only the batches up to the first clean one can overlap rows a failed
    run stored ahead of its checkpoint; each lookup is a full scan (``chunk_id`` has no index).
    """
    col = "resume-dedup-until-clean"
    # Batch 2 is committed, then the crash hits before its checkpoint. After the overlapping batch come the first
    # clean batch and two more, which skip the lookup.
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(
        tmp_path, col, n_docs=_CRASH_COMMITTED_DOCS + 3 * _CRASH_CHECKPOINT_INTERVAL, after_commit=True,
    )
    overlapping_batches = (_CRASH_COMMITTED_DOCS - _CRASH_CHECKPOINTED_DOCS) // _CRASH_CHECKPOINT_INTERVAL

    lookup = AsyncMock(side_effect=import_runner.search_store.stored_chunk_ids)
    finished = await import_runner.resume(import_job, stored_chunk_ids=lookup)

    assert lookup.await_count == overlapping_batches + 1, (
        f"expected lookups for the overlapping and the first clean batch only: {lookup.await_count}"
    )
    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_continues_after_maintenance_deleted_stored_rows(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A resume continues into its partial collection after rows it stored were deleted, e.g. by maintenance.

    Orphan cleanup and TTL pruning can delete rows from a partial import, so fewer stored rows than the
    checkpoint does not mean the collection changed hands: the archive's first doc is still stored. Restarting
    into the exists guard instead would leave a job without force_overwrite stuck on "already exists".
    """
    col = "resume-after-maintenance-delete"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    search_store = import_runner.search_store
    deleted = chunks[1]  # stored by run 1, but not its first doc: the ownership marker survives
    await search_store.delete_document(col, deleted.doc_id)
    assert await search_store.count_chunks(col, DEFAULT_NAMESPACE) == _CRASH_CHECKPOINTED_DOCS - 1, (
        "one of the stored chunks must be gone"
    )

    finished = await import_runner.resume(import_job)

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert await import_runner.all_stored_chunk_ids(col) == sorted(c.chunk_id for c in chunks if c != deleted), (
        "the resume must continue after its checkpoint, neither restoring the deleted row nor storing any chunk twice"
    )
    assert (finished.result["imported"], finished.result["skipped"]) == (len(chunks), 0), (
        f"unexpected counts: {finished.result}"
    )


async def _stored_rows(search_store: SearchStore, collection: str) -> list[tuple[str, str]]:
    """Every stored row's (chunk_id, indexed_at), sorted, duplicates kept: a row's identity as an import stores it."""
    return sorted([(r["chunk_id"], r["indexed_at"]) async for r in search_store.list_chunks_raw(collection, DEFAULT_NAMESPACE)])


async def _import_newer_archive_sharing_first_doc(
    runner: _ImportRunner, tmp_path: Path, collection: str, chunks: list[ChunkRecord], *, namespace: str,
) -> tuple[ImportJob, list[ChunkRecord]]:
    """Import, as another job, a newer archive of *collection*: the same first doc as *chunks*, every later doc new.

    Like a later backup of the same collection, it overwrites the collection and stores the first doc exactly as
    *chunks* archived it — the same ``chunk_id`` and ``indexed_at``. Returns the finished job and its chunks.
    """
    newer_chunks = [chunks[0], *(_chunk(_doc_id(), i) for i in range(1, len(chunks)))]
    archive_path = tmp_path / f"{collection}-newer-{namespace}.tar.gz"
    _make_archive(archive_path, collection, newer_chunks)
    job = _make_import_job(collection, str(archive_path), runner.job_store, force_overwrite=True, namespace=namespace)
    finished = await runner.run(job)
    assert finished.status == JobStatus.DONE, f"the newer import must succeed: {finished.status}: {finished.error}"
    return finished, newer_chunks


#: Changes a crashed import's collection (runner, tmp_path, collection, archived chunks, archive path) so that the
#: crashed job can no longer prove it wrote it.
_Tamper = Callable[[_ImportRunner, Path, str, list[ChunkRecord], Path], Awaitable[None]]


async def _recreate_foreign_collection(
    runner: _ImportRunner, tmp_path: Path, col: str, chunks: list[ChunkRecord], archive_path: Path,
) -> None:
    """Someone deletes the collection and creates another under its name, with more rows than the checkpoint."""
    await runner.search_store.drop_collection(col)
    await _seed_collection(runner.search_store, col, _CRASH_COMMITTED_DOCS)


async def _reingest_same_chunk_ids(
    runner: _ImportRunner, tmp_path: Path, col: str, chunks: list[ChunkRecord], archive_path: Path,
) -> None:
    """The collection is recreated from the archived files: the same chunk_ids, another indexed_at.

    chunk_ids derive from the source path and chunk index, so only the archived indexed_at marks a row as the one
    this job imported.
    """
    await runner.search_store.drop_collection(col)
    reingested = [dataclasses.replace(c, indexed_at=f"{c.indexed_at}-reingested") for c in chunks[:_CRASH_COMMITTED_DOCS]]
    await search_store_seed_partial(runner.search_store, col, reingested)


async def _delete_first_document(
    runner: _ImportRunner, tmp_path: Path, col: str, chunks: list[ChunkRecord], archive_path: Path,
) -> None:
    """The archive's first doc is deleted from the collection (TTL prune, orphan cleanup): an accepted limitation."""
    await runner.search_store.delete_document(col, chunks[0].doc_id)


async def _corrupt_every_archived_vector(
    runner: _ImportRunner, tmp_path: Path, col: str, chunks: list[ChunkRecord], archive_path: Path,
) -> None:
    """The archive no longer has a valid doc (skip mode passes over each), so nothing marks the collection."""
    _make_archive(archive_path, col, chunks, doc_overrides={i: {"vector": "!!!not-base64"} for i in range(len(chunks))})


async def _keep_only_meta_row(
    runner: _ImportRunner, tmp_path: Path, col: str, chunks: list[ChunkRecord], archive_path: Path,
) -> None:
    """The chunks are gone but the meta row survived: the delete route drops the table first, then the meta row."""
    await runner.search_store.update_collection_meta(CollectionMeta(name=col))
    await runner.search_store.drop_collection(col)


def _later_import(namespace: str) -> _Tamper:
    """Another job imports a newer archive sharing the first doc, from *namespace*: its claim revokes the crashed job's."""

    async def tamper(runner: _ImportRunner, tmp_path: Path, col: str, chunks: list[ChunkRecord], archive_path: Path) -> None:
        await _import_newer_archive_sharing_first_doc(runner, tmp_path, col, chunks, namespace=namespace)

    return tamper


_LOST_OWNERSHIP_TAMPERS = [
    pytest.param(_recreate_foreign_collection, "fail", id="foreign-collection-recreated"),
    pytest.param(_reingest_same_chunk_ids, "fail", id="reingested-same-chunk-ids"),
    pytest.param(_delete_first_document, "fail", id="first-doc-deleted"),
    pytest.param(_corrupt_every_archived_vector, "skip", id="archive-without-valid-doc"),
    pytest.param(_keep_only_meta_row, "fail", id="meta-row-only"),
    # The newer archive shares the first doc, so only the revoked claim tells the collection is no longer the job's.
    pytest.param(_later_import(DEFAULT_NAMESPACE), "fail", id="later-import-same-namespace"),
    # Chunk tables are shared across namespaces, so another namespace's claim revokes the job's too.
    pytest.param(_later_import("other-ns"), "fail", id="later-import-other-namespace"),
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("force_overwrite", [False, True], ids=["no-force", "force-overwrite"])
@pytest.mark.parametrize(("tamper", "on_error"), _LOST_OWNERSHIP_TAMPERS)
async def test_import_resume_that_lost_ownership_refuses_existing_collection(
    tmp_path: Path,
    import_runner: _ImportRunner,
    tamper: _Tamper,
    on_error: str,
    force_overwrite: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume that cannot prove it wrote the existing collection refuses it, leaving it and the checkpoint as they are.

    Even under force_overwrite: the collection now holds someone else's data, which the user asked to overwrite
    with an import that started before it existed. Only a new import may drop it.
    """
    col = "resume-lost-ownership"
    import_job, chunks, archive_path = await import_runner.crash_after_first_checkpoint(
        tmp_path, col, force_overwrite=force_overwrite, on_error=on_error,
    )
    await tamper(import_runner, tmp_path, col, chunks, archive_path)
    rows_before = await _stored_rows(import_runner.search_store, col)
    drop = AsyncMock(side_effect=import_runner.search_store.drop_collection)

    with caplog.at_level(logging.WARNING, logger=_ROUTES_EXPORT_LOGGER):
        refused = await import_runner.resume(import_job, drop_collection=drop)

    assert (refused.status, refused.error) == (JobStatus.FAILED, _lost_ownership_error(col))
    drop.assert_not_awaited()
    assert await _stored_rows(import_runner.search_store, col) == rows_before, "the refused resume must write nothing"
    assert (refused.progress["processed"], refused.lines_consumed) == (_CRASH_CHECKPOINTED_DOCS, _CRASH_CHECKPOINTED_DOCS), (
        f"the refusal must keep the checkpoint: {refused.progress}, lines_consumed={refused.lines_consumed}"
    )
    lost_ownership_warnings = [
        r.getMessage() for r in caplog.records
        if r.name == _ROUTES_EXPORT_LOGGER and r.levelno == logging.WARNING and "cannot verify it wrote" in r.getMessage()
    ]
    assert len(lost_ownership_warnings) == 1, f"the lost ownership must be logged once: {lost_ownership_warnings}"
    assert import_job.job_id in lost_ownership_warnings[0] and repr(col) in lost_ownership_warnings[0]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("force_overwrite", [False, True], ids=["no-force", "force-overwrite"])
async def test_import_resume_revoked_by_later_import_restarts_once_collection_is_gone(
    tmp_path: Path,
    import_runner: _ImportRunner,
    force_overwrite: bool,
) -> None:
    """A revoked resume whose collection no longer exists imports the whole archive from scratch, claiming it anew.

    The resumed job's own claim clears its revoked flag (so its next resume may continue) and revokes the later
    import's.
    """
    col = "resume-revoked-collection-gone"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col, force_overwrite=force_overwrite)
    newer_job, _ = await _import_newer_archive_sharing_first_doc(import_runner, tmp_path, col, chunks, namespace=DEFAULT_NAMESPACE)
    assert _stored_import_job(import_runner.job_store, import_job).collection_claim_revoked, "the later claim must revoke"
    await import_runner.search_store.drop_collection(col)

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks, "the restart must import the whole archive")
    assert finished.lines_consumed == len(chunks), "the restart must count archive lines from line 0"
    assert (finished.collection_claimed, finished.collection_claim_revoked) == (True, False), (
        "the restart's own claim must clear its revoked flag"
    )
    assert _stored_import_job(import_runner.job_store, newer_job).collection_claim_revoked, (
        "the restart's claim must revoke the later import's"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_legacy_failed_resume_revoked_by_later_force_overwrite_import_is_refused(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A FAILED job persisted before the claim existed (unclaimed, processed > 0) is revoked like any other.

    The later force_overwrite import shares the job's first doc, so without the revocation the legacy resume would
    append its old docs into the newer collection; the job's own force_overwrite must not drop it either.
    """
    col = "resume-legacy-revoked"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col, force_overwrite=True)
    import_runner.job_store.update(import_job.job_id, collection_claimed=False)  # as the released version left it
    _, newer_chunks = await _import_newer_archive_sharing_first_doc(
        import_runner, tmp_path, col, chunks, namespace=DEFAULT_NAMESPACE,
    )
    assert _stored_import_job(import_runner.job_store, import_job).collection_claim_revoked, "the later claim must revoke"
    rows_before = await _stored_rows(import_runner.search_store, col)

    refused = await import_runner.resume(import_job)

    assert (refused.status, refused.error) == (JobStatus.FAILED, _lost_ownership_error(col))
    assert await _stored_rows(import_runner.search_store, col) == rows_before, "the newer collection must stay as it is"
    assert sorted(chunk_id for chunk_id, _ in rows_before) == sorted(c.chunk_id for c in newer_chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_not_revoked_by_later_import_of_another_collection(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Another collection's import claims only that collection: the crashed job keeps its claim and continues."""
    col = "resume-other-collection-claimed"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col, after_commit=True)
    other_col = "other-collection"
    other_archive = tmp_path / f"{other_col}.tar.gz"
    _make_archive(other_archive, other_col, [_chunk(_doc_id(), 0)])
    other = await import_runner.run(_make_import_job(other_col, str(other_archive), import_runner.job_store))
    assert other.status == JobStatus.DONE, f"the other import must succeed: {other.status}: {other.error}"
    assert not _stored_import_job(import_runner.job_store, import_job).collection_claim_revoked

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_continues_past_an_earlier_claim_by_another_import(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """An import of the collection that claimed it before the crashed job did is revoked by it, not the other way round."""
    col = "resume-earlier-claim"
    older_archive = tmp_path / f"{col}-older.tar.gz"
    _make_archive(older_archive, col, [_chunk(_doc_id(), 0)])
    older = await import_runner.run(_make_import_job(col, str(older_archive), import_runner.job_store))
    assert older.status == JobStatus.DONE, f"the older import must succeed: {older.status}: {older.error}"
    await import_runner.search_store.drop_collection(col)
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    assert _stored_import_job(import_runner.job_store, older).collection_claim_revoked, "the crashed job's claim must revoke"

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_not_revoked_by_fresh_import_refused_as_already_exists(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A fresh import refused "already exists" claims nothing, so the crashed job keeps its claim and continues."""
    col = "resume-refused-fresh-import"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col, after_commit=True)
    other_archive = tmp_path / f"{col}-other.tar.gz"
    _make_archive(other_archive, col, [_chunk(_doc_id(), 0)])
    refused = await import_runner.run(_make_import_job(col, str(other_archive), import_runner.job_store))
    assert (refused.status, refused.error) == (
        JobStatus.FAILED, f"collection {col!r} already exists; use force_overwrite=true to overwrite",
    )
    assert not refused.collection_claimed, "a refused fresh import must claim nothing"
    assert not _stored_import_job(import_runner.job_store, import_job).collection_claim_revoked

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_claim_revokes_other_imports_before_its_own_claim_write(
    tmp_path: Path,
    import_runner: _ImportRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash on a run's own claim write leaves the other imports revoked.

    The other way round, a crash in between would leave two unrevoked claims on one collection.
    """
    col = "claim-order"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    newer_archive = tmp_path / f"{col}-newer.tar.gz"
    _make_archive(newer_archive, col, [_chunk(_doc_id(), 0)])
    newer_job = _make_import_job(col, str(newer_archive), import_runner.job_store, force_overwrite=True)
    original_update_progress = import_runner.job_store.update_progress

    def crash_on_own_claim(job_id: str, *args: object, **fields: object) -> None:
        if job_id == newer_job.job_id and fields.get("collection_claimed"):
            raise _SimulatedCrash("simulated crash on the claim write")
        original_update_progress(job_id, *args, **fields)

    monkeypatch.setattr(import_runner.job_store, "update_progress", crash_on_own_claim)
    crashed = await import_runner.run(newer_job)

    assert (crashed.status, crashed.error) == (JobStatus.FAILED, IMPORT_FAILED_DETAIL)
    assert _stored_import_job(import_runner.job_store, import_job).collection_claim_revoked, (
        "the other import must be revoked before the run's own claim write"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_claim_tolerates_other_import_evicted_during_revocation(
    tmp_path: Path,
    import_runner: _ImportRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another import's job evicted between listing and revoking it is passed over: the run claims and completes.

    The revocation goes on past it, so a remaining import of the collection is still revoked.
    """
    col = "claim-evicted-other"
    import_job, _, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    newer_archive = tmp_path / f"{col}-newer.tar.gz"
    newer_chunks = [_chunk(_doc_id(), 0)]
    _make_archive(newer_archive, col, newer_chunks)
    remaining = _make_import_job(col, str(newer_archive), import_runner.job_store)
    newer_job = _make_import_job(col, str(newer_archive), import_runner.job_store, force_overwrite=True)
    original_update = import_runner.job_store.update

    def evicted_on_revoke(job_id: str, **fields: object) -> object:
        if job_id == import_job.job_id and fields.get("collection_claim_revoked"):
            raise KeyError(job_id)
        return original_update(job_id, **fields)

    monkeypatch.setattr(import_runner.job_store, "update", evicted_on_revoke)
    finished = await import_runner.run(newer_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, newer_chunks)
    assert _stored_import_job(import_runner.job_store, remaining).collection_claim_revoked, (
        "an import not evicted must still be revoked"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_claim_passes_over_other_job_types_of_the_collection(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """An export job of the same collection holds no claim: the revocation leaves it as it is and the run completes."""
    col = "claim-export-job"
    export_job = import_runner.job_store.create_export(
        collection=col, output_path=str(tmp_path / "out.tar.gz"), tmp_path=str(tmp_path / "out.jsonl"),
    )
    archive_path = tmp_path / f"{col}.tar.gz"
    chunks = [_chunk(_doc_id(), 0)]
    _make_archive(archive_path, col, chunks)

    finished = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store))

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)
    assert import_runner.job_store.get(export_job.job_id) == export_job, "the export job must be left as it was"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_refuses_when_claim_revoked_during_ownership_lookup(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Another import's claim landing while the ownership check reads the collection still makes the resume refuse."""
    col = "resume-revoked-during-lookup"
    import_job, _, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col, after_commit=True)
    rows_before = await _stored_rows(import_runner.search_store, col)
    original_get_chunks_by_ids = import_runner.search_store.get_chunks_by_ids

    async def revoked_meanwhile(collection: str, chunk_ids: list[str]) -> list[dict]:
        rows = await original_get_chunks_by_ids(collection, chunk_ids)
        import_runner.job_store.update(import_job.job_id, collection_claim_revoked=True)  # a concurrent claim
        return rows

    refused = await import_runner.resume(import_job, get_chunks_by_ids=revoked_meanwhile)

    assert (refused.status, refused.error) == (JobStatus.FAILED, _lost_ownership_error(col))
    assert await _stored_rows(import_runner.search_store, col) == rows_before, "the refused resume must write nothing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_force_overwrite_resume_continues_into_its_own_collection(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """force_overwrite does not make a resume drop the partial collection it wrote itself: it continues into it.

    The resume ingests only the docs past the rows run 1 committed; a restart would ingest the whole archive again.
    """
    col = "resume-force-own"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(
        tmp_path, col, after_commit=True, force_overwrite=True,
    )

    drop = AsyncMock(side_effect=import_runner.search_store.drop_collection)
    ingest = AsyncMock(side_effect=import_runner.search_store.ingest_chunks)
    finished = await import_runner.resume(import_job, drop_collection=drop, ingest_chunks=ingest)

    drop.assert_not_awaited()
    ingested = sum(len(call.args[1]) for call in ingest.await_args_list)
    assert ingested == len(chunks) - _CRASH_COMMITTED_DOCS, "the resume must continue, ingesting only the remaining docs"
    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_in_non_default_namespace_continues(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A resume of an import into a non-default namespace continues into its partial collection."""
    col = "resume-other-namespace"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(
        tmp_path, col, after_commit=True, namespace="team-a",
    )

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_after_crash_before_first_commit_imports_everything_once(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A run that claimed the collection and created its table, then crashed before committing a row, resumes fully.

    Nothing marks the empty table as the job's own, but nothing else is in it either: the resume restarts from
    scratch into it instead of refusing it.
    """
    col = "resume-before-first-commit"
    chunks = [_chunk(_doc_id(), i) for i in range(_CRASH_ARCHIVE_DOCS)]
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, chunks)
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store)

    crashed = await import_runner.run(import_job, ingest_chunks=_CrashOnCall(import_runner.search_store.ingest_chunks, crash_at=1))
    assert (crashed.status, crashed.progress["processed"]) == (JobStatus.FAILED, 0)
    assert crashed.collection_claimed, "run 1 must have claimed the collection before ingesting"
    assert await import_runner.search_store.count_chunks(col, DEFAULT_NAMESPACE) == 0, "run 1 must commit no row"

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("chunk_id", "not-a-chunk-id", id="chunk_id-malformed"),
        pytest.param("metadata", {"k": 1}, id="metadata-non-str-value"),
    ],
)
async def test_import_skip_mode_resume_uses_first_valid_doc_as_ownership_marker(
    tmp_path: Path,
    import_runner: _ImportRunner,
    field: str,
    value: object,
) -> None:
    """Under on_error='skip' a malformed line 1 is passed over: line 2, the first valid doc, marks the collection."""
    col = "resume-skip-first-doc"
    import_runner.config.jobs.checkpoint_interval = _CRASH_CHECKPOINT_INTERVAL
    chunks = [_chunk(_doc_id(), i) for i in range(_CRASH_ARCHIVE_DOCS)]
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, chunks, doc_overrides={0: {field: value}})
    valid_chunks = chunks[1:]
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store, on_error="skip")
    crash = _CrashOnCall(import_runner.search_store.ingest_chunks, crash_at=_CRASH_BATCHES_CHECKPOINTED + 1)
    crashed = await import_runner.run(import_job, ingest_chunks=crash)
    assert (crashed.status, crashed.progress["processed"]) == (JobStatus.FAILED, _CRASH_CHECKPOINTED_DOCS)

    finished = await import_runner.resume(import_job)

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert await import_runner.all_stored_chunk_ids(col) == sorted(c.chunk_id for c in valid_chunks)
    assert (finished.result["imported"], finished.result["skipped"]) == (len(valid_chunks), 1), (
        f"unexpected counts: {finished.result}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_of_claimed_empty_archive_completes(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A claimed import of an empty archive that failed afterwards resumes to DONE with nothing imported."""
    col = "resume-empty-archive"
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, [])
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store)
    import_runner.job_store.update(
        import_job.job_id, status=JobStatus.FAILED, collection_claimed=True,
    )

    finished = await import_runner.resume(import_job)

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert finished.result == {"imported": 0, "skipped": 0, "total_in_archive": 0}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_into_existing_collection_reports_exists_before_corrupt_first_line(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A fresh fail-mode import is refused for an existing collection before the archive's docs are read."""
    col = "exists-before-corrupt-line"
    seeded = await _seed_collection(import_runner.search_store, col, 2)
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, [_chunk(_doc_id(), i) for i in range(3)], corrupt_index=0)

    failed = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store))

    assert failed.status == JobStatus.FAILED
    assert failed.error == f"collection {col!r} already exists; use force_overwrite=true to overwrite"
    assert await import_runner.all_stored_chunk_ids(col) == sorted(c.chunk_id for c in seeded)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_resume_with_malformed_first_doc_fails_before_ownership_check(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A fail-mode resume whose archive's line 1 became malformed fails in the first-doc scan, keeping its checkpoint."""
    col = "resume-malformed-first-doc"
    import_job, chunks, archive_path = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    _make_archive(archive_path, col, chunks, doc_overrides={0: {"custom_score": "high"}})

    ownership_lookup = AsyncMock(side_effect=import_runner.search_store.get_chunks_by_ids)
    failed = await import_runner.resume(import_job, get_chunks_by_ids=ownership_lookup)

    assert failed.status == JobStatus.FAILED
    assert failed.error == "malformed doc at line 1: a required field is missing or has the wrong type"
    ownership_lookup.assert_not_awaited()
    assert (failed.progress["processed"], failed.lines_consumed) == (_CRASH_CHECKPOINTED_DOCS, _CRASH_CHECKPOINTED_DOCS)
    assert await import_runner.all_stored_chunk_ids(col) == sorted(
        c.chunk_id for c in chunks[:_CRASH_CHECKPOINTED_DOCS]
    ), "the failed resume must write nothing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_cancel_mid_ingest_ends_cancelled_at_the_next_checkpoint(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A DELETE /jobs/{id} during ingesting ends the job CANCELLED at the next checkpoint, ingesting no further batch."""
    col = "cancel-mid-ingest"
    import_runner.config.jobs.checkpoint_interval = _CRASH_CHECKPOINT_INTERVAL
    chunks = [_chunk(_doc_id(), i) for i in range(_CRASH_ARCHIVE_DOCS)]
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, chunks)
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store)
    running = import_runner.job_store.transition(import_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running is not None
    original_ingest = import_runner.search_store.ingest_chunks
    # Recorded, not asserted, in the side effect: _import_task's broad except would swallow an AssertionError there.
    cancel_transitions: list[object] = []

    async def _ingest_then_cancel(collection: str, batch: list[ChunkRecord], **kwargs: Any) -> object:
        result = await original_ingest(collection, batch, **kwargs)
        cancel_transitions.append(
            import_runner.job_store.transition(import_job.job_id, _ACTIVE_STATUSES, JobStatus.CANCELLING)
        )
        return result

    ingest = AsyncMock(side_effect=_ingest_then_cancel)
    cancelled = await import_runner.run(import_job, ingest_chunks=ingest)

    assert len(cancel_transitions) == 1 and cancel_transitions[0] is not None, "the cancel must reach the RUNNING job"
    assert cancelled.status == JobStatus.CANCELLED, f"Expected CANCELLED, got {cancelled.status}: {cancelled.error}"
    assert ingest.await_count == 1, "no batch may be ingested after the cancel"
    assert cancelled.progress["processed"] == _CRASH_CHECKPOINT_INTERVAL
    assert await import_runner.all_stored_chunk_ids(col) == sorted(c.chunk_id for c in chunks[:_CRASH_CHECKPOINT_INTERVAL])
    import_runner.pipeline.recompute_collection_meta.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_restart_crashing_before_its_first_checkpoint_resumes_completely(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A restart from scratch resets the checkpoint, so a crash before the restart's own first checkpoint resumes right.

    Run 1 checkpoints its first batch and crashes; the collection is deleted, so resume 1 restarts from scratch, commits
    its first batch and crashes before checkpointing it. Resume 2 must continue from the reset position (line 0),
    not from run 1's stale skip position, or it would report the re-stored docs as skipped.
    """
    col = "restart-crash-before-checkpoint"
    import_job, chunks, _ = await import_runner.crash_after_first_checkpoint(tmp_path, col)
    search_store = import_runner.search_store
    await search_store.drop_collection(col)

    restart = _CrashOnCall(search_store.ingest_chunks, crash_at=1, after_commit=True)
    restarted = await import_runner.resume(import_job, ingest_chunks=restart)
    assert restarted.status == JobStatus.FAILED, f"the restart must crash, got {restarted.status}"
    assert (restarted.progress["processed"], restarted.lines_consumed) == (0, 0), (
        f"the restart must reset the checkpoint: {restarted.progress}, lines_consumed={restarted.lines_consumed}"
    )
    assert await search_store.count_chunks(col, DEFAULT_NAMESPACE) == _CRASH_CHECKPOINT_INTERVAL, (
        "the restart commits its first batch"
    )

    finished = await import_runner.resume(import_job)

    await import_runner.assert_imported_each_chunk_once(finished, col, chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_rejected_before_collection_is_created_does_not_claim_it(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A fail-mode import whose first archive line is corrupt fails before creating the collection, unclaimed.

    The embedding-dimension scan reads that line first and raises; a claim written before it would let a
    resume bypass the exists guard for a collection this job never wrote.
    """
    col = "rejected-before-create"
    chunks = [_chunk(_doc_id(), i) for i in range(3)]
    archive_path = tmp_path / "rejected-before-create.tar.gz"
    _make_archive(archive_path, col, chunks, corrupt_index=0)
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store, on_error="fail")

    ensure_collection = AsyncMock()
    failed = await import_runner.run(import_job, ensure_collection=ensure_collection)

    assert (failed.status, failed.error) == (JobStatus.FAILED, "Corrupt line 1: not valid UTF-8 JSON")
    ensure_collection.assert_not_awaited()
    assert failed.collection_claimed is False, "a run rejected before creating the collection must not claim it"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_force_overwrite_drop_failure_does_not_claim_collection(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """A force_overwrite import whose drop of the existing collection fails ends FAILED without claiming it."""
    col = "force-drop-fails"
    chunks = await _seed_collection(import_runner.search_store, col, 2)
    archive_path = tmp_path / "force-drop-fails.tar.gz"
    _make_archive(archive_path, col, chunks)
    import_job = _make_import_job(col, str(archive_path), import_runner.job_store, force_overwrite=True)

    failed = await import_runner.run(import_job, drop_collection=AsyncMock(side_effect=OSError("disk gone")))

    assert (failed.status, failed.error) == (JobStatus.FAILED, IMPORT_FAILED_DETAIL)
    assert failed.collection_claimed is False, "the claim must follow the drop, so a failed drop leaves it unset"


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
#: Archived vectors that fail to decode as base64 float32: the refusal chains the decoder's error.
_UNDECODABLE_VECTORS = [
    pytest.param(_DROP, id="missing"),
    pytest.param(7, id="not-a-string"),
    pytest.param("!!!not-base64", id="not-base64"),
    pytest.param(base64.standard_b64encode(bytes(_DIM + 1)).decode("ascii"), id="not-a-float32-multiple"),
]
#: Well-formed float32 vectors LanceDB refuses: it fails a whole batch holding a NaN component.
_NAN_VECTORS = [
    pytest.param(_encode_vector([0.5] * (_DIM - 1) + [math.nan]), id="one-nan-component"),
    pytest.param(_encode_vector([math.nan] * _DIM), id="all-nan"),
]
#: Archived vectors refused per doc: they do not decode to a non-empty float32 vector, or hold a NaN component.
_CORRUPT_VECTORS = [*_UNDECODABLE_VECTORS, pytest.param("", id="empty"), *_NAN_VECTORS]
_DIMENSION_MISMATCH_VECTOR = _encode_vector([0.5] * (_DIM * 2))
#: Of another dimension and holding a NaN: the dimension check, not the NaN refusal, must win.
_DIMENSION_MISMATCH_NAN_VECTOR = _encode_vector([0.5] * (_DIM * 2 - 1) + [math.nan])
#: A float32 vector holding an infinity, which LanceDB stores (unlike NaN).
_INF_VECTOR = _encode_vector([0.5] * (_DIM - 1) + [math.inf])
#: Archive index of the doc holding ``_INF_VECTOR``: an accepted doc, so not named after a refusal.
_INF_INDEX = 1
#: Docs in each vector test's archive; the skip-mode tests skip exactly one of them.
_VECTOR_ARCHIVE_DOCS = 3


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
@pytest.mark.parametrize("vector", _UNDECODABLE_VECTORS)
async def test_import_task_corrupt_vector_names_the_line_not_the_decoder_error(
    vector: object,
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
            search_config, doc_overrides={_REJECTED_INDEX: {"vector": vector}},
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
async def test_import_task_empty_vector_names_the_line(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A vector that decodes to no floats is refused like an undecodable one."""
    finished = await _run_failing_import(
        tmp_path, "empty-vector-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_REJECTED_INDEX: {"vector": ""}},
    )

    assert finished.error == (
        f"corrupt doc (vector) at line {_REJECTED_LINE}: missing or not base64-encoded float32"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["doc_id", "chunk_id", "text", "source_path", "indexed_at"])
async def test_import_task_malformed_doc_names_the_line_not_the_exception(
    field: str,
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    finished = await _run_failing_import(
        tmp_path, "malformed-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_REJECTED_INDEX: {field: _DROP}},
    )

    assert finished.error == (
        f"malformed doc at line {_REJECTED_LINE}: a required field is missing or has the wrong type"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("vector", _NAN_VECTORS)
async def test_import_task_nan_vector_names_the_line(
    vector: str,
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A vector holding NaN, which LanceDB refuses for the whole batch, is refused per doc."""
    finished = await _run_failing_import(
        tmp_path, "nan-vector-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_REJECTED_INDEX: {"vector": vector}},
    )

    assert finished.error == f"corrupt doc (vector) at line {_REJECTED_LINE}: a component is NaN"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("vector", _NAN_VECTORS)
async def test_import_skip_mode_skips_doc_with_nan_vector(
    vector: str,
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Under on_error='skip' a vector holding NaN drops that doc only, not the batch it shares with valid docs."""
    col = "nan-vector-skip"
    chunks = [_chunk(_doc_id(), i) for i in range(_VECTOR_ARCHIVE_DOCS)]
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, chunks, doc_overrides={_REJECTED_INDEX: {"vector": vector}})

    finished = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store, on_error="skip"))

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert (finished.result["imported"], finished.result["skipped"]) == (_VECTOR_ARCHIVE_DOCS - 1, 1)
    assert await import_runner.all_stored_chunk_ids(col) == sorted(
        c.chunk_id for i, c in enumerate(chunks) if i != _REJECTED_INDEX
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_task_vector_of_another_dimension_names_the_line(
    tmp_path: Path,
    tmp_job_store: JobStore,
    tmp_search_store: SearchStore,
    search_config: SearchConfig,
    mock_pipeline: MagicMock,
    mock_embedder_cache: MagicMock,
) -> None:
    """A doc whose vector length differs from the first doc's is a corrupt doc, not an unexpected ingest failure."""
    finished = await _run_failing_import(
        tmp_path, "dim-mismatch-col", tmp_job_store, tmp_search_store, mock_pipeline, mock_embedder_cache,
        search_config, doc_overrides={_REJECTED_INDEX: {"vector": _DIMENSION_MISMATCH_VECTOR}},
    )

    assert finished.error == (
        f"corrupt doc (vector) at line {_REJECTED_LINE}: its dimension differs from the archive's first valid document"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vector",
    [
        pytest.param(_DIMENSION_MISMATCH_VECTOR, id="finite"),
        pytest.param(_DIMENSION_MISMATCH_NAN_VECTOR, id="holding-nan"),
    ],
)
async def test_import_skip_mode_fails_on_vector_of_another_dimension(
    vector: str,
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Under on_error='skip' a doc whose vector length differs from the first valid doc's still fails the job.

    The first valid doc sets the dimension, so were that doc the wrong one, skipping would drop every other doc and
    still end DONE. A NaN component, skippable on its own, must not turn the mismatch into a skip.
    """
    col = "dim-mismatch-skip"
    chunks = [_chunk(_doc_id(), i) for i in range(_VECTOR_ARCHIVE_DOCS)]
    archive_path = tmp_path / "dim-mismatch-skip.tar.gz"
    _make_archive(archive_path, col, chunks, doc_overrides={_REJECTED_INDEX: {"vector": vector}})

    failed = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store, on_error="skip"))

    assert (failed.status, failed.error) == (
        JobStatus.FAILED,
        f"corrupt doc (vector) at line {_REJECTED_LINE}: its dimension differs from the archive's first valid document",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_stores_vector_with_infinite_component(
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Unlike NaN, an infinite vector component is stored by LanceDB, so the import accepts the doc."""
    col = "inf-vector"
    chunks = [_chunk(_doc_id(), i) for i in range(_VECTOR_ARCHIVE_DOCS)]
    archive_path = tmp_path / f"{col}.tar.gz"
    _make_archive(archive_path, col, chunks, doc_overrides={_INF_INDEX: {"vector": _INF_VECTOR}})

    finished = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store))

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert (finished.result["imported"], finished.result["skipped"]) == (len(chunks), 0)
    assert await import_runner.all_stored_chunk_ids(col) == sorted(c.chunk_id for c in chunks)
    rows = await _rows_by_chunk(import_runner.search_store, col)
    assert math.isinf(rows[chunks[_INF_INDEX].chunk_id]["vector"][-1]), "the infinite component must be stored"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("vector", _CORRUPT_VECTORS)
async def test_import_skip_mode_takes_dimension_from_first_valid_doc(
    vector: object,
    tmp_path: Path,
    import_runner: _ImportRunner,
) -> None:
    """Under on_error='skip' a corrupt vector on archive line 1 does not set the collection's dimension: line 2 does."""
    col = "corrupt-first-vector-skip"
    chunks = [_chunk(_doc_id(), i) for i in range(_VECTOR_ARCHIVE_DOCS)]
    archive_path = tmp_path / "corrupt-first-vector-skip.tar.gz"
    _make_archive(archive_path, col, chunks, doc_overrides={0: {"vector": vector}})

    finished = await import_runner.run(_make_import_job(col, str(archive_path), import_runner.job_store, on_error="skip"))

    assert finished.status == JobStatus.DONE, f"Expected DONE, got {finished.status}: {finished.error}"
    assert (finished.result["imported"], finished.result["skipped"]) == (_VECTOR_ARCHIVE_DOCS - 1, 1)
    table = await import_runner.search_store._require_connected().open_table(col)
    assert (await table.schema()).field("vector").type.list_size == _DIM, "the dimension must come from line 2"


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
