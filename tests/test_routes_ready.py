"""Tests for GET /ready endpoint — Task 5.2 (B2)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult, validate_models_async
from archon_search.server.app import create_app
from archon_search.server.schemas import WarmupResult


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app)


@pytest.fixture()
def client_ping_true(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    app.state.search_store.ping = AsyncMock(return_value=True)
    return TestClient(app)


@pytest.fixture()
def client_ping_false(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    app.state.search_store.ping = AsyncMock(return_value=False)
    return TestClient(app)


def test_ready_returns_200_when_storage_ok(client_ping_true: TestClient) -> None:
    # A pending models check gates readiness (S631) — finish validation first.
    _set_validation(client_ping_true, embedder_ok=True, reranker_ok=True)
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["storage"] == "ok"


def test_ready_returns_503_when_storage_fails(client_ping_false: TestClient) -> None:
    # A clean validation keeps checks.models out of the gate: only storage can 503 here.
    _set_validation(client_ping_false, embedder_ok=True, reranker_ok=True)
    response = client_ping_false.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["storage"] == "fail"
    assert body["checks"]["models"] == "ok"


def test_ready_reachable_without_bearer_token(client_ping_true: TestClient) -> None:
    """GET /ready must not require authentication."""
    # A finished validation makes the unauthenticated probe answer 200, not merely "not 401".
    _set_validation(client_ping_true, embedder_ok=True, reranker_ok=True)
    response = client_ping_true.get("/ready")
    assert response.status_code == 200


def test_ready_body_schema_is_bounded(client_ping_true: TestClient) -> None:
    """Response body must contain only 'ready' and 'checks' — no extra fields."""
    response = client_ping_true.get("/ready")
    body = response.json()
    assert set(body.keys()) == {"ready", "checks"}
    assert set(body["checks"].keys()) == {"storage", "models", "sync"}


def test_ready_503_body_is_readiness_response_not_error_detail(
    client_ping_false: TestClient,
) -> None:
    """503 body must be ReadinessResponse shape, not ErrorDetail."""
    # A clean validation keeps checks.models out of the gate: the 503 comes from storage.
    _set_validation(client_ping_false, embedder_ok=True, reranker_ok=True)
    response = client_ping_false.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["storage"] == "fail"
    assert "ready" in body
    assert "checks" in body
    assert "detail" not in body


def test_ready_appears_in_openapi_without_bearer_auth(client: TestClient, auth_headers: dict[str, str]) -> None:
    """GET /ready must appear in OpenAPI without BearerAuth security annotation."""
    openapi = client.get("/openapi.json", headers=auth_headers).json()
    paths = openapi.get("paths", {})
    assert "/ready" in paths, "/ready must appear in OpenAPI paths"
    ready_ops = paths["/ready"]
    for method, operation in ready_ops.items():
        if isinstance(operation, dict):
            security = operation.get("security", None)
            assert security is None or security == [], (
                f"/ready {method} must have no security annotation, got {security}"
            )
    # Both 200 and 503 response shapes documented
    get_op = ready_ops.get("get", {})
    responses = get_op.get("responses", {})
    assert "200" in responses
    assert "503" in responses


def test_watcher_manager_slot_is_none_by_default(tmp_path: Path) -> None:
    """app.state.watcher_manager must be None after create_app."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    assert app.state.watcher_manager is None


def test_ready_does_not_call_collect_readiness() -> None:
    """GET /ready must not import or use collect_readiness — it must stay minimal."""
    from pathlib import Path as _Path

    import archon_search.server.routes_ready as mod

    source = _Path(mod.__file__).read_text()
    assert "collect_readiness" not in source


def _set_validation(client: TestClient, **kwargs: object) -> None:
    """Set ``app.state.model_validation`` to a ModelValidationResult on the client app."""
    client.app.state.model_validation = ModelValidationResult(**kwargs)


