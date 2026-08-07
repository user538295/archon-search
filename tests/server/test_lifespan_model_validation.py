"""Tests for the background model-validation task wired into create_app's lifespan (BE-4)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult
from archon_search.server.app import create_app


def _make_config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


def _make_job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_model_validation_state_none_before_task(tmp_path: Path) -> None:
    """Immediately after lifespan startup, app.state.model_validation is None.

    The background task is replaced with a coroutine that blocks on a
    threading.Event so it cannot complete before the assertion runs. A
    threading.Event (not asyncio.Event) is used because TestClient runs the
    event loop in a worker thread.
    """
    import threading

    release = threading.Event()

    async def _blocking_validate(*args, **kwargs):
        await asyncio.to_thread(release.wait, 5.0)
        return ModelValidationResult(embedder_ok=True, reranker_ok=True)

    cfg = _make_config(tmp_path)
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    with patch(
        "archon_search.server.app.validate_models_async",
        side_effect=_blocking_validate,
    ):
        with TestClient(app):
            assert app.state.model_validation is None
            release.set()


def test_background_task_exception_sets_failure_result(tmp_path: Path) -> None:
    """If validate_models_async raises, the wrapper stores a failure result."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    cfg = _make_config(tmp_path)
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    with patch(
        "archon_search.server.app.validate_models_async",
        side_effect=_boom,
    ):
        with TestClient(app):
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and app.state.model_validation is None:
                time.sleep(0.05)
            assert app.state.model_validation is not None
            assert app.state.model_validation.embedder_ok is False
            assert app.state.model_validation.reranker_ok is False
            assert app.state.model_validation.validated_at is not None
            assert any(
                "failed unexpectedly" in w
                for w in app.state.model_validation.provider_warnings
            )


@pytest.mark.integration
def test_startup_warning_logged_llama_cpp_unreachable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-9 (S7): unreachable llama-server at startup logs a WARNING and boot completes.

    The real (un-patched) ``validate_models_async`` runs as the lifespan background
    task; only ``httpx.AsyncClient`` (the llama-server probe transport) is patched
    to simulate an unreachable server, per the grounding note's mocking convention.
    """
    import logging

    cfg = _make_config(tmp_path)
    cfg.hyde.provider = "llama_cpp"
    cfg.hyde.llama_cpp_base_url = "http://localhost:8080"
    job_store = _make_job_store(tmp_path)
    app = create_app(cfg, job_store)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_cls = MagicMock(return_value=mock_client)

    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        with TestClient(app) as client:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and app.state.model_validation is None:
                time.sleep(0.05)
            # Boot completed normally — /ready is reachable despite the probe failure.
            assert client.get("/ready").status_code == 200

    assert app.state.model_validation is not None
    assert app.state.model_validation.llama_cpp_ok is False
    assert "llama-server unreachable" in caplog.text


@pytest.mark.integration
def test_background_validation_completes_and_sets_app_state(tmp_path, monkeypatch) -> None:
    """make_real_app: poll app.state.model_validation until set; assert validated_at present."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _api_key):
        app = client.app
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and app.state.model_validation is None:
            time.sleep(0.05)
        assert app.state.model_validation is not None
        assert app.state.model_validation.validated_at is not None
        # Stubbed embedder + reranker probes succeed → S1 happy path.
        assert app.state.model_validation.embedder_ok is True
        assert app.state.model_validation.reranker_ok is True
