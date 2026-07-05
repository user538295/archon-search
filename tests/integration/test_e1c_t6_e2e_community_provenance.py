"""E1c / T-6 — e2e tests: community mode traversal provenance (S3, S4).

Covers:
- (a) ``test_explain_local_community_provenance_e2e`` — real corpus, real
      CommunityBuilder, POST /explain graph_mode="local" → assert
      graph_mode_applied="local" and ≥1 result with non-null graph_provenance
      containing a TraversalStep with community_id set  (S3)
- (b) ``test_explain_global_community_provenance_e2e`` — same corpus +
      communities, POST /explain graph_mode="global" → assert
      graph_mode_applied="global" and ≥1 result with non-null graph_provenance
      containing a TraversalStep with community_id set  (S4)
- (c) ``test_mcp_explain_local_community_provenance_e2e`` — same corpus +
      communities via MCP explain tool with graph_mode="local" → assert
      graph_mode_applied="local" and ≥1 non-null graph_provenance with
      community_id  (S3 / MCP parity)

Corpus design (no leidenalg required):
  Both docs contain only "PaymentService" as a named entity.  The stub NLP
  extracts exactly ONE unique entity → graph has 1 node, 0 edges.
  CommunityBuilder.build() takes the early-exit path when ``len(nodes) < 2``,
  returning a single community without invoking Leiden.  This avoids the
  leidenalg optional dependency while exercising the full
  ingest → graph extraction → CommunityBuilder → pipeline.explain() → HTTP
  stack.

Community building strategy:
  After ingest (job DONE), we call CommunityBuilder.build() from the main
  test thread via ``asyncio.run()``, using fresh GraphStore and SearchStore
  connections pointing to the same ``tmp_path/db`` path.  Starlette's
  TestClient runs the ASGI app in a background thread; the main test thread
  has no running event loop, so ``asyncio.run()`` is safe here.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub — extracts only "PaymentService" from any text that contains it
# ---------------------------------------------------------------------------


def _install_payment_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal spaCy stub that extracts 'PaymentService' when present.

    Extracts exactly one entity per doc so the graph has 1 unique node.
    A single-node graph skips Leiden inside CommunityBuilder.build(), making
    the test leidenalg-free.

    Must be called BEFORE make_real_app because create_app calls
    ``_check_graph_deps`` which imports spacy synchronously.
    """

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list[_FakeEnt]) -> None:
            self.ents = ents

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents: list[_FakeEnt] = []
            if "PaymentService" in text:
                ents.append(_FakeEnt("PaymentService", "ORG"))
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------