def test_ready_models_pending_when_validation_none(client_ping_true: TestClient) -> None:
    client_ping_true.app.state.model_validation = None
    response = client_ping_true.get("/ready")
    body = response.json()
    assert body["checks"]["models"] == "pending"
    # A pending models check gates readiness (S631).
    assert body["ready"] is False
    assert response.status_code == 503


def test_ready_models_ok_when_both_pass(client_ping_true: TestClient) -> None:
    _set_validation(client_ping_true, embedder_ok=True, reranker_ok=True)
    response = client_ping_true.get("/ready")
    assert response.json()["checks"]["models"] == "ok"


def test_ready_models_fail_when_embedder_fails(client_ping_true: TestClient) -> None:
    _set_validation(client_ping_true, embedder_ok=False, reranker_ok=True)
    response = client_ping_true.get("/ready")
    assert response.json()["checks"]["models"] == "fail"


def test_ready_models_fail_when_reranker_fails(client_ping_true: TestClient) -> None:
    _set_validation(client_ping_true, embedder_ok=True, reranker_ok=False)
    response = client_ping_true.get("/ready")
    assert response.json()["checks"]["models"] == "fail"


def test_ready_models_fail_precedence_over_warn(client_ping_true: TestClient) -> None:
    _set_validation(
        client_ping_true,
        embedder_ok=True,
        reranker_ok=False,
        provider_warnings=["CPU fallback"],
    )
    response = client_ping_true.get("/ready")
    assert response.json()["checks"]["models"] == "fail"


def test_ready_reports_warn_when_device_stepped_down(client_ping_true: TestClient) -> None:
    """Both models loaded, but the configured accelerator is missing: models: warn, still ready."""
    with patch("archon_search.model_validation.resolve_device", return_value="cpu"):
        result = asyncio.run(validate_models_async(SearchConfig(device="mps"), timeout_seconds=5))
    client_ping_true.app.state.model_validation = result

    response = client_ping_true.get("/ready")

    assert result.provider_warnings == ["device unavailable: mps; models loaded on cpu"]
    assert response.status_code == 200
    assert response.json()["checks"]["models"] == "warn"
    assert response.json()["ready"] is True


def test_ready_models_pending_when_probe_flag_unset(client_ping_true: TestClient) -> None:
    """A non-None result with a still-None probe flag must read as PENDING, not OK/WARN."""
    _set_validation(
        client_ping_true,
        embedder_ok=None,
        reranker_ok=True,
        provider_warnings=["CPU fallback"],
    )
    response = client_ping_true.get("/ready")
    assert response.json()["checks"]["models"] == "pending"


def test_ready_not_degraded_by_probe_fail(client_ping_true: TestClient) -> None:
    """llama_cpp_ok=False must NOT feed the checks.models FAIL condition (S25) —
    /ready stays 200 and "ok" as long as the embedder/reranker probes pass."""
    _set_validation(
        client_ping_true,
        embedder_ok=True,
        reranker_ok=True,
        llama_cpp_ok=False,
    )
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["models"] == "ok"


def test_ready_200_when_only_the_validation_probe_fails(client_ping_true: TestClient) -> None:
    """A failed probe alone is not proof the serving instances are broken.

    Validation builds throwaway instances, and its timeout / crash path reports both flags
    false on a merely slow host. Only a finished warm-up that left the serving instances cold
    gates readiness (see the ``warm-up FAILED`` tests below).
    """
    _set_validation(client_ping_true, embedder_ok=False, reranker_ok=False)
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["models"] == "fail"


@pytest.mark.integration
def test_ready_returns_503_after_store_disconnect(tmp_path: Path) -> None:
    """After disconnect, GET /ready must return 503."""
    import asyncio

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    store = app.state.search_store

    async def setup() -> None:
        await store.connect()
        await store.disconnect()

    # asyncio.run (not get_event_loop().run_until_complete): the latter raises
    # "no current event loop" in MainThread when a prior async test closed the loop.
    asyncio.run(setup())
    client = TestClient(app)
    # A clean validation keeps checks.models out of the gate: only storage can 503 here.
    _set_validation(client, embedder_ok=True, reranker_ok=True)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == "fail"
    assert response.json()["checks"]["models"] == "ok"


