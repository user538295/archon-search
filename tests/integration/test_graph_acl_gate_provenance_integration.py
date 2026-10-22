"""Graph-sourced results expose the stored ACL provenance in ``acl_gate``.

A document is ingested with a ``.acl`` sidecar holding a valid namespace plus the
invalid ``deny-all`` entry, so its stored rows carry ``acl_source='sidecar'``, a
sidecar path, and a fail-open warning.  Communities are built so
``graph_mode='global'`` returns those rows as community representatives.

``POST /search`` (``acl_context=true``) and ``POST /explain`` must report the same
provenance for graph-sourced results as the hybrid path does — not ``source=null``
and empty ``warnings``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "graph-acl-gate-prov"
_QUERY = "PaymentService"
_ENTITY = "PaymentService"
_INVALID_SIDECAR_ENTRY = "deny-all"
_SIDECAR_NAME = "payment_service.txt.acl"


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _write_sidecar_doc(tmp_path: Path) -> Path:
    doc = tmp_path / "payment_service.txt"
    doc.write_text(
        "PaymentService processes all payment transactions securely. "
        "PaymentService is the core billing gateway for the platform.\n",
        encoding="utf-8",
    )
    (tmp_path / _SIDECAR_NAME).write_text(
        f"default\n{_INVALID_SIDECAR_ENTRY}\n", encoding="utf-8",
    )
    return doc


async def _build_communities(db_path: str, cfg: SearchConfig) -> list:
    from archon_search.community_builder import CommunityBuilder
    from archon_search.graph_store import GraphStore
    from archon_search.store import SearchStore

    graph_store = GraphStore(db_path)
    search_store = SearchStore(db_path)
    await graph_store.connect()
    await search_store.connect()
    try:
        builder = CommunityBuilder(graph_store, cfg.graph, search_store=search_store)
        return await builder.build(_COLLECTION, ns="default")
    finally:
        await graph_store.disconnect()
        await search_store.disconnect()


def _assert_sidecar_gate(gate: dict | None) -> None:
    assert isinstance(gate, dict), f"acl_gate must be a populated dict; got {gate!r}"
    assert gate.get("source") == "sidecar", f"acl_gate.source must be 'sidecar'; gate={gate!r}"
    assert gate.get("sidecar_path") == _SIDECAR_NAME, f"acl_gate.sidecar_path must name the sidecar; gate={gate!r}"
    assert any(_INVALID_SIDECAR_ENTRY in w for w in gate.get("warnings", [])), (
        f"acl_gate.warnings must carry the '{_INVALID_SIDECAR_ENTRY}' warning; gate={gate!r}"
    )


@contextmanager
def _app_with_sidecar_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, dict[str, str], SearchConfig]]:
    """Real app with the sidecar doc ingested; yields (client, headers, cfg)."""
    install_graph_engine_stub(monkeypatch, entities=[(_ENTITY, "system")], content_aware=True)
    doc = _write_sidecar_doc(tmp_path)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, toml_content="[database]\nchunk_size = 128\n",
    ) as (client, cfg, api_key):
        headers = _auth(api_key)
        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=api_key)
        yield client, headers, cfg


@contextmanager
def _app_with_global_communities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, dict[str, str]]]:
    """Real app with the sidecar doc ingested and its communities built; yields (client, headers)."""
    with _app_with_sidecar_doc(tmp_path, monkeypatch) as (client, headers, cfg):
        assert asyncio.run(_build_communities(cfg.db_path, cfg)), "no communities were built"
        yield client, headers


def test_search_acl_context_gate_carries_sidecar_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: POST /search without graph_mode reports the sidecar provenance for the same row."""
    with _app_with_sidecar_doc(tmp_path, monkeypatch) as (client, headers, _cfg):
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": _QUERY, "acl_context": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["results"], "hybrid /search returned no results"
    _assert_sidecar_gate(resp.json()["results"][0].get("acl_gate"))


def test_global_graph_search_acl_context_gate_carries_sidecar_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /search graph_mode='global' acl_context=true: every gate reports the sidecar provenance."""
    with _app_with_global_communities(tmp_path, monkeypatch) as (client, headers):
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": _QUERY, "graph_mode": "global", "acl_context": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["graph_expansion_applied"] is True, body
    assert body["results"], "graph_mode='global' /search returned no results"
    for r in body["results"]:
        _assert_sidecar_gate(r.get("acl_gate"))


def test_global_graph_explain_acl_gate_carries_sidecar_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /explain graph_mode='global': community-sourced results report the sidecar provenance."""
    with _app_with_global_communities(tmp_path, monkeypatch) as (client, headers):
        resp = client.post(
            "/explain",
            json={"collection": _COLLECTION, "query": _QUERY, "graph_mode": "global", "top_k": 10},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    graph_results = [r for r in resp.json()["results"] if r.get("graph_provenance") is not None]
    assert graph_results, "graph_mode='global' /explain returned no graph-sourced results"
    for r in graph_results:
        _assert_sidecar_gate(r.get("acl_gate"))
