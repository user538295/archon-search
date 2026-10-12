"""Integration test: GET /status surfaces background model-validation result (D6 BE-8, S1).

After server startup, the background validation task (BE-4) populates
``app.state.model_validation``. The /status route (BE-8) mirrors it into the
``model_validation`` sub-object, which is always an object since S488 — the
all-null pending object until validation completes. These tests boot a real app,
assert that pending object while validation is held, and poll GET /status until
``validated_at`` is set — proving the route reads live background-task state
end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from archon_search.model_validation import ModelValidationResult
from tests.integration.conftest import make_real_app
from tests.test_onnx_provider_fallback import (
    _UNAVAILABLE_PROVIDER_CASES,
    _unavailable_provider_list,
)

pytestmark = pytest.mark.integration

# Upper bound for the background validation task to publish its result under stub models.
_POLL_DEADLINE_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
# Backstop for a stuck hold; lifespan shutdown cancels the task anyway. Must exceed the 30s
# `_await_startup_sync` timeout in tests/integration/conftest.py, which runs before the GET.
_HOLD_DEADLINE_SECONDS = 60.0


def test_status_endpoint_includes_model_validation_after_startup(tmp_path, monkeypatch) -> None:
    """Poll GET /status until model_validation completes (``validated_at`` set); assert the contract fields."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
        mv = None
        while time.monotonic() < deadline:
            resp = client.get("/status", headers=headers)
            assert resp.status_code == 200, resp.text
            mv = resp.json()["model_validation"]
            assert isinstance(mv, dict), f"model_validation must always be an object (S488); got {mv!r}"
            if mv["validated_at"] is not None:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert mv["validated_at"] is not None, f"model_validation never completed within deadline: {mv!r}"
        # All five contract fields survive the real lifespan → route round-trip.
        assert "embedder_ok" in mv
        assert "reranker_ok" in mv
        assert "llama_cpp_ok" in mv
        assert "provider_warnings" in mv
        assert "validated_at" in mv


def test_status_model_validation_is_pending_object_while_validation_runs_s488(
    tmp_path, monkeypatch
) -> None:
    """S488: hold the real lifespan's background validation pending and assert GET /status
    emits the all-null pending object, not ``null``. The patched coroutine polls a
    ``threading.Event`` (the TestClient runs the app on its own loop) instead of running the
    real validation. Its normal exit is lifespan shutdown cancellation: ``server/app.py``
    cancels every ``app.state._background_tasks`` entry on teardown. Setting ``release``
    with the bare ``ModelValidationResult()`` return (for a poll that sees ``release`` before
    that cancel lands) and the ``_HOLD_DEADLINE_SECONDS`` bound are only backstops."""
    import archon_search.server.app as app_module

    entered = threading.Event()
    release = threading.Event()

    async def _held_validate(*_args, **_kwargs) -> ModelValidationResult:
        entered.set()
        deadline = time.monotonic() + _HOLD_DEADLINE_SECONDS
        while not release.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        return ModelValidationResult()

    monkeypatch.setattr(app_module, "validate_models_async", _held_validate)
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        try:
            resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        finally:
            release.set()

    assert entered.is_set(), "patched validate_models_async never ran; the hold did not take effect"
    assert resp.status_code == 200, resp.text
    assert resp.json()["model_validation"] == {
        "embedder_ok": None,
        "reranker_ok": None,
        "llama_cpp_ok": None,
        "provider_warnings": [],
        "validated_at": None,
    }


def test_ready_models_transitions_from_pending_to_ok(tmp_path, monkeypatch) -> None:
    """Poll GET /ready until checks.models leaves "pending"; assert it lands on "ok" (D6 BE-6, S4).

    Note: with stub models the background validation can complete before the first
    poll, so observing the "pending" window is not guaranteed here — the "pending"
    state itself is proven deterministically by the unit test
    ``test_ready_models_pending_when_validation_none``. This test guarantees the
    end state is "ok" (never stuck on "pending") under a real lifespan.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _api_key):
        deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
        models = None
        while time.monotonic() < deadline:
            models = client.get("/ready").json()["checks"]["models"]
            if models != "pending":
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        assert models == "ok"


def _models_check_and_validation(client, api_key: str) -> tuple[str | None, dict]:
    """Poll /ready until ``checks.models`` leaves "pending"; return it and /status's
    ``model_validation``."""
    deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
    models = None
    while time.monotonic() < deadline:
        models = client.get("/ready").json()["checks"]["models"]
        if models != "pending":
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    mv = client.get("/status", headers={"Authorization": f"Bearer {api_key}"}).json()[
        "model_validation"
    ]
    return models, mv


@pytest.mark.parametrize(("provider", "then_cpu"), _UNAVAILABLE_PROVIDER_CASES)
def test_unavailable_provider_reports_models_warn_not_fail(
    tmp_path, monkeypatch, provider: str, then_cpu: bool
) -> None:
    """S635: a configured provider this host's onnxruntime lacks is a fallback, not a failure.

    The models still load on the providers the host has, so /ready reports ``models: warn``
    and /status carries both probes as ok plus the missing-provider warning. Uses the real
    onnxruntime provider list; the real fastembed load is covered by
    ``tests/test_onnx_provider_fallback.py``.
    """
    providers = _unavailable_provider_list(provider, then_cpu)
    toml = f"[database]\nproviders = {json.dumps(providers)}\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
        models, mv = _models_check_and_validation(client, api_key)

    assert models == "warn", mv
    assert mv["embedder_ok"] is True, mv
    assert mv["reranker_ok"] is True, mv
    assert (
        f"configured ONNX providers not available: {providers[0]}" in mv["provider_warnings"]
    ), mv


@pytest.mark.parametrize(("provider", "then_cpu"), _UNAVAILABLE_PROVIDER_CASES)
def test_unavailable_reranker_provider_reports_models_warn_not_fail(
    tmp_path, monkeypatch, provider: str, then_cpu: bool
) -> None:
    """S635: only ``[database].reranker_providers`` names a provider the host lacks → the
    reranker still loads (on CPU), so /ready reports ``models: warn``, not ``fail``."""
    reranker_providers = _unavailable_provider_list(provider, then_cpu)
    toml = f"[database]\nproviders = []\nreranker_providers = {json.dumps(reranker_providers)}\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
        models, mv = _models_check_and_validation(client, api_key)

    assert models == "warn", mv
    assert mv["embedder_ok"] is True, mv
    assert mv["reranker_ok"] is True, mv
    assert (
        f"configured ONNX providers not available: {reranker_providers[0]}"
        in mv["provider_warnings"]
    ), mv
