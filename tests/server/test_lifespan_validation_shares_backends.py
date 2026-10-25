"""The startup validation probes the pipeline's own model instances, never throwaway copies.

The probe used to load its own embedder and cross-encoder next to the warm-up's instances, so the
startup peak held the weights twice (bge-large + bge-reranker-base: ~2x 2.4 GB) before the copies
were freed. The lifespan now hands the probe the serving backends and starts it once the warm-up
has finished with them.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.embedder import ModelEmbedder
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult
from archon_search.reranker import ModelReranker
from archon_search.server import app as app_module
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from tests.test_app import wait_for_models_check_to_settle, wait_for_warmup


@pytest.fixture(autouse=True)
def _offline_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SearchStore, "connect", AsyncMock())
    monkeypatch.setattr(SearchStore, "_run_startup_migrations", AsyncMock())
    monkeypatch.setattr(SearchStore, "disconnect", AsyncMock())
    monkeypatch.setattr(SearchStore, "ping", AsyncMock(return_value=True))


def _app(tmp_path: Path):
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    return create_app(config, JobStore(path=tmp_path / "jobs.json"))


def test_validation_is_handed_the_pipelines_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validate = AsyncMock(return_value=ModelValidationResult(embedder_ok=True, reranker_ok=True))
    monkeypatch.setattr(app_module, "validate_models_async", validate)
    app = _app(tmp_path)

    with TestClient(app):
        wait_for_warmup(app)
        wait_for_models_check_to_settle(app)
        pipeline = app.state.pipeline

        kwargs = validate.await_args.kwargs
        assert kwargs["embedder"] is pipeline.global_embedder.backend
        assert kwargs["reranker"] is pipeline.reranker.backend


def test_validation_starts_only_after_the_warmup_has_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probing a cold instance while the warm-up builds it would just queue behind the same load lock."""
    warmup_done_when_validation_started: list[bool] = []

    async def validate(*_args: object, **_kwargs: object) -> ModelValidationResult:
        warmup_done_when_validation_started.append(app.state._warmup_task.done())
        return ModelValidationResult(embedder_ok=True, reranker_ok=True)

    monkeypatch.setattr(app_module, "validate_models_async", validate)
    app = _app(tmp_path)

    with TestClient(app):
        wait_for_warmup(app)
        wait_for_models_check_to_settle(app)

    assert warmup_done_when_validation_started == [True]


def test_startup_builds_each_model_backend_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default config, real validation: one ModelEmbedder and one ModelReranker serve warm-up AND the probe."""
    built: list[str] = []
    for backend in (ModelEmbedder, ModelReranker):
        original = backend.__init__

        def counting_init(self, *args, _original=original, **kwargs):
            built.append(type(self).__name__)
            _original(self, *args, **kwargs)

        monkeypatch.setattr(backend, "__init__", counting_init)
    app = _app(tmp_path)

    with TestClient(app):
        wait_for_warmup(app)
        wait_for_models_check_to_settle(app)
        assert app.state.model_validation.embedder_ok is True
        assert app.state.model_validation.reranker_ok is True

    assert sorted(built) == ["ModelEmbedder", "ModelReranker"]
