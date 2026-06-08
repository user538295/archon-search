"""Live E2E tests for RAG Fusion — real fastembed + real Anthropic API.

Requires:
  - Real fastembed model weights (BAAI/bge-small-en-v1.5)
  - ANTHROPIC_API_KEY set in the environment
  - anthropic package installed (archon-search[rag_fusion])

Run with:
  uv run pytest -m live_eval tests/eval/live/test_live_rag_fusion.py -v --no-cov

Checkpoint before merge: at least test_live_rag_fusion_returns_applied_true and
test_live_rag_fusion_recall_at_5_meets_floor must pass with a real API key.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

from archon_search.eval.fixtures import build_doc_collection_map, load_eval_corpus
from archon_search.eval.runner import (
    _build_pipeline_with_eval_backends,
    _ingest_corpus,
    load_thresholds,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_EVAL_CORPUS_ROOT = Path(__file__).resolve().parent.parent
_THRESHOLDS_PATH = _EVAL_CORPUS_ROOT / "thresholds.toml"
_COLLECTION = "docs"  # A collection present in the committed eval corpus


def _skip_if_no_api_key() -> None:
    """Skip the test if ANTHROPIC_API_KEY is not set."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — skipping live RAG Fusion test")


def _skip_if_anthropic_not_installed() -> None:
    """Skip the test if the anthropic package is not installed."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        pytest.skip("anthropic package not installed — run: pip install archon-search[rag_fusion]")


# ---------------------------------------------------------------------------
# Helper: build a real pipeline backed by real models + ingest the corpus
# ---------------------------------------------------------------------------


async def _build_live_pipeline(tmp_path: Path):
    """Return a pipeline with real fastembed and the committed eval corpus ingested."""
    pipeline = await _build_pipeline_with_eval_backends(tmp_path, backend="live")
    corpus = load_eval_corpus(_EVAL_CORPUS_ROOT)
    await _ingest_corpus(pipeline, _EVAL_CORPUS_ROOT, corpus)
    return pipeline


# ---------------------------------------------------------------------------
# Helper: get a MCP tool function using the integration test stub pattern
# ---------------------------------------------------------------------------

class _StubFastMCP:
    """Minimal FastMCP stub that records registered tools."""

    def __init__(self, *args, **kwargs):
        self._tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self._tools[name] = fn
            return fn
        return decorator

    def __call__(self, *args, **kwargs):
        return self


def _get_mcp_tool_fn(tool_name: str, pipeline, config, rf_generator=None, writer=None):
    """Build a stub-backed MCP app with the real pipeline and return the named tool.

    Mirrors the pattern from tests/test_integration_rag_fusion.py to avoid using
    FastMCP private internals.
    """
    _fastmcp_module = "fastmcp"
    _prev = sys.modules.get(_fastmcp_module)
    _prev_class = getattr(_prev, "FastMCP", None) if _prev else None

    if _fastmcp_module not in sys.modules:
        _mod = types.ModuleType(_fastmcp_module)
        _mod.FastMCP = _StubFastMCP  # type: ignore[attr-defined]
        _mod.Context = type("Context", (), {})  # type: ignore[attr-defined]
        sys.modules[_fastmcp_module] = _mod
    else:
        sys.modules[_fastmcp_module].FastMCP = _StubFastMCP  # type: ignore[attr-defined]

    _mcp_module = "archon_search.server.mcp"
    sys.modules.pop(_mcp_module, None)
    mcp_mod = importlib.import_module(_mcp_module)

    app = mcp_mod.create_app(
        pipeline,
        _COLLECTION,
        config=config,
        rag_fusion_generator=rf_generator,
        writer=writer,
    )

    if _prev_class is not None:
        sys.modules[_fastmcp_module].FastMCP = _prev_class  # type: ignore[attr-defined]
    sys.modules.pop(_mcp_module, None)

    return app._tools[tool_name]


# ---------------------------------------------------------------------------
# Test 1 — live pipeline returns rag_fusion_applied=True with a real LLM call
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_rag_fusion_returns_applied_true(tmp_path: Path) -> None:
    """pipeline.search with rag_fusion=True and a real generator returns rag_fusion_applied=True
    and rag_fusion_queries_used >= 1 (at least one real variant was generated and searched).
    """
    _skip_if_no_api_key()
    _skip_if_anthropic_not_installed()

    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator

    pipeline = await _build_live_pipeline(tmp_path)
    try:
        config = RAGFusionConfig(enabled=True, num_queries=2)
        generator = RAGFusionGenerator(config)

        result = await pipeline.search(
            "How does the search pipeline work?",
            collection=_COLLECTION,
            embedder=pipeline._global_embedder,
            rag_fusion=True,
            rag_fusion_generator=generator,
            rag_fusion_config=config,
        )
    finally:
        await pipeline.store.disconnect()

    assert result.rag_fusion_applied is True, (
        f"Expected rag_fusion_applied=True, got {result.rag_fusion_applied}. "
        f"rag_fusion_attempted={result.rag_fusion_attempted}, "
        f"rag_fusion_queries_used={result.rag_fusion_queries_used}"
    )
    assert result.rag_fusion_queries_used >= 1, (
        f"Expected at least one successful LLM-generated variant, "
        f"got rag_fusion_queries_used={result.rag_fusion_queries_used}"
    )


# ---------------------------------------------------------------------------
# Test 2 — variants from real LLM surface semantically different documents
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_rag_fusion_variants_are_semantically_different(tmp_path: Path) -> None:
    """Via explain path: sub_query_results is populated with ≥2 entries and the
    real LLM generates semantically distinct queries.
    If all variants return identical top docs, a warning is emitted (not a failure).
    """
    _skip_if_no_api_key()
    _skip_if_anthropic_not_installed()

    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator

    pipeline = await _build_live_pipeline(tmp_path)
    try:
        config = RAGFusionConfig(enabled=True, num_queries=2)
        generator = RAGFusionGenerator(config)

        result = await pipeline.explain(
            "document retrieval and semantic search",
            collection=_COLLECTION,
            embedder=pipeline._global_embedder,
            rag_fusion=True,
            rag_fusion_generator=generator,
            rag_fusion_config=config,
        )
    finally:
        await pipeline.store.disconnect()

    assert result.rag_fusion_applied is True, (
        f"Expected rag_fusion_applied=True; rag_fusion_attempted={result.rag_fusion_attempted}"
    )
    sub_results = result.rag_fusion_sub_query_results
    assert sub_results is not None, "Expected rag_fusion_sub_query_results to be populated"
    assert len(sub_results) >= 2, (
        f"Expected at least 2 sub-query result entries (original + ≥1 variant), got {len(sub_results)}"
    )

    # Check for semantic diversity — warning only if all top docs identical
    all_top_ids = [frozenset(r.top_doc_ids[:3]) for r in sub_results if r.top_doc_ids]
    if len(all_top_ids) >= 2 and all(s == all_top_ids[0] for s in all_top_ids):
        import warnings
        warnings.warn(
            "All RAG Fusion variants returned identical top documents — "
            "semantic diversity not confirmed for this corpus/query combination.",
            stacklevel=1,
        )


# ---------------------------------------------------------------------------
# Test 3 — recall@5 meets floor with rag_fusion=True on each query
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_rag_fusion_recall_at_5_meets_floor(tmp_path: Path) -> None:
    """Run the committed eval query set with rag_fusion=True and real generator.

    Iterates over retrieval-scope queries in the committed eval corpus, calls
    pipeline.search() with rag_fusion=True for each, maps result source paths
    to fixture doc_ids, and computes macro-averaged recall@5 against committed
    labels. Asserts recall@5 >= quality_floors.recall_at_5 from thresholds.toml.

    This is the only test that can measure whether RAG Fusion actually improves
    recall over the single-query baseline — the deterministic eval backend in
    Task 6.3 cannot.
    """
    _skip_if_no_api_key()
    _skip_if_anthropic_not_installed()

    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator

    pipeline = await _build_live_pipeline(tmp_path)
    corpus = load_eval_corpus(_EVAL_CORPUS_ROOT)
    thresholds = load_thresholds(_THRESHOLDS_PATH)
    floor = thresholds.quality_floors.recall_at_5
    path_to_fixture = build_doc_collection_map(corpus)
    corpus_dir = (_EVAL_CORPUS_ROOT / "corpus").resolve()

    # Build label index: query_id -> set of positive doc_ids
    label_index: dict[str, set[str]] = {}
    for label in corpus.labels:
        if getattr(label, "grade", 1) > 0:
            label_index.setdefault(label.query_id, set()).add(label.doc_id)

    config = RAGFusionConfig(enabled=True, num_queries=2)
    generator = RAGFusionGenerator(config)

    per_query_recalls: list[float] = []
    try:
        for query in corpus.queries:
            if query.metric_scope != "retrieval" or query.collection is None:
                continue

            result = await pipeline.search(
                query.text,
                collection=query.collection,
                embedder=pipeline._global_embedder,
                rag_fusion=True,
                rag_fusion_generator=generator,
                rag_fusion_config=config,
            )

            # Map result source_paths to fixture doc_ids
            retrieved_doc_ids: list[str] = []
            seen: set[str] = set()
            for sr in result.results:
                src = Path(sr.source_path)
                try:
                    rel = str(src.relative_to(corpus_dir))
                except ValueError:
                    continue
                fixture_entry = path_to_fixture.get(rel)
                if fixture_entry:
                    doc_id, _ = fixture_entry
                    if doc_id not in seen:
                        seen.add(doc_id)
                        retrieved_doc_ids.append(doc_id)

            gold = label_index.get(query.query_id, set())
            if not gold:
                continue

            hits = sum(1 for d in retrieved_doc_ids[:5] if d in gold)
            per_query_recalls.append(hits / len(gold))
    finally:
        await pipeline.store.disconnect()

    assert per_query_recalls, "No retrieval queries were evaluated — check corpus fixture"
    recall_at_5 = sum(per_query_recalls) / len(per_query_recalls)

    assert recall_at_5 >= floor, (
        f"RAG Fusion recall@5 {recall_at_5:.4f} is below the floor {floor:.4f} "
        f"(evaluated {len(per_query_recalls)} queries). "
        "RAG Fusion should not regress baseline recall."
    )


# ---------------------------------------------------------------------------
# Test 4 — silent fallback when ANTHROPIC_API_KEY is missing
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_rag_fusion_fallback_on_missing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ANTHROPIC_API_KEY unset, pipeline.search falls back silently:
    rag_fusion_applied=False, rag_fusion_attempted=True.

    The pipeline and corpus are built BEFORE the API key is deleted to avoid
    spurious failures in setup code that may also use ANTHROPIC_API_KEY.
    """
    _skip_if_anthropic_not_installed()

    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator

    # Build pipeline and ingest corpus BEFORE removing the key
    pipeline = await _build_live_pipeline(tmp_path)

    # Remove the API key only for the actual search call
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    try:
        config = RAGFusionConfig(enabled=True, num_queries=2)
        generator = RAGFusionGenerator(config)

        result = await pipeline.search(
            "test query for fallback verification",
            collection=_COLLECTION,
            embedder=pipeline._global_embedder,
            rag_fusion=True,
            rag_fusion_generator=generator,
            rag_fusion_config=config,
        )
    finally:
        await pipeline.store.disconnect()

    assert result.rag_fusion_applied is False, (
        f"Expected rag_fusion_applied=False (no API key), got {result.rag_fusion_applied}"
    )
    assert result.rag_fusion_attempted is True, (
        f"Expected rag_fusion_attempted=True (generator was called), got {result.rag_fusion_attempted}"
    )
    assert result.rag_fusion_queries_used == 0, (
        f"Expected rag_fusion_queries_used=0 (fallback), got {result.rag_fusion_queries_used}"
    )


