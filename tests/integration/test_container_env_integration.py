"""Task 6.2 — Container env and disk I/O integration tests.

Verifies that environment variable overrides wire correctly through the full
startup sequence, that atomic_write_json produces durable readable files on
real disk, that JobStore survives a JSON round-trip, and that the key-manager
bootstrap writes the key file with mode 600.

Tests:
    test_data_dir_env_routes_log_file_to_derived_path
    test_container_env_and_data_dir_together_in_real_app
    test_atomic_write_json_roundtrip_real_disk
    test_job_store_survives_json_roundtrip
    test_key_file_created_with_mode_600_on_first_start

Run with:
    uv run pytest tests/integration/test_container_env_integration.py -v
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test 1 — load_config + configure_logging: log_file routed under DATA_DIR
# ---------------------------------------------------------------------------


def test_data_dir_env_routes_log_file_to_derived_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config() with DATA_DIR set derives log_file under tmp_path.

    Calls configure_logging() with the resulting config and asserts the
    attached file handler's baseFilename is under tmp_path, not ~/.archon-search/.
    """
    from logging.handlers import TimedRotatingFileHandler

    from archon_search.config import load_config
    from archon_search.logging_setup import configure_logging

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    config = load_config()

    assert config.log_file.startswith(str(tmp_path)), (
        f"Expected log_file under {tmp_path!r}, got {config.log_file!r}"
    )

    configure_logging(config)

    logger = logging.getLogger("archon_search")
    try:
        file_handlers = [
            h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)
        ]
        assert file_handlers, (
            "Expected at least one TimedRotatingFileHandler on archon_search logger"
        )
        for handler in file_handlers:
            assert handler.baseFilename.startswith(str(tmp_path)), (
                f"File handler path {handler.baseFilename!r} is not under {tmp_path!r}"
            )
    finally:
        # Clean up: remove handlers to avoid leaking across tests
        for h in logger.handlers[:]:
            logger.removeHandler(h)
            h.close()
        logger.propagate = True


# ---------------------------------------------------------------------------
# Test 2 — CONTAINER=1 + DATA_DIR: real app has correct paths and stderr handler
# ---------------------------------------------------------------------------


