"""Minimal LanceDB store wrapper for the archon-search eval harness."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import lancedb
import pyarrow as pa


SCHEMA = pa.schema([
    pa.field("doc_id", pa.string()),
    pa.field("chunk_id", pa.string()),
    pa.field("text", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
    pa.field("source_path", pa.string()),
])


class LanceDBStore:
    """Thin wrapper around a LanceDB table for a single collection."""

    def __init__(self, db_path: Path, collection: str) -> None:
        self._db = lancedb.connect(str(db_path))
        self._collection = collection
        self._table: Any = None

    def create_or_open(self) -> None:
        if self._collection in self._db.table_names():
            self._table = self._db.open_table(self._collection)
        else:
            self._table = self._db.create_table(self._collection, schema=SCHEMA)

    def add(self, rows: list[dict]) -> None:
        assert self._table is not None
        self._table.add(rows)

    def search(self, vector: list[float], top_k: int = 20) -> list[dict]:
        assert self._table is not None
        return (
            self._table.search(vector)
            .limit(top_k)
            .to_list()
        )
