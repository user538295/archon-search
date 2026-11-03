"""Smoke test: HyDE and RAG Fusion response fields through the CLI and MCP against a real server with a fake LLM (S70).

``smoke_server`` is configured (``tests/smoke/conftest.py``) with ``[hyde]`` and ``[rag_fusion]`` pointing at the
session-scoped ``smoke_fake_llm`` (a loopback ``FakeModelServer``), so the expansion path runs for real over HTTP
without a live provider. The model's answer is two lines: RAG Fusion reads each line as a query variant and HyDE
embeds the whole text as its hypothetical document.
"""

from __future__ import annotations

import gc
import json
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.smoke.conftest import (
    FAKE_LLM_HYDE_AND_VARIANTS_REPLY,
    FAKE_LLM_VARIANT_COUNT,
)

pytestmark = pytest.mark.xdist_group("smoke_e2e")

_CLI_RUN_TIMEOUT_S = 120
_MCP_CALL_TIMEOUT_S = 60.0
_COLLECTION = "smoke"
_CHAT_PATH = "/api/chat"
_HYDE_WARNING = "HyDE expansion failed"


def _cli_human(smoke_server, query: str, *flags: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            "uv", "run", "archon-search", "search", query, "--collection", _COLLECTION,
            "--api-url", smoke_server.base_url, "--api-key", smoke_server.api_key, *flags,
        ],
        capture_output=True,
        text=True,
        timeout=_CLI_RUN_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    return completed


def _cli_json(smoke_server, query: str, *flags: str) -> dict[str, Any]:
    return json.loads(_cli_human(smoke_server, query, "--json", *flags).stdout)


async def _mcp_search(smoke_server, query: str, **flags: bool) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {smoke_server.api_key}"}
    transport = StreamableHttpTransport(f"{smoke_server.base_url}/mcp/", headers=headers)
    async with Client(transport, timeout=_MCP_CALL_TIMEOUT_S, init_timeout=_MCP_CALL_TIMEOUT_S) as mcp:
        result = await mcp.call_tool("search", {"query": query, "collection": _COLLECTION, **flags})
    gc.collect()
    payload = result.structured_content
    assert payload is not None, result
    assert "code" not in payload, payload  # a tool-level failure is a normal ``{error, code}`` dict
    return payload


@pytest.fixture
def failing_model_answer(smoke_fake_llm) -> Iterator[None]:
    """Make the fake model answer with nothing usable, then restore the usable two-line answer."""
    smoke_fake_llm.reply_text = ""
    yield
    smoke_fake_llm.reply_text = FAKE_LLM_HYDE_AND_VARIANTS_REPLY


async def test_hyde_and_rag_fusion_fields_via_cli_and_mcp_with_a_fake_llm(smoke_server, smoke_fake_llm) -> None:
    """Each interface reports the expansion that ran: HyDE applied, RAG Fusion's variants used, no warning."""
    chats_before = len(smoke_fake_llm.requests_to(_CHAT_PATH))

    cli_hyde = _cli_json(smoke_server, "hybrid retrieval", "--hyde")
    cli_fusion = _cli_json(smoke_server, "cross-encoder scoring", "--rag-fusion")
    mcp_hyde = await _mcp_search(smoke_server, "chunk ranking", hyde=True)
    mcp_fusion = await _mcp_search(smoke_server, "reranker model", rag_fusion=True)
    mcp_both = await _mcp_search(smoke_server, "vector search", hyde=True, rag_fusion=True)

    assert len(smoke_fake_llm.requests_to(_CHAT_PATH)) - chats_before == 5, "each expansion must reach the fake model once"
    # RAG Fusion wins over HyDE when both are requested: HyDE is suppressed without a warning.
    assert mcp_both["hyde_applied"] is False
    assert mcp_both["rag_fusion_applied"] is True
    assert mcp_both["expansion_warning"] is None
    for hyde_payload in (cli_hyde, mcp_hyde):
        assert hyde_payload["results"], hyde_payload
        assert hyde_payload["hyde_applied"] is True
        assert hyde_payload["rag_fusion_applied"] is False
        assert hyde_payload["rag_fusion_attempted"] is False
        assert hyde_payload["rag_fusion_queries_used"] == 0
        assert hyde_payload["expansion_warning"] is None
    for fusion_payload in (cli_fusion, mcp_fusion):
        assert fusion_payload["results"], fusion_payload
        assert fusion_payload["hyde_applied"] is False
        assert fusion_payload["rag_fusion_applied"] is True
        assert fusion_payload["rag_fusion_attempted"] is True
        assert fusion_payload["rag_fusion_queries_used"] == FAKE_LLM_VARIANT_COUNT, fusion_payload
        assert fusion_payload["expansion_warning"] is None


async def test_unusable_model_answer_degrades_to_the_plain_query_via_cli_and_mcp(
    smoke_server, smoke_fake_llm, failing_model_answer
) -> None:
    """A model that answers nothing degrades to the plain query on both interfaces: HyDE says so in the warning, RAG Fusion reports an attempt with no variants used."""
    chats_before = len(smoke_fake_llm.requests_to(_CHAT_PATH))

    cli_hyde = _cli_json(smoke_server, "warning hyde cli", "--hyde")
    cli_fusion = _cli_json(smoke_server, "warning fusion cli", "--rag-fusion")
    mcp_hyde = await _mcp_search(smoke_server, "warning hyde mcp", hyde=True)
    mcp_fusion = await _mcp_search(smoke_server, "warning fusion mcp", rag_fusion=True)
    cli_human = _cli_human(smoke_server, "warning hyde human", "--hyde")

    # The model was reached and answered empty each time: the degrade is not an unreachable provider.
    assert len(smoke_fake_llm.requests_to(_CHAT_PATH)) - chats_before == 5
    assert f"Note: {_HYDE_WARNING}" in cli_human.stderr.splitlines()
    for hyde_payload in (cli_hyde, mcp_hyde):
        assert hyde_payload["hyde_applied"] is False
        assert hyde_payload["expansion_warning"] == _HYDE_WARNING
    for fusion_payload in (cli_fusion, mcp_fusion):
        assert fusion_payload["rag_fusion_applied"] is False
        assert fusion_payload["rag_fusion_attempted"] is True
        assert fusion_payload["rag_fusion_queries_used"] == 0
