"""S798: re-running an import cancelled mid-``ingesting`` without ``force_overwrite``.

The manual (90_export_import.md, "Cancelling an import"; 100_jobs_and_async_operations.md) says a cancel during
``ingesting`` leaves the batches written so far in place, and that the re-run without ``--force-overwrite`` is
*accepted* (``202``) and the job then ends ``FAILED`` with ``collection '<name>' already exists; use
force_overwrite=true to overwrite`` (``import --wait`` exits ``2``). The route instead answers ``409`` up front,
because the import's first batch already writes the collection's meta row (``ingest_chunks(embedding_model=...)``).
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from archon_search.cli.export_cmd import import_cmd
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.server import routes_export
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from archon_search.types import ImportJob, JobStatus
from tests.test_import_worker import _chunk, _doc_id, _make_archive, _make_import_job
from tests.test_routes_export import _own_exports

_COLLECTION = "s798-target"
_ARCHIVE_DOCS = 6
_CHECKPOINT_INTERVAL = 2
_ALREADY_EXISTS = f"collection {_COLLECTION!r} already exists; use force_overwrite=true to overwrite"


class _Env:
    """A real SearchStore and JobStore behind the real app, left holding a mid-``ingesting``-cancelled import."""

    def __init__(self, tmp_path: Path, auth_headers: dict[str, str]) -> None:
        self.config = SearchConfig()
        self.config.db_path = str(tmp_path / "search")
        self.config.jobs.checkpoint_interval = _CHECKPOINT_INTERVAL
        self.job_store = JobStore(path=tmp_path / "jobs.json")
        self.search_store = SearchStore(tmp_path / "db")
        asyncio.run(self.search_store.connect())
        app = create_app(self.config, self.job_store)
        app.state.search_store = self.search_store
        self.client = TestClient(app, headers=auth_headers)
        archive_dir = _own_exports("s798_rerun_after_ingesting_cancel")
        archive_dir.mkdir(parents=True, exist_ok=True)
        self.archive = archive_dir / f"{_COLLECTION}.tar.gz"
        doc_id = _doc_id()
        _make_archive(
            self.archive, _COLLECTION, [_chunk(doc_id, i) for i in range(_ARCHIVE_DOCS)],
            active_embedding_model=self.config.embedding_model,
        )

    def run_job(self, job_id: str, *, cancel_in_warmup: bool = False) -> ImportJob:
        """Run the queued import job the way the scheduler would, with the model/index steps stubbed out."""
        from unittest.mock import AsyncMock, MagicMock

        job = self.job_store.get(job_id)
        assert isinstance(job, ImportJob)
        pipeline = MagicMock()
        pipeline.recompute_collection_meta = AsyncMock()
        if cancel_in_warmup:

            async def cancel(*_: object, **__: object) -> bool:
                self.job_store.update(job_id, status=JobStatus.CANCELLING)
                return True

            pipeline.warmup_models = AsyncMock(side_effect=cancel)
        else:
            pipeline.warmup_models = AsyncMock(return_value=True)
        cache = MagicMock()
        cache.get_or_load = AsyncMock(return_value=MagicMock())
        asyncio.run(routes_export._import_task(job, self.job_store, self.search_store, pipeline, cache, self.config))
        finished = self.job_store.get(job_id)
        assert isinstance(finished, ImportJob)
        return finished

    def import_archive(self, *, force_overwrite: bool = False, cancel_in_warmup: bool = False) -> ImportJob:
        """Import the archive uncancelled (or cancelled in the final warm-up) and return the finished job."""
        job = _make_import_job(_COLLECTION, str(self.archive), self.job_store, force_overwrite=force_overwrite)
        return self.run_job(job.job_id, cancel_in_warmup=cancel_in_warmup)

    def post_import(self, *, force_overwrite: bool = False) -> object:
        return self.client.post(
            f"/collections/{_COLLECTION}/import",
            json={"path": str(self.archive), "force_overwrite": force_overwrite},
        )

    def cancel_first_import_during_ingesting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Import the archive; a ``DELETE /jobs/{id}`` lands right after the first batch is stored."""
        job = _make_import_job(_COLLECTION, str(self.archive), self.job_store)
        real_ingest = self.search_store.ingest_chunks

        async def ingest_then_cancel(*args: object, **kwargs: object) -> object:
            result = await real_ingest(*args, **kwargs)  # type: ignore[arg-type]
            self.job_store.update(job.job_id, status=JobStatus.CANCELLING)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(self.search_store, "ingest_chunks", ingest_then_cancel)
            cancelled = self.run_job(job.job_id)
        assert cancelled.status == JobStatus.CANCELLED, f"setup: expected CANCELLED, got {cancelled.status}"
        stored = asyncio.run(self.search_store.count_chunks(_COLLECTION, DEFAULT_NAMESPACE))
        assert 0 < stored < _ARCHIVE_DOCS, f"setup: a partial collection must stay, found {stored} chunks"

    def close(self) -> None:
        asyncio.run(self.search_store.disconnect())


@pytest.fixture
def cancelled_env(
    tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Env]:
    env = _Env(tmp_path, auth_headers)
    try:
        env.cancel_first_import_during_ingesting(monkeypatch)
        yield env
    finally:
        env.close()


@pytest.mark.integration
def test_rerun_without_force_after_ingesting_cancel_is_accepted_and_job_fails_already_exists(
    cancelled_env: _Env,
) -> None:
    """90_export_import.md:196 — ``202`` (not ``409``), then the job ends FAILED with the ``already exists`` error."""
    response = cancelled_env.client.post(
        f"/collections/{_COLLECTION}/import", json={"path": str(cancelled_env.archive)},
    )

    assert response.status_code == 202, f"expected 202, got {response.status_code}: {response.text}"
    finished = cancelled_env.run_job(response.json()["job_id"])
    assert finished.status == JobStatus.FAILED
    assert finished.error == _ALREADY_EXISTS


