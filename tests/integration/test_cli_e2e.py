"""Task 4.1 — Ingest path safety and container env e2e tests.

Exercises CLI-level wiring: path safety on POST /ingest, the `serve` entry
point's host-default flip, data-dir env routing for the key file, container
stderr handler attachment, and the `wizard --dry-run` flow.

All tests run without a real TCP server — they use ``TestClient`` (ASGI
transport) or invoke Click commands via ``CliRunner``.

Run with:
    uv run pytest tests/integration/test_cli_e2e.py -v
"""
from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _noop_install_lock() -> Generator[None, None, None]:
    """No-op replacement for _acquire_install_lock to avoid lock contention.

    The real lock writes to ~/.archon-search/.install.lock.  Without this
    patch, parallel xdist workers running wizard tests concurrently with the
    install xdist_group can deadlock on the shared advisory lock.
    """
    yield


# ---------------------------------------------------------------------------
# Test 1 — Path safety: POST /ingest with a traversal path returns 400
# ---------------------------------------------------------------------------


def test_e2e_ingest_path_safety_full_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /ingest with a traversal path returns 400 and creates no job.

    Verifies that the path-safety check fires at the route handler level
    (not silently swallowed) and that no job entry is persisted.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        # POST /ingest with a path that contains '/..' — the validator must reject it.
        resp = client.post(
            "/ingest",
            json={"collection": "test-col", "path": "/foo/../bar"},
            headers=headers,
        )
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
        detail = resp.json().get("detail", "")
        assert detail.startswith("path is unsafe:"), (
            f"expected detail starting with 'path is unsafe:', got {detail!r}"
        )

        # No job should have been created.
        jobs_resp = client.get("/jobs", headers=headers)
        assert jobs_resp.status_code == 200
        assert jobs_resp.json()["items"] == [], (
            "expected empty job list after rejected ingest, "
            f"got: {jobs_resp.json()['items']}"
        )


# ---------------------------------------------------------------------------
# Test 2 — `serve` entry point: load_config(serve=True) → /ready returns 200
# ---------------------------------------------------------------------------


def test_serve_command_with_real_app_responds_to_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config(serve=True) → create_app(config) → GET /ready returns 200.

    Verifies the `serve` entry point's host-default flip (serve=True) does
    not break routing or app startup when using TestClient.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    # Point at a non-existent path so load_config uses defaults instead of the
    # developer's real ~/.archon-search/archon-search.toml (test-isolation guard;
    # same pattern as tests/smoke/conftest.py).
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(tmp_path / "archon-search.toml"))

    from archon_search.config import load_config
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    # load_config(serve=True) flips host default to 0.0.0.0
    config = load_config(serve=True)
    config.db_path = str(tmp_path / "db")
    config.backup.interval_hours = 0

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=config.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(config, job_store, scheduler=scheduler)

    # Assert the host-default flip actually happened (primary invariant of serve=True).
    assert config.host == "0.0.0.0", (
        f"serve=True must set host default to '0.0.0.0', got {config.host!r}"
    )

    with TestClient(app) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200, f"expected 200 from /ready, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 3 — DATA_DIR env: key file is created under data_dir, not ~/.archon-search
# ---------------------------------------------------------------------------


def test_data_dir_env_routes_key_file_under_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARCHON_SEARCH_DATA_DIR=<tmp_path> routes the key file under tmp_path.

    After app startup the key file must exist under tmp_path, not the default
    ~/.archon-search/ location. Verifies the key_manager lazy-path contract.
    """
    # We set DATA_DIR but NOT API_KEY so the key is auto-generated to disk.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    # Ensure ARCHON_SEARCH_API_KEY is absent so key_manager falls through to file.
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    # Ensure ARCHON_SEARCH_KEY_FILE is absent so resolution uses DATA_DIR.
    monkeypatch.delenv("ARCHON_SEARCH_KEY_FILE", raising=False)

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    config = SearchConfig()
    config.db_path = str(tmp_path / "db")
    config.backup.interval_hours = 0

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=config.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(config, job_store, scheduler=scheduler)

    with TestClient(app):
        pass  # lifespan startup triggers key bootstrap

    expected_key_file = tmp_path / ".search.env"
    assert expected_key_file.exists(), (
        f"key file must be created under DATA_DIR ({tmp_path}), "
        f"but {expected_key_file} does not exist"
    )


# ---------------------------------------------------------------------------
# Test 4 — Container stderr handler: ARCHON_SEARCH_CONTAINER=1 attaches StreamHandler
# ---------------------------------------------------------------------------


def test_container_stderr_handler_attached_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARCHON_SEARCH_CONTAINER=1 → configure_logging attaches a stderr StreamHandler.

    Verifies the container logging path at the function level without spinning
    up the full app.
    """
    from archon_search.config import SearchConfig
    from archon_search.logging_setup import configure_logging

    monkeypatch.setenv("ARCHON_SEARCH_CONTAINER", "1")

    config = SearchConfig()
    config.log_file = ""  # no file handler needed

    archon_logger = logging.getLogger("archon_search")
    # Capture original state so we can restore it after the test.
    original_handlers = archon_logger.handlers[:]
    original_propagate = archon_logger.propagate

    # Remove existing handlers to get a clean baseline.
    for h in archon_logger.handlers[:]:
        archon_logger.removeHandler(h)
        h.close()

    try:
        configure_logging(config)

        stderr_handlers = [
            h
            for h in archon_logger.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        ]
        assert stderr_handlers, (
            "expected at least one StreamHandler targeting sys.stderr when "
            "ARCHON_SEARCH_CONTAINER=1, but none found. "
            f"Handlers: {archon_logger.handlers}"
        )
    finally:
        # Restore original logger state to avoid leaking propagate=False
        # or stale StreamHandlers into other tests within the same xdist worker.
        for h in archon_logger.handlers[:]:
            archon_logger.removeHandler(h)
            h.close()
        for h in original_handlers:
            archon_logger.addHandler(h)
        archon_logger.propagate = original_propagate


