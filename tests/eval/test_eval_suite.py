"""End-to-end report-only eval smoke tests
These tests exercise the full eval pipeline against the committed corpus and
runtime config in calibration (report-only) mode. They never call
``assert_thresholds`` — that is the job of gated tests.

The ``eval`` marker excludes these from the default pytest run; invoke them
with ``-m eval`` (or ``pytest tests/eval/``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.eval.runner import assert_thresholds, render_report, run_eval_suite


CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_PATH = CORPUS_ROOT / "runtime.toml"
BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"


_QUALITY_METRIC_FIELDS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
    "routing_accuracy",
)


@pytest.mark.eval
async def test_eval_suite_report_only_smoke() -> None:
    """Full eval suite runs end-to-end against the committed corpus and renders
    a non-empty report — without calling ``assert_thresholds``."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    rendered = render_report(report)
    assert rendered and rendered.strip(), (
        f"render_report returned empty output.\nReport:\n{rendered!r}"
    )


@pytest.mark.eval
async def test_eval_suite_report_only_does_not_assert_thresholds() -> None:
    """Calibration mode (thresholds=None) never raises from missing floors:
    the suite executes, the report renders, and ``report.thresholds`` is None."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    assert report.thresholds is None, (
        f"Expected report.thresholds to be None in calibration mode, "
        f"got {report.thresholds!r}"
    )

    rendered = render_report(report)
    assert rendered and rendered.strip(), (
        f"render_report returned empty output.\nReport:\n{rendered!r}"
    )


@pytest.mark.eval
async def test_eval_suite_is_deterministic_except_latency() -> None:
    """Two fresh runs of the eval suite produce identical quality metrics and
    identical ranked result orderings. Latency percentiles are excluded from
    the comparison because they are wall-clock measurements."""
    report1 = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )
    report2 = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    rendered1 = render_report(report1)
    rendered2 = render_report(report2)

    for field in _QUALITY_METRIC_FIELDS:
        v1 = getattr(report1.metrics, field)
        v2 = getattr(report2.metrics, field)
        assert v1 == v2, (
            f"Quality metric {field!r} differs between runs: {v1!r} vs {v2!r}\n"
            f"--- run 1 report ---\n{rendered1}\n"
            f"--- run 2 report ---\n{rendered2}"
        )

    def ordering(report) -> dict[str, list[str]]:
        return {t.query_id: [r.doc_id for r in t.results] for t in report.traces}

    o1, o2 = ordering(report1), ordering(report2)
    assert o1 == o2, (
        f"Result orderings differ between runs.\n"
        f"--- run 1 report ---\n{rendered1}\n"
        f"--- run 2 report ---\n{rendered2}"
    )


# ---------------------------------------------------------------------------
# gated eval smoke tests
# ---------------------------------------------------------------------------


def _write_baseline(path: Path, base: dict, **overrides) -> Path:
    """Write a copy of *base* with *overrides* applied to *path*."""
    data = dict(base)
    data.update(overrides)
    path.write_text(json.dumps(data))
    return path


@pytest.mark.eval
async def test_eval_suite_gated_smoke(thresholds_path: Path) -> None:
    """Gated suite runs end-to-end against committed thresholds + baseline and
    ``assert_thresholds`` does not raise."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    assert_thresholds(report)  # must not raise


@pytest.mark.eval
async def test_eval_suite_gated_smoke_reports_baseline_deltas(
    thresholds_path: Path,
) -> None:
    """Rendered report contains baseline delta lines."""
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    rendered = render_report(report).lower()
    assert "baseline" in rendered
    assert "delta" in rendered


@pytest.mark.eval
async def test_eval_suite_gated_smoke_rejects_stale_benchmark_or_threshold_hashes(
    thresholds_path: Path,
    tmp_path: Path,
) -> None:
    """A baseline with a stale ``thresholds_hash`` (mismatching the current
    thresholds.toml) fails gating with an explicit refresh message."""
    base = json.loads(BASELINE_JSON.read_text())
    stale = _write_baseline(
        tmp_path / "baseline.json",
        base,
        thresholds_hash="0" * 64,  # obviously wrong
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=stale,
    )
    with pytest.raises(AssertionError, match="(?i)stale|refresh|hash"):
        assert_thresholds(report)


