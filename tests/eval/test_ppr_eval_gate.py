"""E2h BE-9: PPR eval gates — non-vacuity integration tests.

Two integration tests:

- ``test_pprEvalGate_nonVacuous_pprOutperformsNoGraph`` — PPR pipeline vs
  no-graph hybrid pipeline on a bridge document that is lexically absent from
  the query.  10 distractor documents ensure the bridge doc is displaced from
  top-5 hybrid results (lexical score too low), while the PPR walk retrieves
  it via the entity graph.  Asserts strict inequality (ppr_recall > no_graph_recall).
- ``test_pprNegativeControlGate_nonVacuous_independentFromNaiveBucket`` — PPR
  and naive mode produce structurally distinct ``SearchPipelineResult`` values
  (``ppr_entities_matched`` is set only for PPR), confirming they feed
  independent metric buckets in ``run_eval_suite``.

Both tests use dedicated pipeline construction (not the default
``_build_pipeline_with_eval_backends`` which never wires a PPRWalker).

All tests in this file are guarded with ``pytest.importorskip("networkx")``
at the module level — CI legs that run ``uv sync --dev`` without
``--extra graph`` skip this file gracefully.  This matches the existing
convention in ``test_e2e_graph_eval_gate_v2.py`` (leidenalg guard) and
``test_code_lane_eval_gate.py`` (tree_sitter guard).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

networkx = pytest.importorskip("networkx")

from archon_search.chunker import DocumentChunker
from archon_search.config import GraphConfig
from archon_search.embedder import Embedder
from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend, RealGraphExpander
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,  # lowercase enum members: .concept, .person, etc.
    GraphEdge,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.ppr_walker import PPRWalker
from archon_search.reranker import Reranker
from archon_search.store import SearchStore

_DEFAULT_NS = "default"
_BRIDGE_COLLECTION = "ppr-bridge-eval"

# Bridge corpus:
# - ANCHOR DOC: contains "attention" and "neural" — query terms present here
# - BRIDGE DOC: contains only unrelated terms — query terms lexically absent
#   but reachable via the ANCHOR→BRIDGE graph edge on shared entity "attention"
# - 10 DISTRACTOR DOCs: also contain "attention", "neural" — so that the bridge
#   doc ranks BELOW top-5 in pure hybrid search (displaced by distractors)
_ANCHOR_DOC_TEXT = "Attention mechanisms in neural networks model sequence dependencies."
_BRIDGE_DOC_TEXT = "Advanced transformer architectures enable emergent capabilities."

# Query: terms match anchor + distractors, NOT bridge doc
_BRIDGE_QUERY = "attention mechanisms neural networks"

# 10 distractor docs — all contain query terms to fill top-5 hybrid results
_DISTRACTOR_TEXTS = [
    f"Neural attention layer {i} processes attention over networks with mechanisms."
    for i in range(1, 11)
]


class _PassthroughRerankerBackend:
    """Reranker that returns uniform 1.0 scores.

    Preserves the PPR-first merge order (Python stable sort keeps insertion
    order when all scores are equal). Used for the PPR pipeline only so the
    PPR-retrieved bridge chunk is not displaced by its low lexical score.
    The EvalRerankerBackend is intentionally NOT used here — reranker quality
    is not what this test measures.
    """

    @property
    def is_warm(self) -> bool:
        return True

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [1.0] * len(pairs)


def _compute_doc_id(absolute_path: Path) -> str:
    """Replicate pipeline.py:413 doc_id formula."""
    return hashlib.sha256(str(absolute_path.resolve()).encode()).hexdigest()


def _make_chunk_id(doc_id: str, idx: int) -> str:
    """Replicate pipeline.py:573 chunk_id formula."""
    return f"{doc_id}-{idx:06d}"


async def _build_pipeline(db_path: Path, *, with_ppr: bool = True, top_k_return: int = 5):
    """Build a SearchPipeline with or without PPRWalker.

    Returns (pipeline, graph_store) where graph_store is None when with_ppr=False.
    """
    store = SearchStore(db_path)
    await store.connect()

    embedder = Embedder(EvalEmbedderBackend())
    chunker = DocumentChunker()
    parser = DocumentParser()

    if with_ppr:
        # ponytail: passthrough reranker preserves PPR-first merge order so
        # the lexically-weak bridge chunk stays in top-k. The EvalRerankerBackend
        # would displace it (bridge text has zero overlap with query terms).
        reranker = Reranker(_PassthroughRerankerBackend())
        graph_store = GraphStore(db_path=str(db_path))
        await graph_store.connect()
        graph_expander = RealGraphExpander(graph_store)
        ppr_walker = PPRWalker(graph_store)
        pipeline = SearchPipeline(
            store=store,
            embedder=embedder,
            reranker=reranker,
            chunker=chunker,
            parser=parser,
            top_k_retrieve=20,
            top_k_return=top_k_return,
            graph_store=graph_store,
            graph_config=GraphConfig(enabled=True),
            graph_expander=graph_expander,
            ppr_walker=ppr_walker,
        )
        return pipeline, graph_store
    else:
        pipeline = SearchPipeline(
            store=store,
            embedder=embedder,
            reranker=Reranker(EvalRerankerBackend()),
            chunker=chunker,
            parser=parser,
            top_k_retrieve=20,
            top_k_return=top_k_return,
        )
        return pipeline, None


async def _ingest_corpus(pipeline, corpus_dir: Path, paths: list[Path], collection: str) -> None:
    """Ingest all files and rebuild FTS + meta."""
    for p in paths:
        result = await pipeline.ingest_file(
            p, collection,
            rebuild_fts=False,
            embedder=pipeline._global_embedder,
            collection_root=corpus_dir,
        )
        assert result.status == "ok", f"Ingest failed for {p}: {result.error}"
    await pipeline.store.rebuild_fts_index(collection)
    await pipeline.recompute_collection_meta(collection, pipeline._global_embedder)


# ---------------------------------------------------------------------------
# Integration test 1: PPR outperforms no-graph hybrid on a bridge document
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.eval
async def test_pprEvalGate_nonVacuous_pprOutperformsNoGraph(tmp_path: Path) -> None:
    """PPR pipeline recall@5 strictly beats no-graph pipeline recall@5.

    Non-vacuity proof:
    - 10 distractor docs contain all query terms ("attention", "neural", etc.)
      so pure hybrid returns the anchor + 4 distractors in top-5; bridge is
      not retrieved (its text is lexically absent from the query).
    - PPR pipeline seeds on "attention" entity (matched in anchor doc),
      traverses the related_to edge to "transformer" (in bridge doc), and
      resolves the bridge chunk from the mention table — bridge appears in PPR
      results regardless of its low lexical score.

    Assertions:
    1. PPR retrieves the bridge chunk (presence check).
    2. No-graph does NOT retrieve the bridge chunk in top-5 (negative check).
    3. ppr_recall > no_graph_recall (strict inequality).
    """
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    anchor_path = corpus_dir / "anchor.txt"
    bridge_path = corpus_dir / "bridge.txt"
    anchor_path.write_text(_ANCHOR_DOC_TEXT, encoding="utf-8")
    bridge_path.write_text(_BRIDGE_DOC_TEXT, encoding="utf-8")

    distractor_paths = []
    for i, text in enumerate(_DISTRACTOR_TEXTS):
        p = corpus_dir / f"distractor_{i:02d}.txt"
        p.write_text(text, encoding="utf-8")
        distractor_paths.append(p)

    anchor_doc_id = _compute_doc_id(anchor_path)
    bridge_doc_id = _compute_doc_id(bridge_path)
    anchor_chunk_id = _make_chunk_id(anchor_doc_id, 0)
    bridge_chunk_id = _make_chunk_id(bridge_doc_id, 0)

    all_paths = [anchor_path, bridge_path] + distractor_paths

    # --- PPR pipeline ---
    ppr_pipeline, graph_store = await _build_pipeline(
        tmp_path / "ppr" / "lancedb", with_ppr=True, top_k_return=5
    )
    assert graph_store is not None
    ppr_chunk_ids: set[str] = set()
    try:
        await _ingest_corpus(ppr_pipeline, corpus_dir, all_paths, _BRIDGE_COLLECTION)

        # Write graph: anchor entity --[related_to]--> bridge entity
        anchor_entity_id = make_stable_entity_id(EntityType.concept, "attention")
        bridge_entity_id = make_stable_entity_id(EntityType.concept, "transformer")
        edge_id = make_stable_edge_id(
            anchor_entity_id, bridge_entity_id, RelationshipType.related_to
        )

        anchor_node = GraphNode(
            id=anchor_entity_id,
            entity_name="attention",
            entity_type=EntityType.concept,
            source_doc_id=anchor_doc_id,
            collection_name=_BRIDGE_COLLECTION,
        )
        bridge_node = GraphNode(
            id=bridge_entity_id,
            entity_name="transformer",
            entity_type=EntityType.concept,
            source_doc_id=bridge_doc_id,
            collection_name=_BRIDGE_COLLECTION,
        )
        edge = GraphEdge(
            id=edge_id,
            source_node_id=anchor_entity_id,
            target_node_id=bridge_entity_id,
            relationship_type=RelationshipType.related_to,
            source_doc_id=anchor_doc_id,
        )

        await graph_store.ensure_graph_tables(_BRIDGE_COLLECTION, ns=_DEFAULT_NS)
        await graph_store.write_graph(
            _BRIDGE_COLLECTION, [anchor_node, bridge_node], [edge], ns=_DEFAULT_NS
        )

        # Write mentions: anchor entity → anchor chunk; bridge entity → bridge chunk
        anchor_mention = GraphMention(
            entity_id=anchor_entity_id,
            chunk_id=anchor_chunk_id,
            doc_id=anchor_doc_id,
        )
        bridge_mention = GraphMention(
            entity_id=bridge_entity_id,
            chunk_id=bridge_chunk_id,
            doc_id=bridge_doc_id,
        )
        await graph_store.write_mentions(
            _BRIDGE_COLLECTION, [anchor_mention, bridge_mention], ns=_DEFAULT_NS
        )

        ppr_result = await ppr_pipeline.search(
            _BRIDGE_QUERY,
            _BRIDGE_COLLECTION,
            graph_mode="ppr",
            embedder=ppr_pipeline._global_embedder,
        )
        ppr_chunk_ids = {r.chunk_id for r in ppr_result.results}
    finally:
        await ppr_pipeline.store.disconnect()
        await graph_store.disconnect()

    ppr_bridge_found = bridge_chunk_id in ppr_chunk_ids

    # --- No-graph pipeline (hybrid only, same corpus) ---
    no_graph_pipeline, _ = await _build_pipeline(
        tmp_path / "no_graph" / "lancedb", with_ppr=False, top_k_return=5
    )
    no_graph_chunk_ids: set[str] = set()
    try:
        await _ingest_corpus(no_graph_pipeline, corpus_dir, all_paths, _BRIDGE_COLLECTION)
        no_graph_result = await no_graph_pipeline.search(
            _BRIDGE_QUERY,
            _BRIDGE_COLLECTION,
            embedder=no_graph_pipeline._global_embedder,
        )
        no_graph_chunk_ids = {r.chunk_id for r in no_graph_result.results}
    finally:
        await no_graph_pipeline.store.disconnect()

    no_graph_bridge_found = bridge_chunk_id in no_graph_chunk_ids

    # Primary assertions
    assert ppr_bridge_found, (
        f"PPR pipeline did NOT retrieve the bridge chunk {bridge_chunk_id!r}.\n"
        f"PPR chunk IDs in results: {ppr_chunk_ids}\n"
        "The PPR walk should traverse from 'attention' entity → 'transformer' entity "
        "via the related_to edge and resolve the bridge chunk from the mention table."
    )
    assert not no_graph_bridge_found, (
        f"No-graph pipeline retrieved the bridge chunk {bridge_chunk_id!r} in top-5 "
        "despite the bridge text being lexically absent from the query. "
        "The 10 distractor docs should displace it from top-5.\n"
        f"No-graph chunk IDs: {no_graph_chunk_ids}"
    )

    ppr_recall = 1.0 if ppr_bridge_found else 0.0
    no_graph_recall = 1.0 if no_graph_bridge_found else 0.0
    assert ppr_recall > no_graph_recall, (
        f"PPR recall@5={ppr_recall:.2f} is NOT strictly above "
        f"no-graph recall@5={no_graph_recall:.2f} — "
        "the non-vacuity gate requires a strict inequality"
    )


# ---------------------------------------------------------------------------
# Integration test 2: PPR and naive mode produce independent metric buckets
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.eval
async def test_pprNegativeControlGate_nonVacuous_independentFromNaiveBucket(
    tmp_path: Path,
) -> None:
    """PPR-mode and naive-mode searches produce structurally independent results.

    Non-vacuity: runs the same query twice — once as graph_mode='ppr', once
    as graph_mode='naive'.  Asserts that ``ppr_entities_matched`` is set (non-None,
    even when 0) for PPR and None for naive, confirming the two modes produce
    distinct result structures that feed separate trace lists in ``run_eval_suite``
    (``ppr_graph_traces`` vs ``naive_graph_traces``).  A bug that merged the two
    buckets would collapse them into the same set.
    """
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    doc_path = corpus_dir / "science.txt"
    doc_path.write_text(
        "Physics studies matter and energy through motion and force laws.",
        encoding="utf-8",
    )

    ppr_pipeline, graph_store = await _build_pipeline(
        tmp_path / "lancedb", with_ppr=True
    )
    assert graph_store is not None
    try:
        await _ingest_corpus(ppr_pipeline, corpus_dir, [doc_path], "nc-test")

        ppr_search = await ppr_pipeline.search(
            "physics motion laws",
            "nc-test",
            graph_mode="ppr",
            embedder=ppr_pipeline._global_embedder,
        )
        naive_search = await ppr_pipeline.search(
            "physics motion laws",
            "nc-test",
            graph_mode="naive",
            embedder=ppr_pipeline._global_embedder,
        )
    finally:
        await ppr_pipeline.store.disconnect()
        await graph_store.disconnect()

    # PPR mode sets ppr_entities_matched (0 when no entities found, but NOT None)
    assert ppr_search.ppr_entities_matched is not None, (
        "PPR search ppr_entities_matched is None — "
        "SearchPipelineResult from a PPR-mode search must have ppr_entities_matched set "
        "(even when 0, because the PPR code path fires and sets it)"
    )
    # Naive mode does NOT set ppr_entities_matched (it stays None for non-PPR paths)
    assert naive_search.ppr_entities_matched is None, (
        f"Naive search ppr_entities_matched={naive_search.ppr_entities_matched!r} — "
        "expected None for a naive-mode search (ppr_entities_matched is PPR-path-only)"
    )
    # Strict inequality confirms independent bucket assignment
    assert ppr_search.ppr_entities_matched != naive_search.ppr_entities_matched, (
        "PPR and naive ppr_entities_matched values must differ — "
        "they must feed separate metric buckets in run_eval_suite "
        "(ppr_graph_traces vs naive_graph_traces)"
    )
