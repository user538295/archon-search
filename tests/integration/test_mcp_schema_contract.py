"""Task 3.2 — MCP schema contract after real ingest.

Verifies that MCP schema classes correctly gate domain objects from the real
pipeline: no internal fields leak, required public fields are present, and
``model_dump`` round-trips cleanly through Pydantic.

Tests:
    test_mcp_search_real_pipeline_result_passes_pydantic_gate
    test_mcp_list_collections_real_pipeline_result_field_rename
    test_mcp_search_with_context_excludes_transient_chunk_fields
    test_e2e_mcp_search_tool_response_shape_after_real_ingest

Run with:
    uv run pytest tests/integration/test_mcp_schema_contract.py -v
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# FastMCP stub — same pattern as test_mcp_error_paths.py.
# Must be installed before archon_search.server.mcp is imported.
# ---------------------------------------------------------------------------

if "fastmcp" not in sys.modules:
    try:
        import mcp.server.fastmcp as _real_fastmcp_pkg  # type: ignore[import]

        sys.modules["fastmcp"] = _real_fastmcp_pkg  # type: ignore[assignment]
    except ImportError:
        _stub_fastmcp = types.ModuleType("fastmcp")
        _stub_fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        _stub_fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = _stub_fastmcp

import importlib as _importlib

_importlib.import_module("archon_search.server.mcp")


# ---------------------------------------------------------------------------
# Fake FastMCP app — only needs tool registration and direct invocation.
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func

        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func

        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


def _make_mcp_app(pipeline: Any, config: Any = None) -> _FakeApp:
    """Create an MCP app with a real pipeline and stub FastMCP."""
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module

        return mcp_module.create_app(  # type: ignore[call-arg]
            pipeline,
            "default",
            writer=None,
            config=config,
        )


# ---------------------------------------------------------------------------
# Expected public contract fields (used across tests).
# ---------------------------------------------------------------------------

_SEARCH_RESULT_PUBLIC_FIELDS = {
    "doc_id",
    "chunk_id",
    "text",
    "score",
    "source_path",
    "file_type",
    "language",
    "indexed_at",
    "updated_at",
    "ingested_by",
    "metadata",
    "acl",
    "collection",
}

_SEARCH_RESPONSE_PUBLIC_FIELDS = {
    "results",
    "acl_filtered",
    "excluded_collections",
    "hyde_applied",
}

_COLLECTION_LIST_PUBLIC_FIELDS = {
    "name",
    "description",
    "doc_count",
    "chunk_count",
    "last_indexed",
    "last_described",
    "embedding_model",
    "pending_embedding_model",
}

_CONTEXT_CHUNK_PUBLIC_FIELDS = {
    "doc_id",
    "chunk_id",
    "text",
    "source_path",
    "indexed_at",
    "file_type",
    "language",
    "metadata",
    "ingested_by",
    "updated_at",
    "acl",
}

_CONTEXT_CHUNK_EXCLUDED_FIELDS = {"vector", "start_offset", "end_offset", "custom_score"}


# ---------------------------------------------------------------------------
# Test 1 — McpSearchResultSchema.from_result() passes Pydantic gate
# ---------------------------------------------------------------------------


async def test_mcp_search_real_pipeline_result_passes_pydantic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ingest via pipeline → pipeline.search() → McpSearchResultSchema.from_result().

    Asserts no ValidationError and that the serialized dict has exactly the
    public contract fields (no internal fields added, no required fields missing).
    """
    from pydantic import ValidationError

    from archon_search.server.mcp_schemas import McpSearchResultSchema

    doc = tmp_path / "doc.txt"
    doc.write_text("The quick brown fox jumps over the lazy dog. " * 6)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "mcp-schema-search"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Access the real pipeline from app state.
        pipeline = client.app.state.pipeline
        embedder = client.app.state.embedder

        from archon_search.filters import SearchFilters

        result_obj = await pipeline.search(
            "quick brown fox",
            col,
            embedder=embedder,
            filters=SearchFilters(),
        )

    assert result_obj.results, "expected at least one search result"

    for r in result_obj.results:
        try:
            schema = McpSearchResultSchema.from_result(r)
        except ValidationError as exc:
            pytest.fail(f"McpSearchResultSchema.from_result() raised ValidationError: {exc}")

        serialized = schema.model_dump(mode="json")
        actual_keys = set(serialized.keys())

        missing = _SEARCH_RESULT_PUBLIC_FIELDS - actual_keys
        extra = actual_keys - _SEARCH_RESULT_PUBLIC_FIELDS
        assert not missing, f"missing required public fields: {missing}"
        assert not extra, f"unexpected extra fields leaked: {extra}"


