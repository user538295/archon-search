"""Tests for ScoredSearchCandidate in archon_search/_diagnostics.py."""
from __future__ import annotations

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown


def _minimal_breakdown() -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=None,
        vector_score=None,
        vector_score_kind=None,
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.0,
        reranker_score=None,
    )


def test_scored_search_candidate_acl_defaults_to_none() -> None:
    """ScoredSearchCandidate.acl defaults to None when not supplied."""
    cand = ScoredSearchCandidate(
        doc_id="abc123",
        chunk_id="abc123-000000",
        text="some text",
        source_path="/tmp/foo.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
    )
    assert cand.acl is None


def test_scored_search_candidate_accepts_acl_list() -> None:
    """ScoredSearchCandidate.acl stores the supplied list."""
    cand = ScoredSearchCandidate(
        doc_id="abc123",
        chunk_id="abc123-000000",
        text="some text",
        source_path="/tmp/foo.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
        acl=["ns1"],
    )
    assert cand.acl == ["ns1"]


def test_scored_search_candidate_metadata_fields_default() -> None:
    """New A1 metadata fields all have correct defaults when not supplied."""
    cand = ScoredSearchCandidate(
        doc_id="abc123",
        chunk_id="abc123-000000",
        text="some text",
        source_path="/tmp/foo.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
    )
    assert cand.file_type == ""
    assert cand.indexed_at == ""
    assert cand.updated_at == ""
    assert cand.ingested_by == "cli"
    assert cand.language == ""
    assert cand.metadata == {}
