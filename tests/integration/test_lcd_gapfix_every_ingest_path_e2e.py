"""S17, S18 — every path that changes a collection's documents asks for a description, not only a batch ingest.

Real app lifespan, real LanceDB, a real HTTP model host (``FakeModelServer``): the first ingest of a NEW collection through
a single file (REST ``POST /ingest``, MCP ``ingest_file``, CLI ``ingest --path <file>``) returns before the held model call
answers, then the description reads ``generated`` / ``ok``; a collection created and later grown by the sync gets its
description and its regeneration without any ingest request.

Run with:
    uv run pytest tests/integration/test_lcd_gapfix_every_ingest_path_e2e.py --no-cov -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.ingest import ingest as ingest_command
from tests._cli_testclient_bridge import cli_against_app
from tests._fake_model_server import FakeModelServer
from tests.integration.conftest import (
    ingest_file_via_path,
    make_real_app,
    trigger_maintenance_and_wait_for_orphans,
)
from tests.integration.test_lcd_t5_first_description_e2e import (
    _CHAT_PATH,
    _COLLECTION,
    _FIRST_DOCUMENT_MARKER,
    _FIRST_REPLY,
    _SECOND_REPLY,
    _await_chat_requests,
    _await_description,
    _await_ready,
    _booted,
    _description_toml,
    _detail,
    _document,
    _headers,
    _mcp_initialize,
    _mcp_tool,
    _no_proxy_for_loopback,  # noqa: F401 — autouse fixture of the sibling module, re-used here
    _prompt_of,
    _wait_for,
    _write_documents,
)

pytestmark = pytest.mark.integration

_SYNCED_DIRECTORY = "synced-docs"
_SYNCED_COLLECTION = "synced_docs"  # the sync names a collection after its directory, hyphens turned into underscores
_INITIAL_SYNCED_DOCUMENTS = 4
_SHRINK_DOCUMENTS = 5
_HTTP_ACCEPTED = 202


def _assert_pending_then_described(server: FakeModelServer, client: TestClient, api_key: str) -> None:
    """The ingest already returned; the model call is held, so the row reads ``pending`` and then ``generated``/``ok``."""
    _await_chat_requests(server, 1)
    held = _detail(client, api_key)
    assert (held["description"], held["description_source"], held["description_status"]) == ("", None, "pending")
    server.release()
    described = _await_description(client, api_key, _FIRST_REPLY)
    assert (described["description_source"], described["description_status"]) == ("generated", "ok")
    assert _FIRST_DOCUMENT_MARKER in _prompt_of(server.requests_to(_CHAT_PATH)[0])
    assert len(server.requests_to(_CHAT_PATH)) == 1


def test_e2e_first_single_file_over_rest_returns_then_description_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with (
        FakeModelServer(reply_text=_FIRST_REPLY, hold_chat=True) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key),
    ):
        ingest_file_via_path(client, _COLLECTION, str(corpus / "first.txt"), api_key=api_key)  # DONE while the model is held

        _assert_pending_then_described(server, client, api_key)


def test_e2e_first_single_file_over_mcp_returns_then_description_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with (
        FakeModelServer(reply_text=_FIRST_REPLY, hold_chat=True) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url), mcp=True) as (client, api_key),
    ):
        session = _mcp_initialize(client, api_key)
        result = _mcp_tool(
            client, api_key, session, "ingest_file", {"path": str(corpus / "first.txt"), "collection": _COLLECTION}
        )
        assert result["status"] == "ok", result  # returned although the held model call has not answered

        _assert_pending_then_described(server, client, api_key)


def test_e2e_first_single_file_over_the_cli_returns_then_description_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with (
        FakeModelServer(reply_text=_FIRST_REPLY, hold_chat=True) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key),
        cli_against_app(client, api_key, monkeypatch) as cli,
    ):
        result = cli.invoke(
            ingest_command, ["--path", str(corpus / "first.txt"), "--collection", _COLLECTION, "--wait"]
        )
        assert result.exit_code == 0, result.output

        _assert_pending_then_described(server, client, api_key)


def test_e2e_disabled_description_single_file_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url, enabled=False)) as (client, api_key),
    ):
        ingest_file_via_path(client, _COLLECTION, str(corpus / "first.txt"), api_key=api_key)
        detail = _detail(client, api_key)

    assert client.app.state.description_lifecycle is None  # nothing was built, so nothing could be requested
    assert server.requests_to(_CHAT_PATH) == []
    assert (detail["description"], detail["description_status"]) == ("", None)


def _synced_detail(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.get(f"/collections/{_SYNCED_COLLECTION}", headers=_headers(api_key))
    assert response.status_code == 200, response.text
    return response.json()


def _post_sync(client: TestClient, api_key: str) -> None:
    response = client.post("/sync", headers=_headers(api_key))
    assert response.status_code == _HTTP_ACCEPTED, response.text
    job_id = response.json()["job_id"]
    finished: dict[str, Any] = {}

    def done() -> bool:
        finished.update(client.get(f"/jobs/{job_id}", headers=_headers(api_key)).json())
        return finished["status"] in {"DONE", "FAILED"}

    _wait_for(done, "the manual sync")
    assert finished["status"] == "DONE", finished


def test_e2e_sync_created_collection_is_described_and_a_growing_sync_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup sync creates the collection (a batch of the sync's own) and describes it; a later sync that adds one
    document to four (+25%) goes through the incremental path, which asks again, and the rule regenerates."""
    synced = tmp_path / _SYNCED_DIRECTORY
    _write_documents(synced, {f"doc-{i}.txt": _document(f"sync-marker-{i}") for i in range(_INITIAL_SYNCED_DOCUMENTS)})
    toml = (
        f'[collections]\ncollections = ["{synced}"]\n'
        + _description_toml("PLACEHOLDER")
    )
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        toml = toml.replace("PLACEHOLDER", server.base_url)
        with _booted(tmp_path, monkeypatch, toml) as (client, api_key):
            _wait_for(lambda: len(server.requests_to(_CHAT_PATH)) >= 1, "the description of the sync-created collection")
            _wait_for(
                lambda: _synced_detail(client, api_key)["description_status"] == "ok",
                "the sync-created collection's description",
            )
            first = _synced_detail(client, api_key)
            assert (first["description"], first["description_source"]) == (_FIRST_REPLY, "generated")

            server.reply_text = _SECOND_REPLY
            _write_documents(synced, {"doc-new.txt": _document("sync-marker-new")})
            _post_sync(client, api_key)
            _wait_for(
                lambda: _synced_detail(client, api_key)["description"] == _SECOND_REPLY,
                "the regeneration after the incremental sync",
            )

    assert len(server.requests_to(_CHAT_PATH)) == 2
    assert "sync-marker-new" in _prompt_of(server.requests_to(_CHAT_PATH)[1])


