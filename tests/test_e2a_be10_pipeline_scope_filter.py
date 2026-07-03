"""BE-10: Pipeline scope_filter threading — unit and integration tests.

Plan: Documentation/Backlog/e2a-ttl-scoping-team-plan.md Task BE-10.

TDD: tests are written first; implementation goes in pipeline.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    chunk_id: str = "c1",
    scopes: list[str] | None = None,
    text: str = "test text",
    source_path: str = "/path/doc.txt",
    doc_id: str = "doc1",
    collection: str = "test_col",
    rrf_score: float = 0.5,
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
        source_path=source_path,
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None,
            vector_score=None,
            vector_score_kind=None,
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=None,
        ),
        collection=collection,
        scopes=scopes,
    )


def _make_mock_pipeline():
    """Build a SearchPipeline with a mocked store for scope_filter unit tests.

    Returns (pipeline, store_mock, embedder_mock).
    """
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=[])

    embedder = MagicMock()
    embedder.model_name = "stub"
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=None,
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    return pipeline, store, embedder


# ---------------------------------------------------------------------------
# Unit tests — _apply_scope_wildcard_filter helper
# ---------------------------------------------------------------------------


def test_scope_wildcard_filter_helper_matches_prefix():
    """_apply_scope_wildcard_filter with 'user:*' matches 'user:alice' and 'user:alice:thread'
    but excludes 'admin:alice'.
    """
    from archon_search.pipeline import _apply_scope_wildcard_filter

    alice_cand = _make_candidate("c1", scopes=["user:alice"])
    thread_cand = _make_candidate("c2", scopes=["user:alice:thread"])
    admin_cand = _make_candidate("c3", scopes=["admin:alice"])

    result = _apply_scope_wildcard_filter(
        [alice_cand, thread_cand, admin_cand], "user:*"
    )

    assert alice_cand in result, "user:alice should match user:* prefix"
    assert thread_cand in result, "user:alice:thread should match user:* prefix"
    assert admin_cand not in result, "admin:alice should NOT match user:* prefix"
    assert len(result) == 2


def test_scope_wildcard_filter_helper_null_scopes_pass_through():
    """Candidates with scopes=None always pass through (unscoped = shared/global)."""
    from archon_search.pipeline import _apply_scope_wildcard_filter

    unscoped = _make_candidate("c1", scopes=None)
    alice_cand = _make_candidate("c2", scopes=["user:alice"])
    admin_cand = _make_candidate("c3", scopes=["admin:root"])

    result = _apply_scope_wildcard_filter(
        [unscoped, alice_cand, admin_cand], "user:*"
    )

    assert unscoped in result, "unscoped (scopes=None) must always pass through"
    assert alice_cand in result, "user:alice matches user:*"
    assert admin_cand not in result, "admin:root should be excluded"


def test_scope_wildcard_filter_helper_empty_scopes_pass_through():
    """Candidates with scopes=[] always pass through (empty list = unscoped).

    Use a more specific wildcard ('user:alice*') so bob ('user:bob') is excluded,
    proving that empty scopes pass regardless of the wildcard prefix while
    non-matching scoped candidates are filtered out.
    """
    from archon_search.pipeline import _apply_scope_wildcard_filter

    empty_scoped = _make_candidate("c1", scopes=[])
    bob_cand = _make_candidate("c2", scopes=["user:bob"])

    # "user:alice*" has prefix "user:alice"; "user:bob" does NOT start with "user:alice"
    result = _apply_scope_wildcard_filter([empty_scoped, bob_cand], "user:alice*")

    assert empty_scoped in result, "empty scopes [] must always pass through"
    assert bob_cand not in result, "user:bob should not match user:alice* prefix"


# ---------------------------------------------------------------------------
# Unit tests — pipeline.search() threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_search_threads_scope_filter_exact():
    """Exact scope_filter is forwarded to store.hybrid_search_with_trace as scope_filter='user:alice'."""
    pipeline, store, embedder = _make_mock_pipeline()

    await pipeline.search(
        "test query", "my_collection",
        embedder=embedder,
        scope_filter="user:alice",
    )

    assert store.hybrid_search_with_trace.called, "hybrid_search_with_trace must be called"
    call_kwargs = store.hybrid_search_with_trace.call_args.kwargs
    assert call_kwargs.get("scope_filter") == "user:alice", (
        f"Expected scope_filter='user:alice' forwarded to store, got {call_kwargs!r}"
    )


@pytest.mark.asyncio
async def test_pipeline_search_wildcard_applies_postfilter():
    """Wildcard scope_filter: store gets scope_filter=None; post-filter is applied Python-side.

    Candidates with matching prefix pass; unscoped pass; non-matching are excluded.
    Use unique text per candidate so text-based assertions are unambiguous.
    """
    alice_cand = _make_candidate("c1", scopes=["user:alice"], text="alice doc")
    unscoped = _make_candidate("c2", scopes=None, text="shared doc")
    admin_cand = _make_candidate("c3", scopes=["admin:root"], text="admin doc")

    pipeline, store, embedder = _make_mock_pipeline()
    store.hybrid_search_with_trace = AsyncMock(
        return_value=[alice_cand, unscoped, admin_cand]
    )

    result = await pipeline.search(
        "test query", "my_collection",
        embedder=embedder,
        scope_filter="user:*",
    )

    # Store must be called with scope_filter=None (wildcard not passed to SQL)
    call_kwargs = store.hybrid_search_with_trace.call_args.kwargs
    assert call_kwargs.get("scope_filter") is None, (
        f"Wildcard must not be forwarded to store as SQL predicate, got {call_kwargs!r}"
    )

    result_texts = {r.text for r in result.results}
    # alice and unscoped should appear; admin should be excluded (admin: ≠ user: prefix)
    assert alice_cand.text in result_texts, "user:alice should pass wildcard filter"
    assert unscoped.text in result_texts, "unscoped chunk must pass through wildcard filter"
    assert admin_cand.text not in result_texts, "admin:root should be excluded by wildcard filter (prefix mismatch)"


@pytest.mark.asyncio
async def test_pipeline_search_wildcard_includes_null_scoped_chunks():
    """Chunk with scopes=None is included alongside matching scoped chunk; unmatched excluded."""
    from archon_search.pipeline import _apply_scope_wildcard_filter

    alice_cand = _make_candidate("c1", scopes=["user:alice"], text="alice doc")
    null_scoped = _make_candidate("c2", scopes=None, text="shared doc")
    bob_cand = _make_candidate("c3", scopes=["user:bob"], text="bob doc")

    result = _apply_scope_wildcard_filter([alice_cand, null_scoped, bob_cand], "user:alice:*")

    chunk_ids = {c.chunk_id for c in result}
    # user:alice does NOT match prefix "user:alice:" (prefix is "user:alice:")
    assert "c1" not in chunk_ids, "user:alice should not match user:alice:* (not a prefix match)"
    assert "c2" in chunk_ids, "unscoped (None) must always pass through"
    assert "c3" not in chunk_ids, "user:bob should not match user:alice:*"


@pytest.mark.asyncio
async def test_pipeline_search_no_scope_filter_no_op():
    """No scope_filter → all candidates returned, store called with scope_filter=None."""
    alice_cand = _make_candidate("c1", scopes=["user:alice"])
    bob_cand = _make_candidate("c2", scopes=["user:bob"])
    unscoped = _make_candidate("c3", scopes=None)

    pipeline, store, embedder = _make_mock_pipeline()
    store.hybrid_search_with_trace = AsyncMock(
        return_value=[alice_cand, bob_cand, unscoped]
    )

    result = await pipeline.search(
        "test query", "my_collection",
        embedder=embedder,
        # No scope_filter
    )

    call_kwargs = store.hybrid_search_with_trace.call_args.kwargs
    assert call_kwargs.get("scope_filter") is None, "No scope_filter → store gets None"

    result_texts = {r.text for r in result.results}
    assert alice_cand.text in result_texts
    assert bob_cand.text in result_texts
    assert unscoped.text in result_texts


@pytest.mark.asyncio
async def test_pipeline_search_all_callsites_receive_scope_filter():
    """Calling search() and search_many() with scope_filter doesn't raise; store gets scope_filter."""
    from archon_search.collection_meta import CollectionMeta

    alice_cand = _make_candidate("c1", scopes=["user:alice"])
    pipeline, store, embedder = _make_mock_pipeline()
    store.hybrid_search_with_trace = AsyncMock(return_value=[alice_cand])

    # search() — should forward scope_filter
    await pipeline.search(
        "test query", "my_col",
        embedder=embedder,
        scope_filter="user:alice",
    )
    assert store.hybrid_search_with_trace.called

    # search_many() — needs collection meta
    meta = CollectionMeta(name="col_a", namespace="default", active_embedding_model="stub")
    store.get_all_collections_meta = AsyncMock(return_value=[meta])
    store.hybrid_search_with_trace.reset_mock()

    result = await pipeline.search_many(
        "test query", ["col_a"],
        scope_filter="user:alice",
    )
    # Should not raise
    assert result is not None

    # Verify scope_filter was forwarded to the store for search_many
    all_calls = store.hybrid_search_with_trace.call_args_list
    assert any(
        call.kwargs.get("scope_filter") == "user:alice"
        for call in all_calls
    ), "search_many did not forward scope_filter to hybrid_search_with_trace"


