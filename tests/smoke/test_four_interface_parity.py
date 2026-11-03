"""Smoke test: one query through REST, MCP, the OpenAI shim and the CLI ``--json`` against a real server (S66).

``smoke_server`` (``tests/smoke/conftest.py``) runs a real ``archon-search serve`` subprocess with the shim and the
fake-LLM HyDE and RAG Fusion enabled in its TOML. REST, MCP and the CLI must return the same ordered sources, approximately
equal scores and equal derived fields; the shim returns the same sources, in the same order, in its chat text.
"""

from __future__ import annotations

import gc
import json
import re
import subprocess
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.smoke.conftest import FAKE_LLM_VARIANT_COUNT

pytestmark = pytest.mark.xdist_group("smoke_e2e")

_CLI_RUN_TIMEOUT_S = 120
_MCP_CALL_TIMEOUT_S = 60.0
_HTTP_TIMEOUT_S = 60.0
_COLLECTION = "smoke"
_QUERY = "hybrid retrieval reranker"
_SHIM_MODEL = f"archon-search/{_COLLECTION}"
_SCORE_TOLERANCE = 1e-3
_SCORE_FIELDS = ("score", "reranker_score")
_SOURCE_PATTERN = re.compile(r"\[Source: ([^\]]+)\]")


def _headers(smoke_server) -> dict[str, str]:
    return {"Authorization": f"Bearer {smoke_server.api_key}"}


def _rest_search(smoke_server, **extra: Any) -> dict[str, Any]:
    body = {"collection": _COLLECTION, "query": _QUERY, **extra}
    response = httpx.post(f"{smoke_server.base_url}/search", json=body, headers=_headers(smoke_server), timeout=_HTTP_TIMEOUT_S)
    assert response.status_code == 200, response.text
    return response.json()


def _cli_search_json(smoke_server) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "uv", "run", "archon-search", "search", _QUERY, "--collection", _COLLECTION, "--rag-fusion", "--json",
            "--api-url", smoke_server.base_url, "--api-key", smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=_CLI_RUN_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    return json.loads(completed.stdout)


async def _mcp_search(smoke_server) -> dict[str, Any]:
    transport = StreamableHttpTransport(f"{smoke_server.base_url}/mcp/", headers=_headers(smoke_server))
    async with Client(transport, timeout=_MCP_CALL_TIMEOUT_S, init_timeout=_MCP_CALL_TIMEOUT_S) as mcp:
        result = await mcp.call_tool("search", {"query": _QUERY, "collection": _COLLECTION, "rag_fusion": True})
    gc.collect()
    payload = result.structured_content
    assert payload is not None, result
    assert "code" not in payload, payload  # a tool-level failure is a normal ``{error, code}`` dict
    return payload


def _without_scores(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with each hit's float scores removed: everything else must be equal across interfaces."""
    return {
        **payload,
        "results": [{k: v for k, v in hit.items() if k not in _SCORE_FIELDS} for hit in payload["results"]],
    }


def _assert_same_answer(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    assert _without_scores(actual) == _without_scores(expected)
    for field in _SCORE_FIELDS:  # ``reranker_score`` may be ``None``: pytest.approx compares None only with None
        assert [hit[field] for hit in actual["results"]] == pytest.approx(
            [hit[field] for hit in expected["results"]], abs=_SCORE_TOLERANCE
        )


async def test_rest_mcp_and_cli_return_the_same_ordered_results_and_derived_fields_on_a_real_server(smoke_server) -> None:
    """S66: same sources in the same order, approximately equal scores and equal derived fields on three interfaces."""
    # Warm the collection's embedder first, so no cold-embedder degrade skews one interface's ranking.
    _rest_search(smoke_server)

    rest = _rest_search(smoke_server, rag_fusion=True)
    # REST must answer stably, or the cross-interface equality proves nothing.
    _assert_same_answer(_rest_search(smoke_server, rag_fusion=True), rest)
    mcp = await _mcp_search(smoke_server)
    cli = _cli_search_json(smoke_server)

    # Not degenerate: a real ranking with several hits and derived fields that differ from their defaults.
    assert len(rest["results"]) > 1, rest
    assert rest["rag_fusion_applied"] is True
    assert rest["rag_fusion_attempted"] is True
    assert rest["rag_fusion_queries_used"] == FAKE_LLM_VARIANT_COUNT
    for other in (mcp, cli):
        _assert_same_answer(other, rest)


def test_shim_returns_the_same_ordered_sources_on_a_real_server(smoke_server) -> None:
    """S66: the shim's ``[Source: <path>]`` order equals REST's ``source_path`` order for the same query."""
    _rest_search(smoke_server)  # warm-up: no cold-embedder degrade may skew the ranking
    expected_sources = [hit["source_path"] for hit in _rest_search(smoke_server)["results"]]
    assert len(expected_sources) > 1, expected_sources

    response = httpx.post(
        f"{smoke_server.base_url}/v1/chat/completions",
        json={"model": _SHIM_MODEL, "messages": [{"role": "user", "content": _QUERY}]},
        headers=_headers(smoke_server),
        timeout=_HTTP_TIMEOUT_S,
    )

    assert response.status_code == 200, response.text
    content = response.json()["choices"][0]["message"]["content"]
    assert _SOURCE_PATTERN.findall(content) == expected_sources
