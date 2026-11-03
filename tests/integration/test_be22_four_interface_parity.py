"""BE-22 (interface-parity 01-SEARCH, S1): REST, MCP, the CLI ``--json`` and the OpenAI shim agree on one real app.

One ``make_real_app`` with MCP and the shim enabled, a real store holding one seeded collection, and the same query and
collection sent through each interface.  The CLI runs through the in-process bridge, so it reaches the same app as REST
(it proxies ``POST /search``); MCP is called over JSON-RPC and the shim over ``/v1/chat/completions``.  The store and the
pipeline are real; the model backends are the suite's autouse stand-ins (``tests/conftest.py``: zero vectors, a uniform
rerank score), so the ranking comes from full-text search, and RAG Fusion's LLM is a loopback fake model server.

Run with:
    uv run pytest tests/integration/test_be22_four_interface_parity.py --no-cov
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.search import search as cli_search
from archon_search.constants import DEFAULT_NAMESPACE
from tests._cli_testclient_bridge import CliBridge, cli_against_app
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import (
    ingest_doc,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_COLLECTION = "be22-parity"
_MISSING_COLLECTION = "be22-missing"
_QUERY = "widget"
_FILE_TYPE = "md"
_SHIM_MODEL = "archon-search"
# (source path, text): the first is visible through its ACL, the second carries none, the third is hidden from the
# caller's namespace by its frontmatter ACL, the fourth is not a Markdown file.
_DOCUMENTS = (
    (
        "/corpus/be22/alpha-widgets.md",
        f"---\n_acl:\n  - {DEFAULT_NAMESPACE}\n---\n# Widgets\n\nThe widget factory builds a widget every minute.\n"
        + "Each widget is tested twice, and every widget leaves the factory labelled.\n",
    ),
    (
        "/corpus/be22/beta-widgets.md",
        "# Gadgets\n\nA gadget differs from a widget in size; the plant makes both on one line.\n",
    ),
    (
        "/corpus/be22/gamma-widgets.md",
        "---\n_acl:\n  - other-team\n---\n# Sprockets\n\nSprockets turn, and a widget rarely does.\n",
    ),
    ("/corpus/be22/delta-widgets.txt", "Plain text about a widget; the file_type filter drops it.\n"),
)
_VISIBLE_PATHS = {_DOCUMENTS[0][0], _DOCUMENTS[1][0]}
_UNFILTERED_PATHS = _VISIBLE_PATHS | {_DOCUMENTS[3][0]}
_SOURCE_PATTERN = re.compile(r"\[Source: ([^\]]+)\]")
_BLANK_QUERY = "   "
# The shim answers a blank user message itself, before the service, with its own OpenAI 400.
_SHIM_NO_USER_MESSAGE_CODE = "no_user_message"
# The wire text and code of an unknown collection, pinned here (not imported) so a change of the source text fails a test.
_NOT_FOUND_DETAIL = "collection not found"
_NOT_FOUND_CODE = "not_found"
_VALIDATION_CODE = "validation_error"
# What the fake model server answers a RAG Fusion call with: one rewritten query per line.
_RAG_FUSION_REPLY = "widget factory output per minute\nhow often is each widget tested\nwidget labelling"

_REST_BODY: dict[str, Any] = {
    "collection": _COLLECTION,
    "query": _QUERY,
    "filters": {"file_type": _FILE_TYPE},
    "rag_fusion": True,
    "acl_context": True,
}
_MCP_ARGUMENTS: dict[str, Any] = {
    "query": _QUERY,
    "collection": _COLLECTION,
    "file_type": _FILE_TYPE,
    "rag_fusion": True,
    "acl_context": True,
}
_CLI_ARGUMENTS = (_QUERY, "--collection", _COLLECTION, "--file-type", _FILE_TYPE, "--rag-fusion", "--acl-context")


def _rag_fusion_toml(base_url: str) -> str:
    return f'[rag_fusion]\nenabled = true\nprovider = "ollama"\nmodel = "rag-model"\nollama_base_url = "{base_url}"\nollama_context_tokens = 8192\n'


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _seed(client: TestClient, api_key: str) -> None:
    for path, text in _DOCUMENTS:
        ingest_doc(client, _COLLECTION, text, path, api_key=api_key)


def _rest(client: TestClient, api_key: str, body: dict[str, Any]):
    return client.post("/search", json=body, headers=_headers(api_key))


def _cli_json(cli: CliBridge, *args: str) -> tuple[int, dict[str, Any] | None, str]:
    """Run ``search ... --json``; return the exit code, the parsed stdout (``None`` after a failure) and stderr."""
    run = cli.invoke(cli_search, [*args, "--json"])
    payload = json.loads(run.stdout) if run.exit_code == 0 else None
    return run.exit_code, payload, run.stderr


def _chat(client: TestClient, api_key: str, model: str, content: str):
    body = {"model": model, "messages": [{"role": "user", "content": content}]}
    return client.post("/v1/chat/completions", json=body, headers=_headers(api_key))


def _shim_sources(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return _SOURCE_PATTERN.findall(response.json()["choices"][0]["message"]["content"])


def test_rest_mcp_and_cli_return_the_same_ordered_results_and_derived_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: ordered results, ``embedding_model``, ``rag_fusion_*``, ``applied_filters`` and the per-result ``acl_gate`` agree.

    Every derived field is made non-default so a dropped mapping cannot hide behind a schema default: RAG Fusion runs
    against a loopback fake model server, a ``file_type`` filter is applied, ``acl_context`` asks for the gates and one
    seeded document is hidden from the caller's namespace (``acl_filtered``).  The CLI proxies ``POST /search``, so its
    row proves the flag-to-request mapping and the pass-through, not a second service run: the dropped-flag control at
    the end shows it really sends what it is given.
    """
    keep_loopback_out_of_proxies(monkeypatch)
    with FakeModelServer(reply_text=_RAG_FUSION_REPLY) as model_server, make_real_app(
        tmp_path,
        monkeypatch,
        mcp_enabled=True,
        openai_shim_enabled=True,
        toml_content=_rag_fusion_toml(model_server.base_url),
    ) as (client, cfg, api_key):
        _seed(client, api_key)
        # Warm the collection's embedder so no cold-embedder degrade skews one interface's ranking.
        assert _rest(client, api_key, {"collection": _COLLECTION, "query": _QUERY}).status_code == 200

        rest = _rest(client, api_key, _REST_BODY)
        assert rest.status_code == 200, rest.text
        expected = rest.json()
        repeated = _rest(client, api_key, _REST_BODY)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == expected, "REST must answer stably, or the cross-interface equality proves nothing"
        session_id = mcp_initialize(client, api_key)
        mcp = mcp_tool_call(client, api_key, session_id, "search", _MCP_ARGUMENTS)
        with cli_against_app(client, api_key, monkeypatch) as cli:
            cli_exit, cli_payload, cli_stderr = _cli_json(cli, *_CLI_ARGUMENTS)
            _, without_acl_context, _ = _cli_json(cli, *(arg for arg in _CLI_ARGUMENTS if arg != "--acl-context"))

    # The scenario is not degenerate: non-default derived values, a real ranking, a gate on every visible hit.
    assert {hit["source_path"] for hit in expected["results"]} == _VISIBLE_PATHS
    assert expected["rag_fusion_applied"] is True and expected["rag_fusion_attempted"] is True
    assert expected["rag_fusion_queries_used"] > 1
    assert expected["acl_filtered"] is True
    assert expected["applied_filters"]["file_type"] == _FILE_TYPE
    assert expected["embedding_model"] == cfg.embedding_model
    assert [hit["acl_gate"]["allowed_principals"] for hit in expected["results"]] == [[DEFAULT_NAMESPACE], None]
    assert cli_exit == 0, cli_stderr
    # The whole body, not a field list: the three interfaces carry one field set with one set of values, in one order.
    assert mcp == expected
    assert cli_payload == expected
    assert without_acl_context is not None
    assert all(hit["acl_gate"] is None for hit in without_acl_context["results"])