# ---------------------------------------------------------------------------
# Which model states gate readiness: the default embedder and a configured reranker
# ---------------------------------------------------------------------------


def _set_models(
    client: TestClient,
    *,
    warmup: WarmupResult | None,
    embedder_warm: bool = True,
    reranker_warm: bool = True,
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2",
    validation: ModelValidationResult | None = None,
) -> None:
    """Pin the warm-up outcome and what the serving pipeline reports as warm."""
    state = client.app.state
    state.warmup_result = warmup
    state.pipeline = SimpleNamespace(embedder_is_warm=embedder_warm, reranker_is_warm=reranker_warm)
    state.config = SearchConfig(reranker_model=reranker_model)
    state.model_validation = validation or ModelValidationResult(embedder_ok=True, reranker_ok=True)


def test_ready_503_models_pending_while_warmup_runs(client_ping_true: TestClient) -> None:
    _set_models(client_ping_true, warmup=WarmupResult.PENDING, embedder_warm=False, reranker_warm=False)
    response = client_ping_true.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["models"] == "pending"


def test_ready_200_models_ok_after_warmup_done(client_ping_true: TestClient) -> None:
    _set_models(client_ping_true, warmup=WarmupResult.DONE)
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["models"] == "ok"


def test_ready_200_models_warn_after_warmup_done(client_ping_true: TestClient) -> None:
    _set_models(
        client_ping_true,
        warmup=WarmupResult.DONE,
        validation=ModelValidationResult(
            embedder_ok=True, reranker_ok=True, provider_warnings=["device unavailable: mps; models loaded on cpu"]
        ),
    )
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["checks"]["models"] == "warn"


def test_ready_503_when_default_embedder_failed_to_warm(client_ping_true: TestClient) -> None:
    """The default embedder cannot load: every ingest fails and search is FTS-only — not ready."""
    _set_models(
        client_ping_true,
        warmup=WarmupResult.FAILED,
        embedder_warm=False,
        validation=ModelValidationResult(embedder_ok=False, reranker_ok=True),
    )
    response = client_ping_true.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["models"] == "fail"
    assert body["checks"]["storage"] == "ok"


def test_ready_503_when_configured_reranker_failed_to_warm(client_ping_true: TestClient) -> None:
    _set_models(
        client_ping_true,
        warmup=WarmupResult.FAILED,
        reranker_warm=False,
        validation=ModelValidationResult(embedder_ok=True, reranker_ok=False),
    )
    response = client_ping_true.get("/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["checks"]["models"] == "fail"


def test_ready_200_when_no_reranker_is_configured(client_ping_true: TestClient) -> None:
    """An intentionally absent reranker is never cold-and-failed: it must not wedge readiness."""
    _set_models(
        client_ping_true,
        warmup=WarmupResult.FAILED,
        reranker_warm=False,
        reranker_model="",
    )
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["checks"]["models"] == "fail"


def test_ready_200_when_only_a_per_collection_preload_failed(client_ping_true: TestClient) -> None:
    """Warm-up FAILED because a collection's own model did not load; the defaults are warm."""
    _set_models(client_ping_true, warmup=WarmupResult.FAILED)
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["checks"]["models"] == "fail"


def test_ready_recovers_when_a_cold_model_warms_after_failed_warmup(client_ping_true: TestClient) -> None:
    """The gate reads live warm state, so a model built later (a slow load that finished) clears it."""
    _set_models(client_ping_true, warmup=WarmupResult.FAILED, embedder_warm=False)
    assert client_ping_true.get("/ready").status_code == 503

    client_ping_true.app.state.pipeline.embedder_is_warm = True

    assert client_ping_true.get("/ready").status_code == 200