@pytest.mark.eval
async def test_eval_suite_gated_smoke_rejects_calibration_only_baseline(
    thresholds_path: Path,
    tmp_path: Path,
) -> None:
    """Baseline with ``thresholds_hash: null`` fails gating with refresh message."""
    base = json.loads(BASELINE_JSON.read_text())
    calibration_only = _write_baseline(
        tmp_path / "baseline.json",
        base,
        thresholds_hash=None,
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=calibration_only,
    )
    with pytest.raises(AssertionError, match="(?i)calibration|refresh"):
        assert_thresholds(report)


@pytest.mark.eval
async def test_eval_suite_report_only_accepts_calibration_baseline_without_thresholds(
    tmp_path: Path,
) -> None:
    """In report-only mode (no thresholds), a calibration-only baseline is
    accepted: ``run_eval_suite`` succeeds and renders deltas; we do NOT call
    ``assert_thresholds``."""
    base = json.loads(BASELINE_JSON.read_text())
    calibration_only = _write_baseline(
        tmp_path / "baseline.json",
        base,
        thresholds_hash=None,
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=calibration_only,
    )
    assert report.thresholds is None
    assert report.baseline is not None
    assert report.baseline.thresholds_hash is None
    rendered = render_report(report).lower()
    assert "baseline" in rendered
    assert "delta" in rendered


@pytest.mark.eval
async def test_eval_suite_gated_smoke_rejects_stale_eval_hash(
    thresholds_path: Path,
    tmp_path: Path,
) -> None:
    """A baseline with an obviously-wrong ``eval_hash`` fails gating."""
    base = json.loads(BASELINE_JSON.read_text())
    stale = _write_baseline(
        tmp_path / "baseline.json",
        base,
        eval_hash="0" * 64,
    )
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=stale,
    )
    with pytest.raises(AssertionError, match="(?i)stale|refresh|hash"):
        assert_thresholds(report)


# ---------------------------------------------------------------------------
# C2 multilingual fixtures test
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_recall_at_5_multilingual_fr(thresholds_path: Path) -> None:
    """Eval harness asserts recall@5 on French (fr-docs) fixtures >= threshold.

    Runs the full eval suite, filters traces to the fr-docs collection, computes
    recall@5 on that sub-corpus, and asserts it meets the floor from
    thresholds.toml [multilingual].recall_at_5_fr.

    Uses the deterministic eval backend (SHA-256 token hashing) — no real
    model weights needed.  The backend is corpus-aware and label-blind, so
    French tokens produce distinct vectors from English tokens.
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    from archon_search.eval.fixtures import load_eval_corpus
    from archon_search.eval.metrics import compute_recall_at_k

    # Load the recall_at_5_fr floor from thresholds.toml [multilingual] table
    thresholds_data = tomllib.loads(thresholds_path.read_text())
    multilingual = thresholds_data.get("multilingual", {})
    floor = multilingual.get("recall_at_5_fr")
    assert floor is not None, (
        "thresholds.toml missing [multilingual].recall_at_5_fr — "
        "add it after calibrating fr-docs recall@5"
    )

    corpus = load_eval_corpus(CORPUS_ROOT)

    # Verify fr-docs collection exists with at least 5 documents
    fr_docs = [d for d in corpus.documents if d.collection == "fr-docs"]
    assert len(fr_docs) >= 5, (
        f"Expected at least 5 French documents, got {len(fr_docs)}"
    )

    # Verify 5 retrieval-scope queries targeting fr-docs
    fr_queries = [
        q for q in corpus.queries
        if q.collection == "fr-docs" and q.metric_scope == "retrieval"
    ]
    assert len(fr_queries) >= 5, (
        f"Expected at least 5 French retrieval queries, got {len(fr_queries)}"
    )

    # Run the eval suite and compute per-collection recall@5 for fr-docs
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )

    # Filter retrieval traces to fr-docs collection only
    fr_traces = [
        t for t in report.traces
        if t.collection == "fr-docs" and t.metric_scope == "retrieval"
    ]
    assert fr_traces, "No retrieval traces found for fr-docs collection"

    # Filter labels to fr-docs queries
    fr_query_ids = {t.query_id for t in fr_traces}
    fr_labels = [
        lbl for lbl in corpus.labels
        if lbl.query_id in fr_query_ids
    ]

    # Compute recall@5 on the fr-docs sub-corpus
    recall_at_5_fr = compute_recall_at_k(fr_traces, fr_labels, k=5)

    assert recall_at_5_fr >= floor, (
        f"recall@5 on fr-docs ({recall_at_5_fr:.4f}) is below the floor "
        f"({floor}) from thresholds.toml [multilingual].recall_at_5_fr"
    )


@pytest.mark.eval
async def test_multilingual_fr_fixtures_load_cleanly(thresholds_path: Path) -> None:
    """Full eval suite runs end-to-end with multilingual fr-docs fixtures.

    Verifies the fixtures are consistent and the eval harness produces a
    non-empty report that includes fr-docs metrics.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    rendered = render_report(report)
    assert rendered and rendered.strip(), "render_report returned empty output"

    # The report must include all queries (now including fr-docs queries)
    assert report.metrics.recall_at_5 is not None, "recall_at_5 not computed"
    assert report.metrics.recall_at_1 is not None, "recall_at_1 not computed"


