"""S631 regression: GET /ready must never answer ``200 ready:true`` while its own
``checks.models`` reads ``pending``.

Model validation (``app.py`` ``_run_model_validation``) runs as its own background
task, concurrently with warm-up (``server/warmup.py``), and can finish AFTER warm-up
has set ``WarmupResult.DONE``. In that window ``_model_check_status`` reports
``pending`` but ``ready()`` gated only on storage, warm-up and sync, so the probe
answered ``200 {"ready": true, "checks": {"models": "pending"}}``. The docs
(``OperatorGuide/20_monitoring_and_alerts.md`` §"GET /ready", ``80_capacity_and_performance.md``
``eager_load_embedders``) define ``storage ok + models pending`` as still warming (``503``)
and the ``200`` as arriving with ``models: "ok"`` once both models load cleanly.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import VALIDATION_TASK_FAILED_WARNING, ModelValidationResult
from archon_search.server import app as app_module
from archon_search.server.app import create_app
from archon_search.server.schemas import WarmupResult
from archon_search.store import SearchStore
from tests.test_app import (
    MODELS_CHECK_SETTLE_TIMEOUT_SECONDS,
    wait_for_models_check_to_settle,
    wait_for_warmup,
)

#: Self-release ceiling for the gated validator: the test sets the gate long before
#: this, so it only bounds a broken build. Longer than the settle wait that follows
#: the release, so a stuck gate fails that wait, not this one.
_VALIDATION_GATE_TIMEOUT_SECONDS = 3 * MODELS_CHECK_SETTLE_TIMEOUT_SECONDS


@pytest.fixture()
def client_warmup_done(tmp_path: Path) -> TestClient:
    """App with storage up, no startup sync, and warm-up already finished cleanly."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    app.state.search_store.ping = AsyncMock(return_value=True)
    app.state.warmup_result = WarmupResult.DONE
    return TestClient(app)


@pytest.mark.parametrize(
    "validation",
    [
        pytest.param(None, id="validation_task_not_finished"),
        pytest.param(
            ModelValidationResult(embedder_ok=None, reranker_ok=True),
            id="validation_result_with_unset_probe_flag",
        ),
    ],
)
def test_ready_is_503_while_models_pending_after_warmup_done(
    client_warmup_done: TestClient, validation: ModelValidationResult | None
) -> None:
    client_warmup_done.app.state.model_validation = validation

    response = client_warmup_done.get("/ready")

    body = response.json()
    assert body["checks"]["storage"] == "ok"
    assert body["checks"]["sync"] == "ok"
    assert body["checks"]["models"] == "pending"
    assert body["ready"] is False, f"ready:true while checks.models is pending: {body}"
    assert response.status_code == 503


def test_ready_is_200_with_models_ok_once_validation_completes_cleanly(
    client_warmup_done: TestClient,
) -> None:
    client_warmup_done.app.state.model_validation = ModelValidationResult(
        embedder_ok=True, reranker_ok=True
    )

    response = client_warmup_done.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"] == {"storage": "ok", "models": "ok", "sync": "ok"}


@pytest.mark.parametrize(
    ("validation", "expected_models"),
    [
        pytest.param(
            ModelValidationResult(
                embedder_ok=True, reranker_ok=True, provider_warnings=["CPU fallback"]
            ),
            "warn",
            id="validation_warn",
        ),
        pytest.param(
            ModelValidationResult(embedder_ok=False, reranker_ok=True),
            "fail",
            id="validation_fail",
        ),
    ],
)
def test_ready_is_200_when_finished_validation_warns_or_fails(
    client_warmup_done: TestClient, validation: ModelValidationResult, expected_models: str
) -> None:
    """WARN/FAIL end the pending state and stay informational: they never gate ``ready``."""
    client_warmup_done.app.state.model_validation = validation

    response = client_warmup_done.get("/ready")

    body = response.json()
    assert body["checks"]["models"] == expected_models
    assert body["ready"] is True
    assert response.status_code == 200


