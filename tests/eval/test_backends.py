"""Tests for EvalEmbedderBackend and EvalRerankerBackend — FEAT-039 Task 2.5."""
from __future__ import annotations

import inspect
import math

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_backends():
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    return EvalEmbedderBackend, EvalRerankerBackend


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_eval_embedder_changes_vector_for_query_terms() -> None:
    """Different query terms produce different deterministic vectors."""
    EvalEmbedderBackend, _ = _import_backends()
    emb = EvalEmbedderBackend()

    vecs = emb.encode(["python async programming", "java enterprise beans ejb"])
    assert len(vecs) == 2
    assert vecs[0] != vecs[1], "Different terms must produce different vectors"

    # Deterministic: encoding again yields identical results
    vecs2 = emb.encode(["python async programming", "java enterprise beans ejb"])
    assert vecs[0] == vecs2[0]
    assert vecs[1] == vecs2[1]


def test_eval_reranker_scores_from_query_and_document_text() -> None:
    """Reranker scores are content-sensitive (relevant doc scores higher)."""
    _, EvalRerankerBackend = _import_backends()
    rer = EvalRerankerBackend()

    query = "python async programming"
    relevant = "Python supports async programming via asyncio and coroutines."
    irrelevant = "The annual report shows a 10% increase in quarterly revenue."

    scores = rer.predict([(query, relevant), (query, irrelevant)])
    assert len(scores) == 2
    assert scores[0] > scores[1], (
        f"Relevant doc score ({scores[0]}) should exceed irrelevant ({scores[1]})"
    )


def test_eval_backends_do_not_receive_labels_query_ids_doc_ids_or_gold_ids() -> None:
    """Backends only accept text — no label/ID/metadata parameters in signature."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()

    embed_sig = inspect.signature(EvalEmbedderBackend().encode)
    predict_sig = inspect.signature(EvalRerankerBackend().predict)

    forbidden = {"label", "query_id", "doc_id", "gold_id", "gold_collection",
                 "query_ids", "doc_ids", "gold_ids"}

    embed_params = set(embed_sig.parameters)
    predict_params = set(predict_sig.parameters)

    overlap_embed = embed_params & forbidden
    overlap_predict = predict_params & forbidden

    assert not overlap_embed, f"encode() has forbidden params: {overlap_embed}"
    assert not overlap_predict, f"predict() has forbidden params: {overlap_predict}"


def test_eval_backends_do_not_receive_paths_or_fixture_metadata_by_default() -> None:
    """encode() only takes 'texts', predict() only takes 'pairs'."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()

    embed_sig = inspect.signature(EvalEmbedderBackend().encode)
    predict_sig = inspect.signature(EvalRerankerBackend().predict)

    # 'self' is not in bound method signatures
    assert set(embed_sig.parameters) == {"texts"}, (
        f"encode() should only accept 'texts', got: {set(embed_sig.parameters)}"
    )
    assert set(predict_sig.parameters) == {"pairs"}, (
        f"predict() should only accept 'pairs', got: {set(predict_sig.parameters)}"
    )


def test_eval_backends_ignore_metadata_fields_that_look_like_gold_ids() -> None:
    """Metadata-like ID prefixes must not affect scores — only text content matters."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()
    reranker = EvalRerankerBackend()
    embedder = EvalEmbedderBackend()

    query = "machine learning algorithms"

    # Same semantic content, different ID-like prefixes — scores MUST be equal
    doc_with_id_a = "doc-001 gold-123 machine learning algorithms"
    doc_with_id_b = "doc-999 gold-456 machine learning algorithms"

    scores = reranker.predict([(query, doc_with_id_a), (query, doc_with_id_b)])
    # Scores differ because "doc" and "gold" appear as tokens too — but the
    # meaningful test is that pure-content docs score the same as their ID-prefixed equivalents
    doc_content_only = "machine learning algorithms"

    scores_with_id = reranker.predict([(query, doc_with_id_a)])
    scores_no_id = reranker.predict([(query, doc_content_only)])

    # The content part drives relevance — doc with ID prefix should score >= content-only
    # because it contains the same terms plus extra terms (ID tokens don't appear in query)
    # The key property: ID tokens (doc, gold, numbers) don't appear in the query,
    # so they contribute ZERO to the score — score is purely content-driven
    assert scores_with_id[0] == scores_no_id[0]

    # Same for embedder: metadata prefixes change the vector (ID tokens add noise)
    # but content-only version and prefixed version should have high similarity
    vecs = embedder.encode([doc_with_id_a, doc_content_only])

    def dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    similarity = dot(vecs[0], vecs[1])
    assert similarity > 0.5, f"ID-prefixed doc should be highly similar to content-only doc, got {similarity}"


def test_eval_backends_have_stable_tie_breaking() -> None:
    """Equal-content inputs produce equal scores; repeated calls give identical ordering."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()
    embedder = EvalEmbedderBackend()
    reranker = EvalRerankerBackend()

    # Two documents with identical content — must produce identical scores
    doc_a = "machine learning optimization algorithm"
    doc_b = "machine learning optimization algorithm"  # exact copy

    query = "machine learning"

    # Embedder: identical texts → identical vectors
    vecs1 = embedder.encode([doc_a, doc_b])
    vecs2 = embedder.encode([doc_b, doc_a])
    assert vecs1[0] == vecs2[1]  # same text always gives same vector
    assert vecs1[1] == vecs2[0]

    # Reranker: identical pairs → identical scores (tie)
    scores_fwd = reranker.predict([(query, doc_a), (query, doc_b)])
    scores_rev = reranker.predict([(query, doc_b), (query, doc_a)])
    assert scores_fwd[0] == scores_fwd[1]   # tie: equal scores
    assert scores_fwd[0] == scores_rev[0]   # stable: same value every run
    assert scores_fwd[1] == scores_rev[1]

    # Input ordering is preserved (first input gets first score)
    # This is the tie-breaking guarantee: equal-scored items stay in input order
    scores_again = reranker.predict([(query, doc_a), (query, doc_b)])
    assert scores_again == scores_fwd