# ---------------------------------------------------------------------------
# Test 5 — Unauthenticated dotted path: 401 takes precedence over 400
# ---------------------------------------------------------------------------


def test_ingest_unauth_takes_precedence_over_path_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /ingest without Authorization fires 401, not 400, even with unsafe path.

    Auth middleware runs before route handler path validation, so a missing
    token must produce 401 regardless of the request body.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _api_key):
        # No Authorization header — auth middleware fires before path check.
        resp = client.post(
            "/ingest",
            json={"collection": "col", "path": "/foo/../bar"},
        )
        assert resp.status_code == 401, (
            f"expected 401 (auth before path check), got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Test 6 — wizard --dry-run on fresh install: exit 0 and output contains [dry-run]
# ---------------------------------------------------------------------------


def _make_wizard_patches() -> dict:
    """Return a dict of shared patches that prevent real service/disk operations."""
    from archon_search.platform.types import GpuType

    return {
        "detect_gpu": MagicMock(return_value=GpuType.NONE),
        "validate_providers": MagicMock(return_value=False),
        "configure_providers": MagicMock(),
        "write_service_file": MagicMock(),
        "load_service": MagicMock(return_value=0),
        "_wait_for_service": MagicMock(return_value=True),
        "_is_service_running": MagicMock(return_value=False),
    }


def _run_wizard_dry_run(tmp_path: Path) -> object:
    """Invoke wizard --dry-run --non-interactive on a clean tmp_path via CliRunner.

    Patches ``_acquire_install_lock`` to a no-op so parallel xdist workers
    do not contend on ``~/.archon-search/.install.lock``.
    """
    from archon_search.cli.main import main
    from archon_search.install import RealInstaller

    config_path = tmp_path / "archon-search.toml"
    runner = CliRunner()

    with patch.multiple("archon_search.install.installer",
                        _prewarm_models=MagicMock(),
                        _check_disk_space=MagicMock(),
                        _legacy_service_path=MagicMock(return_value=tmp_path / "fake.plist"),
                        _remove_legacy_service=MagicMock(),
                        _acquire_install_lock=_noop_install_lock):
        with patch.multiple(RealInstaller, **_make_wizard_patches()):
            result = runner.invoke(main, [
                "wizard",
                "--dry-run",
                "--non-interactive",
                "--profile", "balanced",
                "--skip-preload",
                "--config", str(config_path),
            ])
    return result


@pytest.mark.xdist_group("install")
def test_wizard_dry_run_fresh_install_via_cli_runner(tmp_path: Path) -> None:
    """wizard --dry-run on clean DATA_DIR: exit 0 and output contains [dry-run].

    Verifies the Click → SearchInstaller wiring at the CLI level without
    mocking the CliRunner or Click internals.
    """
    result = _run_wizard_dry_run(tmp_path)
    assert result.exit_code == 0, (
        f"wizard --dry-run exited {result.exit_code}:\n{result.output}"
    )
    # The installer prints "[DRY RUN]" prefix for dry-run mode.
    assert "[dry-run]" in result.output.lower() or "[DRY RUN]" in result.output, (
        f"expected '[dry-run]' or '[DRY RUN]' in output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# Test 7 — wizard --dry-run is idempotent: two runs, no file mutations
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("install")
def test_wizard_dry_run_idempotent_on_existing_install(tmp_path: Path) -> None:
    """wizard --dry-run twice: exit 0 both times, no config file created.

    Verifies idempotency: the second run sees the same filesystem state as the
    first run (no files mutated by a dry run).
    """
    config_path = tmp_path / "archon-search.toml"

    result1 = _run_wizard_dry_run(tmp_path)
    assert result1.exit_code == 0, (
        f"wizard --dry-run (1st run) exited {result1.exit_code}:\n{result1.output}"
    )
    # Config file must NOT be created during dry run.
    assert not config_path.exists(), (
        f"config file must not be created by --dry-run, but exists at {config_path}"
    )

    result2 = _run_wizard_dry_run(tmp_path)
    assert result2.exit_code == 0, (
        f"wizard --dry-run (2nd run) exited {result2.exit_code}:\n{result2.output}"
    )
    # Still no config file.
    assert not config_path.exists(), (
        f"config file must not be created by second --dry-run, but exists at {config_path}"
    )