# ---------------------------------------------------------------------------
# Test 2 — CollectionListItemSchema renames active_embedding_model → embedding_model
# ---------------------------------------------------------------------------


async def test_mcp_list_collections_real_pipeline_result_field_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real collection → get_all_collections_meta() → CollectionListItemSchema.

    Asserts:
    - ``embedding_model`` present (renamed from ``active_embedding_model``).
    - Internal fields (``centroid_sum_json``, ``namespace``, ``needs_reindex``,
      ``reindex_job_id``, ``mutations_since_recompute``) absent.
    - Serialized keys exactly match the public contract.
    """
    from pydantic import ValidationError

    from archon_search.server.mcp_schemas import CollectionListItemSchema

    doc = tmp_path / "col-list.txt"
    doc.write_text("Collection listing schema contract test document. " * 6)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "mcp-schema-collections"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline
        all_meta = await pipeline.get_all_collections_meta()

    assert all_meta, "expected at least one collection after ingest"

    _INTERNAL_FIELDS = {
        "centroid_sum_json",
        "centroid_sum",
        "namespace",
        "needs_reindex",
        "reindex_job_id",
        "mutations_since_recompute",
        "active_embedding_model",
        "needs_recompute",
        "described_at_doc_count",
        "description_embedding",
    }

    for meta in all_meta:
        try:
            schema = CollectionListItemSchema.from_result(meta)
        except ValidationError as exc:
            pytest.fail(f"CollectionListItemSchema.from_result() raised ValidationError: {exc}")

        serialized = schema.model_dump(mode="json")
        actual_keys = set(serialized.keys())

        # Renamed field must be present under the public name.
        assert "embedding_model" in actual_keys, (
            f"expected 'embedding_model' key in serialized output, got: {actual_keys}"
        )

        # Internal fields must not leak.
        leaked = _INTERNAL_FIELDS & actual_keys
        assert not leaked, f"internal fields leaked into public schema: {leaked}"

        # Exact public contract shape.
        missing = _COLLECTION_LIST_PUBLIC_FIELDS - actual_keys
        extra = actual_keys - _COLLECTION_LIST_PUBLIC_FIELDS
        assert not missing, f"missing required public fields: {missing}"
        assert not extra, f"unexpected extra fields: {extra}"


# ---------------------------------------------------------------------------
# Test 3 — ContextChunkSchema excludes transient fields
# ---------------------------------------------------------------------------


async def test_mcp_search_with_context_excludes_transient_chunk_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real search_with_context → ContextChunkSchema.from_result() excludes transients.

    Asserts no ``vector``, ``start_offset``, or ``end_offset`` appear in
    the serialized context chunks, and that all expected public fields are present.

    Writes a large enough document (~4900 chars) that the default chunk_size=512
    produces at least 9 chunks, giving context_before/context_after neighbors for
    interior chunks.
    """
    from pydantic import ValidationError

    from archon_search.server.mcp_schemas import ContextChunkSchema

    # Write a document large enough (>4000 chars) to guarantee at least 8 chunks
    # at the default chunk_size=512 with overlap. This ensures that search results
    # are not the sole chunk of the document, giving non-empty context windows.
    para_a = (
        "Alpha section discusses machine learning fundamentals in depth. "
        "Gradient descent, backpropagation, and activation functions are covered. "
        "Convolutional networks excel at image recognition tasks broadly speaking. "
        "Transfer learning reuses pretrained model weights for downstream tasks. "
    )
    para_b = (
        "Beta section covers retrieval augmented generation architectures in detail. "
        "Dense passage retrieval fetches semantically relevant chunks from vector stores. "
        "Cross-encoder rerankers then score each candidate passage against the query. "
        "Hybrid retrieval blends BM25 keyword recall with dense vector similarity scores. "
    )
    para_c = (
        "Gamma section explains vector database indexing and similarity search operations. "
        "Approximate nearest neighbor algorithms trade some recall for significant speed. "
        "LanceDB combines columnar storage with a fast vector index for hybrid queries. "
        "Inverted index structures support full-text search alongside vector retrieval. "
    )
    para_d = (
        "Delta section introduces chunk overlap strategies for preserving context. "
        "Overlapping windows ensure that split sentences remain semantically coherent. "
        "Reciprocal rank fusion merges ranked lists from multiple retrieval modes well. "
        "Reranking models score query-passage pairs to improve final result ordering. "
    )
    doc = tmp_path / "context-doc.txt"
    doc.write_text("\n\n".join([para_a, para_b, para_c, para_d] * 4))

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "mcp-schema-context"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline
        embedder = client.app.state.embedder

        from archon_search.filters import SearchFilters

        swc_result = await pipeline.search_with_context(
            "retrieval augmented generation",
            col,
            context_window=1,
            embedder=embedder,
            filters=SearchFilters(),
        )

    assert swc_result.results, "expected at least one result from search_with_context"

    # Collect all context chunks across all results.
    all_chunks = []
    for item in swc_result.results:
        all_chunks.extend(item["context_before"])
        all_chunks.extend(item["context_after"])

    assert all_chunks, (
        "expected adjacent context chunks from a ~1600-char doc with default chunk_size=512; "
        "verify the document content produces multiple chunks"
    )

    for chunk in all_chunks:
        try:
            schema = ContextChunkSchema.from_result(chunk)
        except ValidationError as exc:
            pytest.fail(f"ContextChunkSchema.from_result() raised ValidationError: {exc}")

        serialized = schema.model_dump(mode="json")
        actual_keys = set(serialized.keys())

        # Transient fields must be absent.
        for excluded in _CONTEXT_CHUNK_EXCLUDED_FIELDS:
            assert excluded not in actual_keys, (
                f"transient field {excluded!r} leaked into ContextChunkSchema output"
            )

        # Public fields must be present.
        missing = _CONTEXT_CHUNK_PUBLIC_FIELDS - actual_keys
        assert not missing, f"missing required public fields: {missing}"