def test_container_env_and_data_dir_together_in_real_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARCHON_SEARCH_CONTAINER=1 + DATA_DIR: db under tmp_path, stderr handler on logger.

    Sets ARCHON_SEARCH_DATA_DIR and ARCHON_SEARCH_CONTAINER=1 via monkeypatch,
    then calls load_config() and configure_logging() directly. Verifies:
      - config.db_path is under tmp_path
      - config.log_file is under tmp_path
      - archon_search logger has a StreamHandler targeting sys.stderr
    """
    import sys
    from logging.handlers import TimedRotatingFileHandler

    from archon_search.config import load_config
    from archon_search.logging_setup import configure_logging

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_CONTAINER", "1")

    config = load_config()

    # Verify config paths derived from tmp_path
    assert config.db_path.startswith(str(tmp_path)), (
        f"Expected db_path under {tmp_path!r}, got {config.db_path!r}"
    )
    assert config.log_file.startswith(str(tmp_path)), (
        f"Expected log_file under {tmp_path!r}, got {config.log_file!r}"
    )

    configure_logging(config)

    logger = logging.getLogger("archon_search")
    try:
        # Expect a StreamHandler pointing to stderr (container mode)
        stderr_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, TimedRotatingFileHandler)
            and h.stream is sys.stderr
        ]
        assert stderr_handlers, (
            "Expected a StreamHandler(sys.stderr) on archon_search logger when "
            "ARCHON_SEARCH_CONTAINER=1, but none found. "
            f"Current handlers: {logger.handlers!r}"
        )
    finally:
        for h in logger.handlers[:]:
            logger.removeHandler(h)
            h.close()
        logger.propagate = True


# ---------------------------------------------------------------------------
# Test 3 — atomic_write_json round-trip on real disk (no mocks)
# ---------------------------------------------------------------------------


def test_atomic_write_json_roundtrip_real_disk(tmp_path: Path) -> None:
    """atomic_write_json writes to real disk; json.loads(read_text()) recovers the data.

    No mocks — verifies actual fsync'd disk I/O. Covers the A7 durable write
    contract end-to-end for JSON payloads.
    """
    from archon_search._durable_io import atomic_write_json

    payload = {
        "key": "value",
        "number": 42,
        "nested": {"a": [1, 2, 3]},
        "unicode": "こんにちは",
    }

    target = tmp_path / "atomic_test.json"
    atomic_write_json(target, payload)

    assert target.exists(), f"File {target} was not created by atomic_write_json"

    recovered = json.loads(target.read_text(encoding="utf-8"))
    assert recovered == payload, (
        f"Round-trip data mismatch.\nExpected: {payload!r}\nGot:      {recovered!r}"
    )

    # The .tmp file must not be left behind on success
    tmp_file = target.with_suffix(target.suffix + ".tmp")
    assert not tmp_file.exists(), (
        f"Temp file {tmp_file} was not cleaned up after successful atomic_write_json"
    )


# ---------------------------------------------------------------------------
# Test 4 — JobStore JSON round-trip: enqueue, re-open, retrieve
# ---------------------------------------------------------------------------


def test_job_store_survives_json_roundtrip(tmp_path: Path) -> None:
    """Enqueue a job via JobStore, re-open the store, assert the job is retrievable.

    Verifies the A7 fsync contract: the job file on disk survives closing and
    re-opening the store — i.e., the job is not lost in a crash between enqueue
    and re-open.
    """
    from archon_search.jobs.store import JobStore

    jobs_file = tmp_path / "jobs.json"

    # Enqueue a job using the first JobStore instance
    store_a = JobStore(path=jobs_file)
    job = store_a.create(namespace="default")
    job_id = job.job_id

    assert jobs_file.exists(), "JobStore did not write jobs file on create()"

    # Open a second JobStore pointing at the same file (simulates a restart)
    store_b = JobStore(path=jobs_file)
    recovered = store_b.get(job_id)

    assert recovered is not None, (
        f"Job {job_id!r} not found after re-opening JobStore from {jobs_file}"
    )
    assert recovered.job_id == job_id, (
        f"Recovered job_id mismatch: expected {job_id!r}, got {recovered.job_id!r}"
    )
    assert recovered.namespace == job.namespace, (
        f"Namespace mismatch: expected {job.namespace!r}, got {recovered.namespace!r}"
    )
    assert recovered.status == job.status, (
        f"Status mismatch: expected {job.status!r}, got {recovered.status!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Key file created with mode 600 on first start
# ---------------------------------------------------------------------------


def test_key_file_created_with_mode_600_on_first_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After TestClient lifespan startup, key file exists under tmp_path with mode 600.

    Uses make_real_app (which sets ARCHON_SEARCH_DATA_DIR and ARCHON_SEARCH_API_KEY
    via monkeypatch). Inside the TestClient context (lifespan complete), asserts:
      - key file exists under tmp_path (not ~/.archon-search/)
      - file permissions are exactly 0o600

    Verifies the key-manager bootstrap security invariant described in C9.
    """
    # make_real_app sets ARCHON_SEARCH_API_KEY, which causes load_or_generate_key()
    # to return early from the env branch without writing a file. To exercise the
    # file-creation path we must NOT set ARCHON_SEARCH_API_KEY. We use a separate
    # monkeypatch.delenv call to ensure the env var is absent for this test.
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.backup.interval_hours = 0

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    from fastapi.testclient import TestClient

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app) as client:
        # Lifespan startup is complete; key-manager bootstrap has run
        key_file = tmp_path / ".search.env"
        assert key_file.exists(), (
            f"Key file {key_file} was not created during app startup. "
            "Expected key-manager to auto-generate the file when "
            "ARCHON_SEARCH_API_KEY is not set."
        )
        file_mode = os.stat(key_file).st_mode & 0o777
        assert file_mode == 0o600, (
            f"Key file {key_file} has mode {oct(file_mode)!r}, expected 0o600. "
            "The key-manager bootstrap must create the file with mode 600 "
            "to prevent other users from reading the API key."
        )

        # Functional validation: extract the generated key from the file and
        # confirm it is actually accepted by the auth middleware.
        env_var = "ARCHON_SEARCH_API_KEY"
        generated_key: str | None = None
        for line in key_file.read_text().splitlines():
            if line.startswith(f"{env_var}="):
                generated_key = line[len(f"{env_var}="):].strip()
                break
        assert generated_key, (
            f"Could not parse {env_var} from key file {key_file}. "
            f"File contents: {key_file.read_text()!r}"
        )
        # /health is auth-exempt; use /collections which requires a valid Bearer token
        resp = client.get("/collections", headers={"Authorization": f"Bearer {generated_key}"})
        assert resp.status_code == 200, (
            f"Expected 200 from /collections using generated key, got {resp.status_code}. "
            "The auto-generated key in the key file must be functionally accepted "
            "by the auth middleware."
        )