@pytest.mark.asyncio
async def test_explain_scope_filter_forwarded_through_all_internal_paths():
    """explain() with scope_filter='user:alice' forwards it to _explain_standard via store call."""
    pipeline, store, embedder = _make_mock_pipeline()

    await pipeline.explain(
        "test query",
        collection="my_collection",
        embedder=embedder,
        scope_filter="user:alice",
    )

    assert store.hybrid_search_with_trace.called, "hybrid_search_with_trace must be called"
    call_kwargs = store.hybrid_search_with_trace.call_args.kwargs
    assert call_kwargs.get("scope_filter") == "user:alice", (
        f"Expected scope_filter='user:alice' forwarded to store via _explain_standard, got {call_kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests — graph-mode defensive assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_mode_defensive_assertion_fires_search_graph_mode():
    """_search_graph_mode raises AssertionError when scope_filter is non-None."""
    pipeline, store, embedder = _make_mock_pipeline()

    with pytest.raises(AssertionError, match="scope_filter must be None in graph-mode paths"):
        await pipeline._search_graph_mode(
            "naive", "my_collection", "test query",
            scope_filter="user:alice",
        )


@pytest.mark.asyncio
async def test_graph_mode_defensive_assertion_fires_explain_naive():
    """_explain_naive_graph_candidates raises AssertionError when scope_filter is non-None."""
    pipeline, store, embedder = _make_mock_pipeline()

    with pytest.raises(AssertionError, match="scope_filter must be None in graph-mode paths"):
        await pipeline._explain_naive_graph_candidates(
            "test query", "my_collection",
            embedder=embedder,
            scope_filter="user:alice",
        )


@pytest.mark.asyncio
async def test_graph_mode_defensive_assertion_fires_explain_merge_and_rank():
    """_explain_merge_and_rank asserts scope_filter is None (graph-mode invariant)."""
    pipeline, store, embedder = _make_mock_pipeline()
    dummy_candidate = _make_candidate()

    with pytest.raises(AssertionError, match="scope_filter must be None in graph-mode paths"):
        await pipeline._explain_merge_and_rank(
            [dummy_candidate], "test query", "col_a",
            top_k=5, rerank=False, namespace="default",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            embedder=embedder,
            graph_mode="naive",
            scope_filter="user:alice",
        )


# ---------------------------------------------------------------------------
# Integration tests — real pipeline + store
# ---------------------------------------------------------------------------


async def _make_real_pipeline(tmp_path: Path):
    """Build a real SearchStore + SearchPipeline for scope_filter integration tests.

    Returns (store, pipeline).
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=20,
        top_k_return=10,
    )
    return store, pipeline


async def _ingest_scoped_doc(
    pipeline,
    tmp_path: Path,
    col_name: str,
    text: str,
    filename: str,
    scopes: list[str] | None,
) -> None:
    """Write text to a temp file and ingest it with given scopes."""
    f = tmp_path / filename
    f.write_text(text, encoding="utf-8")
    embedder = pipeline._global_embedder
    await pipeline.ingest_file(
        f, col_name, embedder=embedder, chunk_scopes=scopes
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_search_with_context_scope_filter_applied(tmp_path, monkeypatch):
    """Integration S8: search_with_context with scope_filter='user:alice' returns
    only alice-scoped and unscoped chunks; bob-scoped chunk is absent.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    store, pipeline = await _make_real_pipeline(tmp_path)
    col = "scope_swc_col"
    embedder = pipeline._global_embedder

    # Ingest three docs: alice-scoped, bob-scoped, unscoped
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("alice document content " * 20),
        "alice.txt", scopes=["user:alice"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("bob document content " * 20),
        "bob.txt", scopes=["user:bob"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("shared document content " * 20),
        "shared.txt", scopes=None,
    )

    result = await pipeline.search_with_context(
        "document content", col,
        embedder=embedder,
        scope_filter="user:alice",
    )

    assert result.pipeline_result.results, "Expected at least one result"
    for r in result.pipeline_result.results:
        source = r.source_path
        assert "bob.txt" not in source, (
            f"bob-scoped chunk should be excluded by scope_filter='user:alice', got {source!r}"
        )

    sources = {r.source_path for r in result.pipeline_result.results}
    assert any("alice.txt" in s for s in sources) or any("shared.txt" in s for s in sources), (
        "Expected at least one alice or shared chunk in results"
    )

    await store.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_search_many_scope_filter_filters_across_collections(
    tmp_path, monkeypatch
):
    """Integration: search_many with scope_filter='user:alice' — only alice-scoped and
    unscoped chunks appear across two collections; bob-scoped are excluded.
    """
    from archon_search.collection_meta import CollectionMeta

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    store, pipeline = await _make_real_pipeline(tmp_path)
    col_a = "smany_col_a"
    col_b = "smany_col_b"
    embedder = pipeline._global_embedder

    # Col A: alice + unscoped
    await _ingest_scoped_doc(
        pipeline, tmp_path, col_a,
        ("alice data alpha " * 20),
        "alice_a.txt", scopes=["user:alice"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col_a,
        ("shared data alpha " * 20),
        "shared_a.txt", scopes=None,
    )

    # Col B: bob + unscoped
    await _ingest_scoped_doc(
        pipeline, tmp_path, col_b,
        ("bob data beta " * 20),
        "bob_b.txt", scopes=["user:bob"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col_b,
        ("shared data beta " * 20),
        "shared_b.txt", scopes=None,
    )

    result = await pipeline.search_many(
        "data", [col_a, col_b],
        scope_filter="user:alice",
    )

    assert result.results, "Expected at least one result"
    for r in result.results:
        assert "bob_b.txt" not in r.source_path, (
            f"bob-scoped chunk must be excluded, got {r.source_path!r}"
        )

    await store.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_search_scope_exact_returns_only_matching(tmp_path, monkeypatch):
    """Integration S8: exact scope match returns alice-scoped AND unscoped; bob excluded."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    store, pipeline = await _make_real_pipeline(tmp_path)
    col = "scope_exact_col"
    embedder = pipeline._global_embedder

    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("alice exact content " * 20),
        "alice.txt", scopes=["user:alice"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("bob exact content " * 20),
        "bob.txt", scopes=["user:bob"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("shared exact content " * 20),
        "shared.txt", scopes=None,
    )

    result = await pipeline.search(
        "exact content", col,
        embedder=embedder,
        scope_filter="user:alice",
    )

    assert result.results, "Expected at least one result"
    for r in result.results:
        assert "bob.txt" not in r.source_path, (
            f"bob-scoped chunk excluded by exact scope_filter='user:alice', got {r.source_path!r}"
        )

    await store.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_search_scope_wildcard_returns_prefix_matching(tmp_path, monkeypatch):
    """Integration S9: wildcard scope 'user:alice*' matches alice and alice:thread; excludes bob; includes unscoped.

    Scope 'user:alice*' has prefix 'user:alice' so:
    - scopes=['user:alice']        → passes ('user:alice' starts with 'user:alice')
    - scopes=['user:alice:thread'] → passes ('user:alice:thread' starts with 'user:alice')
    - scopes=['user:bob']          → excluded ('user:bob' does NOT start with 'user:alice')
    - scopes=None                  → passes (unscoped/shared)
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    store, pipeline = await _make_real_pipeline(tmp_path)
    col = "scope_wild_col"
    embedder = pipeline._global_embedder

    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("alice wildcard content " * 20),
        "alice.txt", scopes=["user:alice"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("alice thread content " * 20),
        "alice_thread.txt", scopes=["user:alice:thread"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("bob wildcard content " * 20),
        "bob.txt", scopes=["user:bob"],
    )
    await _ingest_scoped_doc(
        pipeline, tmp_path, col,
        ("shared wildcard content " * 20),
        "shared.txt", scopes=None,
    )

    result = await pipeline.search(
        "wildcard content", col,
        embedder=embedder,
        scope_filter="user:alice*",
    )

    assert result.results, "Expected at least one result"
    for r in result.results:
        assert "bob.txt" not in r.source_path, (
            f"bob-scoped chunk excluded by wildcard scope_filter='user:alice*', got {r.source_path!r}"
        )

    await store.disconnect()
