"""BE-33: an older loader reads a description job as a plain ingest job; ``DELETE /jobs/{id}`` refuses it (S81, S87)."""
from __future__ import annotations

import json

import pytest

import archon_search.jobs.store as store_module
from archon_search.jobs.store import PROCESS_RESTART_ERROR, JobStore
from archon_search.server.routes_jobs import _DESCRIPTION_JOB_NOT_CANCELLABLE_DETAIL
from archon_search.types import DescriptionJob, IngestJob, JobStatus
from tests._job_store_faults import persisted_statuses, recording_job_writes
from tests.integration.conftest import JOBS_FILE_NAME, make_real_app

pytestmark = pytest.mark.integration

#: A ``job_type`` tag the loader has no branch for, as a description row is to a server that predates the kind.
_UNKNOWN_TO_THE_LOADER = "kind-from-the-future"


def test_older_loader_reads_description_job_as_plain_ingest_job(tmp_path, monkeypatch) -> None:
    """A jobs file holding other jobs plus a finished and a running description job: a loader without the
    description branch (the real ``JobStore._load`` with its tag swapped for one it does not know, so it takes the
    same final ``IngestJob(**item)`` fallback an older server takes) loads the whole file — nothing resets — reads
    both description rows as plain ingest jobs, leaves the other jobs as they were, and marks the running one
    ``FAILED`` ``process_restart`` without arming the crash-loop guard. (Only the load branch is swapped: the
    import-time QUEUED-recovery set is not emulated, and no QUEUED description row is written.)"""
    jobs_path = tmp_path / JOBS_FILE_NAME
    store = JobStore(path=jobs_path)
    sync = store.create_sync()
    store.update(sync.job_id, status=JobStatus.DONE)
    rebuild = store.create_community_rebuild("other")
    store.update(rebuild.job_id, status=JobStatus.DONE)
    ingest = store.create(path="/f.txt", collection="other")
    store.update(ingest.job_id, status=JobStatus.DONE)
    finished = store.create_description("docs")
    store.update(finished.job_id, status=JobStatus.DONE, result={"outcome": "generated", "description_status": "ok"})
    running = store.create_description("docs")
    assert {row["job_id"]: row["job_type"] for row in json.loads(jobs_path.read_text())} == {
        sync.job_id: "sync",
        rebuild.job_id: "community_rebuild",
        ingest.job_id: "ingest",
        finished.job_id: "description",
        running.job_id: "description",
    }
    # Each loader gets its own copy: loading rewrites crashed rows in place.
    older_path, current_path = tmp_path / "older.json", tmp_path / "current.json"
    older_path.write_text(jobs_path.read_text())
    current_path.write_text(jobs_path.read_text())
    current = JobStore(path=current_path)
    assert type(current.get(finished.job_id)) is DescriptionJob

    monkeypatch.setattr(store_module, "_DESCRIPTION_JOB_TYPE", _UNKNOWN_TO_THE_LOADER)
    older = JobStore(path=older_path)

    assert {job.job_id for job in older.list()} == {sync.job_id, rebuild.job_id, ingest.job_id, finished.job_id, running.job_id}
    for job in (finished, running):
        assert type(older.get(job.job_id)) is IngestJob
    assert (older.get(finished.job_id).status, older.get(finished.job_id).result) == (
        JobStatus.DONE,
        {"outcome": "generated", "description_status": "ok"},
    )
    for job_id in (sync.job_id, rebuild.job_id, ingest.job_id):
        assert older.get(job_id) == current.get(job_id), "an existing job must read the same either way"
    crashed = older.get(running.job_id)
    assert (crashed.status, crashed.error) == (JobStatus.FAILED, PROCESS_RESTART_ERROR)
    assert older.crashed_ingest_on_load is False
    assert current.crashed_ingest_on_load is False
    recovered = current.get(running.job_id)
    assert type(recovered) is DescriptionJob
    assert (recovered.status, recovered.error) == (JobStatus.FAILED, PROCESS_RESTART_ERROR)


def test_delete_job_answers_409_and_generation_continues(tmp_path, monkeypatch) -> None:
    """DELETE /jobs/{id} on a RUNNING description job is refused: 409, the job is not written (never CANCELLING,
    which a crash would read as a restart), and it stays RUNNING for the generation that runs it. A foreign
    namespace reads it as missing; a terminal job answers the idempotent 200."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        store = client.app.state.job_store
        job = store.create_description("docs")
        foreign = store.create_description("docs", namespace="other-ns")

        with recording_job_writes() as writes:
            delete = client.delete(f"/jobs/{job.job_id}", headers=headers)
            foreign_delete = client.delete(f"/jobs/{foreign.job_id}", headers=headers)

        assert (delete.status_code, delete.json()) == (409, {"detail": _DESCRIPTION_JOB_NOT_CANCELLABLE_DETAIL})
        assert (foreign_delete.status_code, foreign_delete.json()) == (404, {"detail": "Job not found"})
        assert writes.attempts == 0, "a refused DELETE must not write the job store"
        assert store.get(job.job_id).status is JobStatus.RUNNING
        assert persisted_statuses(tmp_path / JOBS_FILE_NAME)[job.job_id] == JobStatus.RUNNING.value
        body = client.get(f"/jobs/{job.job_id}", headers=headers).json()
        assert (body["status"], body["job_type"], body["kind"]) == ("RUNNING", "description", None)

        store.update(job.job_id, status=JobStatus.DONE, result={"outcome": "generated", "description_status": "ok"})
        done = client.delete(f"/jobs/{job.job_id}", headers=headers)
        assert (done.status_code, done.json()["status"]) == (200, "DONE")


def test_kind_filter_selects_description_jobs_and_resume_refuses_them(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        store = client.app.state.job_store
        description = store.create_description("docs")
        ingest = store.create(path="/f.txt", collection="docs")

        listed = client.get("/jobs", params={"kind": "description"}, headers=headers).json()
        resume = client.post(f"/jobs/{description.job_id}/resume", headers=headers)

        assert [item["job_id"] for item in listed["items"]] == [description.job_id]
        assert ingest.job_id not in {item["job_id"] for item in listed["items"]}
        assert (resume.status_code, resume.json()["error"]) == (409, "job_not_resumable")


def test_delete_job_openapi_409_names_the_description_job(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _api_key):
        responses = client.app.openapi()["paths"]["/jobs/{job_id}"]["delete"]["responses"]

        assert "description job" in responses["409"]["description"]
        assert _DESCRIPTION_JOB_NOT_CANCELLABLE_DETAIL in responses["409"]["description"]
