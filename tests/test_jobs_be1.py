"""BE-1 tests: JobKind enum, SyncJob, MetadataReindexJob, store round-trip."""
from __future__ import annotations

from pathlib import Path

import json

from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.types import (
    CommunityRebuildJob,
    JobKind,
    MetadataReindexJob,
    MigrationJob,
    MigrationKind,
    SyncJob,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job_store(tmp_path: Path):
    """Return a fresh JobStore backed by a temp file inside the temp directory."""
    from archon_search.jobs.store import JobStore

    return JobStore(tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_create_sync_job_starts_queued(tmp_path):
    """job_store.create_sync() returns SyncJob with status=QUEUED and kind=JobKind.sync."""
    store = _make_job_store(tmp_path)
    job = store.create_sync(namespace="default")

    assert type(job) is SyncJob
    assert job.status == JobStatus.QUEUED
    assert job.kind == JobKind.sync
    assert job.collection == ""  # SyncJob is whole-server, no collection


def test_create_metadata_reindex_job_starts_queued(tmp_path):
    """Factory returns MetadataReindexJob with status=QUEUED and correct collection."""
    store = _make_job_store(tmp_path)
    job = store.create_metadata_reindex(collection="mycol", namespace="default")

    assert type(job) is MetadataReindexJob
    assert job.status == JobStatus.QUEUED
    assert job.kind == JobKind.metadata_reindex
    assert job.collection == "mycol"


def test_sync_job_kind_emitted_in_job_to_dict(tmp_path):
    """job_to_dict(sync_job)["kind"] == JobKind.sync.value, not None or a bare string."""
    store = _make_job_store(tmp_path)
    sync_job = store.create_sync(namespace="default")

    d = job_to_dict(sync_job)
    assert d["kind"] == JobKind.sync.value  # "sync"
    assert d["kind"] is not None

    meta_job = store.create_metadata_reindex(collection="col", namespace="default")
    d2 = job_to_dict(meta_job)
    assert d2["kind"] == JobKind.metadata_reindex.value  # "metadata_reindex"


def test_sync_and_metadata_reindex_jobs_round_trip_through_store(tmp_path):
    """Write via _write_atomic, read via _load; correct concrete types and enum kind."""
    store = _make_job_store(tmp_path)
    sync_job = store.create_sync(namespace="default")
    meta_job = store.create_metadata_reindex(collection="col", namespace="default")

    # Simulate server restart: new store instance reads from the same file
    store2 = _make_job_store(tmp_path)

    loaded_sync = store2.get(sync_job.job_id)
    loaded_meta = store2.get(meta_job.job_id)

    # Must be the concrete subtype, never bare IngestJob
    assert type(loaded_sync) is SyncJob, f"Expected SyncJob, got {type(loaded_sync)}"
    assert type(loaded_meta) is MetadataReindexJob, f"Expected MetadataReindexJob, got {type(loaded_meta)}"

    # kind must be an enum instance, not a plain string
    # isinstance, not ==: JobKind(str, Enum) means "sync" == JobKind.sync, so == alone is insufficient
    assert isinstance(loaded_sync.kind, JobKind), f"Expected JobKind, got {type(loaded_sync.kind)}"
    assert loaded_sync.kind == JobKind.sync

    # isinstance, not ==: JobKind(str, Enum) means "metadata_reindex" == JobKind.metadata_reindex, so == alone is insufficient
    assert isinstance(loaded_meta.kind, JobKind), f"Expected JobKind, got {type(loaded_meta.kind)}"
    assert loaded_meta.kind == JobKind.metadata_reindex

    # Status must survive (QUEUED — verify crash-recovery did NOT fire)
    assert loaded_sync.status == JobStatus.QUEUED
    assert loaded_meta.status == JobStatus.QUEUED

    # Collection must survive
    assert loaded_sync.collection == ""
    assert loaded_meta.collection == "col"

    # job_to_dict on reloaded jobs must still emit kind correctly (S25: restart → GET /jobs/{id} path)
    assert job_to_dict(loaded_sync)["kind"] == JobKind.sync.value
    assert job_to_dict(loaded_meta)["kind"] == JobKind.metadata_reindex.value


def test_crash_recovery_preserves_concrete_type(tmp_path):
    """RUNNING SyncJob reloads as SyncJob with FAILED + error='process_restart'."""
    store = _make_job_store(tmp_path)
    sync_job = store.create_sync(namespace="default")
    store.update(sync_job.job_id, status=JobStatus.RUNNING)

    # Reload with a fresh store (simulates process restart)
    store2 = _make_job_store(tmp_path)
    reloaded = store2.get(sync_job.job_id)

    assert type(reloaded) is SyncJob
    assert reloaded.status == JobStatus.FAILED
    assert reloaded.error == "process_restart"
    # isinstance, not ==: JobKind(str, Enum) means "sync" == JobKind.sync, so == alone is insufficient
    assert isinstance(reloaded.kind, JobKind)
    assert reloaded.kind == JobKind.sync


def test_crash_recovery_metadata_reindex_preserves_concrete_type(tmp_path):
    """RUNNING MetadataReindexJob reloads as MetadataReindexJob with FAILED."""
    store = _make_job_store(tmp_path)
    meta_job = store.create_metadata_reindex(collection="col", namespace="default")
    store.update(meta_job.job_id, status=JobStatus.RUNNING)

    store2 = _make_job_store(tmp_path)
    reloaded = store2.get(meta_job.job_id)

    assert type(reloaded) is MetadataReindexJob
    assert reloaded.status == JobStatus.FAILED
    assert reloaded.error == "process_restart"
    # isinstance, not ==: JobKind(str, Enum) means "metadata_reindex" == JobKind.metadata_reindex, so == alone is insufficient
    assert isinstance(reloaded.kind, JobKind)
    assert reloaded.kind == JobKind.metadata_reindex


def test_other_job_types_not_misclassified_as_sync_or_metadata_reindex(tmp_path):
    """Negative-control: IngestJob and CommunityRebuildJob round-trip as their own types."""
    store = _make_job_store(tmp_path)
    # Create a SyncJob too, to ensure the ladder handles mixed jobs correctly
    _ = store.create_sync(namespace="default")

    # Create a plain IngestJob via create() (the base factory)
    ingest_job = store.create(
        path="/some/file.txt",
        collection="mycol",
        namespace="default",
    )
    # CommunityRebuildJob via create_community_rebuild
    crj = store.create_community_rebuild(collection="mycol", namespace="default")

    store2 = _make_job_store(tmp_path)

    reloaded_ingest = store2.get(ingest_job.job_id)
    reloaded_crj = store2.get(crj.job_id)

    assert type(reloaded_ingest) is IngestJob, f"Expected IngestJob, got {type(reloaded_ingest)}"
    assert type(reloaded_crj) is CommunityRebuildJob, f"Expected CommunityRebuildJob, got {type(reloaded_crj)}"

    # Neither should have kind
    assert job_to_dict(reloaded_ingest)["kind"] is None
    assert job_to_dict(reloaded_crj)["kind"] is None


def test_job_to_dict_kind_is_none_for_jobs_without_kind_field(tmp_path):
    """job_to_dict returns kind=None for IngestJob (no kind field)."""
    store = _make_job_store(tmp_path)
    ingest_job = store.create(
        path="/some/file.txt",
        collection="mycol",
        namespace="default",
    )
    d = job_to_dict(ingest_job)
    assert d["kind"] is None


def test_migration_job_kind_survives_round_trip_alongside_sync_jobs(tmp_path):
    """MigrationJob.kind (MigrationKind) is not confused with JobKind after BE-1 adds new branches.

    This is the adversarial negative-control: MigrationJob is the only other job type
    with a `kind` attribute. The _load ladder must keep MigrationKind(item["kind"]) and
    not accidentally coerce it through JobKind — and job_to_dict must still emit the
    correct value.
    """
    store = _make_job_store(tmp_path)
    # Create all three job types in the same file to prove mixed-file ladder ordering
    sync_job = store.create_sync(namespace="default")
    meta_job = store.create_metadata_reindex(collection="col", namespace="default")
    mig_job = store.create_migration(
        collection="col",
        kind=MigrationKind.IN_PLACE,
        backup_confirmed=None,
        namespace="default",
    )

    store2 = _make_job_store(tmp_path)

    loaded_sync = store2.get(sync_job.job_id)
    loaded_meta = store2.get(meta_job.job_id)
    loaded_mig = store2.get(mig_job.job_id)

    # MigrationJob must reload as MigrationJob (not SyncJob or MetadataReindexJob)
    assert type(loaded_mig) is MigrationJob, f"Expected MigrationJob, got {type(loaded_mig)}"
    # kind must be MigrationKind, not JobKind
    assert isinstance(loaded_mig.kind, MigrationKind), f"Expected MigrationKind, got {type(loaded_mig.kind)}"
    assert loaded_mig.kind == MigrationKind.IN_PLACE
    # job_to_dict must emit the MigrationKind value, not a JobKind value
    assert job_to_dict(loaded_mig)["kind"] == MigrationKind.IN_PLACE.value

    # The new job types must still reload correctly in the same mixed store
    assert type(loaded_sync) is SyncJob
    assert type(loaded_meta) is MetadataReindexJob
    assert isinstance(loaded_sync.kind, JobKind)
    assert isinstance(loaded_meta.kind, JobKind)


def test_write_atomic_emits_correct_discriminator_string_on_disk(tmp_path):
    """_write_atomic emits job_type='sync'/'metadata_reindex' as the on-disk discriminator.

    Verifies the write-side ladder independently from the read-side (_load), so a
    rename that touches only one side would be caught.
    """
    store = _make_job_store(tmp_path)
    sync_job = store.create_sync(namespace="default")
    meta_job = store.create_metadata_reindex(collection="col", namespace="default")

    jobs_file = tmp_path / "jobs.json"
    raw = json.loads(jobs_file.read_text())

    sync_row = next(r for r in raw if r["job_id"] == sync_job.job_id)
    meta_row = next(r for r in raw if r["job_id"] == meta_job.job_id)

    assert sync_row["job_type"] == JobKind.sync.value, f"Expected 'sync', got {sync_row['job_type']!r}"
    assert meta_row["job_type"] == JobKind.metadata_reindex.value, f"Expected 'metadata_reindex', got {meta_row['job_type']!r}"
    # Also verify kind is persisted as a plain string, not an enum repr
    assert sync_row["kind"] == "sync"
    assert meta_row["kind"] == "metadata_reindex"