def test_error_codes_agree_across_rest_mcp_cli_and_shim_for_not_found_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: an unknown collection and a blank query are the same error kind everywhere, each in its adapter's wire form.

    The shim agrees for ``not_found`` through the service.  For a blank query it answers with its own OpenAI 400 before
    the service is reached (an adapter policy, ``routes_openai_shim.py``), so that row pins the shim's wire form only.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, openai_shim_enabled=True) as (client, _cfg, api_key):
        _seed(client, api_key)
        session_id = mcp_initialize(client, api_key)

        rest_not_found = _rest(client, api_key, {"collection": _MISSING_COLLECTION, "query": _QUERY})
        mcp_not_found = mcp_tool_call(
            client, api_key, session_id, "search", {"query": _QUERY, "collection": _MISSING_COLLECTION}
        )
        shim_not_found = _chat(client, api_key, f"{_SHIM_MODEL}/{_MISSING_COLLECTION}", _QUERY)

        rest_blank = _rest(client, api_key, {"collection": _COLLECTION, "query": _BLANK_QUERY})
        mcp_blank = mcp_tool_call(client, api_key, session_id, "search", {"query": _BLANK_QUERY, "collection": _COLLECTION})
        shim_blank = _chat(client, api_key, f"{_SHIM_MODEL}/{_COLLECTION}", _BLANK_QUERY)

        with cli_against_app(client, api_key, monkeypatch) as cli:
            cli_not_found = cli.invoke(cli_search, [_QUERY, "--collection", _MISSING_COLLECTION, "--json"])
            cli_blank = cli.invoke(cli_search, [_BLANK_QUERY, "--collection", _COLLECTION, "--json"])

    # not_found: one kind, four wire forms.
    assert rest_not_found.status_code == 404
    assert rest_not_found.json() == {"detail": _NOT_FOUND_DETAIL}
    assert mcp_not_found == {"error": _NOT_FOUND_DETAIL, "code": _NOT_FOUND_CODE}
    assert (cli_not_found.exit_code, cli_not_found.stdout) == (1, "")
    assert f"Error: {_NOT_FOUND_DETAIL}\n" in cli_not_found.stderr
    assert shim_not_found.status_code == 404
    assert shim_not_found.json() == {
        "error": {
            "message": f"The model '{_SHIM_MODEL}/{_MISSING_COLLECTION}' does not exist.",
            "type": "invalid_request_error",
            "param": None,
            "code": None,
        }
    }

    # validation: REST's 422 list, MCP's coded dict, the CLI's one error line, the shim's own OpenAI 400.
    assert rest_blank.status_code == 422
    rest_message = rest_blank.json()["detail"][0]["msg"]
    assert mcp_blank["code"] == _VALIDATION_CODE
    assert mcp_blank["error"]
    assert mcp_blank["error"] in rest_message
    assert (cli_blank.exit_code, cli_blank.stdout) == (1, "")
    assert cli_blank.stderr.startswith("Error: ")
    assert mcp_blank["error"] in cli_blank.stderr
    assert shim_blank.status_code == 400
    assert shim_blank.json()["error"]["code"] == _SHIM_NO_USER_MESSAGE_CODE


def test_shim_returns_the_same_ordered_sources_in_its_chat_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: the ``[Source: <path>]`` order of the shim's chat text equals REST's ``source_path`` order."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, openai_shim_enabled=True) as (client, _cfg, api_key):
        _seed(client, api_key)
        warm_up = _rest(client, api_key, {"collection": _COLLECTION, "query": _QUERY})
        rest = _rest(client, api_key, {"collection": _COLLECTION, "query": _QUERY})
        shim = _chat(client, api_key, f"{_SHIM_MODEL}/{_COLLECTION}", _QUERY)

    assert warm_up.status_code == 200 and rest.status_code == 200, (warm_up.text, rest.text)
    expected_sources = [hit["source_path"] for hit in rest.json()["results"]]
    assert expected_sources == [hit["source_path"] for hit in warm_up.json()["results"]], "REST must rank stably"
    assert set(expected_sources) == _UNFILTERED_PATHS
    assert _shim_sources(shim) == expected_sources
