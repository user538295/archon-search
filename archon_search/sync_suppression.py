"""Sticky sentinel for the startup-sync crash-loop guard.

The lifespan's crash-loop guard (``server/app.py``) suppresses the startup
collection sync when the previous process died mid-ingest. Suppression must
survive a process restart — otherwise a supervisor that restarts more than
once (launchd ``KeepAlive``, systemd ``Restart=``) sees crash / skip / crash /
skip instead of the loop actually breaking, because the crashed job row is
only RUNNING (and therefore guard-triggering) on the very next load; by the
boot after that it is already terminal (``FAILED``) and says nothing.

This sentinel is the durable, server-wide record of that suppression. It is
deliberately NOT stored in the jobs file: the jobs file is a namespace-scoped
job ledger and a forensic artifact (see
``Documentation/Backlog/2026-08-19-000-oom-crash-incident-report.md``),
while suppression is server-wide, namespace-independent state — mixing the
two would be exactly the domain-model mismatch a clean-code pass would flag.

Path resolution mirrors ``archon_search.jobs.model.get_jobs_file()``: resolved
fresh on every call from ``get_data_dir()``, with no per-path env var override
(``ARCHON_SEARCH_DATA_DIR`` remains the only scope knob — see ``paths.py``).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from archon_search._durable_io import atomic_write_json
from archon_search.paths import get_data_dir

logger = logging.getLogger(__name__)

_SENTINEL_FILENAME = "archon-search-sync-suppressed.json"


def get_sync_suppressed_file() -> Path:
    """Return the sync-suppression sentinel file path, resolved fresh on every call.

    Always derived from ``get_data_dir()``; there is no per-path env var
    override (deliberately scoped to ``ARCHON_SEARCH_DATA_DIR`` only, mirroring
    ``jobs.model.get_jobs_file()``).
    """
    return get_data_dir() / _SENTINEL_FILENAME


def write_sync_suppressed_sentinel() -> None:
    """Persist the crash-loop guard's suppression so it survives a restart.

    Body is a small JSON object with exactly one field, ``suppressed_at`` (an
    ISO timestamp) — enough to tell an operator *when* the guard fired.  Does
    not grow into a schema: if a second field is ever needed, use a bare empty
    file instead.

    Fail-open, matching ``_start_startup_sync_job``'s posture: a write failure
    is logged at WARNING and never raised. The startup sync itself was already
    suppressed by the time this is called, so a sentinel write failure must
    not additionally fail startup.
    """
    path = get_sync_suppressed_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"suppressed_at": datetime.now(UTC).isoformat()})
    except OSError:
        logger.warning(
            "could not write sync-suppression sentinel %s; suppression will not "
            "survive a process restart",
            path,
            exc_info=True,
        )


def clear_sync_suppressed_sentinel() -> None:
    """Remove the sentinel on the sanctioned resume path (a clean manual sync).

    Fail-open: an unlink failure is logged at WARNING and never raised — it
    must not fail the sync request that triggered the clear.
    """
    path = get_sync_suppressed_file()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "could not clear sync-suppression sentinel %s", path, exc_info=True
        )
