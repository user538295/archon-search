"""Smoke tests: per-request ``top_k`` on a real server through REST, the CLI, the OpenAI shim and MCP (S68, S71).

The shared ``smoke`` collection's chunk count varies with test order, so these tests seed a dedicated
``smoke_topk`` collection of single-chunk documents (more than the default result count of 5, fewer than the
candidate pool of ``max(top_k_retrieve * 3, 20)`` = 45), then ask for an exact count on each interface.
"""

from __future__ import annotations

import gc
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from archon_search.config import SearchConfig
from tests.smoke.conftest import SmokeServer, _seed_corpus

pytestmark = pytest.mark.xdist_group("smoke_e2e")

_CLI_RUN_TIMEOUT_S = 120
_MCP_CALL_TIMEOUT_S = 60.0
_HTTP_TIMEOUT_S = 60.0
_COLLECTION = "smoke_topk"
_QUERY = "retrieval pipeline stage"
_SHIM_MODEL = f"archon-search/{_COLLECTION}"
_DOC_COUNT = 12
#: The count every interface answers when no ``top_k`` is given: ``[database].top_k_return``, unset in the smoke TOML.
_DEFAULT_RESULT_COUNT = SearchConfig().top_k_return
_EXACT_COUNT = 8  # above the default count, below the document count and the candidate pool of 45
_SMALL_COUNT = 3
_SHIM_REQUEST_TOP_K = 1
_SOURCE_PATTERN = re.compile(r"\[Source: ([^\]]+)\]")

_STAGES = (
    "tokenizing the query into normalised terms",
    "embedding the query into a dense vector",
    "scanning the full-text index for lexical matches",
    "searching the vector index for nearest neighbours",
    "fusing both ranked lists with reciprocal rank fusion",
    "filtering candidates by source path and file type",
    "reranking the fused candidates with a cross-encoder",
    "trimming the ranking to the requested result count",
    "attaching the source path and metadata to every hit",
    "recording the stage timings in the telemetry log",
    "returning the ranked chunks to the calling client",
    "caching the query embedding for repeated requests",
)
assert len(_STAGES) == _DOC_COUNT


@pytest.fixture(scope="session")
def topk_collection(smoke_server: SmokeServer, tmp_path_factory: pytest.TempPathFactory) -> str:
    """Seed ``smoke_topk`` (the collection name is the directory's basename) with ``_DOC_COUNT`` one-chunk documents."""
    corpus_dir: Path = tmp_path_factory.mktemp(_COLLECTION, numbered=False)
    for index, stage in enumerate(_STAGES):
        (corpus_dir / f"stage{index:02d}.txt").write_text(f"Retrieval pipeline stage {index}: {stage}.")
    _seed_corpus(smoke_server.base_url, smoke_server.api_key, corpus_dir, smoke_server.proc, collection=_COLLECTION)
    detail = httpx.get(
        f"{smoke_server.base_url}/collections/{_COLLECTION}", headers=_headers(smoke_server), timeout=_HTTP_TIMEOUT_S
    ).json()
    # One chunk per document: the exact-N assertions below rely on a known pool of ``_DOC_COUNT`` chunks.
    assert (detail["doc_count"], detail["chunk_count"]) == (_DOC_COUNT, _DOC_COUNT), detail
    return _COLLECTION


def _headers(smoke_server: SmokeServer) -> dict[str, str]:
    return {"Authorization": f"Bearer {smoke_server.api_key}"}


