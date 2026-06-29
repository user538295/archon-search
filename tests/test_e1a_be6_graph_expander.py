"""Unit + integration tests for GraphExpander — E1a BE-6.

Tests verify:
- expand() appends neighbour names to the query text
- expand() is a no-op when no query tokens match any node name
- expand() is a no-op when nodes are matched but have no neighbours
- expand() does not duplicate entity names already present in the query
- No raw query string appears in any logging call (privacy invariant)
- Multi-word entity matching (N-grams up to N=3)
- Integration: real GraphStore with seeded nodes + edges
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(name: str, etype: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(etype.value, name),
        entity_name=name,
        entity_type=etype,
        source_doc_id="doc-1",
        collection_name="test-col",
    )


def _edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, RelationshipType.related_to.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-1",
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expander_empty_query_is_noop() -> None:
    """expand() must be a no-op for empty or whitespace-only queries."""
    mock_store = AsyncMock()

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)

    for q in ("", "   "):
        mock_store.reset_mock()
        result = await expander.expand(q, "mycol")
        assert result.expansion_applied is False
        assert result.expanded_text == q
        mock_store.find_nodes_by_name.assert_not_called()


@pytest.mark.asyncio
async def test_expander_appends_neighbour_names() -> None:
    """expand() must append neighbour entity names to the original query."""
    auth_node = _node("AuthService", EntityType.system)
    token_node = _node("TokenValidator", EntityType.system)

    mock_store = AsyncMock()
    # findNodesByName returns the matched node for "authservice"
    mock_store.find_nodes_by_name = AsyncMock(return_value=[auth_node])
    # getNeighbours returns TokenValidator as a neighbour of AuthService
    mock_store.get_neighbours = AsyncMock(return_value=[token_node])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("AuthService usage", "mycol")

    assert result.expansion_applied is True
    assert "TokenValidator" in result.expanded_text
    assert result.original_query == "AuthService usage"
    # The original text should still be there
    assert "AuthService usage" in result.expanded_text
    # Contract fields must be populated
    assert result.entity_names_found == ["AuthService"]
    assert result.neighbour_names_added == ["TokenValidator"]


@pytest.mark.asyncio
async def test_expander_no_entities_is_noop() -> None:
    """expand() must be a no-op when no query tokens match any node name."""
    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("what is the meaning of life", "mycol")

    assert result.expansion_applied is False
    assert result.expanded_text == "what is the meaning of life"
    # get_neighbours should not be called if no nodes matched
    mock_store.get_neighbours.assert_not_called()


@pytest.mark.asyncio
async def test_expander_empty_graph_is_noop() -> None:
    """expand() must be a no-op when nodes are matched but have no neighbours."""
    auth_node = _node("AuthService", EntityType.system)

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[auth_node])
    mock_store.get_neighbours = AsyncMock(return_value=[])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("AuthService", "mycol")

    assert result.expansion_applied is False
    assert result.expanded_text == "AuthService"
    # entity_names_found is populated even when there are no neighbours
    assert result.entity_names_found == ["AuthService"]
    assert result.neighbour_names_added == []


@pytest.mark.asyncio
async def test_expander_does_not_duplicate_entity_names() -> None:
    """expand() must NOT append entity names that are already in the query."""
    auth_node = _node("AuthService", EntityType.system)
    # TokenValidator is also in the query
    token_node = _node("TokenValidator", EntityType.system)

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[auth_node])
    mock_store.get_neighbours = AsyncMock(return_value=[token_node])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    # TokenValidator is already in the original query
    result = await expander.expand("AuthService TokenValidator", "mycol")

    # TokenValidator should NOT be appended again
    assert result.expanded_text.count("TokenValidator") == 1
    # Token-set matching: "tokenvalidator" is in query tokens → suppressed → no expansion
    assert result.expansion_applied is False


@pytest.mark.asyncio
async def test_expander_matches_multi_word_entities() -> None:
    """expand() must match multi-word node names (N-gram expansion up to N=3)."""
    token_validator_node = _node("Token Validator", EntityType.concept)
    user_node = _node("UserStore", EntityType.system)

    mock_store = AsyncMock()
    # 'token validator' (lowercase) should match 'Token Validator' node
    mock_store.find_nodes_by_name = AsyncMock(return_value=[token_validator_node])
    mock_store.get_neighbours = AsyncMock(return_value=[user_node])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("what does Token Validator do", "mycol")

    assert result.expansion_applied is True
    assert "UserStore" in result.expanded_text


@pytest.mark.asyncio
async def test_expander_batches_all_ngrams_in_single_findnodes_call() -> None:
    """ALL N-gram candidates must be batched into ONE findNodesByName call."""
    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    await expander.expand("AuthService checks Token Validator rules", "mycol")

    # find_nodes_by_name called exactly once (batched)
    assert mock_store.find_nodes_by_name.call_count == 1
    # The single call should include N-gram candidates
    call_args = mock_store.find_nodes_by_name.call_args
    names_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("names", [])
    # Should have multiple candidates from N-grams
    assert len(names_arg) > 1


@pytest.mark.asyncio
async def test_expander_ngram_cap_at_3() -> None:
    """expand() must cap N-gram size at 3 (never produce 4-token N-grams)."""
    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    # Query has 5 tokens
    await expander.expand("a b c d e", "mycol")

    call_args = mock_store.find_nodes_by_name.call_args
    names_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("names", [])
    # No 4-or-more token N-gram should appear (max 3 words separated by spaces)
    for name in names_arg:
        assert len(name.split()) <= 3, f"Found N-gram with more than 3 tokens: {name!r}"


@pytest.mark.asyncio
async def test_expander_multiword_neighbour_not_duplicated_when_present_in_query() -> None:
    """Multi-word neighbour names already in query must not be appended again (C2-I-1)."""
    # "Token Validator" is already in the query — must not be appended
    token_validator_node = _node("Token Validator", EntityType.concept)
    user_node = _node("Token Validator", EntityType.concept)  # same name as the matched node

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[token_validator_node])
    # Neighbour is also "Token Validator" (already in query)
    mock_store.get_neighbours = AsyncMock(return_value=[user_node])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    # "Token Validator" is already in the query — suppress it
    result = await expander.expand("Token Validator usage", "mycol")

    assert result.expansion_applied is False
    assert result.expanded_text == "Token Validator usage"
    assert result.neighbour_names_added == []


@pytest.mark.asyncio
async def test_expander_cross_node_neighbour_dedup() -> None:
    """When two matched nodes share the same neighbour, it appears only once in expanded text (C2-T-3)."""
    auth_node = _node("AuthService", EntityType.system)
    user_node = _node("UserStore", EntityType.system)
    # Both nodes share the same neighbour: TokenValidator
    token_node = _node("TokenValidator", EntityType.system)

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[auth_node, user_node])
    # get_neighbours returns the same node twice (one per matched entity)
    mock_store.get_neighbours = AsyncMock(return_value=[token_node, token_node])

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("AuthService UserStore", "mycol")

    # TokenValidator should appear only once, not twice
    assert result.expanded_text.count("TokenValidator") == 1
    assert result.neighbour_names_added == ["TokenValidator"]


@pytest.mark.asyncio
async def test_expander_find_nodes_failure_returns_noop() -> None:
    """expand() must return a no-op ExpandedQuery when find_nodes_by_name raises (C2-T-2)."""
    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(side_effect=RuntimeError("db gone"))

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("AuthService", "mycol")

    assert result.expansion_applied is False
    assert result.expanded_text == "AuthService"
    assert result.entity_names_found == []
    assert result.neighbour_names_added == []


@pytest.mark.asyncio
async def test_expander_get_neighbours_failure_returns_noop() -> None:
    """expand() must return a no-op ExpandedQuery when get_neighbours raises (C2-T-2)."""
    auth_node = _node("AuthService", EntityType.system)

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[auth_node])
    mock_store.get_neighbours = AsyncMock(side_effect=FileNotFoundError("table missing"))

    from archon_search.graph_expander import GraphExpander

    expander = GraphExpander(graph_store=mock_store)
    result = await expander.expand("AuthService", "mycol")

    assert result.expansion_applied is False
    assert result.expanded_text == "AuthService"


# ---------------------------------------------------------------------------
# Privacy guard — no raw query logged
# ---------------------------------------------------------------------------

# Import the same regex helpers as test_no_query_log_in_hyde.py
_LOG_PREFIX = r"(?:logging\.|_logger\.|(?<![_\w])logger\.)"
_FSTRING_QUERY_IN_LOG = re.compile(
    _LOG_PREFIX + r"""\w+\s*\([^)]*f['"][^'"]*\{query(?![_\w(])""",
    re.DOTALL,
)
_LOG_OPENER = re.compile(_LOG_PREFIX + r"\w+\s*\(", re.DOTALL)
_FINGERPRINT_CALL = re.compile(r"_query_fingerprint\s*\([^)]*\)")
_STRING_LITERAL = re.compile(
    r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
    re.DOTALL,
)
_BARE_QUERY_TOKEN = re.compile(r"(?<!\w)query(?!\w)")


