"""BE-37 (collection-descriptions plan, C7, S39, S52, S66): the ``description`` block of ``GET /status`` on a real app.

``/ready`` never reads description health, and the block is purely additive for consumers that predate it.

Run with: ``uv run pytest tests/integration/test_be37_status_description_block.py -m integration``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.server.schemas import StatusResponse
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import ingest_file_via_path
from tests.integration.conftest import make_real_app as _make_real_app

pytestmark = pytest.mark.integration

_MODEL = "description-model"
_CONFIGURED_WINDOW = 8192
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.02
_DESCRIPTION_KEY = "description"
_COLLECTION = "status-notes"
_REPLY = "Notes written for the status block."
#: Startup tasks that call the provider: they must end before the app shuts down (a task cancelled mid-request leaves a
#: pooled connection that is closed on a dead loop at garbage collection and fails an unrelated later test).
_STARTUP_TASKS = ("_startup_sync_task", "_llm_window_task", "_description_probe_task")
#: Every top-level field of the ``/status`` body that existed before the description block.
_PRE_EXISTING_FIELDS = frozenset(
    {
        "running",
        "pid",
        "version",
        "collections",
        "readiness",
        "backup",
        "store_schema_version",
        "collections_schema_behind",
        "maintenance",
        "model_validation",
        "telemetry",
        "mcp",
        "search",
        "hyde",
        "rag_fusion",
        "failed_expired_ingest_count",
        "graph",
        "code_parsers",
        "warmup_result",
        "sync_result",
    }
)


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _toml(base_url: str) -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
    )


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


@contextlib.contextmanager
def make_real_app(*args: Any, **kwargs: Any) -> Iterator[tuple[TestClient, Any, str]]:
    """``make_real_app`` that lets the provider-calling startup tasks end before the app shuts down."""
    with _make_real_app(*args, **kwargs) as (client, config, key):
        try:
            yield client, config, key
        finally:
            for name in _STARTUP_TASKS:
                task = getattr(client.app.state, name, None)
                if task is not None:
                    _wait_for(task.done, f"the {name} to end")


def _probe_done(client: TestClient) -> bool:
    return client.app.state.description_reachability is not None


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _status(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.get("/status", headers=_headers(api_key))
    assert response.status_code == 200, response.text
    return response.json()


def test_status_description_block_reports_the_probe_and_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        FakeModelServer() as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_toml(server.base_url)) as (client, _config, api_key),
    ):
        _wait_for(lambda: _probe_done(client), "the startup probe")
        block = _status(client, api_key)[_DESCRIPTION_KEY]

    assert block == {
        "provider": "ollama",
        "key_available": True,
        "reachable": True,
        "effective_context_tokens": _CONFIGURED_WINDOW,
        "context_window_source": "configured",
        "backfill_on_startup": False,
        "last_attempt_ok": None,
        "last_attempt_at": None,
    }


def test_status_description_block_shows_the_last_attempt_after_a_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        FakeModelServer(reply_text=_REPLY) as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_toml(server.base_url)) as (client, _config, api_key),
    ):
        _wait_for(lambda: _probe_done(client), "the startup probe")
        _wait_for(client.app.state._llm_window_task.done, "the context-window resolution")
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "notes.txt").write_text("Archon search keeps local documents searchable.\n" * 20, encoding="utf-8")
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=api_key)
        before = _status(client, api_key)[_DESCRIPTION_KEY]
        _wait_for(lambda: _status(client, api_key)[_DESCRIPTION_KEY]["last_attempt_ok"] is not None, "the last attempt")
        after = _status(client, api_key)[_DESCRIPTION_KEY]

    assert before["reachable"] is True
    assert after["last_attempt_ok"] is True
    assert after["last_attempt_at"] is not None


def test_ready_unaffected_when_description_provider_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        unreachable_port() as port,
        make_real_app(tmp_path, monkeypatch, toml_content=_toml(f"http://127.0.0.1:{port}")) as (client, _config, api_key),
    ):
        _wait_for(lambda: _probe_done(client), "the startup probe")
        _wait_for(lambda: client.get("/ready").status_code == 200, "readiness")
        ready = client.get("/ready")
        status = _status(client, api_key)

    assert status[_DESCRIPTION_KEY]["reachable"] is False
    assert status[_DESCRIPTION_KEY]["effective_context_tokens"] == _CONFIGURED_WINDOW
    assert status["model_validation"]["validated_at"] is not None
    assert status["model_validation"]["provider_warnings"] == []
    assert ready.status_code == 200
    assert _DESCRIPTION_KEY not in ready.text


def test_status_response_additive_for_existing_consumers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _config, api_key):
        body = _status(client, api_key)
        openapi = client.get("/openapi.json", headers=_headers(api_key)).json()
        schema = openapi["components"]["schemas"]["StatusResponse"]

    assert body[_DESCRIPTION_KEY] is None
    assert _PRE_EXISTING_FIELDS <= body.keys()
    assert _DESCRIPTION_KEY not in schema.get("required", [])
    # A consumer that never heard of the block still parses the body once the block is dropped.
    legacy = {key: value for key, value in body.items() if key != _DESCRIPTION_KEY}
    assert StatusResponse.model_validate(legacy).description is None
