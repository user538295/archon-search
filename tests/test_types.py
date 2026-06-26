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
    MigrationJob,
    MigrationSpec,
    MigrationKind,
)


def test_job_status_all_values():
    values = {s.value for s in JobStatus}
    assert values == {"PENDING", "QUEUED", "RUNNING", "DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED", "CANCELLING"}


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
        MigrationJob(job_id="mj", status=JobStatus.QUEUED, created_at="t", updated_at="t",
                     collection="col", kind=MigrationKind.REWRITE),
        MigrationSpec(name="m", kind=MigrationKind.IN_PLACE, description="desc", introduced_at=0),
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


# --- Task 1.1: transient offset fields on ChunkRecord ---

def test_chunk_record_default_offsets():
    """ChunkRecord constructed without offsets has start_offset == -1 and end_offset == -1."""
    from archon_search._types import ChunkRecord
    record = ChunkRecord(
        doc_id="abc",
        chunk_id="abc-000000",
        text="hello",
        vector=[0.0, 0.0, 0.0, 0.0],
        source_path="/tmp/test.md",
        indexed_at="2026-01-01T00:00:00.000000Z",
    )
    assert record.start_offset == -1
    assert record.end_offset == -1


def test_chunk_record_offset_fields_not_in_schema():
    """start_offset and end_offset must NOT appear in SearchStore._schema() column names."""
    from archon_search.store import SearchStore
    schema = SearchStore._schema(4)
    field_names = [f.name for f in schema]
    assert "start_offset" not in field_names
    assert "end_offset" not in field_names


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


# --- BE-1: MigrationJob, MigrationSpec, MigrationKind ---


def test_migration_kind_enum_values():
    values = {k.value for k in MigrationKind}
    assert values == {"in_place", "rewrite", "export_rebuild"}


def test_migration_kind_string_round_trip():
    assert MigrationKind("in_place") == MigrationKind.IN_PLACE
    assert MigrationKind("rewrite") == MigrationKind.REWRITE
    assert MigrationKind("export_rebuild") == MigrationKind.EXPORT_REBUILD


def test_migration_spec_dataclass_fields():
    spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="Add namespace column to chunks table",
        introduced_at=0,
    )
    assert spec.name == "migrate_namespace"
    assert spec.kind == MigrationKind.IN_PLACE
    assert spec.description == "Add namespace column to chunks table"
    assert spec.introduced_at == 0
    spec2 = MigrationSpec(
        name="migrate_reembed",
        kind=MigrationKind.REWRITE,
        description="Re-embed after model upgrade",
        introduced_at=1,
    )
    assert spec2.introduced_at == 1
    assert spec2.kind == MigrationKind.REWRITE


def test_migration_job_dataclass_fields():
    job = MigrationJob(
        job_id="m1",
        status=JobStatus.QUEUED,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        collection="docs",
        kind=MigrationKind.REWRITE,
    )
    assert job.job_id == "m1"
    assert job.status == JobStatus.QUEUED
    assert job.collection == "docs"
    assert job.kind == MigrationKind.REWRITE
    assert job.migrations_applied == []
    assert job.backup_confirmed is None
    assert job.source == "user"
    assert isinstance(job, IngestJob)
    assert job.namespace == "default"
    assert job.progress is None
    assert job.result is None
    assert job.error is None


def test_migration_job_migrations_applied_not_shared():
    a = MigrationJob(
        job_id="a", status=JobStatus.QUEUED,
        created_at="t", updated_at="t", collection="col", kind=MigrationKind.IN_PLACE,
    )
    b = MigrationJob(
        job_id="b", status=JobStatus.QUEUED,
        created_at="t", updated_at="t", collection="col", kind=MigrationKind.IN_PLACE,
    )
    a.migrations_applied.append("migrate_namespace")
    assert b.migrations_applied == []


def test_migration_job_serializable():
    job = MigrationJob(
        job_id="m2",
        status=JobStatus.DONE,
        created_at="t",
        updated_at="t",
        collection="docs",
        kind=MigrationKind.IN_PLACE,
        migrations_applied=["migrate_namespace"],
        backup_confirmed=True,
    )
    d = dataclasses.asdict(job)
    assert d["kind"] == "in_place"
    assert d["migrations_applied"] == ["migrate_namespace"]
    assert d["backup_confirmed"] is True
    assert json.dumps(d)  # must be JSON-serializable


def test_migration_job_export_rebuild_kind():
    job = MigrationJob(
        job_id="m3",
        status=JobStatus.QUEUED,
        created_at="t",
        updated_at="t",
        collection="col",
        kind=MigrationKind.EXPORT_REBUILD,
    )
    assert job.kind == MigrationKind.EXPORT_REBUILD
    d = dataclasses.asdict(job)
    assert d["kind"] == "export_rebuild"
    assert json.dumps(d)  # must be JSON-serializable


def test_migration_job_backup_confirmed_false_distinct_from_none():
    job = MigrationJob(
        job_id="m4",
        status=JobStatus.QUEUED,
        created_at="t",
        updated_at="t",
        collection="col",
        kind=MigrationKind.REWRITE,
        backup_confirmed=False,
    )
    assert job.backup_confirmed is False
    d = dataclasses.asdict(job)
    assert d["backup_confirmed"] is False  # not None


def test_migration_spec_serializable():
    spec = MigrationSpec(
        name="migrate_namespace",
        kind=MigrationKind.IN_PLACE,
        description="Add namespace column",
        introduced_at=0,
    )
    d = dataclasses.asdict(spec)
    assert d["name"] == "migrate_namespace"
    assert d["kind"] == "in_place"
    assert d["description"] == "Add namespace column"
    assert d["introduced_at"] == 0
    assert json.dumps(d)  # must be JSON-serializable


def test_migration_kind_invalid_value_raises():
    with pytest.raises(ValueError):
        MigrationKind("bogus_kind")


def test_migration_job_dict_round_trip_requires_kind_coercion():
    """Verify that dataclasses.asdict() produces 'in_place' string (not MigrationKind.IN_PLACE),
    and that reconstruction from the dict requires explicit MigrationKind() coercion.
    This documents the known contract for JobStore._load() (BE-10).
    """
    job = MigrationJob(
        job_id="m5",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        collection="docs",
        kind=MigrationKind.REWRITE,
        migrations_applied=["migrate_namespace"],
        backup_confirmed=True,
        namespace="tenantA",
        progress={"processed": 50, "total": 200, "phase": "rewrite"},
        result={"migrated_chunks": 50, "migrations_applied": ["migrate_namespace"], "kind": "rewrite"},
        error=None,
    )
    d = dataclasses.asdict(job)
    # asdict converts enum to its string value
    assert d["kind"] == "rewrite"
    assert d["status"] == "DONE"
    # Reconstruction from dict requires coercion: kind comes back as str, not MigrationKind
    raw_kind = d["kind"]
    assert isinstance(raw_kind, str)
    # Coercion works correctly — BE-10 _load() must do this
    assert MigrationKind(raw_kind) == MigrationKind.REWRITE
    # Full round-trip with manual coercion
    d_copy = dict(d)
    d_copy["kind"] = MigrationKind(d_copy["kind"])
    d_copy["status"] = JobStatus(d_copy["status"])
    reconstructed = MigrationJob(**d_copy)
    assert reconstructed.kind == MigrationKind.REWRITE
    assert reconstructed.migrations_applied == ["migrate_namespace"]
    assert reconstructed.backup_confirmed is True
    assert reconstructed.namespace == "tenantA"
    assert json.dumps(dataclasses.asdict(reconstructed))  # still JSON-serializable