def _extract_call_args(source: str, open_paren_pos: int) -> str:
    depth = 1
    i = open_paren_pos + 1
    while i < len(source) and depth > 0:
        c = source[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return source[open_paren_pos + 1 : i - 1] if depth == 0 else ""


def _bare_query_in_log_violations(source: str) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    for m in _LOG_OPENER.finditer(source):
        open_pos = m.end() - 1
        args_text = _extract_call_args(source, open_pos)
        sanitised = _STRING_LITERAL.sub("__STR__", args_text)
        sanitised = _FINGERPRINT_CALL.sub("__FP__", sanitised)
        if _BARE_QUERY_TOKEN.search(sanitised):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append((lineno, snippet))
    return violations


def test_no_query_log_in_graph_expander() -> None:
    """graph_expander.py must not pass the raw query variable to any logging call."""
    expander_path = Path(__file__).parent.parent / "archon_search" / "graph_expander.py"
    source = expander_path.read_text(encoding="utf-8")

    violations: list[str] = []

    for m in _FSTRING_QUERY_IN_LOG.finditer(source):
        lineno = source.count("\n", 0, m.start()) + 1
        snippet = source.splitlines()[lineno - 1].strip()
        violations.append(f"  line {lineno} (f-string): {snippet}")

    for lineno, snippet in _bare_query_in_log_violations(source):
        violations.append(f"  line {lineno} (bare arg): {snippet}")

    assert not violations, (
        "Raw query string passed to logging in archon_search/graph_expander.py:\n"
        + "\n".join(violations)
        + "\nUse _query_fingerprint(query) in all logging calls."
    )


# ---------------------------------------------------------------------------
# Integration test — real GraphStore with seeded graph data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_expander_with_real_graph_store(tmp_path: Path) -> None:
    """Integration: pre-seed graph tables, expand a query, assert neighbour appears."""
    from archon_search.graph_expander import GraphExpander
    from archon_search.graph_store import GraphStore

    db_path = tmp_path / "graph_expander_test"
    store = GraphStore(db_path)
    await store.connect()

    collection = "integ-col"
    await store.ensure_graph_tables(collection)

    auth_node = GraphNode(
        id=make_stable_entity_id("system", "AuthService"),
        entity_name="AuthService",
        entity_type=EntityType.system,
        source_doc_id="doc-1",
        collection_name=collection,
    )
    token_node = GraphNode(
        id=make_stable_entity_id("system", "TokenValidator"),
        entity_name="TokenValidator",
        entity_type=EntityType.system,
        source_doc_id="doc-1",
        collection_name=collection,
    )
    edge = GraphEdge(
        id=make_stable_edge_id(auth_node.id, token_node.id, "related_to"),
        source_node_id=auth_node.id,
        target_node_id=token_node.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-1",
    )
    await store.write_graph(collection, [auth_node, token_node], [edge])

    expander = GraphExpander(graph_store=store)
    result = await expander.expand("AuthService", collection)

    assert result.expansion_applied is True
    assert "TokenValidator" in result.expanded_text

    await store.disconnect()
