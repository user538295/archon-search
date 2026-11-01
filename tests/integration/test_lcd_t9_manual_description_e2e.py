"""T-9 (collection-descriptions plan, S26, S27, S28, S30, S60, S61, S62) — set, clear and protection of a manual description.

Real app lifespan and real LanceDB; REST through the TestClient, MCP through the real ``/mcp`` endpoint, the CLI through the
Click runner bridged to the app (``tests/_cli_testclient_bridge.py``). Every set and clear is read back through the three
read surfaces (REST detail, MCP ``get_collection_meta``, CLI ``collection info``).

Run with: ``uv run pytest tests/integration/test_lcd_t9_manual_description_e2e.py -m integration``.

``collection describe`` is covered by ``test_lcd_t10_describe_e2e.py``, including the refusal of ``describe`` for a foreign
namespace (S27): this file asserts the set and clear routes only.
"""

from __future__ import annotations

import contextlib
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.collection import collection as collection_command
from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE, MAX_DESCRIPTION_CHARS
from archon_search.server.schemas import WarmupResult
from tests._cli_testclient_bridge import CliBridge, cli_against_app
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import ingest_file_via_path, make_real_app
from tests.integration.test_mcp_roundtrip_t2 import (
    _extract_tool_text,
    _mcp_call_tool,
    _mcp_initialize,
)

pytestmark = pytest.mark.integration

_COLLECTION = "manual-e2e"
_OWNER_NAMESPACE = "team-owner"
_OTHER_NAMESPACE = "team-other"
_MODEL = "description-model"
_CHAT_PATH = "/api/chat"
_FIRST_REPLY = "Notes about the first ingest of this collection."
_SECOND_REPLY = "Notes about the collection after the clear."
_CONFIGURED_WINDOW = 8192
_PROVIDER_TIMEOUT_SECONDS = 120
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_DESCRIBED_DOCUMENTS = 5
_GROWN_DOCUMENTS = 10  # +100% from the 5 described documents
_FINAL_DOCUMENTS = 11
_SURFACES = ("rest", "mcp", "cli")
_INVALID_TEXTS = {
    "empty": "",
    "whitespace-only": "   ",
    "line-break": "first line\nsecond line",
    "tab": "a\ttab in the text",
    "line-separator": "a\u2028separator in the text",
    "control-character": "a bell\x07 in the text",
    "over-long": "x" * (MAX_DESCRIPTION_CHARS + 1),
}
_MAX_LENGTH_TEXT = "y" * MAX_DESCRIPTION_CHARS
_PADDED_TEXT = "  padded on both sides  "
_MAX_LENGTH_NON_ASCII_TEXT = "é" * MAX_DESCRIPTION_CHARS  # 1200 bytes in UTF-8: the limit counts characters
_UNKNOWN_COLLECTION = "no-such-collection"
_NOT_FOUND_CODE = "not_found"
_VALIDATION_CODE = "validation_error"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(_POLL_SECONDS)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _document(index: int) -> str:
    return f"Field notes {index} about the survey of the valley and how it was carried out.\n" * 3


def _write_corpus(directory: Path, count: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"doc-{index}.txt").write_text(_document(index), encoding="utf-8")
    return directory


class _Keys(NamedTuple):
    owner: str
    other: str


@contextlib.contextmanager
def _owner_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Keys, CliBridge]]:
    """``[description]`` absent; a namespace key that is not admin owns a meta-only collection (no config-path entry)."""
    corpus = _write_corpus(tmp_path / "corpus", _DESCRIBED_DOCUMENTS)
    keys = _Keys(owner=secrets.token_hex(32), other=secrets.token_hex(32))
    namespaces = {keys.owner: _OWNER_NAMESPACE, keys.other: _OTHER_NAMESPACE}
    with (
        make_real_app(tmp_path, monkeypatch, namespaces=namespaces, mcp_enabled=True) as (client, config, _admin),
        cli_against_app(client, keys.owner, monkeypatch) as cli,
    ):
        assert _COLLECTION not in [Path(path).name for path in config.collections], "no config-path entry"
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=keys.owner)
        yield client, keys, cli


