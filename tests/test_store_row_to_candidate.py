"""The shared raw-row decoders in ``store.py``: ``decode_row_common_fields``, ``decode_row_acl_fields``
and ``row_to_scored_candidate``.

Every row → ``ScoredSearchCandidate`` conversion (the hybrid fuse step and every graph
path) goes through ``row_to_scored_candidate``, so hybrid and graph-sourced results
report the same fields for the same stored row — only ``score_breakdown`` differs.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord
from archon_search.pipeline import _row_to_graph_candidate
from archon_search.store import (
    SearchStore,
    decode_row_acl_fields,
    decode_row_common_fields,
    row_to_scored_candidate,
)

_COLLECTION = "row-builder"
_SIDECAR_SOURCE = "sidecar"
_SIDECAR_PATH = "docs/secret.md.acl"
_WARNINGS = ["acl sidecar entry 'deny-all' is not a valid namespace; ignored"]
_SCOPES = ["user:alice"]
_INDEXED_AT = "2024-01-01T00:00:00"
_UPDATED_AT = "2024-02-01T00:00:00"


def _breakdown() -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=None, vector_score=None, vector_score_kind=None,
        fts_rank=0, fts_score=1.5, fts_score_kind="bm25",
        rrf_score=0.5, reranker_score=None,
    )


def _row(**overrides: Any) -> dict[str, Any]:
    """A raw LanceDB chunk row carrying G15 ACL provenance and scope tags."""
    row: dict[str, Any] = {
        "chunk_id": "chunk-1",
        "source_path": "/docs/secret.md",
        "text": "stored text",
        "doc_id": "doc-1",
        "language": "en",
        "acl": ["default"],
        "scopes": list(_SCOPES),
        "acl_source": _SIDECAR_SOURCE,
        "acl_sidecar_path": _SIDECAR_PATH,
        "acl_warning": list(_WARNINGS),
        "metadata": '{"team": "billing"}',
        "ingested_by": "watcher",
        "file_type": "md",
        "indexed_at": _INDEXED_AT,
        "updated_at": _UPDATED_AT,
    }
    row.update(overrides)
    return row


def _null_column_row() -> dict[str, Any]:
    """A row whose optional columns are stored null (legacy / pre-migration data)."""
    return _row(
        file_type=None, language=None, updated_at=None, metadata=None, ingested_by=None,
        acl=None, scopes=None, acl_source=None, acl_sidecar_path=None, acl_warning=None,
    )


# ---------------------------------------------------------------------------
# decode_row_common_fields
# ---------------------------------------------------------------------------


def test_decode_row_common_fields_reads_every_common_column() -> None:
    assert decode_row_common_fields(_row()) == {
        "file_type": "md",
        "language": "en",
        "indexed_at": _INDEXED_AT,
        "updated_at": _UPDATED_AT,
        "ingested_by": "watcher",
        "metadata": {"team": "billing"},
    }


def test_decode_row_common_fields_coerces_null_columns_to_defaults() -> None:
    """Null optional columns become "", {}, "cli"; a null updated_at falls back to indexed_at."""
    assert decode_row_common_fields(_null_column_row()) == {
        "file_type": "",
        "language": "",
        "indexed_at": _INDEXED_AT,
        "updated_at": _INDEXED_AT,
        "ingested_by": "cli",
        "metadata": {},
    }


def test_decode_row_common_fields_defaults_missing_columns() -> None:
    """A row lacking every common column (pre-A1/A2 table) decodes to the empty defaults."""
    assert decode_row_common_fields({"chunk_id": "chunk-1"}) == {
        "file_type": "", "language": "", "indexed_at": "", "updated_at": "", "ingested_by": "cli", "metadata": {},
    }


def test_decode_row_common_fields_null_indexed_at_decodes_to_empty_string() -> None:
    """A null indexed_at decodes to "", and with updated_at also null the fallback is "" too, never None."""
    assert decode_row_common_fields(_row(indexed_at=None))["indexed_at"] == ""
    fields = decode_row_common_fields(_row(indexed_at=None, updated_at=None))
    assert (fields["indexed_at"], fields["updated_at"]) == ("", "")


def test_decode_row_common_fields_normalizes_legacy_ingested_by() -> None:
    assert decode_row_common_fields(_row(ingested_by="archon-search-cli"))["ingested_by"] == "cli"


# ---------------------------------------------------------------------------
# decode_row_acl_fields
# ---------------------------------------------------------------------------


def test_decode_row_acl_fields_reads_every_acl_column() -> None:
    assert decode_row_acl_fields(_row()) == {
        "acl": ["default"],
        "scopes": _SCOPES,
        "acl_source": _SIDECAR_SOURCE,
        "acl_sidecar_path": _SIDECAR_PATH,
        "acl_warning": _WARNINGS,
    }


def test_decode_row_acl_fields_defaults_missing_columns() -> None:
    """A table lacking the ACL / scope / provenance columns decodes to the no-ACL defaults."""
    assert decode_row_acl_fields({"chunk_id": "chunk-1"}) == {
        "acl": None, "scopes": None, "acl_source": None, "acl_sidecar_path": None, "acl_warning": [],
    }


def test_decode_row_acl_fields_widens_null_acl_warning_to_empty_list() -> None:
    """Null columns (pre-G15 rows) decode to None, except acl_warning which is always a list."""
    fields = decode_row_acl_fields(
        _row(acl=None, scopes=None, acl_source=None, acl_sidecar_path=None, acl_warning=None)
    )

    assert fields == {
        "acl": None, "scopes": None, "acl_source": None, "acl_sidecar_path": None, "acl_warning": [],
    }


def test_decode_row_acl_fields_drops_non_list_acl_warning() -> None:
    """A malformed scalar acl_warning is never split into characters; it decodes to []."""
    assert decode_row_acl_fields(_row(acl_warning="oops"))["acl_warning"] == []


def test_decode_row_acl_fields_treats_non_list_acl_and_scopes_as_absent() -> None:
    """LanceDB ``to_list`` yields Python lists; any other shape is treated as no ACL / no scopes."""
    fields = decode_row_acl_fields(_row(acl="default", scopes=("user:alice",)))

    assert fields["acl"] is None
    assert fields["scopes"] is None


def test_decode_row_acl_fields_copies_lists() -> None:
    """The decoded lists are copies — mutating a candidate never mutates the source row."""
    row = _row()
    fields = decode_row_acl_fields(row)

    assert fields["acl"] is not row["acl"]
    assert fields["scopes"] is not row["scopes"]
    assert fields["acl_warning"] is not row["acl_warning"]


# ---------------------------------------------------------------------------
# row_to_scored_candidate
# ---------------------------------------------------------------------------


def test_scored_candidate_carries_every_stored_field() -> None:
    breakdown = _breakdown()

    candidate = row_to_scored_candidate(_row(), _COLLECTION, breakdown)

    assert candidate == ScoredSearchCandidate(
        doc_id="doc-1",
        chunk_id="chunk-1",
        text="stored text",
        source_path="/docs/secret.md",
        score_breakdown=breakdown,
        collection=_COLLECTION,
        acl=["default"],
        file_type="md",
        indexed_at=_INDEXED_AT,
        updated_at=_UPDATED_AT,
        ingested_by="watcher",
        language="en",
        metadata={"team": "billing"},
        scopes=_SCOPES,
        acl_source=_SIDECAR_SOURCE,
        acl_sidecar_path=_SIDECAR_PATH,
        acl_warning=_WARNINGS,
    )


def test_scored_candidate_requires_identity_columns() -> None:
    """doc_id / chunk_id / text / source_path are non-optional chunk columns; a row without one is a bug."""
    row = _row()
    del row["doc_id"]

    with pytest.raises(KeyError):
        row_to_scored_candidate(row, _COLLECTION, _breakdown())


# ---------------------------------------------------------------------------
# Parity: hybrid fuse step vs graph builder, on a real LanceDB table
# ---------------------------------------------------------------------------

_DIM = 4
_VECTOR = [0.1, 0.2, 0.3, 0.4]
_DOC_ID = "d" * 64
_CHUNK_ID = f"{_DOC_ID}-000000"
_TEXT = "stored provenance text"

#: The chunk-table columns, all nullable, so a row can store null in every optional column (legacy data).
_NULLABLE_CHUNK_SCHEMA = pa.schema([
    pa.field("doc_id", pa.utf8()),
    pa.field("chunk_id", pa.utf8()),
    pa.field("text", pa.utf8()),
    pa.field("vector", pa.list_(pa.float32(), _DIM)),
    pa.field("source_path", pa.utf8()),
    pa.field("indexed_at", pa.utf8()),
    pa.field("file_type", pa.utf8()),
    pa.field("language", pa.utf8()),
    pa.field("metadata", pa.utf8()),
    pa.field("custom_score", pa.float32()),
    pa.field("ingested_by", pa.utf8()),
    pa.field("updated_at", pa.utf8()),
    pa.field("acl", pa.list_(pa.utf8())),
    pa.field("expires_at", pa.utf8()),
    pa.field("scopes", pa.list_(pa.utf8())),
    pa.field("acl_source", pa.utf8()),
    pa.field("acl_sidecar_path", pa.utf8()),
    pa.field("acl_warning", pa.list_(pa.utf8())),
])


async def _seed_populated(db_path: Path) -> None:
    """Ingest one fully populated chunk through the production write path."""
    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(_COLLECTION, _DIM)
        await store.ingest_chunks(_COLLECTION, [ChunkRecord(
            doc_id=_DOC_ID, chunk_id=_CHUNK_ID, text=_TEXT, vector=_VECTOR, source_path="/docs/secret.md",
            indexed_at=_INDEXED_AT, updated_at=_UPDATED_AT, file_type="md", language="en",
            metadata={"team": "billing"}, ingested_by="watcher", acl=["default"], scopes=list(_SCOPES),
            acl_source=_SIDECAR_SOURCE, acl_sidecar_path=_SIDECAR_PATH, acl_warning=list(_WARNINGS),
        )])
    finally:
        await store.disconnect()


async def _seed_null_columns(
    db_path: Path, chunk_id: str = _CHUNK_ID, indexed_at: str | None = _INDEXED_AT,
) -> None:
    """Write one chunk whose optional columns are all stored null — ingest_chunks never writes that shape."""
    db_path.mkdir(parents=True, exist_ok=True)
    raw_db = await lancedb.connect_async(str(db_path))
    row = dict.fromkeys(_NULLABLE_CHUNK_SCHEMA.names)
    row.update(doc_id=_DOC_ID, chunk_id=chunk_id, text=_TEXT, vector=_VECTOR, source_path="/docs/secret.md",
               indexed_at=indexed_at)
    await raw_db.create_table(_COLLECTION, data=[row], schema=_NULLABLE_CHUNK_SCHEMA)
    raw_db.close()


def _without_breakdown(candidate: ScoredSearchCandidate) -> dict[str, Any]:
    fields = dataclasses.asdict(candidate)
    del fields["score_breakdown"]
    return fields


@pytest.mark.parametrize("seed", [_seed_populated, _seed_null_columns], ids=["populated", "null-columns"])
async def test_hybrid_and_graph_candidates_agree_on_everything_but_score(seed: Any, tmp_path: Path) -> None:
    """The same stored row yields the same candidate on the hybrid and graph paths, bar score_breakdown.

    The hybrid side runs the real fuse step over a row matched by both the vector and the FTS leg; the
    graph side reads that row back the way graph paths do (``get_chunks_by_ids``).
    """
    db_path = tmp_path / "db"
    await seed(db_path)
    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.rebuild_fts_index(_COLLECTION)
        [hybrid] = await store.hybrid_search_with_trace(_COLLECTION, _VECTOR, "provenance", 20)
        [graph_row] = await store.get_chunks_by_ids(_COLLECTION, [_CHUNK_ID])
    finally:
        await store.disconnect()

    assert (hybrid.score_breakdown.vector_rank, hybrid.score_breakdown.fts_rank) == (0, 0), "row must hit both legs"
    assert _without_breakdown(hybrid) == _without_breakdown(_row_to_graph_candidate(graph_row, _COLLECTION))


async def test_fetch_adjacent_chunks_decodes_null_indexed_at_to_empty_string(tmp_path: Path) -> None:
    """A neighbour row with every optional column null, indexed_at included, still builds a ChunkRecord."""
    db_path = tmp_path / "db"
    await _seed_null_columns(db_path, chunk_id=f"{_DOC_ID}-000001", indexed_at=None)
    store = SearchStore(db_path)
    await store.connect()
    try:
        [neighbour] = await store.fetch_adjacent_chunks(_COLLECTION, _DOC_ID, center_idx=0, window=1)
    finally:
        await store.disconnect()

    assert (neighbour.indexed_at, neighbour.updated_at, neighbour.ingested_by) == ("", "", "cli")
    assert (neighbour.acl, neighbour.acl_warning) == (None, [])


async def test_list_chunks_raw_decodes_null_columns_but_keeps_metadata_raw(tmp_path: Path) -> None:
    """Export decodes a null-column row like every read path (ingested_by normalized), except metadata,
    which stays the stored JSON string so the archive carries it verbatim."""
    db_path = tmp_path / "db"
    await _seed_null_columns(db_path, indexed_at=None)
    store = SearchStore(db_path)
    await store.connect()
    try:
        [row] = [r async for r in store.list_chunks_raw(_COLLECTION, "default")]
    finally:
        await store.disconnect()

    exported = {k: row[k] for k in ("file_type", "language", "indexed_at", "updated_at", "ingested_by", "metadata")}
    assert exported == {
        "file_type": "", "language": "", "indexed_at": "", "updated_at": "", "ingested_by": "cli", "metadata": "{}",
    }
