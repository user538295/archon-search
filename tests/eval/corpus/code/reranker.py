"""Cross-encoder reranker: re-score (query, passage) pairs."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class RankedResult:
    doc_id: str
    score: float
    text: str


def rerank(
    query: str,
    candidates: list[tuple[str, str]],  # (doc_id, text)
    score_fn: callable,  # (query: str, passage: str) -> float
    top_k: int = 5,
) -> list[RankedResult]:
    """Re-score each candidate with a cross-encoder and return top-k.

    Args:
        query: The search query.
        candidates: List of (doc_id, passage_text) pairs.
        score_fn: Scoring function that returns a float relevance score.
        top_k: Number of results to return after reranking.

    Returns:
        List of :class:`RankedResult` sorted by descending score.
    """
    scored = [
        RankedResult(doc_id=doc_id, score=score_fn(query, text), text=text)
        for doc_id, text in candidates
    ]
    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:top_k]
