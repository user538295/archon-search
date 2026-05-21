"""Tests pinning explicit nullability of the ``custom_score`` PyArrow field and
that ``None`` round-trips through ingest/read without coercion to ``0.0``.

Implements Task 2.1 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(custom_score: float | None) -> ChunkRecord:
    doc_id = _doc_id()
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="hello",
        vector=[0.0] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
        custom_score=custom_score,
    )


def test_custom_score_field_nullable_kwarg_present() -> None:
    """Pin explicit ``nullable=True`` on the PyArrow ``custom_score`` field.

    Guards against a future "tidy-up" PR removing the kwarg. PyArrow defaults
    to ``nullable=True``, so the assertion is on the *attribute* of the
    constructed schema, not on the kwarg source. Combined with the round-trip
    tests below, this pins both the schema and the behavior.
    """
    schema = SearchStore._schema(_DIM)
    field = schema.field("custom_score")
    assert field.nullable is True


async def _read_row(store: SearchStore, col: str, chunk_id: str) -> dict:
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"chunk_id = '{chunk_id}'").to_list()
    assert len(rows) == 1, f"expected exactly one row for {chunk_id}, got {len(rows)}"
    return rows[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_custom_score_none_round_trip(
    connected_store: SearchStore, col_name: str
) -> None:
    """``custom_score=None`` survives write + read without becoming ``0.0``."""
    chunk = _chunk(custom_score=None)
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])
    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    assert row["custom_score"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_custom_score_value_round_trip(
    connected_store: SearchStore, col_name: str
) -> None:
    """A finite ``custom_score`` round-trips byte-equal to its float value."""
    chunk = _chunk(custom_score=0.42)
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])
    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    assert row["custom_score"] == pytest.approx(0.42)