def _rest_search(smoke_server: SmokeServer, **extra: Any) -> dict[str, Any]:
    body = {"collection": _COLLECTION, "query": _QUERY, **extra}
    response = httpx.post(
        f"{smoke_server.base_url}/search", json=body, headers=_headers(smoke_server), timeout=_HTTP_TIMEOUT_S
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cli_search_json(smoke_server: SmokeServer, *extra_args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "uv", "run", "archon-search", "search", _QUERY, "--collection", _COLLECTION, "--json", *extra_args,
            "--api-url", smoke_server.base_url, "--api-key", smoke_server.api_key,
        ],
        capture_output=True,
        text=True,
        timeout=_CLI_RUN_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    return json.loads(completed.stdout)


def _chunk_ids(payload: dict[str, Any]) -> list[str]:
    return [hit["chunk_id"] for hit in payload["results"]]


def test_top_k_returns_exactly_n_on_rest_and_the_cli(smoke_server, topk_collection) -> None:
    """S68: ``top_k=N`` yields exactly N results on REST and on ``--top-k N``; without it, the default count."""
    _rest_search(smoke_server)  # warm-up: a cold embedder would degrade the answer to FTS-only

    assert len(_rest_search(smoke_server)["results"]) == _DEFAULT_RESULT_COUNT
    assert len(_rest_search(smoke_server, top_k=_EXACT_COUNT)["results"]) == _EXACT_COUNT
    assert len(_rest_search(smoke_server, top_k=1)["results"]) == 1
    assert len(_cli_search_json(smoke_server, "--top-k", str(_EXACT_COUNT))["results"]) == _EXACT_COUNT


def test_top_k_results_are_the_first_n_of_the_default_query(smoke_server, topk_collection) -> None:
    """S68: the ``top_k`` answer is the same ranking, cut at N: the default answer's ``chunk_id``s are its prefix."""
    _rest_search(smoke_server)  # warm-up
    default_ids = _chunk_ids(_rest_search(smoke_server))
    # REST must answer stably, or the prefix comparison below proves nothing (a cold-model degrade would reorder).
    assert _chunk_ids(_rest_search(smoke_server)) == default_ids
    wider_ids = _chunk_ids(_rest_search(smoke_server, top_k=_EXACT_COUNT))
    cli_ids = _chunk_ids(_cli_search_json(smoke_server, "--top-k", str(_EXACT_COUNT)))

    assert len(default_ids) == _DEFAULT_RESULT_COUNT
    assert len(set(wider_ids)) == _EXACT_COUNT
    assert wider_ids[:_DEFAULT_RESULT_COUNT] == default_ids
    assert cli_ids == wider_ids
    assert _chunk_ids(_rest_search(smoke_server, top_k=_SMALL_COUNT)) == default_ids[:_SMALL_COUNT]


def test_openai_client_search_ignores_request_top_k(smoke_server, topk_collection) -> None:
    """S71: a real ``openai`` client's ``extra_body={"top_k": 1}`` does not change the shim's count (the default)."""
    _rest_search(smoke_server)  # warm-up
    sent_bodies: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> None:
        sent_bodies.append(json.loads(request.content))

    with httpx.Client(
        headers=_headers(smoke_server), timeout=_HTTP_TIMEOUT_S, event_hooks={"request": [_capture]}
    ) as http_client:
        client = openai.OpenAI(
            base_url=f"{smoke_server.base_url}/v1", api_key=smoke_server.api_key, http_client=http_client, max_retries=0
        )
        completion = client.chat.completions.create(
            model=_SHIM_MODEL,
            messages=[{"role": "user", "content": _QUERY}],
            extra_body={"top_k": _SHIM_REQUEST_TOP_K},
        )

    # The request really carried ``top_k``: the unchanged count below is the shim ignoring it, not the SDK dropping it.
    assert [body["top_k"] for body in sent_bodies] == [_SHIM_REQUEST_TOP_K]
    content = completion.choices[0].message.content
    assert content is not None
    assert len(_SOURCE_PATTERN.findall(content)) == _DEFAULT_RESULT_COUNT


async def test_mcp_call_uses_top_k_and_acl_context_on_a_real_server(smoke_server, topk_collection) -> None:
    """S71: a real ``fastmcp`` client's ``top_k`` returns exactly that many results, each with its ``acl_gate``."""
    _rest_search(smoke_server)  # warm-up
    transport = StreamableHttpTransport(f"{smoke_server.base_url}/mcp/", headers=_headers(smoke_server))
    async with Client(transport, timeout=_MCP_CALL_TIMEOUT_S, init_timeout=_MCP_CALL_TIMEOUT_S) as mcp:
        with_top_k = await mcp.call_tool(
            "search", {"query": _QUERY, "collection": _COLLECTION, "top_k": _SMALL_COUNT, "acl_context": True}
        )
        without = await mcp.call_tool("search", {"query": _QUERY, "collection": _COLLECTION})
    gc.collect()

    payload = with_top_k.structured_content
    assert payload is not None and "code" not in payload, with_top_k
    assert len(payload["results"]) == _SMALL_COUNT
    # No ACL is declared on the seeded files: the gate is present, open to all, with no warnings.
    assert all(
        hit["acl_gate"]["allowed_principals"] is None and hit["acl_gate"]["warnings"] == []
        for hit in payload["results"]
    ), payload["results"]
    default_payload = without.structured_content
    assert default_payload is not None and "code" not in default_payload, without
    assert len(default_payload["results"]) == _DEFAULT_RESULT_COUNT
    assert all(hit["acl_gate"] is None for hit in default_payload["results"])
