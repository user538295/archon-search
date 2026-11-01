"""Tests for the background model-validation task wired into create_app's lifespan (BE-4)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult
from archon_search.server.app import create_app
from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port
from tests.test_app import wait_for_models_check_to_settle

_WINDOW_LOOKUP_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.02


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
    ), TestClient(app):
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
    ), TestClient(app):
        wait_for_models_check_to_settle(app)
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
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-9 (S7): unreachable llama-server at startup logs ONE WARNING and boot completes.

    ``[hyde]`` is enabled on the llama_cpp provider, so two startup paths ask the same down server (the context-window
    lookup of the hyde client and the background validation's reachability probe) and share the one per-server WARNING,
    whichever finds it first. Nothing is patched: the server is a loopback port nothing answers on.
    """
    import logging
    import time

    keep_loopback_out_of_proxies(monkeypatch)
    with unreachable_port() as port:
        server = f"127.0.0.1:{port}"
        cfg = _make_config(tmp_path)
        cfg.hyde.enabled = True
        cfg.hyde.provider = "llama_cpp"
        cfg.hyde.llama_cpp_base_url = f"http://{server}"
        app = create_app(cfg, _make_job_store(tmp_path))

        with caplog.at_level(logging.WARNING), TestClient(app) as client:
            # /ready gates on a pending checks.models, which lasts until both the background warm-up and the background
            # validation finish (S631); the lifespan awaits neither, nor the window lookup: wait all out, or this asserts
            # on a race rather than on the probe failure.
            wait_for_models_check_to_settle(app)
            deadline = time.monotonic() + _WINDOW_LOOKUP_WAIT_SECONDS
            while not app.state._llm_window_task.done():
                assert time.monotonic() < deadline, "the context-window lookup did not end"
                time.sleep(_POLL_SECONDS)
            # Boot completed normally — /ready is reachable despite the probe failure.
            assert client.get("/ready").status_code == 200

    assert app.state.model_validation is not None
    assert app.state.model_validation.llama_cpp_ok is False
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING and server in r.getMessage()]
    assert "is not reachable" in record.getMessage()
    assert record.name == "archon_search.llm_client.llama_cpp_client"


@pytest.mark.integration
def test_background_validation_completes_and_sets_app_state(tmp_path, monkeypatch) -> None:
    """make_real_app: poll app.state.model_validation until set; assert validated_at present."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _api_key):
        app = client.app
        wait_for_models_check_to_settle(app)
        assert app.state.model_validation is not None
        assert app.state.model_validation.validated_at is not None
        # Stubbed embedder + reranker probes succeed → S1 happy path.
        assert app.state.model_validation.embedder_ok is True
        assert app.state.model_validation.reranker_ok is True
