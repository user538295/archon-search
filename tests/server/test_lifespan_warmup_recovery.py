"""H2: the real lifespan heals a failed startup warm-up without any search or ingest traffic.

The suite-wide model-backend stub (``_stub_model_backends`` in ``tests/conftest.py``) is
wrapped so the default embedder fails its first loads and then works — a transient
download failure. ``/ready`` must flip from 503 to 200 on its own, and the model check
must be re-run on the healed models.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.embedder import ModelEmbedder
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult
from archon_search.server import app as app_module
from archon_search.server import warmup as warmup_module
from archon_search.server.app import create_app
from archon_search.server.schemas import WarmupResult
from archon_search.store import SearchStore
from tests.test_app import wait_for_warmup

#: Loads of the default embedder that fail before the model "becomes available".
_FAILING_LOADS = 4
_RECOVERY_DELAY_SECONDS = 0.01
_HEAL_DEADLINE_SECONDS = 20.0
_POLL_SECONDS = 0.02
#: Startup probe plus the re-check after healing.
_VALIDATION_RUNS = 2
#: Retries the recovery must have made before the test shuts the app down.
_MIN_RETRIES = 3
_NOT_READY = 503


def test_ready_turns_200_on_its_own_after_a_transient_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_ok = ModelValidationResult(embedder_ok=True, reranker_ok=True)
    validate = AsyncMock(return_value=validation_ok)
    monkeypatch.setattr(app_module, "validate_models_async", validate)
    monkeypatch.setattr(SearchStore, "connect", AsyncMock())
    monkeypatch.setattr(SearchStore, "_run_startup_migrations", AsyncMock())
    monkeypatch.setattr(SearchStore, "disconnect", AsyncMock())
    monkeypatch.setattr(SearchStore, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(SearchStore, "get_all_collections_meta", AsyncMock(return_value=[]))
    monkeypatch.setattr(warmup_module, "_RECOVERY_INITIAL_DELAY_SECONDS", _RECOVERY_DELAY_SECONDS)
    monkeypatch.setattr(warmup_module, "_RECOVERY_MAX_DELAY_SECONDS", _RECOVERY_DELAY_SECONDS)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.reranker_model = ""
    stubbed_encode = ModelEmbedder.encode
    loads = {"failed": 0}

    def _encode(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        if loads["failed"] < _FAILING_LOADS:
            loads["failed"] += 1
            raise RuntimeError("the model could not be downloaded")
        return stubbed_encode(self, texts)

    monkeypatch.setattr(ModelEmbedder, "encode", _encode)
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    with TestClient(app) as client:
        wait_for_warmup(app)
        deadline = time.monotonic() + _HEAL_DEADLINE_SECONDS
        status = client.get("/ready").status_code
        while status != 200 and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            status = client.get("/ready").status_code
        # The re-validation is the recovery task's last step: wait for it too.
        while validate.await_count < _VALIDATION_RUNS and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)

    assert loads["failed"] == _FAILING_LOADS
    assert status == 200
    assert app.state.warmup_result == WarmupResult.DONE
    assert validate.await_count == _VALIDATION_RUNS


def test_shutdown_cancels_a_recovery_that_never_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A permanently unloadable model keeps the recovery retrying; leaving the lifespan must still end it."""
    monkeypatch.setattr(app_module, "validate_models_async", AsyncMock(return_value=ModelValidationResult(embedder_ok=True, reranker_ok=True)))
    for name, mock in (
        ("connect", AsyncMock()),
        ("_run_startup_migrations", AsyncMock()),
        ("disconnect", AsyncMock()),
        ("ping", AsyncMock(return_value=True)),
        ("get_all_collections_meta", AsyncMock(return_value=[])),
    ):
        monkeypatch.setattr(SearchStore, name, mock)
    monkeypatch.setattr(warmup_module, "_RECOVERY_INITIAL_DELAY_SECONDS", _RECOVERY_DELAY_SECONDS)
    monkeypatch.setattr(warmup_module, "_RECOVERY_MAX_DELAY_SECONDS", _RECOVERY_DELAY_SECONDS)
    attempts = {"count": 0}

    def _always_fails(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        attempts["count"] += 1
        raise RuntimeError("the model could not be downloaded")

    monkeypatch.setattr(ModelEmbedder, "encode", _always_fails)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.reranker_model = ""
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    with TestClient(app) as client:
        wait_for_warmup(app)
        deadline = time.monotonic() + _HEAL_DEADLINE_SECONDS
        while attempts["count"] < _MIN_RETRIES and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
        still_failing = client.get("/ready").status_code

    assert attempts["count"] >= _MIN_RETRIES
    assert still_failing == _NOT_READY  # the replica really is unfit while the model cannot load
    assert app.state.warmup_result == WarmupResult.FAILED
    assert not app.state._background_tasks
