"""Tests for _diagnostics module (ScoredSearchCandidate, SearchScoreBreakdown)."""
from __future__ import annotations

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown


def _make_breakdown() -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=0,
        vector_score=0.5,
        vector_score_kind="distance",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.1,
        reranker_score=None,
    )


def _make_candidate(**kwargs: object) -> ScoredSearchCandidate:
    defaults: dict[str, object] = dict(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello",
        source_path="/path/doc.md",
        score_breakdown=_make_breakdown(),
        collection="col",
    )
    defaults.update(kwargs)
    return ScoredSearchCandidate(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# acl field
# ---------------------------------------------------------------------------


def test_scored_search_candidate_acl_defaults_to_none() -> None:
    c = _make_candidate()
    assert c.acl is None


def test_scored_search_candidate_accepts_acl_list() -> None:
    c = _make_candidate(acl=["ns1", "ns2"])
    assert c.acl == ["ns1", "ns2"]


# ---------------------------------------------------------------------------
# A1/A2 metadata fields (Task 2.2.5)
# ---------------------------------------------------------------------------


def test_scored_search_candidate_metadata_fields_default() -> None:
    c = _make_candidate()
    assert c.file_type == ""
    assert c.indexed_at == ""
    assert c.updated_at == ""
    assert c.ingested_by == "cli"
    assert c.language is None
    assert c.metadata == {}
