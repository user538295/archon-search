"""POST /backup/trigger — manually fan out scheduled backups (D2 Task 4.1).

Enumerates collections in the caller's namespace and enqueues a ``source="backup"``
export job for each one not excluded, not already in-flight, and not already
queued. Returns 202 with the queued job ids and the skipped collections plus a
machine-readable reason for each skip.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from archon_search.config import SearchConfig
from archon_search.jobs.backup_loop import BackupLoop
from archon_search.jobs.store import JobStore
from archon_search.server.schemas import (
    BackupTriggerResponse,
    ErrorDetail,
    SkippedItem,
)
from archon_search.store import SearchStore

logger = logging.getLogger(__name__)

router = APIRouter()

# Mirror the format used by the periodic trigger loop so manual and scheduled
# backups produce visually identical archive filenames.
_ARCHIVE_TIMESTAMP_FORMAT: str = "%Y%m%dT%H%M%SZ"


@router.post(
    "/backup/trigger",
    status_code=202,
    response_model=BackupTriggerResponse,
    responses={401: {"model": ErrorDetail}},
)
async def trigger_backup(request: Request) -> BackupTriggerResponse:
    """Enqueue backup export jobs for every eligible collection in the caller's namespace."""
    ns: str = request.state.namespace
    backup_loop: BackupLoop = request.app.state.backup_loop
    config: SearchConfig = request.app.state.config
    job_store: JobStore = request.app.state.job_store
    search_store: SearchStore = request.app.state.search_store

    collections = await search_store.list_collections()
    # Build O(1) lookup of (ns, col) already represented by a QUEUED backup job
    # so we don't rescan the queue per collection.
    queued = job_store.list_queued_bulk()
    queued_backup_keys: set[tuple[str, str]] = {
        (j.namespace, j.collection)
        for j in queued
        if getattr(j, "source", "user") == "backup"
    }

    ts = datetime.now(timezone.utc).strftime(_ARCHIVE_TIMESTAMP_FORMAT)
    queued_ids: list[str] = []
    skipped: list[SkippedItem] = []

    for info in collections:
        if info.namespace != ns:
            continue
        col = info.name
        if backup_loop._is_excluded(ns, col):
            skipped.append(SkippedItem(collection=col, reason="excluded"))
            continue
        if backup_loop.is_collection_in_flight(ns, col):
            skipped.append(SkippedItem(collection=col, reason="already_active"))
            continue
        if (ns, col) in queued_backup_keys:
            skipped.append(SkippedItem(collection=col, reason="already_queued"))
            continue
        archive_path = str(
            Path(config.backup.output_dir) / ns / f"{col}.backup.{ts}.tar.gz"
        )
        tmp_path = f"{archive_path}.tmp"
        try:
            job = job_store.create_export(
                col, archive_path, tmp_path, namespace=ns, source="backup"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "trigger_backup: failed to enqueue backup for %s/%s: %s", ns, col, exc
            )
            skipped.append(SkippedItem(collection=col, reason="enqueue_failed"))
            continue
        backup_loop.track(job.job_id, ns, col)
        queued_ids.append(job.job_id)

    return BackupTriggerResponse(queued=queued_ids, skipped=skipped)
