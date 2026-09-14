"""S293: ``SearchPipeline.ingest_documents`` — the inline-text ingest mode.

``POST /ingest`` accepts a ``documents`` payload, and it is the only ingest mode
a network client has (it cannot place files on the server host). These tests pin
that the mode really writes chunks, that ``source_path`` is the document's
logical identity (re-ingesting it replaces the document), and that a malformed
item is reported as an error rather than as a silent success.
"""
from __future__ import annotations

import pytest

from archon_search.constants import (
    DEFAULT_NAMESPACE,
    INLINE_CHUNK_METADATA_KEY,
    INLINE_CHUNK_METADATA_VALUE,
)
from archon_search.store import parse_metadata
from tests.test_e2a_be3_pipeline_ttl import _make_pipeline, _read_chunks

_TEXT = "Volcanoes erupt when magma rises through the crust. " * 10
_SOURCE_PATH = "/corpus/alpha/volcanoes.md"


@pytest.mark.asyncio
async def test_ingest_documents_writes_chunks_and_collection_meta(connected_store, col_name):
    pipeline = _make_pipeline(connected_store)

    results = await pipeline.ingest_documents(
        [{"text": _TEXT, "source_path": _SOURCE_PATH}],
        col_name,
        embedder=pipeline._global_embedder,
    )

    assert [r.status for r in results] == ["ok"]
    assert results[0].chunks_created > 0
    rows = await _read_chunks(connected_store, col_name)
    assert rows, "documents ingest must write chunks"
    assert {row["source_path"] for row in rows} == {_SOURCE_PATH}
    assert {row["doc_id"] for row in rows} == {results[0].doc_id}
    meta = await connected_store.get_collection_meta(col_name, namespace=DEFAULT_NAMESPACE)
    assert meta is not None and meta.doc_count == 1


@pytest.mark.asyncio
async def test_ingest_documents_reingest_same_source_path_replaces_document(
    connected_store, col_name
):
    pipeline = _make_pipeline(connected_store)
    embedder = pipeline._global_embedder
    payload = [{"text": _TEXT, "source_path": _SOURCE_PATH}]

    first = await pipeline.ingest_documents(payload, col_name, embedder=embedder)
    second = await pipeline.ingest_documents(payload, col_name, embedder=embedder)

    assert first[0].doc_id == second[0].doc_id
    rows = await _read_chunks(connected_store, col_name)
    assert len(rows) == second[0].chunks_created


@pytest.mark.asyncio
async def test_ingest_documents_reports_malformed_items_as_errors(connected_store, col_name):
    pipeline = _make_pipeline(connected_store)

    results = await pipeline.ingest_documents(
        [
            {"text": "", "source_path": _SOURCE_PATH},
            {"text": _TEXT},
            {"text": _TEXT, "source_path": _SOURCE_PATH},
        ],
        col_name,
        embedder=pipeline._global_embedder,
    )

    assert [r.status for r in results] == ["error", "error", "ok"]
    # "parse_error" is reserved for a genuine ParseError; a malformed request item
    # must be distinguishable from an unparseable document.
    assert all(r.code == "invalid_document" for r in results[:2])
    assert {row["source_path"] for row in await _read_chunks(connected_store, col_name)} == {
        _SOURCE_PATH
    }


@pytest.mark.asyncio
async def test_ingest_documents_empty_payload_is_a_noop(connected_store, col_name):
    pipeline = _make_pipeline(connected_store)

    assert await pipeline.ingest_documents([], col_name, embedder=pipeline._global_embedder) == []


@pytest.mark.asyncio
async def test_ingest_documents_never_reads_an_on_disk_acl_sidecar(
    connected_store, col_name, tmp_path
):
    """Inline ingest must not probe the server filesystem for ``<source_path>.acl``.

    ``source_path`` is client-supplied and names no file the client placed there,
    so reading a sidecar beside it would apply a stranger's ACL to the document,
    turn the endpoint into a file-existence oracle, and echo an absolute server
    path back to the client. Inline ACL comes from front matter (or the collection
    default) only.
    """
    doc = tmp_path / "victim.md"
    (tmp_path / "victim.md.acl").write_text("other-tenant\n")
    pipeline = _make_pipeline(connected_store)

    results = await pipeline.ingest_documents(
        [{"text": _TEXT, "source_path": str(doc)}],
        col_name,
        embedder=pipeline._global_embedder,
    )

    assert results[0].status == "ok"
    assert results[0].warnings == []
    rows = await _read_chunks(connected_store, col_name)
    assert rows and all(row["acl"] is None for row in rows)


@pytest.mark.asyncio
async def test_ingest_documents_doc_id_matches_the_delete_derivation(connected_store, col_name):
    """A non-normalized ``source_path`` must still produce a deletable document.

    ``delete_by_source_path`` (and sync/watcher/maintenance through it) derives
    the doc_id from the *resolved* path; an inline ingest that hashed the raw
    string would write chunks nothing could ever remove.
    """
    unnormalized = "/corpus/alpha/./sub/../volcanoes.md"
    pipeline = _make_pipeline(connected_store)

    results = await pipeline.ingest_documents(
        [{"text": _TEXT, "source_path": unnormalized}],
        col_name,
        embedder=pipeline._global_embedder,
    )

    assert results[0].status == "ok"
    rows = await _read_chunks(connected_store, col_name)
    assert {row["source_path"] for row in rows} == {_SOURCE_PATH}
    deleted = await pipeline.delete_by_source_path(col_name, unnormalized)
    assert deleted == results[0].chunks_created
    assert await _read_chunks(connected_store, col_name) == []


@pytest.mark.asyncio
async def test_ingest_documents_marks_chunks_as_having_no_backing_file(
    connected_store, col_name
):
    """Inline chunks carry the marker the maintenance orphan sweep keys off.

    Without it the next maintenance pass deletes every inline document, because
    its ``source_path`` names no file on disk.
    """
    pipeline = _make_pipeline(connected_store)

    await pipeline.ingest_documents(
        [{"text": _TEXT, "source_path": _SOURCE_PATH}],
        col_name,
        embedder=pipeline._global_embedder,
    )

    rows = await _read_chunks(connected_store, col_name)
    assert rows and all(
        parse_metadata(row["metadata"]).get(INLINE_CHUNK_METADATA_KEY)
        == INLINE_CHUNK_METADATA_VALUE
        for row in rows
    )
