"""MCP list tools declare the error result they can return.

``ingest_directory``, ``list_collections``, ``get_collections_meta`` and ``list_documents`` return a list on
success and the sanitized ``{"error", "code"}`` object on failure. Their return annotation names both, so the
advertised ``outputSchema`` accepts the error and FastMCP serializes it without a pydantic serializer warning.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import (
    make_real_app,
    mcp_headers,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = pytest.mark.integration

_LIST_TOOLS = ("ingest_directory", "list_collections", "get_collections_meta", "list_documents")
_ERROR_KEYS = {"error", "code"}
_INVALID_PARAMETER_CODE = "invalid_parameter"
_NON_POSITIVE_TTL_SECONDS = 0
_SERIALIZER_WARNING = "Pydantic serializer warnings"


def _tools_by_name(client: TestClient, token: str, session_id: str) -> dict[str, dict]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=mcp_headers(token, session_id),
    )
    assert response.status_code == 200, response.text[:300]
    data_lines = [line[5:].strip() for line in response.text.split("\n") if line.startswith("data:")]
    assert data_lines, response.text[:300]
    return {tool["name"]: tool for tool in json.loads(data_lines[-1])["result"]["tools"]}


@pytest.fixture
def mcp_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _config, admin_key):
        yield client, admin_key, mcp_initialize(client, admin_key)


@pytest.mark.parametrize("tool_name", _LIST_TOOLS)
def test_output_schema_of_a_list_tool_accepts_the_error_object(mcp_world, tool_name: str) -> None:
    client, key, session_id = mcp_world

    schema = _tools_by_name(client, key, session_id)[tool_name]["outputSchema"]

    arms = schema["properties"]["result"]["anyOf"]
    assert any(arm.get("type") == "array" for arm in arms), arms
    error_arms = [arm for arm in arms if arm.get("type") == "object"]
    assert len(error_arms) == 1, arms
    assert _ERROR_KEYS <= set(error_arms[0]["properties"]), error_arms[0]


def test_an_error_result_from_a_list_tool_serializes_without_a_pydantic_warning(mcp_world, tmp_path: Path) -> None:
    client, key, session_id = mcp_world

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = mcp_tool_call(
            client,
            key,
            session_id,
            "ingest_directory",
            {"path": str(tmp_path), "chunk_ttl_seconds": _NON_POSITIVE_TTL_SECONDS},
        )

    assert set(result) == _ERROR_KEYS
    assert result["code"] == _INVALID_PARAMETER_CODE
    assert not [w for w in caught if _SERIALIZER_WARNING in str(w.message)]
