"""Chunk-ACL hidden documents must not leak through the document list or the graph surfaces.

Scenario (two namespaces): collection ``shared`` belongs to namespace ``team-b`` and holds
  * ``open.md``      — no ACL (visible to everyone)
  * ``team-b.md``    — ``acl=['team-b']`` (visible to the caller)
  * ``team-a.md``    — ``acl=['team-a']`` (hidden from the team-b caller)

The request is made with the ``team-b`` key.  Search already drops ``team-a.md`` chunks
(``apply_acl_filter``); the document list, the graph inspection routes and the matching MCP
tools must hide it too: its doc id / source path, the entities whose only evidence is a hidden
chunk, the hidden chunk ids in edge ``source_chunk_ids`` and the code-symbol edges that came
from the hidden document.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search._types import ChunkRecord, normalize_iso_utc
from archon_search.collection_meta import CollectionMeta
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)
from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import make_real_app, mcp_initialize, mcp_tool_call

pytestmark = pytest.mark.integration

_EMBEDDING_DIM = 384
_OWNER_NS = "team-b"
_HIDDEN_FROM_OWNER_ACL = ["team-a"]
_COLLECTION = "shared"
_COLLECTION_2 = "shared-two"

_OPEN_PATH = "/corpus/open.md"
_VISIBLE_PATH = "/corpus/team-b.md"
_HIDDEN_PATH = "/corpus/team-a-secret.md"


def _doc_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


_OPEN_DOC = _doc_id(_OPEN_PATH)
_VISIBLE_DOC = _doc_id(_VISIBLE_PATH)
_HIDDEN_DOC = _doc_id(_HIDDEN_PATH)

_OPEN_CHUNK = f"{_OPEN_DOC}-000000"
_VISIBLE_CHUNK = f"{_VISIBLE_DOC}-000000"
_HIDDEN_CHUNK = f"{_HIDDEN_DOC}-000000"

# Entities (prose): OPEN has visible evidence, SECRET only hidden evidence, SHARED both.
_E_OPEN = make_stable_entity_id(EntityType.concept.value, "OpenTopic")
_E_SECRET = make_stable_entity_id(EntityType.concept.value, "SecretProject")
_E_SHARED = make_stable_entity_id(EntityType.concept.value, "SharedTopic")
# Code symbols (def/ref graph: no mentions, provenance is the source document).
_S_ROOT = make_stable_entity_id(EntityType.code_symbol.value, "root_fn")
_S_VISIBLE = make_stable_entity_id(EntityType.code_symbol.value, "visible_callee")
_S_SECRET = make_stable_entity_id(EntityType.code_symbol.value, "secret_callee")
_S_HIDDEN_ROOT = make_stable_entity_id(EntityType.code_symbol.value, "hidden_root_fn")

_SECRET_NAMES = ("SecretProject", "secret_callee", "hidden_root_fn")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _chunk(doc_id: str, path: str, acl: list[str] | None) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=f"content of {path}",
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=path,
        indexed_at=normalize_iso_utc(datetime.now(UTC)),
        acl=acl,
    )


async def _seed_chunks(store: Any, collection: str, *, extra_docs: int = 0) -> list[str]:
    """Store the three scenario documents (plus *extra_docs* open ones) in *collection*."""
    await store.ensure_collection(collection, _EMBEDDING_DIM)
    chunks = [
        _chunk(_OPEN_DOC, _OPEN_PATH, None),
        _chunk(_VISIBLE_DOC, _VISIBLE_PATH, [_OWNER_NS]),
        _chunk(_HIDDEN_DOC, _HIDDEN_PATH, _HIDDEN_FROM_OWNER_ACL),
    ]
    for i in range(extra_docs):
        chunks.append(_chunk(_doc_id(f"/corpus/extra-{i}.md"), f"/corpus/extra-{i}.md", None))
    await store.ingest_chunks(collection, chunks, namespace=_OWNER_NS)
    await store.rebuild_fts_index(collection)
    await store.update_collection_meta(
        CollectionMeta(
            name=collection,
            active_embedding_model="test-model",
            doc_count=len({c.doc_id for c in chunks}),
            chunk_count=len(chunks),
            namespace=_OWNER_NS,
        )
    )
    return sorted(c.doc_id for c in chunks)


def _node(entity_id: str, name: str, etype: EntityType, doc_id: str, collection: str) -> GraphNode:
    return GraphNode(
        id=entity_id,
        entity_name=name,
        entity_type=etype,
        source_doc_id=doc_id,
        collection_name=collection,
    )


def _edge(source: str, target: str, rel: RelationshipType, doc_id: str, method: str | None) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(source, target, rel.value),
        source_node_id=source,
        target_node_id=target,
        relationship_type=rel,
        source_doc_id=doc_id,
        extraction_method=method,
    )


async def _seed_graph(db_path: str, collection: str) -> None:
    """Prose graph backed by mentions + a def/ref code graph backed by source documents."""
    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=_OWNER_NS)
        nodes = [
            _node(_E_OPEN, "OpenTopic", EntityType.concept, _OPEN_DOC, collection),
            # last writer of SHARED was the hidden document: the node must still show (visible evidence).
            _node(_E_SHARED, "SharedTopic", EntityType.concept, _HIDDEN_DOC, collection),
            _node(_E_SECRET, "SecretProject", EntityType.concept, _HIDDEN_DOC, collection),
            _node(_S_ROOT, "root_fn", EntityType.code_symbol, _OPEN_DOC, collection),
            _node(_S_VISIBLE, "visible_callee", EntityType.code_symbol, _OPEN_DOC, collection),
            _node(_S_SECRET, "secret_callee", EntityType.code_symbol, _HIDDEN_DOC, collection),
            _node(_S_HIDDEN_ROOT, "hidden_root_fn", EntityType.code_symbol, _HIDDEN_DOC, collection),
        ]
        edges = [
            # OPEN-SHARED co-occur in the visible chunk AND the hidden chunk.
            _edge(_E_OPEN, _E_SHARED, RelationshipType.related_to, _HIDDEN_DOC, None),
            # SECRET-SHARED co-occur only in the hidden chunk.
            _edge(_E_SECRET, _E_SHARED, RelationshipType.related_to, _HIDDEN_DOC, None),
            _edge(_S_ROOT, _S_VISIBLE, RelationshipType.calls, _OPEN_DOC, "extracted"),
            _edge(_S_ROOT, _S_SECRET, RelationshipType.calls, _HIDDEN_DOC, "extracted"),
            _edge(_S_HIDDEN_ROOT, _S_VISIBLE, RelationshipType.calls, _HIDDEN_DOC, "extracted"),
        ]
        await gs.write_graph(collection, nodes, edges, ns=_OWNER_NS)
        mentions = [
            GraphMention(entity_id=_E_OPEN, chunk_id=_OPEN_CHUNK, doc_id=_OPEN_DOC),
            GraphMention(entity_id=_E_SHARED, chunk_id=_OPEN_CHUNK, doc_id=_OPEN_DOC),
            GraphMention(entity_id=_E_OPEN, chunk_id=_HIDDEN_CHUNK, doc_id=_HIDDEN_DOC),
            GraphMention(entity_id=_E_SHARED, chunk_id=_HIDDEN_CHUNK, doc_id=_HIDDEN_DOC),
            GraphMention(entity_id=_E_SECRET, chunk_id=_HIDDEN_CHUNK, doc_id=_HIDDEN_DOC),
        ]
        await gs.write_mentions(collection, mentions, ns=_OWNER_NS)
    finally:
        await gs.disconnect()


@contextmanager
def _env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, extra_docs: int = 0
) -> Iterator[tuple[TestClient, str, list[str]]]:
    """Real app with a graph + MCP; ``team-b`` owns two seeded collections. Yields (client, team-b key, doc ids)."""
    install_graph_engine_stub(monkeypatch)
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
        namespaces={key_a: "team-a", key_b: _OWNER_NS},
    ) as (client, cfg, _default_key):
        store = client.app.state.search_store
        doc_ids: list[str] = []
        for collection in (_COLLECTION, _COLLECTION_2):
            doc_ids = asyncio.run(_seed_chunks(store, collection, extra_docs=extra_docs))
            asyncio.run(_seed_graph(cfg.db_path, collection))
        yield client, key_b, doc_ids


def _assert_no_secret(payload: Any) -> None:
    text = str(payload)
    for needle in (*_SECRET_NAMES, _HIDDEN_CHUNK, _HIDDEN_DOC, _HIDDEN_PATH):
        assert needle not in text, f"{needle!r} leaked: {text[:600]}"


# ---------------------------------------------------------------------------
# Document listing
# ---------------------------------------------------------------------------


def test_rest_documents_list_hides_acl_hidden_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        resp = client.get(f"/collections/{_COLLECTION}/documents", headers=_auth(key))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert {item["doc_id"] for item in body["items"]} == {_OPEN_DOC, _VISIBLE_DOC}
        assert {item["source_path"] for item in body["items"]} == {_OPEN_PATH, _VISIBLE_PATH}
        assert body["total"] == 2, "total must not reveal the hidden document"
        _assert_no_secret(body)


def test_mcp_list_documents_hides_acl_hidden_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        session = mcp_initialize(client, key)
        payload = mcp_tool_call(client, key, session, "list_documents", {"collection": _COLLECTION})
        assert {item["doc_id"] for item in payload} == {_OPEN_DOC, _VISIBLE_DOC}
        _assert_no_secret(payload)


def test_documents_list_pagination_is_stable_over_hidden_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walking pages of 2 yields every visible doc exactly once, never a hidden one; total is the visible count."""
    with _env(tmp_path, monkeypatch, extra_docs=4) as (client, key, all_docs):
        visible_expected = sorted(set(all_docs) - {_HIDDEN_DOC})
        seen: list[str] = []
        cursor: str | None = None
        totals: set[int] = set()
        for _ in range(len(all_docs)):
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            body = client.get(f"/collections/{_COLLECTION}/documents", params=params, headers=_auth(key)).json()
            seen.extend(item["doc_id"] for item in body["items"])
            totals.add(body["total"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == visible_expected
        assert totals == {len(visible_expected)}


# ---------------------------------------------------------------------------
# Graph — REST
# ---------------------------------------------------------------------------


def test_rest_graph_hides_entities_and_chunk_ids_from_acl_hidden_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        resp = client.get(f"/graph/{_COLLECTION}", headers=_auth(key))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = {n["entity_name"] for n in body["nodes"]}
        assert "SecretProject" not in names
        assert {"OpenTopic", "SharedTopic"} <= names
        assert body["node_count"] == len(body["nodes"]), "node_count must not count hidden entities"
        by_pair = {(e["source_entity_id"], e["target_entity_id"]): e for e in body["edges"]}
        assert (_E_SECRET, _E_SHARED) not in by_pair, "edge supported only by a hidden chunk leaked"
        shown = by_pair[(_E_OPEN, _E_SHARED)]
        assert shown["source_chunk_ids"] == [_OPEN_CHUNK]
        assert shown["weight"] == 1, "weight must count visible co-occurrences only"
        node_counts = {n["entity_name"]: n["chunk_count"] for n in body["nodes"]}
        assert node_counts["SharedTopic"] == 1
        _assert_no_secret(body)


def test_rest_graph_cross_collection_hides_acl_hidden_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        resp = client.get(
            "/graph/cross-collection", params={"collections": f"{_COLLECTION},{_COLLECTION_2}"}, headers=_auth(key)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = {n["entity_name"] for n in body["nodes"]}
        assert "SecretProject" not in names
        assert {"OpenTopic", "SharedTopic"} <= names
        assert body["node_count"] == len(body["nodes"])
        assert all(_HIDDEN_CHUNK not in e["source_chunk_ids"] for e in body["edges"])
        assert (_E_SECRET, _E_SHARED) not in {(e["source_entity_id"], e["target_entity_id"]) for e in body["edges"]}
        _assert_no_secret(body)


def test_rest_graph_graphml_export_hides_acl_hidden_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("networkx")
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        resp = client.get(f"/graph/{_COLLECTION}", params={"format": "graphml"}, headers=_auth(key))
        assert resp.status_code == 200, resp.text
        _assert_no_secret(resp.text)


def test_rest_graph_impact_hides_acl_hidden_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        resp = client.get(f"/graph/{_COLLECTION}/impact/root_fn", params={"direction": "callees"}, headers=_auth(key))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [e["entity_name"] for e in body["callees"]["direct"]] == ["visible_callee"]
        _assert_no_secret(body)

        callers = client.get(
            f"/graph/{_COLLECTION}/impact/visible_callee", params={"direction": "callers"}, headers=_auth(key)
        ).json()
        assert [e["entity_name"] for e in callers["callers"]["direct"]] == ["root_fn"]
        _assert_no_secret(callers)


def test_rest_graph_impact_of_a_hidden_symbol_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root itself comes from a hidden document: same answer as a symbol that does not exist."""
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        hidden = client.get(f"/graph/{_COLLECTION}/impact/hidden_root_fn", headers=_auth(key)).json()
        missing = client.get(f"/graph/{_COLLECTION}/impact/no_such_symbol", headers=_auth(key)).json()
        assert hidden["callers"] == missing["callers"]
        assert hidden["callees"] == missing["callees"]
        assert hidden["depth_used"] == missing["depth_used"] == 0


# ---------------------------------------------------------------------------
# Graph — MCP
# ---------------------------------------------------------------------------


def test_mcp_get_graph_hides_acl_hidden_entities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        session = mcp_initialize(client, key)
        result = mcp_tool_call(client, key, session, "get_graph", {"collection": _COLLECTION})
        names = {n["entity_name"] for n in result["top_nodes"]}
        assert "SecretProject" not in names
        assert {"OpenTopic", "SharedTopic"} <= names
        assert result["node_count"] == len(result["top_nodes"])
        assert (_E_SECRET, _E_SHARED) not in {(e["source_entity_id"], e["target_entity_id"]) for e in result["top_edges"]}
        _assert_no_secret(result)


def test_mcp_get_graph_cross_collection_hides_acl_hidden_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        session = mcp_initialize(client, key)
        result = mcp_tool_call(
            client, key, session, "get_graph_cross_collection", {"collections": [_COLLECTION, _COLLECTION_2]}
        )
        assert "SecretProject" not in {n["entity_name"] for n in result["top_nodes"]}
        _assert_no_secret(result)


def test_mcp_graph_impact_hides_acl_hidden_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _env(tmp_path, monkeypatch) as (client, key, _docs):
        session = mcp_initialize(client, key)
        result = mcp_tool_call(
            client, key, session, "graph_impact", {"collection": _COLLECTION, "symbol": "root_fn", "direction": "callees"}
        )
        assert [e["entity_name"] for e in result["callees"]["direct"]] == ["visible_callee"]
        _assert_no_secret(result)
        hidden = mcp_tool_call(client, key, session, "graph_impact", {"collection": _COLLECTION, "symbol": "hidden_root_fn"})
        assert hidden["depth_used"] == 0
        assert hidden["callees"]["direct"] == []