# ---------------------------------------------------------------------------
# Test 4 — E2E: MCP search tool response shape after real ingest
# ---------------------------------------------------------------------------


async def test_e2e_mcp_search_tool_response_shape_after_real_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full e2e: real app ingest → MCP search tool → validate McpSearchResponse shape.

    Calls the MCP ``search`` tool function directly (same pattern as
    test_mcp_error_paths.py) on a real pipeline after ingesting via TestClient.
    Asserts:
    - Response dict has exactly the McpSearchResponse public fields.
    - No extra keys, no missing required keys.
    - ``results`` is a non-empty list.
    - Each result item has exactly the McpSearchResultSchema public fields.
    """
    doc = tmp_path / "e2e-mcp-doc.txt"
    doc.write_text("Fascinating document about the lifecycle of stars in galaxies. " * 6)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "mcp-e2e-search"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline

        # Build MCP app against the real pipeline (same collection as ingested into).
        # The store must be connected; call inside the context manager.
        mcp_app = _make_mcp_app(pipeline, config=cfg)

        # Call the search tool directly (tool functions are async).
        response = await mcp_app.tools["search"](
            query="lifecycle of stars",
            collection=col,
        )

        assert isinstance(response, dict), f"expected dict from search tool, got {type(response)}"

        # Must not be an error response.
        assert "code" not in response, (
            f"search tool returned error: {response}"
        )

        # Top-level shape must exactly match McpSearchResponse.
        actual_top_keys = set(response.keys())
        missing_top = _SEARCH_RESPONSE_PUBLIC_FIELDS - actual_top_keys
        extra_top = actual_top_keys - _SEARCH_RESPONSE_PUBLIC_FIELDS
        assert not missing_top, f"missing required top-level keys: {missing_top}"
        assert not extra_top, f"unexpected extra top-level keys: {extra_top}"

        # Results must be non-empty after real ingest.
        assert response["results"], "expected non-empty results after real ingest"

        # Each result item must have exactly the public contract fields.
        for item in response["results"]:
            item_keys = set(item.keys())
            missing_item = _SEARCH_RESULT_PUBLIC_FIELDS - item_keys
            extra_item = item_keys - _SEARCH_RESULT_PUBLIC_FIELDS
            assert not missing_item, f"missing result fields: {missing_item}"
            assert not extra_item, f"extra result fields: {extra_item}"
