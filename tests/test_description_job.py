"""BE-33: the ``DescriptionJob`` kind (job type ``description``) — persisted shape, delete-guard class, crash recovery."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from archon_search.jobs.model import _job_type, job_to_dict
from archon_search.jobs.store import (
    COLLECTION_WRITING_JOB_STATUSES,
    NEVER_BLOCKING_JOB_KINDS,
    PROCESS_RESTART_ERROR,
    JobStore,
    may_write_collection,
)
from archon_search.types import DescriptionJob, IngestJob, JobStatus

#: Keys of a persisted row that are not ``IngestJob`` fields: the type tag an older loader pops and ignores.
_TYPE_TAG = "job_type"
#: Base fields a row omits while they hold their defaults (``JobStore._write_atomic``).
_OMITTED_WHEN_DEFAULT = {"ingest_options", "cancel_requested", "cancelled_before_start"}


def _persisted_rows(jobs_path: Path) -> list[dict]:
    return json.loads(jobs_path.read_text())


def test_persisted_description_job_holds_only_base_ingest_fields(tmp_path: Path) -> None:
    """A description job adds no field to ``IngestJob``, so its row holds base fields and the ``job_type`` tag only:
    an older loader (unknown tag -> ``IngestJob(**item)``) rebuilds it, where an extra key would raise inside the load
    and reset the whole jobs file (S81)."""
    jobs_path = tmp_path / "jobs.json"
    job = JobStore(path=jobs_path).create_description("docs", namespace="team-a")

    base_fields = {f.name for f in dataclasses.fields(IngestJob)}
    assert {f.name for f in dataclasses.fields(DescriptionJob)} == base_fields
    (row,) = _persisted_rows(jobs_path)
    assert row[_TYPE_TAG] == "description"
    assert set(row) - {_TYPE_TAG} == base_fields - _OMITTED_WHEN_DEFAULT

    rebuilt = IngestJob(**{k: v for k, v in row.items() if k != _TYPE_TAG} | {"status": JobStatus(row["status"])})
    assert (rebuilt.job_id, rebuilt.collection, rebuilt.namespace) == (job.job_id, "docs", "team-a")
    assert job_to_dict(job)["kind"] is None


def test_description_job_is_created_running_for_its_collection_and_reloads_as_its_own_kind(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    created = JobStore(path=jobs_path).create_description("docs", namespace="team-a")

    reloaded = JobStore(path=jobs_path).get(created.job_id)

    assert created.status is JobStatus.RUNNING
    assert type(reloaded) is DescriptionJob
    assert (reloaded.collection, reloaded.namespace) == ("docs", "team-a")
    assert _job_type(reloaded) == "description"
    assert job_to_dict(reloaded)["job_type"] == "description"


def test_description_job_classified_never_blocking(tmp_path: Path) -> None:
    """A describe run only fills the meta row through the guarded write (and finds a dropped collection gone), so it
    is a never-blocking kind: in that set, not in the writing table, and ``may_write_collection`` is False in every
    status (S87)."""
    assert DescriptionJob in NEVER_BLOCKING_JOB_KINDS
    assert DescriptionJob not in COLLECTION_WRITING_JOB_STATUSES
    running = JobStore(path=tmp_path / "jobs.json").create_description("docs")
    for status in JobStatus:
        assert may_write_collection(dataclasses.replace(running, status=status)) is False, status


def test_running_description_job_at_crash_loads_failed_without_arming_guard(tmp_path: Path) -> None:
    """A describe run killed mid-flight reloads FAILED ``process_restart`` like any crashed job, but its
    ``job_type`` is outside the ingest family, so the crash-loop guard stays unarmed (a model call is no
    ingest workload)."""
    jobs_path = tmp_path / "jobs.json"
    created = JobStore(path=jobs_path).create_description("docs")

    reloaded_store = JobStore(path=jobs_path)

    reloaded = reloaded_store.get(created.job_id)
    assert type(reloaded) is DescriptionJob
    assert (reloaded.status, reloaded.error) == (JobStatus.FAILED, PROCESS_RESTART_ERROR)
    assert reloaded_store.crashed_ingest_on_load is False