# ---------------------------------------------------------------------------
# C1 per-collection dispatch test
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_exercises_per_collection_dispatch(tmp_path: Path) -> None:
    """Verify per-collection embedder dispatch: two collections ingested with
    different EvalEmbedderBackend model names get distinct active_embedding_model
    values stored in CollectionMeta.

    This test verifies C1 dispatch is exercised and not silently bypassed. The
    assertion mirrors what the HTTP layer exposes as SearchResponse.embedding_model
    (populated from meta.active_embedding_model in routes_search.py).

    Implementation note: EvalEmbedderBackend.encode() produces identical vectors
    regardless of model_name (SHA-256 token hashing is name-agnostic). This test
    therefore validates metadata tracking only — that active_embedding_model is
    correctly stored and differs per collection. Automatic dispatch (pipeline
    looking up the embedder from the cache during search_many) is exercised by
    the integration test suite for C1.

    Data flow: ingest_file → ingest_chunks sets active_embedding_model from the
    passed embedder. recompute_collection_meta preserves the existing value when
    the collection already has a meta row — it does not reset model assignment.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    global_embedder = Embedder(EvalEmbedderBackend("eval-sha256-v1"))
    alt_embedder = Embedder(EvalEmbedderBackend("eval-sha256-alt-v1"))
    reranker = Reranker(EvalRerankerBackend())

    store = SearchStore(tmp_path / "lancedb")
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=global_embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=256),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=10,
    )

    try:
        # Create minimal corpus documents for each collection.
        global_doc = tmp_path / "global_doc.txt"
        global_doc.write_text(
            "Cosine similarity measures the angle between two vectors. "
            "Vector search indexes use HNSW or IVF for approximate nearest neighbours."
        )
        alt_doc = tmp_path / "alt_doc.txt"
        alt_doc.write_text(
            "Transformer self-attention computes query key value projections. "
            "Multi-head attention allows the model to attend to different representation subspaces."
        )

        # Ingest each collection with its respective embedder.
        # ingest_file → ingest_chunks records embedder.model_name as active_embedding_model.
        # recompute_collection_meta preserves the existing value (used here to build centroid).
        await pipeline.ingest_file(
            global_doc, "global-col", rebuild_fts=False, embedder=global_embedder
        )
        await pipeline.store.rebuild_fts_index("global-col")
        await pipeline.recompute_collection_meta("global-col", global_embedder)

        await pipeline.ingest_file(
            alt_doc, "alt-col", rebuild_fts=False, embedder=alt_embedder
        )
        await pipeline.store.rebuild_fts_index("alt-col")
        await pipeline.recompute_collection_meta("alt-col", alt_embedder)

        # Verify active_embedding_model is stored per-collection.
        global_meta = await pipeline.store.get_collection_meta("global-col")
        alt_meta = await pipeline.store.get_collection_meta("alt-col")

        assert global_meta is not None, "CollectionMeta missing for global-col"
        assert alt_meta is not None, "CollectionMeta missing for alt-col"
        assert global_meta.active_embedding_model == "eval-sha256-v1", (
            f"global-col active_embedding_model={global_meta.active_embedding_model!r}, "
            "expected 'eval-sha256-v1'"
        )
        assert alt_meta.active_embedding_model == "eval-sha256-alt-v1", (
            f"alt-col active_embedding_model={alt_meta.active_embedding_model!r}, "
            "expected 'eval-sha256-alt-v1'"
        )
        # The key assertion: models differ between the two collections.
        assert global_meta.active_embedding_model != alt_meta.active_embedding_model, (
            "Per-collection dispatch not exercised: both collections report the same "
            f"active_embedding_model={global_meta.active_embedding_model!r}"
        )

        # Run search on both collections to confirm dispatch works end-to-end.
        global_results = await pipeline.search(
            "cosine similarity vector search", "global-col", embedder=global_embedder
        )
        alt_results = await pipeline.search(
            "multi-head attention transformer", "alt-col", embedder=alt_embedder
        )
        assert global_results.results, "Expected search results from global-col"
        assert alt_results.results, "Expected search results from alt-col"
    finally:
        await pipeline.store.disconnect()


# ---------------------------------------------------------------------------
# C3b page provenance tests
# ---------------------------------------------------------------------------


def test_eval_includes_page_provenance_query() -> None:
    """queries.jsonl must include the page_provenance_001 query."""
    queries_file = CORPUS_ROOT / "queries.jsonl"
    query_ids = [
        json.loads(line)["query_id"]
        for line in queries_file.read_text().splitlines()
        if line.strip()
    ]
    assert "page_provenance_001" in query_ids, (
        "page_provenance_001 not found in queries.jsonl — "
        "add it per Task 5.2 of the C3b plan"
    )


@pytest.mark.eval
async def test_eval_page_provenance_pdf_has_page_metadata(tmp_path: Path) -> None:
    """The page_provenance_001 PDF document produces chunks with _page_start metadata after ingest.

    NOTE: With chunk_size=256, the fixture PDF's short content (~20 words total across 3 pages)
    fits in a single chunk starting at offset 0, so _page_start="1" not "2". This test verifies
    that page metadata IS present and is a string — not that it equals "2". The primary purpose
    is a coarse end-to-end ingest regression check; the _page_start=="2" boundary verification
    is covered by Task 4.2's unit tests with a forced small chunk_size.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    corpus_pdf = CORPUS_ROOT / "corpus" / "pdf-fixtures" / "three_page.pdf"
    assert corpus_pdf.exists(), (
        f"Eval corpus PDF not generated: {corpus_pdf}. "
        "The autouse session-scoped fixture in tests/eval/conftest.py "
        "should generate it before this test runs."
    )

    eval_backend = EvalEmbedderBackend()
    embedder = Embedder(eval_backend)
    reranker = Reranker(EvalRerankerBackend())
    store = SearchStore(tmp_path / "lancedb")
    await store.connect()
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=256),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=10,
    )
    try:
        result = await pipeline.ingest_file(
            corpus_pdf, "pdf-fixtures", rebuild_fts=True, embedder=embedder
        )
        assert result.error is None, f"Ingest failed: {result.error}"

        # Search for "beta content" — should land in page 2.
        # Use EvalEmbedderBackend.encode() directly to obtain the query vector
        # (mirrors the approach from Task 4.2's integration test spec).
        query_vec = eval_backend.encode(["beta content"])[0]
        results = await store.hybrid_search(
            collection="pdf-fixtures",
            query_vector=query_vec,
            query_text="beta content",
            top_k=10,
        )
        pdf_chunks = [r for r in results if any(
            kw in r.text.lower() for kw in ("alpha", "beta", "gamma")
        )]
        assert pdf_chunks, "No PDF content chunks found after ingest"
        # With chunk_size=256 (eval runner default), the short page content
        # ("alpha content", "beta content", "gamma content" ≈ 20 words total)
        # fits in a single chunk starting at offset 0 → _page_start="1".
        # The primary eval purpose is retrieval (the document IS found for the
        # page_provenance_001 query). We verify page metadata IS present — the
        # exact page number depends on chunk boundaries.
        for chunk in pdf_chunks:
            page = chunk.metadata.get("_page_start")
            assert page is not None, (
                f"_page_start missing from PDF chunk. Chunk text: {chunk.text!r}"
            )
            assert isinstance(page, str), (
                f"_page_start should be a string, got {type(page)!r}"
            )
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# C4 HyDE regression scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_hyde_regression_scenario(tmp_path: Path) -> None:
    """HyDE with a deterministic (query-derived) vector does not break recall on the
    committed corpus.

    Runs the committed retrieval queries through the full pipeline with a mocked
    HyDEGenerator whose ``generate()`` returns the same vector as the normal
    embedder (i.e., the query embedding itself).  This is the identity case:
    ``recall@5`` with HyDE must equal the baseline recall@5 because the vector
    used for ANN lookup is identical.

    Then also tests with ``resolve_hyde_vector(hyde=True, ...)`` to verify the
    full resolution chain (not just ``pipeline.search()`` directly).

    The deterministic embedder cannot measure semantic improvement; this scenario
    only verifies HyDE does not *break* recall.  Measuring recall *improvement*
    from HyDE requires ``@pytest.mark.live`` with real fastembed + real Claude
    API — not part of the default eval gate.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.config import HyDEConfig
    from archon_search.embedder import Embedder
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    from archon_search.eval.fixtures import build_doc_collection_map, load_eval_corpus
    from archon_search.eval.metrics import compute_recall_at_k
    from archon_search.eval.types import EvalSearchResult, QueryEvalTrace
    from archon_search.hyde import HyDEGenerator, resolve_hyde_vector
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    # Load committed corpus and quality floors
    corpus = load_eval_corpus(CORPUS_ROOT)
    thresholds_data = tomllib.loads((CORPUS_ROOT / "thresholds.toml").read_text())
    recall_at_5_floor: float = thresholds_data["quality_floors"]["recall_at_5"]

    eval_backend = EvalEmbedderBackend()
    embedder = Embedder(eval_backend)
    reranker = Reranker(EvalRerankerBackend())
    store = SearchStore(tmp_path / "lancedb")
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=256),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=10,
    )

    try:
        # Ingest the full committed corpus
        corpus_dir = (CORPUS_ROOT / "corpus").resolve()
        by_collection: dict[str, list] = {}
        for d in corpus.documents:
            by_collection.setdefault(d.collection, []).append(d)
        for collection, docs in by_collection.items():
            for d in docs:
                p = (CORPUS_ROOT / "corpus" / d.relative_path).resolve()
                r = await pipeline.ingest_file(
                    p, collection, rebuild_fts=False, embedder=embedder,
                    collection_root=corpus_dir,
                )
                assert r.error is None, f"Ingest failed for {d.doc_id}: {r.error}"
            await pipeline.store.rebuild_fts_index(collection)

        # Build fixture path → doc_id mapping for result translation
        path_to_fixture = build_doc_collection_map(corpus)

        from archon_search._diagnostics import SearchScoreBreakdown

        def _map_result(raw, corpus_root):
            corpus_d = (corpus_root / "corpus").resolve()
            try:
                rel = str(Path(raw.source_path).resolve().relative_to(corpus_d))
            except ValueError:
                rel = raw.source_path
            entry = path_to_fixture.get(rel)
            if entry is None:
                return None
            fixture_doc_id, _ = entry
            return EvalSearchResult(
                doc_id=fixture_doc_id,
                runtime_doc_id=raw.doc_id,
                chunk_id=raw.chunk_id,
                text=raw.text,
                source_path=raw.source_path,
                collection=raw.collection,
                score_breakdown=SearchScoreBreakdown(
                    vector_rank=None,
                    vector_score=None,
                    vector_score_kind=None,
                    fts_rank=None,
                    fts_score=None,
                    fts_score_kind=None,
                    rrf_score=raw.score,
                    reranker_score=None,
                ),
            )

        # Mock HyDEGenerator: returns the query's own embedding (identity case)
        # This ensures recall@5 == baseline (no degradation, no improvement).
        from unittest.mock import AsyncMock, MagicMock

        hyde_config = HyDEConfig(enabled=True, max_requests_per_minute=60)
        mock_generator = MagicMock(spec=HyDEGenerator)

        async def _identity_generate(query: str) -> list[float]:
            return await embedder.embed_one(query)

        mock_generator.generate = AsyncMock(side_effect=_identity_generate)

        # Run retrieval queries with HyDE (identity vector) and collect traces
        retrieval_queries = [
            q for q in corpus.queries if q.metric_scope == "retrieval"
        ]

        hyde_traces: list[QueryEvalTrace] = []
        for q in retrieval_queries:
            assert q.collection is not None
            # Full HyDE resolution chain: resolve_hyde_vector → pipeline.search
            hyde_vector, hyde_applied = await resolve_hyde_vector(
                q.text, True, mock_generator, hyde_config
            )
            assert hyde_applied is True, (
                f"Expected hyde_applied=True for query {q.query_id!r}, "
                f"got {hyde_applied!r} (generator returned None)"
            )
            result = await pipeline.search(
                q.text,
                q.collection,
                embedder=embedder,
                query_vector=hyde_vector,
            )
            # Map chunk results back to fixture doc_ids
            mapped = []
            for r in result.results:
                mapped_r = _map_result(r, CORPUS_ROOT)
                if mapped_r is not None:
                    mapped.append(mapped_r)
            hyde_traces.append(
                QueryEvalTrace(
                    query_id=q.query_id,
                    query_text=q.text,
                    collection=q.collection,
                    metric_scope="retrieval",
                    results=mapped,
                )
            )

        # Compute recall@5 on the committed corpus with HyDE (identity vector)
        recall_at_5_hyde = compute_recall_at_k(hyde_traces, corpus.labels, k=5)

        # The allowed regression is the same as max_floor_drop_without_waiver
        allowed_regression: float = thresholds_data.get("policy", {}).get(
            "max_floor_drop_without_waiver", 0.05
        )

        assert recall_at_5_hyde >= recall_at_5_floor - allowed_regression, (
            f"HyDE regression scenario failed: recall@5 with identity vector "
            f"({recall_at_5_hyde:.4f}) dropped below floor ({recall_at_5_floor:.4f}) "
            f"minus allowed_regression ({allowed_regression:.4f}). "
            f"HyDE plumbing has broken recall on the committed corpus."
        )

        # Also verify generate() was called for each query
        assert mock_generator.generate.call_count == len(retrieval_queries), (
            f"Expected HyDEGenerator.generate() called {len(retrieval_queries)} times, "
            f"got {mock_generator.generate.call_count}"
        )

    finally:
        await store.disconnect()


@pytest.mark.eval
async def test_eval_hyde_false_fast_path_no_overhead(tmp_path: Path) -> None:
    """``resolve_hyde_vector(hyde=False, ...)`` fast-path executes and returns (None, False).

    Verifies that the HyDE fast-path (hyde=False) neither crashes nor calls
    the generator — confirming zero overhead for non-HyDE requests.
    """
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator, resolve_hyde_vector

    config = HyDEConfig(enabled=True, max_requests_per_minute=60)
    mock_generator = MagicMock(spec=HyDEGenerator)
    mock_generator.generate = AsyncMock(return_value=[0.1] * 128)

    vector, applied = await resolve_hyde_vector(
        "how do I uninstall the CLI?", False, mock_generator, config
    )

    assert vector is None, "Expected None vector for hyde=False fast path"
    assert applied is False, "Expected hyde_applied=False for hyde=False fast path"
    mock_generator.generate.assert_not_called()
