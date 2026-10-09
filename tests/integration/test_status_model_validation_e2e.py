"""Integration test: GET /status surfaces background model-validation result (D6 BE-8, S1).

After server startup, the background validation task (BE-4) populates
``app.state.model_validation``. The /status route (BE-8) mirrors it into the
``model_validation`` sub-object. This test boots a real app, polls GET /status
until ``model_validation`` is non-null, and asserts the ``embedder_ok`` field is
present — proving the route reads live background-task state end-to-end.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.integration.conftest import make_real_app
from tests.test_onnx_provider_fallback import (
    _UNAVAILABLE_PROVIDER_CASES,
    _unavailable_provider_list,
)

pytestmark = pytest.mark.integration

# Upper bound for the background validation task to publish its result under stub models.
_POLL_DEADLINE_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05


def test_status_endpoint_includes_model_validation_after_startup(tmp_path, monkeypatch) -> None:
    """Poll GET /status until model_validation is populated; assert embedder_ok present."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
        mv = None
        while time.monotonic() < deadline:
            resp = client.get("/status", headers=headers)
            assert resp.status_code == 200, resp.text
            mv = resp.json()["model_validation"]
            if mv is not None:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert mv is not None, "model_validation never populated within deadline"
        # All four contract fields survive the real lifespan → route round-trip.
        assert "embedder_ok" in mv
        assert "reranker_ok" in mv
        assert "provider_warnings" in mv
        assert "validated_at" in mv
        assert mv["validated_at"] is not None


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
