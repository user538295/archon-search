"""BE-1: Tests for acl_source, acl_sidecar_path, acl_warning provenance fields
on ChunkRecord, SearchResult, and ScoredSearchCandidate.
"""
from __future__ import annotations

import dataclasses

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord, SearchResult


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


def _minimal_chunk_record() -> ChunkRecord:
    return ChunkRecord(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        vector=[0.0, 0.0],
        source_path="/tmp/test.md",
        indexed_at="2026-01-01T00:00:00.000000Z",
    )


def _minimal_search_result() -> SearchResult:
    return SearchResult(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/test.md",
    )


def _minimal_scored_candidate() -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        source_path="/tmp/test.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
    )


# --- ChunkRecord ---

def test_chunk_record_has_provenance_fields() -> None:
    """ChunkRecord accepts and stores all three provenance fields."""
    record = _minimal_chunk_record()
    assert record.acl_source is None
    assert record.acl_sidecar_path is None
    assert record.acl_warning == []


def test_chunk_record_acl_source_set() -> None:
    """ChunkRecord.acl_source can be set to any string or None."""
    record = ChunkRecord(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        vector=[0.0],
        source_path="/tmp/test.md",
        indexed_at="2026-01-01T00:00:00.000000Z",
        acl_source="frontmatter",
    )
    assert record.acl_source == "frontmatter"


def test_chunk_record_acl_sidecar_path_set() -> None:
    """ChunkRecord.acl_sidecar_path can be set to a string or None."""
    record = ChunkRecord(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        vector=[0.0],
        source_path="/tmp/test.md",
        indexed_at="2026-01-01T00:00:00.000000Z",
        acl_sidecar_path="doc.acl",
    )
    assert record.acl_sidecar_path == "doc.acl"


def test_chunk_record_acl_warning_set() -> None:
    """ChunkRecord.acl_warning accepts a non-empty list."""
    record = ChunkRecord(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        vector=[0.0],
        source_path="/tmp/test.md",
        indexed_at="2026-01-01T00:00:00.000000Z",
        acl_warning=["sidecar too large"],
    )
    assert record.acl_warning == ["sidecar too large"]


def test_chunk_record_acl_warning_not_shared() -> None:
    """acl_warning default must not be shared across instances (field(default_factory=list))."""
    a = _minimal_chunk_record()
    b = _minimal_chunk_record()
    assert a.acl_warning is not b.acl_warning


def test_chunk_record_provenance_fields_in_dataclass_fields() -> None:
    """All three provenance field names appear in ChunkRecord's dataclass fields."""
    field_names = {f.name for f in dataclasses.fields(ChunkRecord)}
    assert "acl_source" in field_names
    assert "acl_sidecar_path" in field_names
    assert "acl_warning" in field_names


# --- SearchResult ---

def test_search_result_has_provenance_fields() -> None:
    """SearchResult carries the three provenance fields with correct defaults."""
    result = _minimal_search_result()
    assert result.acl_source is None
    assert result.acl_sidecar_path is None
    assert result.acl_warning == []


def test_search_result_acl_source_set() -> None:
    """SearchResult.acl_source can be set to a string value."""
    result = SearchResult(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/test.md",
        acl_source="sidecar",
    )
    assert result.acl_source == "sidecar"


def test_search_result_acl_sidecar_path_set() -> None:
    """SearchResult.acl_sidecar_path can be set to a string or None."""
    result = SearchResult(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/test.md",
        acl_sidecar_path="doc.acl",
    )
    assert result.acl_sidecar_path == "doc.acl"


def test_search_result_acl_warning_set() -> None:
    """SearchResult.acl_warning accepts a non-empty list."""
    result = SearchResult(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/test.md",
        acl_warning=["sidecar too large"],
    )
    assert result.acl_warning == ["sidecar too large"]


def test_search_result_acl_warning_not_shared() -> None:
    """SearchResult.acl_warning default must not be shared across instances."""
    a = _minimal_search_result()
    b = _minimal_search_result()
    assert a.acl_warning is not b.acl_warning


def test_search_result_provenance_fields_in_dataclass_fields() -> None:
    """All three provenance field names appear in SearchResult's dataclass fields."""
    field_names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "acl_source" in field_names
    assert "acl_sidecar_path" in field_names
    assert "acl_warning" in field_names


# --- ScoredSearchCandidate ---

def test_scored_candidate_has_provenance_fields() -> None:
    """ScoredSearchCandidate carries the three provenance fields with correct defaults."""
    cand = _minimal_scored_candidate()
    assert cand.acl_source is None
    assert cand.acl_sidecar_path is None
    assert cand.acl_warning == []


def test_scored_candidate_acl_source_set() -> None:
    """ScoredSearchCandidate.acl_source can be set to a string value."""
    cand = ScoredSearchCandidate(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        source_path="/tmp/test.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
        acl_source="collection_default",
    )
    assert cand.acl_source == "collection_default"


def test_scored_candidate_acl_sidecar_path_set() -> None:
    """ScoredSearchCandidate.acl_sidecar_path can be set to a string or None."""
    cand = ScoredSearchCandidate(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        source_path="/tmp/test.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
        acl_sidecar_path="doc.acl",
    )
    assert cand.acl_sidecar_path == "doc.acl"


def test_scored_candidate_acl_warning_set() -> None:
    """ScoredSearchCandidate.acl_warning accepts a non-empty list."""
    cand = ScoredSearchCandidate(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        source_path="/tmp/test.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
        acl_warning=["sidecar too large"],
    )
    assert cand.acl_warning == ["sidecar too large"]


def test_scored_candidate_acl_warning_not_shared() -> None:
    """ScoredSearchCandidate.acl_warning default must not be shared across instances."""
    a = _minimal_scored_candidate()
    b = _minimal_scored_candidate()
    assert a.acl_warning is not b.acl_warning


def test_scored_candidate_provenance_fields_in_dataclass_fields() -> None:
    """All three provenance field names appear in ScoredSearchCandidate's dataclass fields."""
    field_names = {f.name for f in dataclasses.fields(ScoredSearchCandidate)}
    assert "acl_source" in field_names
    assert "acl_sidecar_path" in field_names
    assert "acl_warning" in field_names
