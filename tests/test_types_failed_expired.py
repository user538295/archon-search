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


def test_cli_commands_share_helpers_terminal_statuses():
    """backup_cmd and export_cmd (export --wait and import --wait) reuse _helpers._TERMINAL_STATUSES."""
    import archon_search.cli._helpers as helpers_mod
    import archon_search.cli.backup_cmd as backup_mod
    import archon_search.cli.export_cmd as export_mod
    assert backup_mod._TERMINAL_STATUSES is helpers_mod._TERMINAL_STATUSES
    assert export_mod._TERMINAL_STATUSES is helpers_mod._TERMINAL_STATUSES


def test_failed_expired_is_terminal_in_helpers():
    """cli/_helpers.py _TERMINAL_STATUSES includes FAILED_EXPIRED (a JobStatus member, equal to the wire string)."""
    import archon_search.cli._helpers as helpers_mod
    assert "FAILED_EXPIRED" in helpers_mod._TERMINAL_STATUSES


def test_job_status_enum_members():
    """Adding FAILED_EXPIRED must not remove or rename any existing JobStatus member."""
    existing = {"PENDING", "QUEUED", "RUNNING", "DONE", "FAILED", "CANCELLED", "CANCELLING"}
    actual = {s.value for s in JobStatus}
    assert existing.issubset(actual), f"Existing members changed: {existing - actual}"


def test_in_progress_statuses_are_not_terminal_in_helpers():
    """cli/_helpers.py _TERMINAL_STATUSES excludes every in-progress status, so --wait keeps polling them."""
    import archon_search.cli._helpers as helpers_mod
    in_progress = {JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLING}
    assert in_progress.isdisjoint(helpers_mod._TERMINAL_STATUSES)
