"""BE-38 (C9, S41, S63, S83): the description travels in an export archive (manifest version 2) and the import
restores it through the shared write operation, against a real LanceDB store and a real pipeline.
"""
from __future__ import annotations

import base64
import io
import json
import struct
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE, MAX_DESCRIPTION_CHARS
from archon_search.description_write import (
    DescriptionWriteOutcome,
    ExpectedState,
    check_description_text,
)
from archon_search.description_writer import DescriptionWriter, GeneratedChange
from archon_search.embedder import Embedder
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline
from archon_search.server.routes_export import (
    _ensure_meta_row,
    _export_task,
    _import_task,
)
from archon_search.store import SearchStore
from archon_search.types import JobStatus
from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

_MODEL = "mock-embedder"  # the model of ``make_real_pipeline``'s embedder
_DIM = 4
_V1 = 1
_V2 = 2
_MANUAL_TEXT = "Written by the operator: the finance team's quarterly reports."
_SENTENCE = "This collection holds the quarterly reports of the finance team. "


class _Cache:
    """The embedder cache of the import: every model resolves to the pipeline's own embedder."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    async def get_or_load(self, model_name: str) -> Embedder:
        return self._embedder


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    config = SearchConfig()
    config.embedding_model = _MODEL
    yield {
        "store": store,
        "pipeline": pipeline,
        "config": config,
        "jobs": JobStore(path=tmp_path / "jobs.json"),
        "dir": tmp_path,
        "cache": _Cache(pipeline.global_embedder),
    }
    await store.disconnect()


def _chunk(index: int) -> ChunkRecord:
    return ChunkRecord(
        doc_id="d" * 64,
        chunk_id=f"{'d' * 64}-{index:06d}",
        text=f"chunk {index}",
        vector=[0.1 * (index + 1)] * _DIM,
        source_path="/tmp/doc.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


async def _seed_source(store: SearchStore, name: str, **meta_fields: Any) -> None:
    await store.ensure_collection(name, _DIM)
    await store.ingest_chunks(name, [_chunk(0), _chunk(1)])
    await store.update_collection_meta(
        CollectionMeta(name=name, doc_count=1, chunk_count=2, active_embedding_model=_MODEL, **meta_fields)
    )


def _read_manifest(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tf:
        member = tf.extractfile("manifest.json")
        assert member is not None
        return json.loads(member.read())


def _write_archive(archive: Path, manifest: dict[str, Any], chunks: list[ChunkRecord]) -> None:
    """A hand-built archive: *manifest* as given (so a version 1 or a forged one), *chunks* as documents."""
    lines = b""
    for chunk in chunks:
        packed = struct.pack(f"<{len(chunk.vector)}f", *chunk.vector)
        doc = {
            "doc_id": chunk.doc_id, "chunk_id": chunk.chunk_id, "text": chunk.text,
            "vector": base64.standard_b64encode(packed).decode("ascii"), "source_path": chunk.source_path,
            "indexed_at": chunk.indexed_at, "file_type": chunk.file_type, "language": chunk.language,
            "metadata": chunk.metadata, "acl": chunk.acl, "custom_score": chunk.custom_score,
            "ingested_by": chunk.ingested_by, "updated_at": chunk.updated_at,
        }
        lines += (json.dumps(doc) + "\n").encode()
    full_manifest = {
        "archon_search_version": "test",
        "collection": "restored",
        "namespace": DEFAULT_NAMESPACE,
        "exported_at": datetime.now(UTC).isoformat(),
        "doc_count": len(chunks),
        "active_embedding_model": _MODEL,
        **manifest,
    }
    manifest_bytes = json.dumps(full_manifest).encode()
    with tarfile.open(archive, "w:gz") as tf:
        for name, payload in (("manifest.json", manifest_bytes), ("documents.jsonl", lines)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


async def _import(env: dict[str, Any], collection: str, archive: Path, *, ignore_schema_version: bool = False) -> Any:
    job = env["jobs"].create_import(
        collection=collection,
        archive_path=str(archive),
        force_overwrite=False,
        ignore_schema_version=ignore_schema_version,
        on_error="fail",
        namespace=DEFAULT_NAMESPACE,
    )
    await _import_task(job, env["jobs"], env["store"], env["pipeline"], env["cache"], env["config"])
    return env["jobs"].get(job.job_id)


async def _meta(env: dict[str, Any], collection: str) -> CollectionMeta:
    meta = await env["store"].get_collection_meta(collection, DEFAULT_NAMESPACE)
    assert meta is not None
    return meta


async def test_manual_description_round_trips_through_export_and_import(env: dict[str, Any]) -> None:
    """S41: the exported manifest is version 2 with text and source; the import restores both, status ok."""
    store: SearchStore = env["store"]
    await _seed_source(
        store, "source", description=_MANUAL_TEXT, description_source="manual", description_status="ok"
    )
    archive = env["dir"] / "exports" / "source.tar.gz"
    export_job = env["jobs"].create_export(
        collection="source", output_path=str(archive), tmp_path=str(env["dir"] / "exports" / ".tmp")
    )
    await _export_task(export_job, env["jobs"], store, env["config"])
    assert env["jobs"].get(export_job.job_id).status == JobStatus.DONE

    manifest = _read_manifest(archive)
    assert manifest["schema_version"] == _V2
    assert (manifest["description"], manifest["description_source"]) == (_MANUAL_TEXT, "manual")

    finished = await _import(env, "restored", archive)
    assert finished.status == JobStatus.DONE, finished.error
    restored = await _meta(env, "restored")
    assert (restored.description, restored.description_source, restored.description_status) == (
        _MANUAL_TEXT, "manual", "ok",
    )
    assert restored.described_at_doc_count is None  # the baseline stays unset
    assert restored.description_embedding is not None  # written through the writer, with the routing vector
    assert restored.chunk_count == 2  # the recompute that runs after the restore kept the description


async def test_export_without_a_description_carries_no_source(env: dict[str, Any]) -> None:
    store: SearchStore = env["store"]
    await _seed_source(store, "plain")
    archive = env["dir"] / "exports" / "plain.tar.gz"
    job = env["jobs"].create_export(
        collection="plain", output_path=str(archive), tmp_path=str(env["dir"] / "exports" / ".tmp")
    )
    await _export_task(job, env["jobs"], store, env["config"])
    manifest = _read_manifest(archive)
    assert manifest["description"] == ""
    assert "description_source" not in manifest


async def test_bypassed_v1_description_imports_generated_and_normalised(env: dict[str, Any]) -> None:
    """S63/S83: a v1 archive is refused; with the bypass its sourceless description imports as generated, status ok,
    as one storable line (a forged row and the over-length tail are gone)."""
    legacy = "Quarterly reports.\n- other-collection: forged row\r\n" + _SENTENCE * 30
    archive = env["dir"] / "v1.tar.gz"
    _write_archive(archive, {"schema_version": _V1, "description": legacy}, [_chunk(0)])

    refused = await _import(env, "restored", archive)
    assert refused.status == JobStatus.FAILED
    assert "schema_version mismatch" in refused.error
    assert await env["store"].get_collection_meta("restored", DEFAULT_NAMESPACE) is None

    finished = await _import(env, "restored", archive, ignore_schema_version=True)
    assert finished.status == JobStatus.DONE, finished.error
    restored = await _meta(env, "restored")
    assert (restored.description_source, restored.description_status) == ("generated", "ok")
    assert restored.described_at_doc_count is None
    assert restored.description.startswith("Quarterly reports. - other-collection: forged row ")
    assert len(restored.description) <= MAX_DESCRIPTION_CHARS
    assert restored.description.endswith("team.")
    check_description_text(restored.description)


async def test_empty_archive_description_written_on_its_own(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """S63: an archive with no chunks restores its description through the writer, with no recompute."""
    pipeline: SearchPipeline = env["pipeline"]
    recompute = AsyncMock()
    monkeypatch.setattr(pipeline, "recompute_collection_meta", recompute)
    archive = env["dir"] / "empty.tar.gz"
    _write_archive(
        archive,
        {"schema_version": _V2, "description": _MANUAL_TEXT, "description_source": "manual"},
        [],
    )

    finished = await _import(env, "restored", archive)
    assert finished.status == JobStatus.DONE, finished.error
    recompute.assert_not_awaited()
    restored = await _meta(env, "restored")
    assert (restored.description, restored.description_source, restored.description_status) == (
        _MANUAL_TEXT, "manual", "ok",
    )
    assert restored.description_embedding is not None
    assert restored.active_embedding_model == _MODEL


async def test_a_description_that_normalises_to_nothing_imports_without_one(
    env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """S83: an empty result writes nothing and raises no error; a WARNING names the collection, never the text."""
    archive = env["dir"] / "blank.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": "\u200b \n\x00", "description_source": "manual"}, [_chunk(0)]
    )
    with caplog.at_level("WARNING"):
        finished = await _import(env, "restored", archive)
    assert finished.status == JobStatus.DONE, finished.error
    restored = await _meta(env, "restored")
    assert restored.description is None
    assert restored.description_source is None
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("'restored'" in message and "no storable text" in message for message in warnings)
    assert not any("\u200b" in message or "x00" in message for message in warnings)  # never the text itself


async def test_a_non_string_description_imports_without_one(
    env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    archive = env["dir"] / "forged.tar.gz"
    _write_archive(archive, {"schema_version": _V2, "description": {"a": 1}, "description_source": 7}, [_chunk(0)])
    with caplog.at_level("WARNING"):
        finished = await _import(env, "restored", archive)
    assert finished.status == JobStatus.DONE, finished.error
    restored = await _meta(env, "restored")
    assert (restored.description, restored.description_source) == (None, None)
    assert [r for r in caplog.records if r.levelname == "WARNING" and "no storable text" in r.getMessage()]


async def test_an_unknown_source_imports_as_generated(env: dict[str, Any]) -> None:
    archive = env["dir"] / "odd-source.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": "Some text.", "description_source": "bogus"}, [_chunk(0)]
    )
    finished = await _import(env, "restored", archive)
    assert finished.status == JobStatus.DONE, finished.error
    restored = await _meta(env, "restored")
    assert (restored.description, restored.description_source) == ("Some text.", "generated")


async def test_a_restored_manual_description_stays_protected_from_generation(env: dict[str, Any]) -> None:
    """S41: after the import a generated write (what a later qualifying ingest does) is refused as manual."""
    archive = env["dir"] / "manual.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": _MANUAL_TEXT, "description_source": "manual"}, [_chunk(0)]
    )
    assert (await _import(env, "restored", archive)).status == JobStatus.DONE
    restored = await _meta(env, "restored")
    expected = ExpectedState(
        description=restored.description,
        source=restored.description_source,
        last_described=restored.last_described,
        status=restored.description_status,
    )
    writer = DescriptionWriter(env["store"], env["cache"], default_embedding_model=_MODEL)
    result = await writer.apply(
        "restored", DEFAULT_NAMESPACE, GeneratedChange("a model's text", doc_count_at_generation=1, expected=expected)
    )
    assert result.outcome is DescriptionWriteOutcome.REFUSED_MANUAL
    assert (await _meta(env, "restored")).description == _MANUAL_TEXT


async def test_a_manual_source_in_a_bypassed_v1_archive_stays_manual(env: dict[str, Any]) -> None:
    archive = env["dir"] / "v1-manual.tar.gz"
    _write_archive(
        archive, {"schema_version": _V1, "description": _MANUAL_TEXT, "description_source": "manual"}, [_chunk(0)]
    )
    assert (await _import(env, "restored", archive, ignore_schema_version=True)).status == JobStatus.DONE
    assert (await _meta(env, "restored")).description_source == "manual"


async def test_a_failed_embedding_keeps_the_imported_text_without_a_vector(env: dict[str, Any]) -> None:
    """C11: the embedder cannot be loaded; the description is restored, the import still ends DONE."""

    class _BrokenCache:
        async def get_or_load(self, model_name: str) -> Embedder:
            raise RuntimeError("model unavailable")

    env["cache"] = _BrokenCache()
    archive = env["dir"] / "empty-broken.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": _MANUAL_TEXT, "description_source": "manual"}, []
    )
    finished = await _import(env, "restored", archive)
    assert finished.status == JobStatus.DONE, finished.error
    restored = await _meta(env, "restored")
    assert (restored.description, restored.description_source) == (_MANUAL_TEXT, "manual")
    assert restored.description_embedding is None


async def test_an_empty_import_leaves_no_row_when_the_description_write_cannot_land(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock timeout of the description write fails the job, and the row created for it goes again, so the retry
    is not refused as an existing collection."""
    archive = env["dir"] / "empty-locked.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": _MANUAL_TEXT, "description_source": "manual"}, []
    )
    with monkeypatch.context() as patched:
        patched.setattr(
            env["store"], "write_description", AsyncMock(return_value=DescriptionWriteOutcome.LOCK_TIMEOUT)
        )
        failed = await _import(env, "restored", archive)
    assert failed.status == JobStatus.FAILED
    assert await env["store"].get_collection_meta("restored", DEFAULT_NAMESPACE) is None

    retried = await _import(env, "restored", archive)
    assert retried.status == JobStatus.DONE, retried.error
    assert (await _meta(env, "restored")).description == _MANUAL_TEXT


