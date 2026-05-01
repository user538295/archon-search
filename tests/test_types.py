"""Tests for canonical domain types — Task 1.2."""
import dataclasses
import json
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
        language=None,
    )
    assert chunk.metadata == {}
    assert chunk.custom_score is None
    assert chunk.ingested_by == "archon-search-cli"
    assert chunk.updated_at == ""


def test_chunk_metadata_default_not_shared():
    a = Chunk(chunk_id="c1", doc_id="d1", text="t", source_path="/f", collection="col",
              indexed_at="t", file_type="md", language=None)
    b = Chunk(chunk_id="c2", doc_id="d2", text="t", source_path="/f", collection="col",
              indexed_at="t", file_type="md", language=None)
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
              indexed_at="t", file_type="md", language=None),
    ]
    for obj in instances:
        d = dataclasses.asdict(obj)
        json.dumps(d)  # must not raise
