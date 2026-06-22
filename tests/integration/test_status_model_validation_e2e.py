"""Integration test: GET /status surfaces background model-validation result (D6 BE-8, S1).

After server startup, the background validation task (BE-4) populates
``app.state.model_validation``. The /status route (BE-8) mirrors it into the
``model_validation`` sub-object. This test boots a real app, polls GET /status
until ``model_validation`` is non-null, and asserts the ``embedder_ok`` field is
present — proving the route reads live background-task state end-to-end.
"""
from __future__ import annotations

import time

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def test_status_endpoint_includes_model_validation_after_startup(tmp_path, monkeypatch) -> None:
    """Poll GET /status until model_validation is populated; assert embedder_ok present."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        deadline = time.monotonic() + 5.0
        mv = None
        while time.monotonic() < deadline:
            resp = client.get("/status", headers=headers)
            assert resp.status_code == 200, resp.text
            mv = resp.json()["model_validation"]
            if mv is not None:
                break
            time.sleep(0.05)

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
        deadline = time.monotonic() + 5.0
        models = None
        while time.monotonic() < deadline:
            models = client.get("/ready").json()["checks"]["models"]
            if models != "pending":
                break
            time.sleep(0.05)
        assert models == "ok"