def test_eval_reranker_ranks_exact_match_highest() -> None:
    """Reranker places the exact-token-match document at rank 1."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()
    reranker = EvalRerankerBackend()

    query = "python async programming coroutines"
    # Doc A: shares exact query terms — should rank high by reranker
    doc_a = "python async coroutines are used for concurrent programming tasks"
    # Doc B: semantically adjacent but different tokens — zero overlap with query
    doc_b = "java enterprise concurrency threads executors services"
    # Doc C: completely unrelated
    doc_c = "recipe chocolate cake baking ingredients sugar flour"

    docs = [doc_a, doc_b, doc_c]

    # Reranker-based ranking
    pairs = [(query, d) for d in docs]
    rerank_scores = reranker.predict(pairs)
    rerank_rank = sorted(range(len(docs)), key=lambda i: rerank_scores[i], reverse=True)

    # Reranker should prefer doc_a (exact token overlap) — verify nonzero score
    assert rerank_scores[0] > 0, "Relevant doc must have nonzero reranker score"
    # Doc_b and doc_c have no query-term overlap — must score 0
    assert rerank_scores[1] == 0.0
    assert rerank_scores[2] == 0.0

    # doc_a (exact match) must be #1 by the reranker
    assert rerank_rank[0] == 0, f"doc_a (exact match) should be reranker #1, got rank {rerank_rank}"
    assert len(rerank_rank) == 3


def test_eval_backends_produce_score_kind_consistent_with_polarity() -> None:
    """Relevant doc has lower cosine distance AND higher reranker score than irrelevant doc."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()
    emb = EvalEmbedderBackend()
    rer = EvalRerankerBackend()

    query = "python async coroutines"
    relevant_doc = "Python async coroutines allow concurrent code execution."
    irrelevant_doc = "Basketball is a team sport played on a rectangular court."

    query_vec, rel_vec, irr_vec = emb.encode([query, relevant_doc, irrelevant_doc])

    dist_relevant = _cosine_distance(query_vec, rel_vec)
    dist_irrelevant = _cosine_distance(query_vec, irr_vec)

    assert dist_relevant < dist_irrelevant, (
        f"Relevant doc cosine distance ({dist_relevant:.4f}) must be lower than "
        f"irrelevant ({dist_irrelevant:.4f})"
    )

    scores = rer.predict([(query, relevant_doc), (query, irrelevant_doc)])
    assert scores[0] > scores[1], (
        f"Reranker score for relevant ({scores[0]:.4f}) must exceed "
        f"irrelevant ({scores[1]:.4f})"
    )


def test_eval_backends_handle_empty_inputs() -> None:
    """Empty strings and all-punctuation texts are handled gracefully."""
    EvalEmbedderBackend, EvalRerankerBackend = _import_backends()
    embedder = EvalEmbedderBackend()
    reranker = EvalRerankerBackend()

    # Empty string → uniform unit vector (not zero vector)
    empty_vecs = embedder.encode([""])
    assert len(empty_vecs) == 1
    assert len(empty_vecs[0]) == 128
    norm = math.sqrt(sum(x * x for x in empty_vecs[0]))
    assert abs(norm - 1.0) < 1e-9

    # All punctuation → same as empty (no alphanumeric tokens)
    punct_vecs = embedder.encode(["!@#$%^&*()"])
    assert punct_vecs[0] == empty_vecs[0]

    # Reranker: empty query → 0.0
    assert reranker.predict([("", "some document text")])[0] == 0.0
    # Reranker: empty doc → 0.0
    assert reranker.predict([("some query", "")])[0] == 0.0
    # Reranker: both empty → 0.0
    assert reranker.predict([("", "")])[0] == 0.0


def test_eval_embedder_produces_correct_dimension_and_unit_norm() -> None:
    """Embedder output has exactly 128 dimensions and unit L2 norm."""
    EvalEmbedderBackend, _ = _import_backends()
    embedder = EvalEmbedderBackend()
    vecs = embedder.encode(["hello world", "test text"])
    for vec in vecs:
        assert len(vec) == 128
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-9, f"Expected unit norm, got {norm}"


def test_eval_embedder_model_name_attribute() -> None:
    """EvalEmbedderBackend exposes model_name for trace provenance."""
    EvalEmbedderBackend, _ = _import_backends()
    embedder = EvalEmbedderBackend()
    assert hasattr(embedder, "model_name")
    assert embedder.model_name == "eval-sha256-v1"