def _rest_detail(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get(f"/collections/{_COLLECTION}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _mcp_tool(client: TestClient, token: str, session: str, tool: str, arguments: dict[str, Any]) -> Any:
    return _extract_tool_text(_mcp_call_tool(client, token, session, tool, arguments), tool)


def _mcp_detail(client: TestClient, token: str, session: str) -> dict[str, Any]:
    return _mcp_tool(client, token, session, "get_collection_meta", {"name": _COLLECTION})


def _cli_info(cli: CliBridge, token: str) -> dict[str, str]:
    result = cli.invoke(collection_command, ["info", _COLLECTION], token=token)
    assert result.exit_code == 0, result.output
    fields: dict[str, str] = {}
    for line in result.output.splitlines():
        name, separator, value = line.partition(": ")
        if separator:
            fields[name] = value
    return fields


def _assert_description_everywhere(
    client: TestClient, cli: CliBridge, token: str, session: str, text: str | None
) -> None:
    """*text* ``None`` means cleared: empty description, source and status null (the CLI omits all three)."""
    rest, mcp = _rest_detail(client, token), _mcp_detail(client, token, session)
    info = _cli_info(cli, token)
    if text is None:
        assert rest["description"] == ""  # REST reports an empty description as "" (T-6), MCP as null
        assert mcp["description"] is None
        for view in (rest, mcp):
            assert (view["description_source"], view["description_status"]) == (None, None)
        assert not {"description", "description_source", "description_status"} & info.keys()
        return
    for view in (rest, mcp):
        assert (view["description"], view["description_source"], view["description_status"]) == (text, "manual", "ok")
    assert (info["description"], info["description_source"], info["description_status"]) == (text, "manual", "ok")


def _assert_set_answer(surface: str, outcome: Any, text: str) -> None:
    """A successful set answered with the stored manual state (CLI: its confirmation line)."""
    if surface == "cli":
        assert outcome.exit_code == 0, outcome.output
        assert "description_source: manual" in outcome.output and "description_status: ok" in outcome.output
        return
    body = outcome.json() if surface == "rest" else outcome
    if surface == "rest":
        assert outcome.status_code == 200, outcome.text
    assert (body["description"], body["description_source"], body["description_status"]) == (text, "manual", "ok")


def _assert_clear_answer(surface: str, outcome: Any) -> None:
    """A successful clear answered with the empty state (CLI: its confirmation and the hint to run describe)."""
    if surface == "cli":
        assert outcome.exit_code == 0, outcome.output
        assert f"collection describe {_COLLECTION}" in outcome.output
        return
    body = outcome.json() if surface == "rest" else outcome
    if surface == "rest":
        assert outcome.status_code == 200, outcome.text
    assert not body["description"]
    assert (body["description_source"], body["description_status"]) == (None, None)


def _assert_not_found(surface: str, refusal: tuple[Any, str]) -> None:
    """*refusal* is the ``_refusal`` pair: the status, code or exit code first, then the normalised text."""
    expected = {"rest": 404, "mcp": _NOT_FOUND_CODE, "cli": 1}[surface]
    assert refusal[0] == expected, refusal
    assert "not found" in refusal[1].lower(), refusal


def _set(surface: str, client: TestClient, cli: CliBridge, token: str, session: str, text: str, collection: str = _COLLECTION) -> Any:
    """Set through *surface*; returns the raw REST response, the MCP payload or the CLI result."""
    if surface == "rest":
        return client.put(f"/collections/{collection}/description", json={"description": text}, headers=_headers(token))
    if surface == "mcp":
        arguments = {"collection_name": collection, "description": text}
        return _mcp_tool(client, token, session, "set_collection_description", arguments)
    return cli.invoke(collection_command, ["set-description", collection, text], token=token)


def _clear(surface: str, client: TestClient, cli: CliBridge, token: str, session: str, collection: str = _COLLECTION) -> Any:
    if surface == "rest":
        return client.delete(f"/collections/{collection}/description", headers=_headers(token))
    if surface == "mcp":
        return _mcp_tool(client, token, session, "clear_collection_description", {"collection_name": collection})
    return cli.invoke(collection_command, ["clear-description", collection], token=token)


def _refusal(surface: str, outcome: Any, collection: str) -> Any:
    """What a refused set or clear tells its caller, with the collection name normalised away."""
    if surface == "rest":
        return (outcome.status_code, outcome.text.replace(collection, "<name>"))
    if surface == "mcp":
        return (outcome["code"], outcome["error"].replace(collection, "<name>"))
    return (outcome.exit_code, outcome.output.replace(collection, "<name>"))


def _stored_or_none(client: TestClient, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
    store = client.app.state.search_store
    return client.portal.call(lambda: store.get_collection_meta(_COLLECTION, namespace=namespace))  # type: ignore[union-attr]


def _stored(client: TestClient, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta:
    meta = _stored_or_none(client, namespace)
    assert meta is not None
    return meta


def _generated_and_ok(client: TestClient, token: str, text: str) -> bool:
    view = _rest_detail(client, token)
    return (view["description"], view["description_source"], view["description_status"]) == (text, "generated", "ok")


def test_e2e_namespace_key_sets_and_clears_on_meta_only_collection_over_rest_mcp_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_app(tmp_path, monkeypatch) as (client, keys, cli):
        token = keys.owner
        session = _mcp_initialize(client, token)
        assert _rest_detail(client, token)["path"] == ""
        admin_only = client.post("/collections", json={"path": str(tmp_path)}, headers=_headers(token))
        assert admin_only.status_code == 403, "the key is not an admin key, so the success below needs no admin right"
        _assert_description_everywhere(client, cli, token, session, None)

        for surface in _SURFACES:
            text = f"Hand written note set through {surface}."
            _assert_set_answer(surface, _set(surface, client, cli, token, session, text), text)
            _assert_description_everywhere(client, cli, token, session, text)
            stored = _stored(client, _OWNER_NAMESPACE)
            assert stored.description_embedding, "the routing vector is refreshed with the text"
            assert stored.last_described is not None

            _assert_clear_answer(surface, _clear(surface, client, cli, token, session))
            _assert_description_everywhere(client, cli, token, session, None)
            stored = _stored(client, _OWNER_NAMESPACE)
            assert (stored.last_described, stored.description_embedding) == (None, None), "a clear resets every field"

        # A set over a manual text replaces it, and a clear works from a different surface than the set.
        _assert_set_answer("rest", _set("rest", client, cli, token, session, "First manual text."), "First manual text.")
        _assert_set_answer("mcp", _set("mcp", client, cli, token, session, "Second manual text."), "Second manual text.")
        _assert_description_everywhere(client, cli, token, session, "Second manual text.")
        _assert_clear_answer("cli", _clear("cli", client, cli, token, session))
        _assert_description_everywhere(client, cli, token, session, None)


def test_e2e_foreign_namespace_key_gets_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set and clear for a collection of another namespace answer exactly like a collection that does not exist and leave
    the owner's description alone. ``describe`` is asserted by ``test_lcd_t10_describe_e2e.py``."""
    with _owner_app(tmp_path, monkeypatch) as (client, keys, cli):
        owner_session = _mcp_initialize(client, keys.owner)
        assert _set("rest", client, cli, keys.owner, owner_session, "Owner text.").status_code == 200
        other_session = _mcp_initialize(client, keys.other)

        for surface in _SURFACES:
            answers: dict[str, list[Any]] = {}
            for name in (_COLLECTION, _UNKNOWN_COLLECTION):  # foreign and unknown must be indistinguishable
                answers[name] = [
                    _refusal(
                        surface,
                        _set(surface, client, cli, keys.other, other_session, "Intruder text.", collection=name),
                        name,
                    ),
                    _refusal(surface, _clear(surface, client, cli, keys.other, other_session, collection=name), name),
                ]
            assert answers[_COLLECTION] == answers[_UNKNOWN_COLLECTION], surface
            for refusal in answers[_COLLECTION]:
                _assert_not_found(surface, refusal)

        _assert_description_everywhere(client, cli, keys.owner, owner_session, "Owner text.")
        assert _stored_or_none(client, _OTHER_NAMESPACE) is None


def test_e2e_invalid_manual_text_rejected_on_every_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _owner_app(tmp_path, monkeypatch) as (client, keys, cli):
        token = keys.owner
        session = _mcp_initialize(client, token)

        writer = client.app.state.description_writer
        original = writer.apply
        applied: list[Any] = []

        async def recording(*args: Any, **kwargs: Any) -> Any:
            applied.append(args)
            return await original(*args, **kwargs)

        monkeypatch.setattr(writer, "apply", recording)
        assert _set("rest", client, cli, token, session, "Kept text.").status_code == 200
        assert len(applied) == 1, "the recorder sees a write that does reach the writer"

        for label, text in _INVALID_TEXTS.items():
            rest = _set("rest", client, cli, token, session, text)
            assert rest.status_code == 422, f"{label}: {rest.text}"
            mcp = _set("mcp", client, cli, token, session, text)
            assert mcp["code"] == _VALIDATION_CODE, f"{label}: {mcp}"
            command = _set("cli", client, cli, token, session, text)
            assert command.exit_code == 1, f"{label}: {command.output}"
            assert command.output.startswith("Error:"), f"{label}: {command.output}"
            assert "contacting server" not in command.output and "server returned" not in command.output, label

        assert len(applied) == 1, "no surface reached the writer with invalid text"
        _assert_description_everywhere(client, cli, token, session, "Kept text.")  # never trimmed, never replaced

        # The limit is inclusive, non-ASCII text counts characters, and valid text is stored as given: padding is not
        # trimmed on any surface.
        for text in (_MAX_LENGTH_TEXT, _PADDED_TEXT, _MAX_LENGTH_NON_ASCII_TEXT):
            for surface in _SURFACES:
                _assert_set_answer(surface, _set(surface, client, cli, token, session, text), text)
                assert _rest_detail(client, token)["description"] == text, surface
                assert _mcp_detail(client, token, session)["description"] == text, surface
                assert _cli_info(cli, token)["description"].strip() == text.strip(), surface


def _description_toml(base_url: str) -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
    )


def test_e2e_manual_description_survives_ingest_and_clear_then_regenerates_on_next_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _write_corpus(tmp_path / "corpus", _DESCRIBED_DOCUMENTS)
    manual = "Hand written note that generation must never replace."
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_description_toml(server.base_url), mcp_enabled=True) as (
            client,
            _config,
            token,
        ),
        cli_against_app(client, token, monkeypatch) as cli,
    ):
        _wait_for(
            lambda: getattr(client.app.state, "warmup_result", None) is WarmupResult.DONE,
            "the lifespan warm-up",
        )
        _wait_for(client.app.state._llm_window_task.done, "the context-window resolution")
        session = _mcp_initialize(client, token)
        lifecycle = client.app.state.description_lifecycle

        def chats() -> int:
            return len(server.requests_to(_CHAT_PATH))

        asked = 0  # the chat requests expected so far; raised whenever a generation is expected

        def assert_no_generation(why: str) -> None:
            """Nothing is admitted (admission is synchronous, the model call is not) and the model was asked nothing."""
            assert not lifecycle.is_pending(_COLLECTION), why
            assert chats() == asked, why

        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=token)
        _wait_for(lambda: _generated_and_ok(client, token, _FIRST_REPLY), "the generated description")
        asked = chats()
        assert asked == 1

        assert _set("mcp", client, cli, token, session, manual)["description_source"] == "manual"
        assert_no_generation("a set asks the model for nothing")

        _write_corpus(corpus, _GROWN_DOCUMENTS)  # +100% from the 5 described documents: the 20% rule qualifies it
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=token)
        assert _rest_detail(client, token)["doc_count"] == _GROWN_DOCUMENTS
        _assert_description_everywhere(client, cli, token, session, manual)
        assert_no_generation("an ingest never asks the model about a manual description")
        assert _stored(client).described_at_doc_count == _DESCRIBED_DOCUMENTS, "the baseline is the generation's"

        cleared = _clear("cli", client, cli, token, session)
        assert cleared.exit_code == 0, cleared.output
        assert f"collection describe {_COLLECTION}" in cleared.output
        _assert_description_everywhere(client, cli, token, session, None)
        assert _stored(client).described_at_doc_count is None, "a clear unsets the baseline"
        assert_no_generation("a clear starts no generation")

        server.reply_text = _SECOND_REPLY
        _write_corpus(corpus, _FINAL_DOCUMENTS)
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=token)
        _wait_for(lambda: _generated_and_ok(client, token, _SECOND_REPLY), "the regenerated description")
        asked = chats()
        assert asked == 2
        info = _cli_info(cli, token)
        assert (info["description"], info["description_source"], info["description_status"]) == (
            _SECOND_REPLY,
            "generated",
            "ok",
        )
        assert _mcp_detail(client, token, session)["description"] == _SECOND_REPLY

        # The REST and MCP clears start nothing either, and a set over a manual text replaces it.
        assert _set("rest", client, cli, token, session, manual).status_code == 200
        assert _set("mcp", client, cli, token, session, "Second hand written note.")["description_source"] == "manual"
        _assert_description_everywhere(client, cli, token, session, "Second hand written note.")
        assert _clear("mcp", client, cli, token, session)["description_status"] is None
        assert _clear("rest", client, cli, token, session).status_code == 200
        _assert_description_everywhere(client, cli, token, session, None)
        assert_no_generation("the REST and MCP clears start no generation")