def _build_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Create two docs containing only "PaymentService" as a named entity.

    Stub NLP extracts [PaymentService(ORG)] from each doc → 1 unique graph
    node, 0 edges.  CommunityBuilder.build() takes the early-exit path for
    single-node graphs (no leidenalg needed).
    """
    doc1 = tmp_path / "payment_service_main.txt"
    doc1.write_text(
        "PaymentService processes all payment transactions securely. "
        "PaymentService is the core billing gateway for the platform. "
        "PaymentService handles credit card authorisation and settlement.\n",
        encoding="utf-8",
    )
    doc2 = tmp_path / "payment_service_refunds.txt"
    doc2.write_text(
        "PaymentService manages refunds and chargebacks efficiently. "
        "PaymentService integrates with multiple payment providers.\n",
        encoding="utf-8",
    )
    return doc1, doc2


# ---------------------------------------------------------------------------
# Community building helper
# ---------------------------------------------------------------------------


async def _build_communities_async(db_path: str, col: str, cfg) -> list:
    """Build communities for *col* using fresh store connections.

    Creates new GraphStore and SearchStore instances pointing at *db_path* so
    that community building runs in the main test thread's event loop without
    sharing asyncio state with the TestClient's ASGI thread.

    With a single-entity corpus the early-exit path fires (len(nodes) < 2),
    returning one community without running Leiden.

    Returns the list of built Community objects.
    """
    from archon_search.community_builder import CommunityBuilder
    from archon_search.graph_store import GraphStore
    from archon_search.store import SearchStore

    graph_store = GraphStore(db_path)
    search_store = SearchStore(db_path)
    await graph_store.connect()
    await search_store.connect()
    try:
        builder = CommunityBuilder(graph_store, cfg.graph, search_store=search_store)
        return await builder.build(col, ns="default")
    finally:
        await graph_store.disconnect()
        await search_store.disconnect()


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _assert_community_traversal_step(prov: dict, *, expected_entity: str | None = None) -> None:
    """Assert graph_provenance dict contains a TraversalStep with community_id set.

    Checks:
    - prov is a dict with a non-empty 'steps' list
    - first step has 'community_id' set (not None, not empty string)
    - 'chunk_id' is set (community mode always sets chunk_id)
    - 'relationship' is None (community mode, not naive)
    - 'entity' and 'entity_id' are non-empty strings
    - if expected_entity is given, 'entity' matches it exactly
    """
    assert isinstance(prov, dict), (
        f"Expected graph_provenance to be a dict; got {type(prov).__name__!r}: {prov!r}"
    )
    assert "steps" in prov, (
        f"Expected 'steps' key in graph_provenance; got keys: {list(prov.keys())}"
    )
    steps = prov["steps"]
    assert isinstance(steps, list) and steps, (
        f"Expected non-empty steps list; got: {steps!r}"
    )
    step = steps[0]
    assert step.get("entity"), (
        f"TraversalStep.entity must be non-empty; step={step!r}"
    )
    if expected_entity is not None:
        assert step["entity"] == expected_entity, (
            f"TraversalStep.entity={step['entity']!r} does not match expected {expected_entity!r}; "
            f"step={step!r}"
        )
    assert step.get("entity_id"), (
        f"TraversalStep.entity_id must be non-empty; step={step!r}"
    )
    assert step.get("community_id"), (
        f"TraversalStep.community_id must be set in community mode; step={step!r}"
    )
    assert step.get("chunk_id"), (
        f"TraversalStep.chunk_id must be set in community mode; step={step!r}"
    )
    assert step.get("relationship") is None, (
        f"TraversalStep.relationship should be None in community mode; step={step!r}"
    )


# ---------------------------------------------------------------------------
# MCP helpers (mirroring T-3 pattern)
# ---------------------------------------------------------------------------


def _mcp_headers(token: str, session_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


def _mcp_initialize(client, token: str) -> str:
    """Send MCP initialize + notifications/initialized; return session_id."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t6-community-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool and return the parsed SSE result payload."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call ({tool_name}) failed: {resp.status_code} {resp.text[:300]}"
    )
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, (
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str) -> dict:
    """Extract and parse the JSON text from an MCP tool response."""
    assert result, f"Tool '{tool_name}' returned empty result dict"
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    assert not rpc_result.get("isError"), (
        f"Tool '{tool_name}' returned isError=True (unhandled exception): {rpc_result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# (a) test_explain_local_community_provenance_e2e  (S3)
# ---------------------------------------------------------------------------


def test_explain_local_community_provenance_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode="local" → ≥1 result with community_id in
    TraversalStep  (S3).

    Setup:
    - Two docs containing only "PaymentService" → 1 unique graph node.
    - CommunityBuilder.build() returns 1 community via early-exit (no Leiden).
    - Query: "PaymentService" with graph_mode="local".

    Expected: _explain_community_candidates finds PaymentService node → fetches
    community → fetches representative chunks → attaches GraphProvenance with
    TraversalStep(community_id=...) to each community-retrieved candidate.
    """
    _install_payment_spacy_stub(monkeypatch)

    col = "t6-s3-local-community"
    doc1, doc2 = _build_corpus(tmp_path)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        toml_content="[database]\nchunk_size = 128\n",
    ) as (client, cfg, api_key):
        auth = _auth(api_key)

        # Ingest corpus (triggers graph extraction for each doc).
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)

        # Build communities from main thread using a fresh store connection.
        # asyncio.run() is safe here: Starlette's TestClient runs the ASGI app
        # in a background thread — the main test thread has no running event loop.
        communities = asyncio.run(
            _build_communities_async(cfg.db_path, col, cfg)
        )
        assert communities, (
            "CommunityBuilder.build() returned no communities. "
            "Expected at least 1 (PaymentService entity → single-node early exit)."
        )

        resp = client.post(
            "/explain",
            json={
                "query": "PaymentService",
                "collection": col,
                "graph_mode": "local",
                "top_k": 10,
            },
            headers=auth,
        )

        assert resp.status_code == 200, (
            f"POST /explain graph_mode='local' failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()

        # S3: response-level graph_mode_applied must be "local".
        assert body.get("graph_mode_applied") == "local", (
            f"Expected graph_mode_applied='local'; got {body.get('graph_mode_applied')!r}. "
            f"Response keys: {list(body.keys())}"
        )

        results = body.get("results", [])
        assert results, (
            "Expected non-empty results after ingest; "
            "cannot verify graph_provenance on an empty list"
        )

        # S3: at least one result must have non-null graph_provenance with community_id.
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            "Expected at least one result with non-null graph_provenance in local mode; "
            f"got zero. Total results: {len(results)}. "
            f"This means community lookup failed — check that CommunityBuilder.build() "
            f"wrote communities and that find_nodes_by_name matched 'PaymentService'. "
            f"Sample results: "
            f"{[{k: v for k, v in r.items() if k in ('chunk_id', 'graph_provenance')} for r in results[:3]]}"
        )

        for gr in graph_results:
            _assert_community_traversal_step(gr["graph_provenance"], expected_entity="PaymentService")


# ---------------------------------------------------------------------------
# (b) test_explain_global_community_provenance_e2e  (S4)
# ---------------------------------------------------------------------------


def test_explain_global_community_provenance_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode="global" → ≥1 result with community_id in
    TraversalStep  (S4).

    Uses the same corpus as S3.  Global mode aggregates community
    representative chunks across all communities without an entity-matching
    step (list_community_representatives → get_chunks_by_ids).
    """
    _install_payment_spacy_stub(monkeypatch)

    col = "t6-s4-global-community"
    doc1, doc2 = _build_corpus(tmp_path)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        toml_content="[database]\nchunk_size = 128\n",
    ) as (client, cfg, api_key):
        auth = _auth(api_key)

        ingest_file_via_path(client, col, str(doc1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)

        communities = asyncio.run(
            _build_communities_async(cfg.db_path, col, cfg)
        )
        assert communities, (
            "CommunityBuilder.build() returned no communities. "
            "Expected at least 1 (PaymentService entity → single-node early exit)."
        )

        resp = client.post(
            "/explain",
            json={
                "query": "PaymentService",
                "collection": col,
                "graph_mode": "global",
                "top_k": 10,
            },
            headers=auth,
        )

        assert resp.status_code == 200, (
            f"POST /explain graph_mode='global' failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()

        # S4: response-level graph_mode_applied must be "global".
        assert body.get("graph_mode_applied") == "global", (
            f"Expected graph_mode_applied='global'; got {body.get('graph_mode_applied')!r}. "
            f"Response keys: {list(body.keys())}"
        )

        results = body.get("results", [])
        assert results, (
            "Expected non-empty results after ingest; "
            "cannot verify graph_provenance on an empty list"
        )

        # S4: at least one result must have non-null graph_provenance with community_id.
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            "Expected at least one result with non-null graph_provenance in global mode; "
            f"got zero. Total results: {len(results)}. "
            f"Global mode uses list_community_representatives; check that communities "
            f"were written and that representative_chunk_ids are non-empty. "
            f"Sample results: "
            f"{[{k: v for k, v in r.items() if k in ('chunk_id', 'graph_provenance')} for r in results[:3]]}"
        )

        for gr in graph_results:
            _assert_community_traversal_step(gr["graph_provenance"])

        # S4: in global mode, entity and entity_id are both the community UUID.
        # This is the semantically distinct contract vs. local mode (where entity is
        # the matched entity name, not the community ID).
        step = graph_results[0]["graph_provenance"]["steps"][0]
        assert step["entity"] == step["entity_id"] == step["community_id"], (
            f"In global mode, entity, entity_id, and community_id must all equal the "
            f"community UUID; got entity={step['entity']!r}, entity_id={step['entity_id']!r}, "
            f"community_id={step['community_id']!r}"
        )


# ---------------------------------------------------------------------------
# (c) test_mcp_explain_local_community_provenance_e2e  (S3 / MCP parity)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("mcp")
def test_mcp_explain_local_community_provenance_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP explain tool with graph_mode="local" → result dict carries
    graph_mode_applied="local" and ≥1 non-null graph_provenance with
    community_id  (S3 / MCP parity).

    Uses the same corpus as S3/S4: two docs with "PaymentService" entity.
    """
    _install_payment_spacy_stub(monkeypatch)

    col = "t6-s3-mcp-local"
    doc1, doc2 = _build_corpus(tmp_path)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
        toml_content="[database]\nchunk_size = 128\n",
    ) as (client, cfg, api_key):
        # Ingest corpus.
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)

        # Build communities.
        communities = asyncio.run(
            _build_communities_async(cfg.db_path, col, cfg)
        )
        assert communities, (
            "CommunityBuilder.build() returned no communities."
        )

        # MCP handshake.
        session_id = _mcp_initialize(client, api_key)

        # Call MCP explain tool with graph_mode="local".
        raw = _mcp_call_tool(
            client,
            api_key,
            session_id,
            "explain",
            {
                "query": "PaymentService",
                "collection": col,
                "graph_mode": "local",
                "top_k": 10,
            },
        )
        parsed = _extract_tool_text(raw, "explain")

        assert isinstance(parsed, dict), (
            f"Expected dict from MCP explain; got {type(parsed).__name__!r}: {parsed!r}"
        )
        assert "error" not in parsed, (
            f"Unexpected error in MCP explain result: {parsed!r}"
        )

        # S3/MCP: graph_mode_applied must be "local".
        assert parsed.get("graph_mode_applied") == "local", (
            f"Expected graph_mode_applied='local' in MCP explain result; "
            f"got {parsed.get('graph_mode_applied')!r}. "
            f"Response keys: {list(parsed.keys())}"
        )

        # S3/MCP: at least one result item must carry non-null graph_provenance with community_id.
        results = parsed.get("results", [])
        assert results, (
            "Expected non-empty results in MCP explain response"
        )
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            "Expected at least one result item with non-null graph_provenance from "
            f"MCP explain with graph_mode='local'; got zero. "
            f"Results: "
            f"{[{k: v for k, v in r.items() if k in ('chunk_id', 'graph_provenance')} for r in results]}"
        )

        for gr in graph_results:
            _assert_community_traversal_step(gr["graph_provenance"], expected_entity="PaymentService")
