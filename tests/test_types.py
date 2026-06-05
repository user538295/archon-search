"""Tests for canonical domain types."""
import dataclasses
import json
import pytest
from datetime import datetime, timezone, timedelta
from archon_search._types import SearchResult, IngestedBy, normalize_iso_utc
from archon_search.types import (
    JobStatus,
    IngestJob,
    ReindexJob,
    DeleteJob,
    Query,
    RouteResponse,
    Collection,
    CollectionDetail,
    Chunk,
)


def test_job_status_all_values():
    values = {s.value for s in JobStatus}
    assert values == {"PENDING", "RUNNING", "DONE", "FAILED", "CANCELLED", "CANCELLING"}


def test_ingest_job_instantiation():
    job = IngestJob(job_id="abc", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")
    assert job.job_id == "abc"
    assert job.status == JobStatus.PENDING
    assert job.result is None
    assert job.error is None


def test_ingest_job_serializable():
    job = IngestJob(job_id="x", status=JobStatus.DONE, created_at="t", updated_at="t", result={"count": 1})
    d = dataclasses.asdict(job)
    assert d["job_id"] == "x"
    assert d["status"] == "DONE"
    assert d["result"] == {"count": 1}
    assert json.dumps(d)  # must be JSON-serializable, not just dict-convertible


def test_reindex_job_is_ingest_job():
    job = ReindexJob(job_id="r1", status=JobStatus.RUNNING, created_at="t", updated_at="t")
    assert isinstance(job, IngestJob)


def test_delete_job_deleted_ids_default():
    job = DeleteJob(job_id="d1", status=JobStatus.PENDING, created_at="t", updated_at="t")
    assert job.deleted_ids == []


def test_delete_job_deleted_ids_set():
    job = DeleteJob(job_id="d2", status=JobStatus.DONE, created_at="t", updated_at="t", deleted_ids=["id1", "id2"])
    assert job.deleted_ids == ["id1", "id2"]


def test_query_instantiation():
    q = Query(text="hello")
    assert q.text == "hello"
    assert q.slots is None


def test_query_with_slots():
    q = Query(text="hello", slots=5)
    assert q.slots == 5


def test_route_response_instantiation():
    rr = RouteResponse(
        pre_context="some context",
        pinned_names=["col1"],
        routable_names=["col2", "col3"],
        decomposer_invoked=True,
    )
    assert rr.pre_context == "some context"
    assert rr.pinned_names == ["col1"]
    assert rr.routable_names == ["col2", "col3"]
    assert rr.decomposer_invoked is True


def test_route_response_pre_context_none():
    rr = RouteResponse(pre_context=None, pinned_names=[], routable_names=[], decomposer_invoked=False)
    d = dataclasses.asdict(rr)
    assert d["pre_context"] is None
    assert d["decomposer_invoked"] is False


def test_collection_instantiation():
    c = Collection(name="docs", path="/tmp/docs", description="My docs", doc_count=10, chunk_count=100, status="ready")
    assert c.watching is False


def test_collection_detail_extends_collection():
    cd = CollectionDetail(
        name="docs", path="/tmp/docs", description="My docs",
        doc_count=10, chunk_count=100, status="ready",
        embedding_model="BAAI/bge-small-en-v1.5",
        centroid_present=True,
        last_indexed="2026-01-01T00:00:00Z",
    )
    assert isinstance(cd, Collection)
    assert cd.centroid_present is True
    assert cd.last_indexed == "2026-01-01T00:00:00Z"


def test_collection_detail_last_indexed_default_none():
    cd = CollectionDetail(
        name="docs", path="/tmp", description="", doc_count=0, chunk_count=0,
        status="empty", embedding_model="model", centroid_present=False,
    )
    assert cd.last_indexed is None


def test_chunk_metadata_default_empty_dict():
    chunk = Chunk(
        chunk_id="docid-000000",
        doc_id="docid",
        text="hello",
        source_path="/tmp/file.txt",
        collection="docs",
        indexed_at="2026-01-01T00:00:00Z",
        file_type="text",
        language="",
    )
    assert chunk.metadata == {}
    assert chunk.custom_score is None
    assert chunk.ingested_by == "archon-search-cli"
    assert chunk.updated_at == ""


def test_chunk_metadata_default_not_shared():
    a = Chunk(chunk_id="c1", doc_id="d1", text="t", source_path="/f", collection="col",
              indexed_at="t", file_type="md", language="")
    b = Chunk(chunk_id="c2", doc_id="d2", text="t", source_path="/f", collection="col",
              indexed_at="t", file_type="md", language="")
    a.metadata["key"] = "val"
    assert b.metadata == {}


def test_delete_job_deleted_ids_not_shared():
    a = DeleteJob(job_id="a", status=JobStatus.PENDING, created_at="t", updated_at="t")
    b = DeleteJob(job_id="b", status=JobStatus.PENDING, created_at="t", updated_at="t")
    a.deleted_ids.append("x")
    assert b.deleted_ids == []


def test_chunk_id_format():
    doc_id = "abc123"
    chunk_id = f"{doc_id}-{0:06d}"
    assert chunk_id == "abc123-000000"


def test_chunk_serializable():
    chunk = Chunk(
        chunk_id="c1",
        doc_id="d1",
        text="text",
        source_path="/f",
        collection="col",
        indexed_at="t",
        file_type="md",
        language="en",
        metadata={"key": "val"},
        custom_score=0.9,
    )
    d = dataclasses.asdict(chunk)
    assert d["metadata"] == {"key": "val"}
    assert d["custom_score"] == 0.9
    assert json.dumps(d)  # must be JSON-serializable


def test_ingest_job_namespace_default():
    job = IngestJob(job_id="x", status=JobStatus.PENDING, created_at="t", updated_at="t")
    assert job.namespace == "default"


def test_ingest_job_namespace_explicit():
    job = IngestJob(job_id="x", status=JobStatus.PENDING, created_at="t", updated_at="t", namespace="tenantA")
    assert job.namespace == "tenantA"


def test_ingest_job_splat_pre_5c_dict():
    from archon_search.constants import DEFAULT_NAMESPACE
    item = {"job_id": "x", "status": JobStatus.PENDING, "created_at": "t", "updated_at": "t"}
    job = IngestJob(**item)
    assert job.namespace == DEFAULT_NAMESPACE


def test_search_result_language_defaults_to_empty_string():
    result = SearchResult(
        doc_id="abc", chunk_id="abc-000000", text="hello", score=0.9, source_path="/tmp/file.txt"
    )
    assert result.language == ""


def test_search_result_language_carried_when_set():
    result = SearchResult(
        doc_id="abc", chunk_id="abc-000000", text="hello", score=0.9, source_path="/tmp/file.txt",
        language="en",
    )
    assert result.language == "en"


def test_search_result_has_collection_field():
    r = SearchResult(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/file.txt",
        collection="col_a",
    )
    assert r.collection == "col_a"


def test_search_result_collection_defaults_to_empty_string():
    r = SearchResult(
        doc_id="abc", chunk_id="abc-000000", text="hello", score=0.9, source_path="/tmp/file.txt"
    )
    assert r.collection == ""


def test_excluded_collection_carries_name_and_reason():
    from archon_search._types import ExcludedCollection

    ec = ExcludedCollection(name="col_b", reason="acl")
    assert ec.name == "col_b"
    assert ec.reason == "acl"


def test_search_result_ingested_by_remains_ingested_by_literal():
    import typing
    hints = typing.get_type_hints(SearchResult)
    # ingested_by must be typed as IngestedBy (the Literal) — not plain str
    assert hints["ingested_by"] == IngestedBy


def test_all_types_json_serializable():
    instances = [
        IngestJob(job_id="j", status=JobStatus.DONE, created_at="t", updated_at="t"),
        ReindexJob(job_id="r", status=JobStatus.RUNNING, created_at="t", updated_at="t"),
        DeleteJob(job_id="d", status=JobStatus.CANCELLED, created_at="t", updated_at="t"),
        Query(text="q"),
        RouteResponse(pre_context=None, pinned_names=[], routable_names=[], decomposer_invoked=False),
        Collection(name="n", path="/p", description="d", doc_count=0, chunk_count=0, status="ready"),
        CollectionDetail(name="n", path="/p", description="d", doc_count=0, chunk_count=0,
                         status="ready", embedding_model="m", centroid_present=False),
        Chunk(chunk_id="c", doc_id="d", text="t", source_path="/f", collection="col",
              indexed_at="t", file_type="md", language=""),
    ]
    for obj in instances:
        d = dataclasses.asdict(obj)
        json.dumps(d)  # must not raise


def test_normalize_iso_utc_naive_datetime():
    dt = datetime(2026, 5, 21, 10, 0, 0, 123456)
    result = normalize_iso_utc(dt)
    assert result == "2026-05-21T10:00:00.123456Z"


def test_normalize_iso_utc_aware_datetime():
    # UTC+2 offset; 12:00 local = 10:00 UTC
    tz_plus2 = timezone(timedelta(hours=2))
    dt = datetime(2026, 5, 21, 12, 0, 0, 0, tzinfo=tz_plus2)
    result = normalize_iso_utc(dt)
    assert result == "2026-05-21T10:00:00.000000Z"


def test_normalize_iso_utc_string_round_trip():
    fixed = "2026-05-21T10:00:00.123456Z"
    assert normalize_iso_utc(fixed) == fixed


def test_normalize_iso_utc_variable_precision_string():
    # No microseconds
    assert normalize_iso_utc("2026-05-21T10:00:00Z") == "2026-05-21T10:00:00.000000Z"
    # With microseconds
    assert normalize_iso_utc("2026-05-21T10:00:00.123456Z") == "2026-05-21T10:00:00.123456Z"


def test_normalize_iso_utc_plus_zero_offset_string():
    result = normalize_iso_utc("2026-05-21T10:00:00+00:00")
    assert result == "2026-05-21T10:00:00.000000Z"


def test_lexicographic_order_preserved():
    earlier = normalize_iso_utc(datetime(2026, 1, 1, 0, 0, 0))
    later = normalize_iso_utc(datetime(2026, 6, 1, 0, 0, 0))
    assert earlier < later


def test_normalize_iso_utc_non_utc_offset_string():
    # "+05:30" offset: 15:30 local = 10:00 UTC
    result = normalize_iso_utc("2026-05-21T15:30:00+05:30")
    assert result == "2026-05-21T10:00:00.000000Z"


def test_normalize_iso_utc_zero_microseconds():
    dt = datetime(2026, 1, 1, 0, 0, 0)  # zero microseconds
    assert normalize_iso_utc(dt) == "2026-01-01T00:00:00.000000Z"


def test_normalize_iso_utc_max_microseconds():
    dt = datetime(2026, 1, 1, 23, 59, 59, 999999)
    assert normalize_iso_utc(dt) == "2026-01-01T23:59:59.999999Z"


def test_lexicographic_order_preserved_time_difference():
    earlier = normalize_iso_utc(datetime(2026, 5, 21, 9, 59, 59, 999999))
    later = normalize_iso_utc(datetime(2026, 5, 21, 10, 0, 0, 0))
    assert earlier < later


def test_normalize_iso_utc_three_digit_millis():
    # 3-digit millis become 6-digit microseconds
    result = normalize_iso_utc("2026-05-21T10:00:00.123Z")
    assert result == "2026-05-21T10:00:00.123000Z"


def test_normalize_iso_utc_invalid_string_raises():
    with pytest.raises(ValueError):
        normalize_iso_utc("not-a-date")


def test_fixed_width_pattern_matches_normalize_iso_utc_output():
    """_FIXED_WIDTH_PATTERN in store.py must always match normalize_iso_utc output.

    This test is the machine-verified sync contract between the two modules.
    If either changes its format, this test fails immediately.
    """
    from datetime import timezone
    from archon_search.store import _FIXED_WIDTH_PATTERN

    samples = [
        datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 21, 12, 34, 56, 123456, tzinfo=timezone.utc),
        datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
        datetime.now(timezone.utc),
    ]
    for dt in samples:
        result = normalize_iso_utc(dt)
        assert _FIXED_WIDTH_PATTERN.match(result), (
            f"_FIXED_WIDTH_PATTERN does not match normalize_iso_utc output: {result!r}"
        )