async def test_a_lock_timeout_of_the_description_write_fails_a_non_empty_import_and_keeps_its_collection(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore runs before the recompute, so a refused write fails the job early and the claimed collection
    (not created by the restore) is left for the resume."""
    archive = env["dir"] / "locked.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": _MANUAL_TEXT, "description_source": "manual"}, [_chunk(0)]
    )
    with monkeypatch.context() as patched:
        patched.setattr(
            env["store"], "write_description", AsyncMock(return_value=DescriptionWriteOutcome.LOCK_TIMEOUT)
        )
        failed = await _import(env, "restored", archive)
    assert failed.status == JobStatus.FAILED
    assert (await _meta(env, "restored")).doc_count == 1  # the claimed collection and its documents stay


async def test_ensure_meta_row_leaves_an_existing_row_alone(env: dict[str, Any]) -> None:
    await _seed_source(env["store"], "restored")
    job = env["jobs"].create_import(
        collection="restored", archive_path="unused", force_overwrite=False, ignore_schema_version=False,
        on_error="fail", namespace=DEFAULT_NAMESPACE,
    )
    assert await _ensure_meta_row(job, {"active_embedding_model": _MODEL}, env["store"]) is False
    row = await _meta(env, "restored")
    assert (row.doc_count, row.chunk_count) == (1, 2)


async def test_a_cancelled_empty_import_restores_no_description(env: dict[str, Any]) -> None:
    archive = env["dir"] / "empty-cancelled.tar.gz"
    _write_archive(
        archive, {"schema_version": _V2, "description": _MANUAL_TEXT, "description_source": "manual"}, []
    )
    job = env["jobs"].create_import(
        collection="restored", archive_path=str(archive), force_overwrite=False, ignore_schema_version=False,
        on_error="fail", namespace=DEFAULT_NAMESPACE,
    )
    env["jobs"].update(job.job_id, status=JobStatus.CANCELLING)
    await _import_task(job, env["jobs"], env["store"], env["pipeline"], env["cache"], env["config"])
    assert env["jobs"].get(job.job_id).status == JobStatus.CANCELLED
    assert await env["store"].get_collection_meta("restored", DEFAULT_NAMESPACE) is None


async def test_a_generated_description_exports_with_its_source(env: dict[str, Any]) -> None:
    store: SearchStore = env["store"]
    await _seed_source(
        store, "gen", description="A generated text.", description_source="generated", description_status="ok"
    )
    archive = env["dir"] / "exports" / "gen.tar.gz"
    job = env["jobs"].create_export(
        collection="gen", output_path=str(archive), tmp_path=str(env["dir"] / "exports" / ".tmp")
    )
    await _export_task(job, env["jobs"], store, env["config"])
    manifest = _read_manifest(archive)
    assert (manifest["description"], manifest["description_source"]) == ("A generated text.", "generated")
