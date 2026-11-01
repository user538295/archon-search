"""BE-32 (collection-descriptions plan, C8, S60, S61) — MCP tools that set or clear a description by hand.

The count test reads the tools a real FastMCP app advertises; the owner test calls both tools over the mounted ``/mcp``
endpoint with a namespace key that is not admin, against a collection that has a meta row and no config path.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archon_search.key_manager import KeyStore
from archon_search.server.routes_collections import _all_collection_paths
from tests._mcp_tools import registered_tools
from tests.integration.conftest import (
    ingest_file_via_path,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = pytest.mark.integration

_OWNER_NAMESPACE = "team-owner"
_OTHER_NAMESPACE = "team-other"
_COLLECTION = "manual-notes"
_MANUAL_TEXT = "Hand written note about the planning documents."
_NEW_TOOLS = {"set_collection_description", "clear_collection_description"}
_KEY_TOOLS = {"create_key", "list_keys", "revoke_key", "rotate_key"}
_TOTAL_WITH_KEY_STORE = 22
_TOTAL_WITHOUT_KEY_STORE = 18
_DOCUMENTS = 3


def test_mcp_tool_count_is_22_with_key_store_and_18_without() -> None:
    without = registered_tools(key_store=None)
    with_keys = registered_tools(key_store=MagicMock(spec=KeyStore))

    assert len(without) == _TOTAL_WITHOUT_KEY_STORE, sorted(without)
    assert len(with_keys) == _TOTAL_WITH_KEY_STORE, sorted(with_keys)
    assert _NEW_TOOLS <= set(without), "registered before the key-store gate"
    assert set(with_keys) - set(without) == _KEY_TOOLS


def _write_corpus(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(_DOCUMENTS):
        (directory / f"note-{index}.txt").write_text(
            f"Field notes {index} about the survey of the valley and how it was carried out.\n" * 3
        )
    return directory


def test_mcp_set_and_clear_work_on_meta_only_collection_without_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-admin namespace key sets and clears the description of a meta-only collection over MCP, and a key of
    another namespace gets ``not_found`` and changes nothing."""
    corpus = _write_corpus(tmp_path / "corpus")
    owner_key, other_key = secrets.token_hex(32), secrets.token_hex(32)
    namespaces = {owner_key: _OWNER_NAMESPACE, other_key: _OTHER_NAMESPACE}
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, namespaces=namespaces) as (client, config, _admin):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=owner_key)
        assert _COLLECTION not in _all_collection_paths(config).values(), "no config-path entry"
        owner_session = mcp_initialize(client, owner_key)

        set_result = mcp_tool_call(
            client, owner_key, owner_session, "set_collection_description",
            {"collection_name": _COLLECTION, "description": _MANUAL_TEXT},
        )

        assert "error" not in set_result, set_result
        assert set_result["name"] == _COLLECTION
        assert (set_result["description"], set_result["description_source"], set_result["description_status"]) == (
            _MANUAL_TEXT, "manual", "ok",
        )
        stored = mcp_tool_call(client, owner_key, owner_session, "get_collection_meta", {"name": _COLLECTION})
        assert stored["description"] == _MANUAL_TEXT

        invalid = mcp_tool_call(
            client, owner_key, owner_session, "set_collection_description",
            {"collection_name": _COLLECTION, "description": "two\nlines"},
        )
        assert invalid["code"] == "validation_error"
        kept = mcp_tool_call(client, owner_key, owner_session, "get_collection_meta", {"name": _COLLECTION})
        assert kept["description"] == _MANUAL_TEXT

        other_session = mcp_initialize(client, other_key)
        for tool, arguments in (
            ("set_collection_description", {"collection_name": _COLLECTION, "description": "an intruder's text"}),
            ("clear_collection_description", {"collection_name": _COLLECTION}),
        ):
            refused = mcp_tool_call(client, other_key, other_session, tool, arguments)
            assert refused["code"] == "not_found", refused

        untouched = mcp_tool_call(client, owner_key, owner_session, "get_collection_meta", {"name": _COLLECTION})
        assert untouched["description"] == _MANUAL_TEXT, "a foreign set or clear changes nothing"
        assert untouched["description_source"] == "manual"

        cleared = mcp_tool_call(
            client, owner_key, owner_session, "clear_collection_description", {"collection_name": _COLLECTION}
        )

        assert "error" not in cleared, cleared
        assert not cleared["description"]
        assert cleared["description_source"] is None
        assert cleared["description_status"] is None
