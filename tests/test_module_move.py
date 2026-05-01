"""Tests verifying that archon/search/ modules are importable from archon_search (Task 2.1)."""
from __future__ import annotations


def test_chunker_importable_from_archon_search() -> None:
    """Chunker must be importable from archon_search after the module move."""
    from archon_search.chunker import DocumentChunker  # noqa: PLC0415

    assert DocumentChunker is not None


def test_store_importable_from_archon_search() -> None:
    """SearchStore must be importable from archon_search after the module move."""
    from archon_search.store import SearchStore  # noqa: PLC0415

    assert SearchStore is not None
