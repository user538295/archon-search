"""Tests for BE-4 — FAILED_EXPIRED terminal state in JobStatus enum and all _TERMINAL_STATUSES sets."""
from archon_search.types import JobStatus


def test_failed_expired_in_enum():
    """JobStatus.FAILED_EXPIRED exists and round-trips through the string constructor."""
    assert JobStatus.FAILED_EXPIRED == JobStatus("FAILED_EXPIRED")
    assert JobStatus.FAILED_EXPIRED.value == "FAILED_EXPIRED"


def test_failed_expired_is_terminal_in_store():
    """jobs/store.py _TERMINAL_STATUSES includes FAILED_EXPIRED."""
    import archon_search.jobs.store as store_mod
    assert JobStatus.FAILED_EXPIRED in store_mod._TERMINAL_STATUSES


def test_failed_expired_is_terminal_in_routes_jobs():
    """server/routes_jobs.py _TERMINAL_STATUSES includes FAILED_EXPIRED."""
    import archon_search.server.routes_jobs as routes_mod
    assert JobStatus.FAILED_EXPIRED in routes_mod._TERMINAL_STATUSES


def test_failed_expired_is_terminal_in_backup_cmd():
    """cli/backup_cmd.py _TERMINAL_STATUSES includes FAILED_EXPIRED string literal."""
    import archon_search.cli.backup_cmd as backup_mod
    assert "FAILED_EXPIRED" in backup_mod._TERMINAL_STATUSES


def test_failed_expired_is_terminal_in_export_cmd():
    """cli/export_cmd.py _TERMINAL_STATUSES includes FAILED_EXPIRED string literal."""
    import archon_search.cli.export_cmd as export_mod
    assert "FAILED_EXPIRED" in export_mod._TERMINAL_STATUSES


def test_failed_expired_is_terminal_in_collection():
    """cli/collection.py _TERMINAL_STATUSES includes FAILED_EXPIRED string literal."""
    import archon_search.cli.collection as collection_mod
    assert "FAILED_EXPIRED" in collection_mod._TERMINAL_STATUSES


def test_job_status_enum_members():
    """Adding FAILED_EXPIRED must not remove or rename any existing JobStatus member."""
    existing = {"PENDING", "QUEUED", "RUNNING", "DONE", "FAILED", "CANCELLED", "CANCELLING"}
    actual = {s.value for s in JobStatus}
    assert existing.issubset(actual), f"Existing members changed: {existing - actual}"


def test_failed_expired_stops_import_wait_poll():
    """FAILED_EXPIRED in export_cmd._TERMINAL_STATUSES stops the import --wait poll loop (export_cmd.py:240)."""
    import archon_search.cli.export_cmd as export_mod
    assert "FAILED_EXPIRED" in export_mod._TERMINAL_STATUSES