def test_e2e_orphan_sweep_that_shrinks_a_collection_regenerates_its_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S18 shrink: five described documents, one source file deleted from disk, the maintenance sweep removes its chunks
    (-20%) and the lifespan-wired hook asks again, so the description is regenerated."""
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {f"doc-{i}.txt": _document(f"orphan-marker-{i}") for i in range(_SHRINK_DOCUMENTS)})
    with FakeModelServer(reply_text=_FIRST_REPLY) as server:
        toml = _description_toml(server.base_url)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml, maintenance_enabled=True) as (
            client,
            _config,
            api_key,
        ):
            _await_ready(client)
            ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=api_key)
            _await_description(client, api_key, _FIRST_REPLY)

            server.reply_text = _SECOND_REPLY
            (corpus / "doc-0.txt").unlink()
            trigger_maintenance_and_wait_for_orphans(client, _COLLECTION, api_key=api_key)
            regenerated = _await_description(client, api_key, _SECOND_REPLY)

    assert regenerated["description_source"] == "generated"
    assert len(server.requests_to(_CHAT_PATH)) == 2
    first_prompt, second_prompt = (_prompt_of(request) for request in server.requests_to(_CHAT_PATH))
    assert "orphan-marker-0" in first_prompt  # the document was described while it still existed
    assert "orphan-marker-0" not in second_prompt
