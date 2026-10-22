"""S391: the per-result ``reranker_score`` and the absent ``rerank`` input must be documented.

Ticket S391 (``Documentation/Backlog/S391-no_doc_pairs_reranker_score_with_mcp.md``).

Wire truth (derived from code, never hardcoded):

* ``McpSearchResultSchema`` (``server/mcp_schemas.py``) and the REST ``SearchResultSchema``
  (``server/routes_search.py``) both carry ``reranker_score: float | None`` — added by S343. The
  API reference documented the REST field; the MCP developer guide and the user manual did not.
  The REST schema is the MCP one plus ``acl_gate``; ``search_with_context`` items wrap an
  ``McpSearchResultSchema``.
* The registered ``search`` tool runs the bounded ``warmup_for_search``; the registered
  ``search_with_context`` tool does not, so it waits for a cold cross-encoder.
* The input schemas the real ``FastMCP`` app advertises (``tools/list``) carry no ``rerank``
  property on ``search`` / ``search_with_context``, only on ``explain``; REST ``SearchRequest`` has
  no ``rerank`` field either.
* The ``search`` tool's cold-reranker wait is ``RERANKER_WARMUP_WAIT_SECONDS``
  (``server/_search_budget.py``).

Doc contract:

* ``DeveloperGuide/05_mcp_integration.md`` enumerates exactly the ``McpSearchResultSchema`` fields
  and states REST carries the same fields plus ``acl_gate``. The pre-S391 wordings — REST "drops
  the ``acl`` and ``collection`` fields", REST's ``SearchResultSchema.from_result`` "omits" ``acl``
  — must not return. Tool counts are guarded by ``test_mcp_tool_count_docs.py``.
* ``UserManual/60_searching.md`` "## MCP tools" documents ``reranker_score`` with each of its
  ``null`` cases (no reranker ran; the ``search`` tool's still-cold cross-encoder, stated with the
  code's wait; an empty ``[database].reranker_model`` on both tools), states ``search_with_context`` waits for a
  cold cross-encoder instead, and states ``search`` takes no ``rerank`` input, pointing to the
  ``explain`` tool. Scoped to that section so the reranker exceptions in the doc's Principles
  cannot satisfy it.
* ``UserManual/60_searching.md`` REST ``POST /search`` "Each result includes" field list names
  exactly the ``SearchResultSchema`` fields.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastmcp.tools import FunctionTool, Tool

from archon_search.server._search_budget import RERANKER_WARMUP_WAIT_SECONDS, warmup_for_search
from archon_search.server.mcp_schemas import McpSearchResultSchema, SearchWithContextItemSchema
from archon_search.server.routes_search import SearchRequest, SearchResultSchema
from tests._mcp_tools import input_properties, registered_tools

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEV_GUIDE = _REPO_ROOT / "Documentation" / "DeveloperGuide" / "05_mcp_integration.md"
_SEARCHING = _REPO_ROOT / "Documentation" / "UserManual" / "60_searching.md"

_RERANKER_SCORE = "reranker_score"
_RERANK_PARAM = "rerank"
_REST_ONLY_RESULT_FIELD = "acl_gate"
_SWC_RESULT_FIELD = "result"
_SEARCH_TOOL = "search"
_SWC_TOOL = "search_with_context"
_EXPLAIN_TOOL = "explain"
_RERANKER_MODEL_KEY = "`[database].reranker_model`"
_MCP_SECTION = "MCP tools"
_REST_SECTION = "REST `POST /search`"
_SEARCH_ROW = "| `search` |"
_REST_SCHEMA_CODE = "`SearchResultSchema`"
_EXPLAIN_NO_RERANK = "rerank=false"
_SAME_FIELDS = "same fields"
_WARMUP_HELPER = warmup_for_search.__name__
# The pre-S391 wordings of REST/MCP per-result parity — both wrong, REST carries `acl` and `collection`.
_BASELINE_REST_DROPS = "drops the `acl` and `collection` fields"
_BASELINE_REST_OMITS_ACL = "`SearchResultSchema.from_result` (`routes_search.py:47`) omits it"
_MCP_FIELDS_LINE = re.compile(r"Each `McpSearchResultSchema` dict has the fields `\{([^}]*)\}`")
# The REST field list runs from its anchor to the em-dash gloss or the sentence end.
_REST_FIELDS_LIST = re.compile(r"Each result includes (.*?)(?: — |\.\s|\.$)")
_CODE_SPAN = re.compile(r"`([^`]+)`")
# Periods end sentences, except inside the "e.g." / "i.e." abbreviations.
_SENTENCE_SPLIT = re.compile(r"(?<!e\.g)(?<!i\.e)\.\s")
_NULL = re.compile(r"\bnull\b")
_NO_RERANKER = re.compile(r"\bno reranker (?:ran|runs)\b")
_CROSS_ENCODER = re.compile(r"\bcross-encoder\b")
# "still cold" — a bare ``cold|warm`` also matched "warms up" and the "cold-start" link text.
_STILL_COLD = re.compile(r"\bstill cold\b")
_EMPTY = re.compile(r"\bempty\b")
# Positive wording only: "never waits" / "does not wait" must not satisfy it.
_WAITS_FOR_COLD = re.compile(r"(?<!never )\bwaits for a cold cross-encoder\b")
_NO_RERANK_INPUT = re.compile(r"\b(?:takes|accepts) no `rerank` input\b")
_BOTH_TOOLS = re.compile(r"\bboth tools\b", re.IGNORECASE)


def _code(name: str) -> str:
    return f"`{name}`"


def _section(doc: str, heading: str) -> str:
    """Return the body of the level-2 ``heading`` section, up to the next level-2 heading."""
    parts = re.split(rf"^## {re.escape(heading)}$", doc, maxsplit=1, flags=re.MULTILINE)
    assert len(parts) == 2, f"presence anchor: '## {heading}' section missing"
    return re.split(r"^## ", parts[1], maxsplit=1, flags=re.MULTILINE)[0]


def _sentences(text: str) -> list[str]:
    return [s for line in text.splitlines() for s in _SENTENCE_SPLIT.split(line)]


def _tool_source(tool: Tool) -> str:
    assert isinstance(tool, FunctionTool), f"wire-truth drift: {tool.name} is no longer a function tool"
    return inspect.getsource(tool.fn)


@pytest.fixture(scope="module")
def tools() -> dict[str, Tool]:
    return registered_tools(key_store=None)


@pytest.fixture(scope="module")
def base_tools(tools: dict[str, Tool]) -> dict[str, set[str]]:
    return {name: input_properties(tool) for name, tool in tools.items()}


def test_dev_guide_enumerates_exactly_the_mcp_search_result_fields() -> None:
    schema_fields = set(McpSearchResultSchema.model_fields)
    assert _RERANKER_SCORE in schema_fields, "wire-truth drift: McpSearchResultSchema lost reranker_score"

    match = _MCP_FIELDS_LINE.search(_DEV_GUIDE.read_text(encoding="utf-8"))
    assert match, "presence anchor: 05_mcp_integration.md McpSearchResultSchema field list missing"
    documented = {field.strip() for field in match.group(1).split(",")}

    assert documented == schema_fields, (
        f"05_mcp_integration.md McpSearchResultSchema field list drifted: "
        f"omits {sorted(schema_fields - documented)}, invents {sorted(documented - schema_fields)}"
    )


def test_dev_guide_rest_parity_claim_matches_the_schemas() -> None:
    mcp_fields = set(McpSearchResultSchema.model_fields)
    rest_fields = set(SearchResultSchema.model_fields)
    assert mcp_fields <= rest_fields, f"wire-truth drift: MCP-only result fields {sorted(mcp_fields - rest_fields)}"
    assert rest_fields - mcp_fields == {_REST_ONLY_RESULT_FIELD}, (
        f"wire-truth drift: REST-only result fields are now {sorted(rest_fields - mcp_fields)}"
    )

    doc = _DEV_GUIDE.read_text(encoding="utf-8")
    assert any(
        _REST_SCHEMA_CODE in s and _code(_REST_ONLY_RESULT_FIELD) in s and _SAME_FIELDS in s
        for s in _sentences(doc)
    ), f"05_mcp_integration.md must say REST SearchResultSchema has the same fields plus {_REST_ONLY_RESULT_FIELD}"
    assert _BASELINE_REST_DROPS not in doc, "05_mcp_integration.md again says REST drops `acl` and `collection`"
    assert _BASELINE_REST_OMITS_ACL not in doc, "05_mcp_integration.md again says REST from_result omits `acl`"


def test_searching_mcp_section_documents_reranker_score_null_semantics(
    tools: dict[str, Tool], base_tools: dict[str, set[str]],
) -> None:
    assert _RERANKER_SCORE in McpSearchResultSchema.model_fields, "wire-truth drift: reranker_score gone"
    swc_result = SearchWithContextItemSchema.model_fields[_SWC_RESULT_FIELD].annotation
    assert swc_result is McpSearchResultSchema, "wire-truth drift: search_with_context item result schema changed"
    assert _RERANK_PARAM not in base_tools[_SWC_TOOL], "wire-truth drift: search_with_context takes rerank"
    assert _WARMUP_HELPER in _tool_source(tools[_SEARCH_TOOL]), "wire-truth drift: search lost its bounded warm-up"
    assert _WARMUP_HELPER not in _tool_source(tools[_SWC_TOOL]), (
        "wire-truth drift: search_with_context now bounds its warm-up, so it no longer waits for a cold cross-encoder"
    )

    sentences = _sentences(_section(_SEARCHING.read_text(encoding="utf-8"), _MCP_SECTION))
    score_sentences = [s for s in sentences if _RERANKER_SCORE in s]
    assert score_sentences, "60_searching.md MCP tools section never names the per-result reranker_score"
    assert any(_code(_SWC_TOOL) in s for s in score_sentences), (
        "60_searching.md MCP tools section must say search_with_context results carry reranker_score"
    )
    assert any(_NULL.search(s) and _NO_RERANKER.search(s) for s in score_sentences), (
        "60_searching.md MCP tools section must say reranker_score is null when no reranker ran"
    )

    wait = f"{RERANKER_WARMUP_WAIT_SECONDS:g} s"
    cold = [s for s in sentences if _STILL_COLD.search(s) and _CROSS_ENCODER.search(s)]
    assert cold, "60_searching.md MCP tools section must cover the still-cold cross-encoder case"
    assert any(
        wait in s and _NO_RERANKER.search(s) and _code(_SEARCH_TOOL) in s
        and _code(_SWC_TOOL) not in s and not _BOTH_TOOLS.search(s)
        for s in cold
    ), f"the still-cold case must say no reranker runs on the `search` tool alone after the {wait} wait"

    empty_model = [s for s in sentences if _RERANKER_MODEL_KEY in s and _EMPTY.search(s)]
    assert empty_model, f"60_searching.md MCP tools section must cover an empty {_RERANKER_MODEL_KEY}"
    assert any(_NO_RERANKER.search(s) and _BOTH_TOOLS.search(s) for s in empty_model), (
        f"the empty {_RERANKER_MODEL_KEY} case must say no reranker runs, on both tools"
    )

    assert any(_code(_SWC_TOOL) in s and _WAITS_FOR_COLD.search(s) for s in sentences), (
        "60_searching.md MCP tools section must say search_with_context waits for a cold cross-encoder"
    )


def test_searching_mcp_section_states_search_takes_no_rerank_input(
    base_tools: dict[str, set[str]],
) -> None:
    assert _RERANK_PARAM not in base_tools[_SEARCH_TOOL], "wire-truth drift: MCP search now takes rerank"
    assert _RERANK_PARAM in base_tools[_EXPLAIN_TOOL], "wire-truth drift: MCP explain lost rerank"

    section = _section(_SEARCHING.read_text(encoding="utf-8"), _MCP_SECTION)
    search_rows = [line for line in section.splitlines() if line.startswith(_SEARCH_ROW)]
    assert search_rows, "presence anchor: MCP tools table has no `search` row"
    assert not any(_RERANK_PARAM in row for row in search_rows), "MCP tools `search` row lists a rerank input"

    sentences = _sentences(section)
    assert any(_code(_SEARCH_TOOL) in s and _NO_RERANK_INPUT.search(s) for s in sentences), (
        "60_searching.md MCP tools section must state the `search` tool takes no `rerank` input"
    )
    assert any(_code(_EXPLAIN_TOOL) in s and _EXPLAIN_NO_RERANK in s for s in sentences), (
        "60_searching.md MCP tools section must point to the `explain` tool with rerank=false"
    )


def test_searching_rest_result_paragraph_names_exactly_the_result_fields() -> None:
    assert _RERANKER_SCORE in SearchResultSchema.model_fields, "wire-truth drift: REST reranker_score gone"
    assert _RERANK_PARAM not in SearchRequest.model_fields, "wire-truth drift: REST SearchRequest takes rerank"

    section = _section(_SEARCHING.read_text(encoding="utf-8"), _REST_SECTION)
    lists = _REST_FIELDS_LIST.findall(section)
    assert len(lists) == 1, "presence anchor: expected one 'Each result includes' field list"
    documented = set(_CODE_SPAN.findall(lists[0]))
    schema_fields = set(SearchResultSchema.model_fields)

    assert documented == schema_fields, (
        f"60_searching.md REST per-result field list drifted: "
        f"omits {sorted(schema_fields - documented)}, invents {sorted(documented - schema_fields)}"
    )