def test_ready_reports_sync_and_models_pending_together(
    client_warmup_done: TestClient,
) -> None:
    """A running startup sync and a pending models check surface side by side in one
    ``503`` — neither masks the other's ``pending``."""
    unfinished_sync_task = MagicMock()
    unfinished_sync_task.done.return_value = False
    client_warmup_done.app.state._startup_sync_task = unfinished_sync_task
    client_warmup_done.app.state.model_validation = None

    response = client_warmup_done.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {"storage": "ok", "models": "pending", "sync": "pending"},
    }


def test_ready_is_200_with_models_fail_after_failed_warmup_before_validation(
    client_warmup_done: TestClient,
) -> None:
    """A FAILED warm-up ends the pending state as ``fail`` even while validation runs."""
    client_warmup_done.app.state.warmup_result = WarmupResult.FAILED
    client_warmup_done.app.state.model_validation = None

    response = client_warmup_done.get("/ready")

    body = response.json()
    assert body["checks"]["models"] == "fail"
    assert body["ready"] is True
    assert response.status_code == 200


@pytest.fixture
def _offline_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real lifespan off LanceDB I/O (precedent: ``run_warmup`` in
    ``tests/server/test_lifespan_warmup_outcome.py``)."""
    monkeypatch.setattr(SearchStore, "connect", AsyncMock())
    monkeypatch.setattr(SearchStore, "_run_startup_migrations", AsyncMock())
    monkeypatch.setattr(SearchStore, "disconnect", AsyncMock())
    monkeypatch.setattr(SearchStore, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(SearchStore, "get_all_collections_meta", AsyncMock(return_value=[]))


@pytest.mark.usefixtures("_offline_store")
def test_lifespan_ready_503_until_validation_finishes_after_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real lifespan: warm-up DONE but validation still running → 503 ``models: pending``;
    once validation stores its result → 200."""
    release = threading.Event()

    async def _gated_validate(*args: object, **kwargs: object) -> ModelValidationResult:
        # threading.Event: TestClient runs the app's loop on a portal thread.
        await asyncio.to_thread(release.wait, _VALIDATION_GATE_TIMEOUT_SECONDS)
        return ModelValidationResult(embedder_ok=True, reranker_ok=True)

    monkeypatch.setattr(app_module, "validate_models_async", _gated_validate)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))

    with TestClient(app) as client:
        try:
            wait_for_warmup(app)
            assert app.state.warmup_result == WarmupResult.DONE
            assert app.state.model_validation is None

            pending = client.get("/ready")

            assert pending.json()["checks"]["storage"] == "ok"
            assert pending.json()["checks"]["sync"] == "ok"
            assert pending.json()["checks"]["models"] == "pending"
            assert pending.json()["ready"] is False
            assert pending.status_code == 503
        finally:
            release.set()

        wait_for_models_check_to_settle(app)
        ready = client.get("/ready")

    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["checks"]["models"] == "ok"


class _RaisingWarmProbeEmbedder:
    """An embedder whose ``is_warm`` read raises — the very first statement of the
    lifespan's ``_run_model_validation``."""

    @property
    def is_warm(self) -> bool:
        raise RuntimeError("is_warm probe exploded")


@pytest.mark.usefixtures("_offline_store")
def test_lifespan_validation_stores_failure_when_is_warm_raises(tmp_path: Path) -> None:
    """C1-I-7: a raising ``embedder.is_warm`` must still end the pending state.

    Read outside the task's ``try``, the exception escaped the task and left
    ``model_validation`` at ``None`` — ``checks.models: pending`` and ``/ready`` 503 for
    the process lifetime (S631). Inside it, the task stores a failure result.
    """
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    # Only the validation task reads app.state.embedder after create_app: the pipeline
    # and HyDE generator already captured the real instance.
    app.state.embedder = _RaisingWarmProbeEmbedder()

    with TestClient(app) as client:
        wait_for_models_check_to_settle(app)
        ready = client.get("/ready")

    # Anchor: warm-up itself succeeded, so the ``fail`` below comes from validation.
    assert app.state.warmup_result == WarmupResult.DONE
    assert app.state.model_validation.embedder_ok is False
    assert app.state.model_validation.reranker_ok is False
    assert VALIDATION_TASK_FAILED_WARNING in app.state.model_validation.provider_warnings
    assert ready.json()["checks"]["models"] == "fail"
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