# ---------------------------------------------------------------------------
# Test 5 — full HTTP stack: TestClient + real LLM + real store
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_search_with_context_rag_fusion(tmp_path: Path) -> None:
    """Full HTTP stack: POST /search with rag_fusion=True and real generator returns
    200, rag_fusion_applied=true, and results list present (may be empty on short corpus).
    """
    _skip_if_no_api_key()
    _skip_if_anthropic_not_installed()

    from fastapi.testclient import TestClient

    from archon_search.config import RAGFusionConfig, SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    # Ingest one document into the live pipeline's store so results can be returned
    config = SearchConfig()
    config.rag_fusion = RAGFusionConfig(enabled=True, num_queries=2)
    config.paths.data_dir = str(tmp_path)

    app = create_app(config, JobStore(tmp_path / "jobs.db"))

    with TestClient(app, headers={"Authorization": f"Bearer {app.state.api_key}"}) as client:
        # Ingest a document through the HTTP API first
        corpus_dir = _EVAL_CORPUS_ROOT / "corpus"
        sample_files = list(corpus_dir.rglob("*.md"))[:1]
        if not sample_files:
            sample_files = list(corpus_dir.rglob("*.txt"))[:1]
        if not sample_files:
            pytest.skip("No corpus files found for live HTTP test")

        ingest_resp = client.post(
            "/ingest/file",
            json={"path": str(sample_files[0]), "collection": _COLLECTION},
        )
        assert ingest_resp.status_code in (200, 202), (
            f"Ingest failed: {ingest_resp.status_code} {ingest_resp.text}"
        )

        # Now search with rag_fusion=True
        resp = client.post(
            "/search",
            json={
                "query": "document search pipeline",
                "collection": _COLLECTION,
                "rag_fusion": True,
            },
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("rag_fusion_applied") is True, (
        f"Expected rag_fusion_applied=true in response; got: {data}"
    )
    assert "results" in data, f"Expected 'results' key in response; got: {data}"
    # results may be empty on a single-document corpus but the key must be present


# ---------------------------------------------------------------------------
# Test 6 — MCP search tool with real LLM returns rag_fusion_applied=True
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_mcp_search_rag_fusion(tmp_path: Path) -> None:
    """MCP search tool with rag_fusion=True and real generator returns
    rag_fusion_applied=True, rag_fusion_queries_used >= 1.

    Uses the _get_mcp_tool_fn stub pattern (same as test_integration_rag_fusion.py)
    to avoid relying on FastMCP private internals.
    """
    _skip_if_no_api_key()
    _skip_if_anthropic_not_installed()

    from fastapi.testclient import TestClient

    from archon_search.config import RAGFusionConfig, SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    config = SearchConfig()
    config.rag_fusion = RAGFusionConfig(enabled=True, num_queries=2)
    config.paths.data_dir = str(tmp_path)

    app = create_app(config, JobStore(tmp_path / "jobs.db"))

    # Use TestClient context to initialize the app and pipeline, then extract refs
    # The tool invocation happens INSIDE the context so the store stays connected.
    with TestClient(app, headers={"Authorization": f"Bearer {app.state.api_key}"}) as client:
        # Ingest a sample file so the store is populated
        corpus_dir = _EVAL_CORPUS_ROOT / "corpus"
        sample_files = list(corpus_dir.rglob("*.md"))[:1]
        if not sample_files:
            pytest.skip("No corpus files found for live MCP test")

        ingest_resp = client.post(
            "/ingest/file",
            json={"path": str(sample_files[0]), "collection": _COLLECTION},
        )
        assert ingest_resp.status_code in (200, 202)

        pipeline = app.state.pipeline
        rf_generator = app.state.rag_fusion_generator

        # Invoke the MCP tool function inside the context so the store remains connected
        tool_fn = _get_mcp_tool_fn("search", pipeline, config, rf_generator=rf_generator)
        result = await tool_fn(
            query="document retrieval architecture",
            collection=_COLLECTION,
            rag_fusion=True,
        )

    assert isinstance(result, dict), f"Expected dict result, got {type(result)}: {result}"
    assert result.get("rag_fusion_applied") is True, (
        f"Expected rag_fusion_applied=True in MCP result; got: {result}"
    )
    assert result.get("rag_fusion_queries_used", 0) >= 1, (
        f"Expected rag_fusion_queries_used >= 1; got: {result}"
    )
