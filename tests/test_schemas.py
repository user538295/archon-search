"""Tests for Pydantic schema structure (Tasks 6.x)."""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Task 6.4 — SearchResponse.embedding_model field
# ---------------------------------------------------------------------------


def test_search_response_has_embedding_model_field() -> None:
    """SearchResponse schema declares an embedding_model: str field."""
    from archon_search.server.routes_search import SearchResponse

    fields = SearchResponse.model_fields
    assert "embedding_model" in fields, "SearchResponse must have an embedding_model field"
    # Verify it accepts a string value (not optional)
    sr = SearchResponse(results=[], acl_filtered=False, embedding_model="BAAI/bge-small-en-v1.5")
    assert sr.embedding_model == "BAAI/bge-small-en-v1.5"
