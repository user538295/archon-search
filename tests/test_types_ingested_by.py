"""Tests pinning the ``IngestedBy`` Literal and ``INGESTED_BY_VALUES`` constant.

Implements Task 3.1 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import typing

import pytest

from archon_search._types import ChunkRecord, IngestedBy
from archon_search.constants import INGESTED_BY_VALUES, LEGACY_INGESTED_BY


def test_ingested_by_values_constant_matches_literal() -> None:
    """``INGESTED_BY_VALUES`` and ``IngestedBy`` Literal args must agree exactly."""
    assert typing.get_args(IngestedBy) == INGESTED_BY_VALUES
    assert len(INGESTED_BY_VALUES) == 4


@pytest.mark.parametrize("value", ["cli", "http", "watcher", "reindex"])
def test_chunk_record_accepts_each_ingested_by_value(value: str) -> None:
    record = ChunkRecord(
        doc_id="d",
        chunk_id="c",
        text="t",
        vector=[0.0],
        source_path="/tmp/x.md",
        indexed_at="2026-05-21T00:00:00+00:00",
        ingested_by=value,  # type: ignore[arg-type]
    )
    assert record.ingested_by == value


def test_legacy_value_not_in_literal() -> None:
    """Legacy ``"archon-search-cli"`` is normalized at boundaries; never in the type."""
    assert LEGACY_INGESTED_BY == "archon-search-cli"
    assert LEGACY_INGESTED_BY not in typing.get_args(IngestedBy)
    assert LEGACY_INGESTED_BY not in INGESTED_BY_VALUES