@pytest.mark.integration
def test_cli_import_wait_rerun_without_force_after_ingesting_cancel_exits_2(cancelled_env: _Env) -> None:
    """90_export_import.md:104,196 — ``import --wait`` of that re-run exits ``2`` with ``Import FAILED: <error>``."""

    def post(url: str, json: dict, headers: dict | None = None, **_: object) -> object:
        response = cancelled_env.client.post(urlsplit(url).path, json=json)
        if response.status_code == 202:
            cancelled_env.run_job(response.json()["job_id"])
        return response

    def get(url: str, headers: dict | None = None, **_: object) -> object:
        return cancelled_env.client.get(urlsplit(url).path)

    from unittest.mock import patch

    with (
        patch("archon_search.cli.export_cmd.httpx.post", side_effect=post),
        patch("archon_search.cli.export_cmd.httpx.get", side_effect=get),
    ):
        result = CliRunner().invoke(
            import_cmd,
            [_COLLECTION, str(cancelled_env.archive), "--wait", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 2, f"exit={result.exit_code}, output={result.output!r}"
    assert f"Import FAILED: {_ALREADY_EXISTS}" in result.output


def _assert_conflict(response: object) -> None:
    assert response.status_code == 409, f"expected 409, got {response.status_code}: {response.text}"  # type: ignore[attr-defined]
    assert response.json()["error"] == "collection_exists"  # type: ignore[attr-defined]


@pytest.mark.integration
def test_leftover_job_is_an_ingesting_cancel(cancelled_env: _Env) -> None:
    """Setup guard: the leftover is a CANCELLED, claimed import in its ``ingesting`` phase, with a meta row stored."""
    (job,) = [j for j in cancelled_env.job_store.list() if isinstance(j, ImportJob)]
    assert job.status == JobStatus.CANCELLED
    assert job.collection_claimed
    assert (job.progress or {}).get("phase") == "ingesting"
    assert asyncio.run(cancelled_env.search_store.get_collection_meta(_COLLECTION, DEFAULT_NAMESPACE)) is not None


@pytest.mark.integration
def test_complete_collection_without_cancelled_job_is_409(
    tmp_path: Path, auth_headers: dict[str, str],
) -> None:
    env = _Env(tmp_path, auth_headers)
    try:
        assert env.import_archive().status == JobStatus.DONE
        _assert_conflict(env.post_import())
    finally:
        env.close()


@pytest.mark.integration
def test_warmup_cancel_leaves_complete_collection_and_rerun_is_409(
    tmp_path: Path, auth_headers: dict[str, str],
) -> None:
    env = _Env(tmp_path, auth_headers)
    try:
        cancelled = env.import_archive(cancel_in_warmup=True)
        assert cancelled.status == JobStatus.CANCELLED
        assert (cancelled.progress or {}).get("phase") == "indexing"
        _assert_conflict(env.post_import())
    finally:
        env.close()


@pytest.mark.integration
def test_rerun_after_force_overwrite_import_completed_is_409(cancelled_env: _Env) -> None:
    """A ``force_overwrite`` import revokes the cancelled job's claim: the collection it left is gone."""
    assert cancelled_env.import_archive(force_overwrite=True).status == JobStatus.DONE

    _assert_conflict(cancelled_env.post_import())


@pytest.mark.integration
def test_cancelled_job_of_another_namespace_does_not_let_rerun_through(
    tmp_path: Path, auth_headers: dict[str, str],
) -> None:
    env = _Env(tmp_path, auth_headers)
    try:
        assert env.import_archive().status == JobStatus.DONE
        (done,) = [j for j in env.job_store.list() if isinstance(j, ImportJob)]
        foreign = _make_import_job(_COLLECTION, str(env.archive), env.job_store, namespace="other-ns")
        env.job_store.update(
            foreign.job_id, status=JobStatus.CANCELLED, collection_claimed=True,
        )
        env.job_store.update_progress(foreign.job_id, 1, _ARCHIVE_DOCS, "ingesting")
        assert done.namespace == DEFAULT_NAMESPACE
        _assert_conflict(env.post_import())
    finally:
        env.close()


@pytest.mark.integration
def test_mcp_rerun_without_force_after_ingesting_cancel_is_accepted(cancelled_env: _Env) -> None:
    """MCP ``import_collection`` answers like REST: a job, not ``collection_exists``."""
    from unittest.mock import MagicMock

    from tests.test_mcp_export import _make_mcp_app

    pipeline = MagicMock()
    pipeline.store = cancelled_env.search_store
    app = _make_mcp_app(pipeline, config=cancelled_env.config, job_store=cancelled_env.job_store)
    tool = app.tools["import_collection"]

    accepted = asyncio.run(tool(collection=_COLLECTION, path=str(cancelled_env.archive)))

    assert "job_id" in accepted, accepted
    finished = cancelled_env.run_job(accepted["job_id"])
    assert finished.status == JobStatus.FAILED
    assert finished.error == _ALREADY_EXISTS


@pytest.mark.integration
def test_mcp_complete_collection_is_collection_exists(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    from unittest.mock import MagicMock

    from tests.test_mcp_export import _make_mcp_app

    env = _Env(tmp_path, auth_headers)
    try:
        assert env.import_archive().status == JobStatus.DONE
        pipeline = MagicMock()
        pipeline.store = env.search_store
        tool = _make_mcp_app(pipeline, config=env.config, job_store=env.job_store).tools["import_collection"]

        result = asyncio.run(tool(collection=_COLLECTION, path=str(env.archive)))

        assert result["code"] == "collection_exists"
    finally:
        env.close()
