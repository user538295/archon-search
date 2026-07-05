"""Route integration tests for GET /graph/{collection} salience parameter — E2c T-1 and T-2.

Tests the single-collection graph inspection endpoint's salience query parameter (T-1):
  - tfidf mode echoes salience_mode in response
  - tfidf reranks nodes vs frequency (domain-specific entity outranks ubiquitous)
  - frequency default echoes salience_mode
  - explicit frequency produces identical response to default
  - invalid salience value → 422
  - tfidf graphml format returns application/xml with float salience attributes
  - graph disabled → 422 regardless of salience param
  - tfidf IDF is namespace-scoped (larger namespace → lower IDF → lower salience)

Tests the cross-collection graph inspection endpoint's salience parameter (T-2):
  - tfidf mode echoes salience_mode in cross-collection response
  - invalid salience value → 422 (with body assertion on salience field)
  - frequency default echoes salience_mode in cross-collection response
  - explicit frequency produces identical cross-collection response to default
  - graph disabled → 422 on cross-collection endpoint
  - IDF denominator uses ALL namespace collections (not just listed ones)

Run with:
    uv run pytest tests/integration/test_routes_graph_salience.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import sys
import types
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

# Embedding dimension produced by the fastembed stub (see tests/_search_stubs.py).
_STUB_EMBEDDING_DIM = 384

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    """Return Authorization header dict."""
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub spaCy module that returns NO named entities.

    Must be called BEFORE make_real_app(graph_enabled=True) because create_app
    calls _check_graph_deps which imports spaCy synchronously.
    Uses monkeypatch.setitem so the stub auto-reverts after each test.
    """

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

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


async def _seed_node_with_mentions(
    db_path: str,
    collection: str,
    entity_id: str,
    entity_name: str,
    chunk_ids: list[str],
    ns: str = "default",
) -> None:
    """Write a single graph node and its mentions into GraphStore.

    Safe to call via asyncio.run() from the main test thread while TestClient
    runs in a background thread with its own event loop. The GraphStore uses
    async LanceDB writes; opening a fresh connection per call avoids sharing
    the app's internal store instance.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import EntityType, GraphMention, GraphNode

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        node = GraphNode(
            id=entity_id,
            entity_name=entity_name,
            entity_type=EntityType.concept,
            source_doc_id="seeded-doc",
            collection_name=collection,
        )
        await gs.write_graph(collection, [node], [], ns=ns)
        mention_objs = [
            GraphMention(entity_id=entity_id, chunk_id=cid, doc_id="seeded-doc")
            for cid in chunk_ids
        ]
        await gs.write_mentions(collection, mention_objs, ns=ns)
    finally:
        await gs.disconnect()


async def _seed_namespace_collection(
    db_path: str,
    collection: str,
    namespace: str,
    chunk_count: int = 1,
) -> None:
    """Create a collection table and meta row with the correct namespace.

    Unlike ingest_file_via_path (which stores the meta with DEFAULT_NAMESPACE due to
    ingest_chunks not propagating namespace to _do_update_meta_on_add), this helper
    directly calls store.update_collection_meta with the target namespace.

    Safe to call via asyncio.run() — opens a fresh SearchStore connection that is
    independent from the running app's connection (LanceDB supports concurrent access).
    """
    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, _STUB_EMBEDDING_DIM)
        doc_id = hashlib.sha256(collection.encode()).hexdigest()
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{i:06d}",
                text=f"seed content {i}",
                vector=[0.0] * _STUB_EMBEDDING_DIM,
                source_path=f"/fake/{collection}.txt",
                indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
            )
            for i in range(chunk_count)
        ]
        await store.ingest_chunks(collection, chunks)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=1,
            chunk_count=chunk_count,
            namespace=namespace,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()


def _extract_salience_from_graphml(graphml_bytes: bytes) -> dict[str, float]:
    """Parse GraphML bytes and return a mapping of node id → salience float.

    networkx GraphML uses <key id="dN" attr.name="salience"/> at the top
    and <data key="dN">value</data> inside each <node>. The node's id attribute
    is used as the dict key for stable, order-independent entity keying.
    """
    root = ET.fromstring(graphml_bytes)
    # Determine XML namespace prefix (e.g. "{http://graphml.graphdrawing.org/graphml}")
    xml_ns = ""
    if "}" in root.tag:
        xml_ns = root.tag.split("}")[0] + "}"

    # Find the key element whose attr.name == "salience"
    salience_key_id: str | None = None
    for key_elem in root.findall(f"{xml_ns}key"):
        if key_elem.get("attr.name") == "salience":
            salience_key_id = key_elem.get("id")
            break

    if salience_key_id is None:
        return {}

    # Collect salience values from all <node> elements inside <graph>
    graph_elem = root.find(f".//{xml_ns}graph")
    if graph_elem is None:
        return {}

    saliences: dict[str, float] = {}
    for node_elem in graph_elem.findall(f"{xml_ns}node"):
        node_id = node_elem.get("id", "")
        for data_elem in node_elem.findall(f"{xml_ns}data"):
            if data_elem.get("key") == salience_key_id and data_elem.text is not None:
                saliences[node_id] = float(data_elem.text)

    return saliences


# ---------------------------------------------------------------------------
# Test 1: tfidf echoes salience_mode
# ---------------------------------------------------------------------------


def test_get_graph_tfidf_echoes_salience_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/{col}?salience=tfidf → 200, response body has salience_mode == 'tfidf'.

    Verifies the route echoes the salience mode in the JSON body regardless of
    whether the collection contains graph data.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-tfidf-echo.txt"
        doc_file.write_text("hello world test content for tfidf echo", encoding="utf-8")
        ingest_file_via_path(client, "col-tfidf-echo", str(doc_file), api_key=api_key)

        response = client.get("/graph/col-tfidf-echo?salience=tfidf", headers=_auth(api_key))

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "salience_mode" in data, "Expected salience_mode in response body"
        assert data["salience_mode"] == "tfidf", (
            f"Expected salience_mode='tfidf', got {data['salience_mode']!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 helpers: shared fixture for ranking comparison tests
# ---------------------------------------------------------------------------


@contextmanager
def _make_rank_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Context manager that sets up 3 collections with two seeded entities.

    Multi-collection namespace setup (all in default namespace):
      - 3 collections: col-rank-a, col-rank-b, col-rank-c
      - Entity D ("DomainSpecific"): node in col-rank-a ONLY (df=1)
        IDF_D = log((3+1)/1) = log(4) ≈ 1.386
      - Entity U ("Ubiquitous"): node in all 3 collections (df=3)
        IDF_U = log((3+1)/3) = log(4/3) ≈ 0.288

    Mentions in col-rank-a (chunk_size=50 → many chunks per file):
      - D: 3 distinct chunk_ids → chunk_count=3
      - U: 10 distinct chunk_ids → chunk_count=10

    Yields: (client, cfg, api_key, entity_d_id, entity_u_id)
    """
    from archon_search.graph_types import EntityType, make_stable_entity_id

    _install_spacy_stub(monkeypatch)

    # chunk_size=50 guarantees many real chunks so frequency saliences stay below 1.0
    toml_content = "[database]\nchunk_size = 50\n"

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, toml_content=toml_content
    ) as (client, cfg, api_key):
        many_sentences = "\n".join(
            f"This is sentence number {i} in the test document for graph inspection."
            for i in range(60)
        )
        for col_name in ("col-rank-a", "col-rank-b", "col-rank-c"):
            col_file = tmp_path / f"{col_name}.txt"
            col_file.write_text(many_sentences, encoding="utf-8")
            ingest_file_via_path(client, col_name, str(col_file), api_key=api_key)

        entity_d_id = make_stable_entity_id(EntityType.concept.value, "DomainSpecific")
        entity_u_id = make_stable_entity_id(EntityType.concept.value, "Ubiquitous")

        # Entity D in col-rank-a ONLY (df=1): 3 synthetic mention chunk_ids
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "col-rank-a", entity_d_id, "DomainSpecific",
            [f"chunk-d-{i:06d}" for i in range(3)],
        ))

        # Entity U in all 3 collections (df=3):
        # 10 mentions in col-rank-a (for chunk_count comparison), 1 each in others (for IDF df)
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "col-rank-a", entity_u_id, "Ubiquitous",
            [f"chunk-u-a-{i:06d}" for i in range(10)],
        ))
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "col-rank-b", entity_u_id, "Ubiquitous",
            ["chunk-u-b-000000"],
        ))
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "col-rank-c", entity_u_id, "Ubiquitous",
            ["chunk-u-c-000000"],
        ))

        yield client, cfg, api_key, entity_d_id, entity_u_id


# ---------------------------------------------------------------------------
# Test 2a: frequency mode ranks U above D
# ---------------------------------------------------------------------------


def test_get_graph_frequency_ranks_ubiquitous_above_domain_specific(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """frequency mode: U (10 chunks) ranks above D (3 chunks)."""
    with _make_rank_fixture(tmp_path, monkeypatch) as (client, cfg, api_key, _d, _u):
        resp_freq = client.get("/graph/col-rank-a?salience=frequency", headers=_auth(api_key))
        assert resp_freq.status_code == 200, (
            f"Frequency request failed: {resp_freq.status_code} {resp_freq.text}"
        )
        data_freq = resp_freq.json()
        assert data_freq["salience_mode"] == "frequency"
        freq_nodes = data_freq["nodes"]
        assert freq_nodes, "Expected non-empty node list in frequency mode"
        freq_names = [n["entity_name"] for n in freq_nodes]
        assert "DomainSpecific" in freq_names, (
            f"Entity DomainSpecific missing from frequency response; got: {freq_names}"
        )
        assert "Ubiquitous" in freq_names, (
            f"Entity Ubiquitous missing from frequency response; got: {freq_names}"
        )
        # Ubiquitous has 10 chunks vs DomainSpecific's 3 → U ranks first
        assert freq_names.index("Ubiquitous") < freq_names.index("DomainSpecific"), (
            f"Expected Ubiquitous before DomainSpecific in frequency mode; order: {freq_names}"
        )


# ---------------------------------------------------------------------------
# Test 2b: tfidf mode ranks D above U, with numeric ratio verification
# ---------------------------------------------------------------------------


def test_get_graph_tfidf_ranks_domain_specific_above_ubiquitous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tfidf mode: D (unique, IDF=log(4)) ranks above U (ubiquitous, IDF=log(4/3)).

    Also verifies the numeric salience ratio:
      D: chunk_count=3, df=1, N=3 → IDF_D = log((3+1)/1) = log(4)
      U: chunk_count=10, df=3       → IDF_U = log((3+1)/3) = log(4/3)
      TF cancels (same total_chunk_count denominator) →
        expected_ratio = (3 × log(4)) / (10 × log(4/3))
    """
    with _make_rank_fixture(tmp_path, monkeypatch) as (client, cfg, api_key, _d, _u):
        resp_tfidf = client.get("/graph/col-rank-a?salience=tfidf", headers=_auth(api_key))
        assert resp_tfidf.status_code == 200, (
            f"TF-IDF request failed: {resp_tfidf.status_code} {resp_tfidf.text}"
        )
        data_tfidf = resp_tfidf.json()
        assert data_tfidf["salience_mode"] == "tfidf"
        tfidf_nodes = data_tfidf["nodes"]
        assert tfidf_nodes, "Expected non-empty node list in tfidf mode"
        tfidf_names = [n["entity_name"] for n in tfidf_nodes]
        assert "DomainSpecific" in tfidf_names, (
            f"Entity DomainSpecific missing from tfidf response; got: {tfidf_names}"
        )
        assert "Ubiquitous" in tfidf_names, (
            f"Entity Ubiquitous missing from tfidf response; got: {tfidf_names}"
        )
        # DomainSpecific has IDF=log(4)≈1.386 vs Ubiquitous's IDF=log(4/3)≈0.288 → D ranks first
        assert tfidf_names.index("DomainSpecific") < tfidf_names.index("Ubiquitous"), (
            f"Expected DomainSpecific before Ubiquitous in tfidf mode; order: {tfidf_names}"
        )

        # Numeric ratio assertion: verifies the IDF formula, not just ordering.
        # D: chunk_count=3, df=1, N=3 → IDF_D = log((3+1)/1) = log(4)
        # U: chunk_count=10, df=3     → IDF_U = log((3+1)/3) = log(4/3)
        # TF cancels because both use the same total_chunk_count denominator.
        node_d = next(n for n in tfidf_nodes if n["entity_name"] == "DomainSpecific")
        node_u = next(n for n in tfidf_nodes if n["entity_name"] == "Ubiquitous")
        expected_ratio = (3 * math.log(4)) / (10 * math.log(4 / 3))
        actual_ratio = node_d["salience"] / node_u["salience"]
        assert math.isclose(actual_ratio, expected_ratio, rel_tol=1e-6), (
            f"Expected salience ratio D/U ≈ {expected_ratio:.4f}, got {actual_ratio:.4f}"
        )


# ---------------------------------------------------------------------------
# Test 3: frequency default echoes salience_mode
# ---------------------------------------------------------------------------


def test_get_graph_frequency_default_echoes_salience_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/{col} (no salience param) → 200, salience_mode == 'frequency'.

    Verifies that omitting ?salience= applies the 'frequency' default and the
    response body echoes it.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-freq-default.txt"
        doc_file.write_text("frequency default mode test document", encoding="utf-8")
        ingest_file_via_path(client, "col-freq-default", str(doc_file), api_key=api_key)

        response = client.get("/graph/col-freq-default", headers=_auth(api_key))

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "salience_mode" in data, "Expected salience_mode in response body"
        assert data["salience_mode"] == "frequency", (
            f"Expected salience_mode='frequency' for default (no param), "
            f"got {data['salience_mode']!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: explicit frequency identical to default
# ---------------------------------------------------------------------------


def test_get_graph_explicit_frequency_identical_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/{col}?salience=frequency → response body identical to omitting ?salience=.

    Both explicit and implicit frequency must produce exactly the same response.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-explicit-freq.txt"
        doc_file.write_text("explicit frequency equals default test", encoding="utf-8")
        ingest_file_via_path(client, "col-explicit-freq", str(doc_file), api_key=api_key)

        resp_default = client.get("/graph/col-explicit-freq", headers=_auth(api_key))
        resp_explicit = client.get(
            "/graph/col-explicit-freq?salience=frequency", headers=_auth(api_key)
        )

        assert resp_default.status_code == 200
        assert resp_explicit.status_code == 200
        assert resp_default.json() == resp_explicit.json(), (
            "Explicit ?salience=frequency should produce identical body to omitting the param"
        )


# ---------------------------------------------------------------------------
# Test 5: invalid salience → 422
# ---------------------------------------------------------------------------


def test_get_graph_invalid_salience_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/{col}?salience=bm25 → 422.

    FastAPI enum validation rejects any value not in {'frequency', 'tfidf'}.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-invalid-sal.txt"
        doc_file.write_text("invalid salience test document", encoding="utf-8")
        ingest_file_via_path(client, "col-invalid-sal", str(doc_file), api_key=api_key)

        response = client.get("/graph/col-invalid-sal?salience=bm25", headers=_auth(api_key))

        assert response.status_code == 422, (
            f"Expected 422 for ?salience=bm25, got {response.status_code}: {response.text}"
        )


# ---------------------------------------------------------------------------
# Test 6: tfidf graphml returns xml with float salience attribute
# ---------------------------------------------------------------------------


def test_get_graph_tfidf_graphml_returns_xml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/{col}?salience=tfidf&format=graphml → 200 application/xml.

    Assertions:
    - Content-Type is application/xml
    - At least one node's salience attribute in the GraphML is a float > 0
    - Salience values under tfidf differ from those under frequency
      (IDF factor = log(2) ≈ 0.693 ≠ 1, so tfidf ≠ frequency for any TF > 0)
    """
    from archon_search.graph_types import EntityType, make_stable_entity_id

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-graphml.txt"
        doc_file.write_text("graphml export test document content", encoding="utf-8")
        ingest_file_via_path(client, "col-graphml", str(doc_file), api_key=api_key)

        # Seed one entity with 1 mention so salience > 0 in tfidf mode
        entity_id = make_stable_entity_id(EntityType.concept.value, "GraphMLEntity")
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "col-graphml", entity_id, "GraphMLEntity",
            ["chunk-gml-000000"],
        ))

        # --- TF-IDF GraphML request ---
        resp_tfidf = client.get(
            "/graph/col-graphml?salience=tfidf&format=graphml",
            headers=_auth(api_key),
        )
        assert resp_tfidf.status_code == 200, (
            f"Expected 200, got {resp_tfidf.status_code}: {resp_tfidf.text}"
        )
        content_type = resp_tfidf.headers.get("content-type", "")
        assert "application/xml" in content_type, (
            f"Expected application/xml content-type, got: {content_type!r}"
        )

        tfidf_by_entity = _extract_salience_from_graphml(resp_tfidf.content)
        assert tfidf_by_entity, (
            "Expected at least one node salience attribute in tfidf GraphML"
        )
        assert any(s > 0.0 for s in tfidf_by_entity.values()), (
            f"Expected at least one salience > 0 in tfidf GraphML; got: {tfidf_by_entity}"
        )

        # --- Frequency GraphML for comparison ---
        resp_freq = client.get(
            "/graph/col-graphml?salience=frequency&format=graphml",
            headers=_auth(api_key),
        )
        assert resp_freq.status_code == 200
        freq_by_entity = _extract_salience_from_graphml(resp_freq.content)
        assert freq_by_entity, (
            "Expected at least one node salience attribute in frequency GraphML"
        )

        # Per-entity ratio assertion: verifies the IDF formula, not just inequality.
        # Single-collection namespace (N=1) with entity df=1:
        #   IDF = log((1+1)/1) = log(2) ≈ 0.693
        #   tfidf_salience = freq_salience × log(2)
        # Keyed by entity id to avoid positional pairing failures when node order differs.
        # Assert key-set identity first — disjoint keys with equal counts would silently
        # skip all per-entity assertions in the loop below. Both responses come from the
        # same graph (same entity_ids), so keys must be identical regardless of sort order.
        assert tfidf_by_entity.keys() == freq_by_entity.keys(), (
            f"GraphML node-id keys must match across salience modes; "
            f"tfidf keys={sorted(tfidf_by_entity)}, freq keys={sorted(freq_by_entity)}"
        )
        for entity_key, actual_tfidf in tfidf_by_entity.items():
            freq_val = freq_by_entity[entity_key]
            expected_tfidf = freq_val * math.log(2)
            assert math.isclose(actual_tfidf, expected_tfidf, rel_tol=1e-6), (
                f"Entity {entity_key!r}: expected tfidf_salience = freq_salience × log(2) = "
                f"{freq_val:.6f} × {math.log(2):.6f} = {expected_tfidf:.6f}, "
                f"got {actual_tfidf:.6f}"
            )


# ---------------------------------------------------------------------------
# Test 7: graph disabled → 422
# ---------------------------------------------------------------------------


def test_get_graph_tfidf_graph_disabled_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/{col}?salience=tfidf → 422 when graph.enabled=false.

    The existing graph-disabled guard fires before salience is evaluated.
    No spaCy stub is required here — create_app skips the spaCy check when
    graph.enabled=false.
    """
    # graph_enabled=False (default) — no spaCy stub needed
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        response = client.get("/graph/any-collection?salience=tfidf", headers=_auth(api_key))

        assert response.status_code == 422, (
            f"Expected 422 when graph disabled, got {response.status_code}: {response.text}"
        )
        assert "graph inspection requires [graph] enabled=true" in response.json()["detail"], (
            f"Expected graph-disabled error message, got: {response.json()['detail']!r}"
        )


# ---------------------------------------------------------------------------
# Test 8: namespace IDF isolation
# ---------------------------------------------------------------------------


def test_get_graph_tfidf_namespace_idf_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TF-IDF salience is namespace-scoped: same entity gets different IDF per namespace.

    Setup:
      - ns-A: 3 collections (nsa-1, nsa-2, nsa-3); entity E appears in all 3 (df_A=3)
        IDF_A = log((3+1)/3) = log(4/3) ≈ 0.288
      - ns-B: 1 collection (nsb-1); entity E appears there only (df_B=1)
        IDF_B = log((1+1)/1) = log(2) ≈ 0.693

    Entity E has the same chunk_count in nsa-1 and nsb-1 (5 synthetic mentions each)
    and both collections have the same total_chunk_count (20 chunks).

    Therefore:
      TF_A = TF_B = 5 / 20 = 0.25  (identical; ÷20 makes the division non-trivial)
      salience_A = 0.25 × log(4/3) ≈ 0.072
      salience_B = 0.25 × log(2)   ≈ 0.173

    assert salience_A < salience_B  (IDF_A < IDF_B).

    Uses two api keys (different namespaces) within the same app instance.
    """
    from archon_search.graph_types import EntityType, make_stable_entity_id

    _install_spacy_stub(monkeypatch)

    # Two namespace api keys
    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        namespaces={key_a: "ns-a", key_b: "ns-b"},
    ) as (client, cfg, api_key):
        # --- Create 3 collections in ns-A (directly via store, to set namespace correctly) ---
        # Note: ingest_file_via_path cannot be used here because store.ingest_chunks does not
        # propagate the namespace argument to _do_update_meta_on_add, so the meta row ends up
        # with DEFAULT_NAMESPACE="default" even when a non-default namespace key is used.
        # The direct store approach (same pattern as test_acl_namespace_integration.py) is the
        # only way to create collections with a non-default namespace in the meta.
        for col in ("nsa-1", "nsa-2", "nsa-3"):
            asyncio.run(_seed_namespace_collection(cfg.db_path, col, "ns-a", chunk_count=20))

        # --- Create 1 collection in ns-B ---
        asyncio.run(_seed_namespace_collection(cfg.db_path, "nsb-1", "ns-b", chunk_count=20))

        # --- Entity E: seeded in all 4 collections with 5 mentions each ---
        entity_e_id = make_stable_entity_id(EntityType.concept.value, "EntityE")

        for col in ("nsa-1", "nsa-2", "nsa-3"):
            asyncio.run(_seed_node_with_mentions(
                cfg.db_path, col, entity_e_id, "EntityE",
                [f"chunk-{col}-{i:06d}" for i in range(5)],
                ns="ns-a",
            ))

        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "nsb-1", entity_e_id, "EntityE",
            [f"chunk-nsb-1-{i:06d}" for i in range(5)],
            ns="ns-b",
        ))

        # --- ns-A: query nsa-1 with key_a → IDF uses N=3 namespace collections ---
        resp_a = client.get("/graph/nsa-1?salience=tfidf", headers=_auth(key_a))
        assert resp_a.status_code == 200, (
            f"ns-A request failed: {resp_a.status_code} {resp_a.text}"
        )
        nodes_a = resp_a.json()["nodes"]
        assert nodes_a, "Expected non-empty nodes in ns-A response"
        node_e_a = next((n for n in nodes_a if n["entity_name"] == "EntityE"), None)
        assert node_e_a is not None, (
            f"EntityE not found in ns-A nodes: {[n['entity_name'] for n in nodes_a]}"
        )
        salience_a = node_e_a["salience"]

        # --- ns-B: query nsb-1 with key_b → IDF uses N=1 namespace collections ---
        resp_b = client.get("/graph/nsb-1?salience=tfidf", headers=_auth(key_b))
        assert resp_b.status_code == 200, (
            f"ns-B request failed: {resp_b.status_code} {resp_b.text}"
        )
        nodes_b = resp_b.json()["nodes"]
        assert nodes_b, "Expected non-empty nodes in ns-B response"
        node_e_b = next((n for n in nodes_b if n["entity_name"] == "EntityE"), None)
        assert node_e_b is not None, (
            f"EntityE not found in ns-B nodes: {[n['entity_name'] for n in nodes_b]}"
        )
        salience_b = node_e_b["salience"]

        # Verify TF is identical across namespaces (isolates IDF as the causal variable)
        assert node_e_a["chunk_count"] == node_e_b["chunk_count"] == 5, (
            f"Expected chunk_count=5 for both namespaces to isolate IDF; "
            f"got chunk_count_a={node_e_a['chunk_count']}, chunk_count_b={node_e_b['chunk_count']}"
        )

        # Absolute-value assertions: TF = 5/20 = 0.25 (5 mentions / 20 chunks)
        # ns-A: IDF = log((3+1)/3) = log(4/3); salience_A = (5/20) × log(4/3)
        # ns-B: IDF = log((1+1)/1) = log(2);   salience_B = (5/20) × log(2)
        expected_salience_a = (5 / 20) * math.log(4 / 3)
        expected_salience_b = (5 / 20) * math.log(2)
        assert math.isclose(salience_a, expected_salience_a, rel_tol=1e-6), (
            f"ns-A salience mismatch: expected {expected_salience_a:.6f} "
            f"((5/20)×log(4/3)), got {salience_a:.6f}"
        )
        assert math.isclose(salience_b, expected_salience_b, rel_tol=1e-6), (
            f"ns-B salience mismatch: expected {expected_salience_b:.6f} "
            f"((5/20)×log(2)), got {salience_b:.6f}"
        )

        # --- Assert namespace IDF isolation: salience_A < salience_B ---
        # IDF_A = log(4/3) ≈ 0.288 (df=3 across 3 collections)
        # IDF_B = log(2)   ≈ 0.693 (df=1 in a single-collection namespace)
        assert salience_a < salience_b, (
            f"Expected salience_A (ns-A, df=3, IDF≈{math.log(4/3):.4f}) "
            f"< salience_B (ns-B, df=1, IDF≈{math.log(2):.4f}), "
            f"but got salience_A={salience_a:.6f}, salience_B={salience_b:.6f}"
        )


# ---------------------------------------------------------------------------
# Test 9: unknown collection with ?salience=tfidf → 404
# ---------------------------------------------------------------------------


def test_get_graph_tfidf_unknown_collection_returns_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/nonexistent?salience=tfidf → 404.

    The collection-existence guard (routes_graph.py) fires BEFORE the tfidf
    IDF presence-fanout. A regression reordering these would turn a clean 404
    into a 500.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        response = client.get(
            "/graph/collection-does-not-exist?salience=tfidf", headers=_auth(api_key)
        )
        assert response.status_code == 404, (
            f"Expected 404 for unknown collection with tfidf, "
            f"got {response.status_code}: {response.text}"
        )


# ---------------------------------------------------------------------------
# Test 10 (unit): _cross_collection_view_to_response propagates salience_mode
# ---------------------------------------------------------------------------


def test_cross_collection_view_to_response_includes_salience_mode() -> None:
    """_cross_collection_view_to_response(view) → response.salience_mode == view.salience_mode.

    Unit test: does not require a running app or database.

    Verifies that view.salience_mode wires the response field, consistent with
    _view_to_response which also reads from view.salience_mode (not a separate parameter).
    """
    from archon_search.graph_inspector import CrossCollectionGraphView
    from archon_search.server.routes_graph import _cross_collection_view_to_response

    view = CrossCollectionGraphView(
        collections=["col-a", "col-b"],
        nodes=[],
        edges=[],
        node_count=0,
        edge_count=0,
        truncated=False,
        salience_mode="tfidf",
    )

    response = _cross_collection_view_to_response(view)

    assert response.salience_mode == "tfidf", (
        f"Expected salience_mode='tfidf' (from view.salience_mode), "
        f"got {response.salience_mode!r}"
    )


# ---------------------------------------------------------------------------
# Test 15 (integration, C1-MOD-2): cross-collection tfidf + graphml → application/xml
# ---------------------------------------------------------------------------


def test_cross_collection_route_tfidf_graphml_returns_xml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/cross-collection?salience=tfidf&format=graphml → 200 application/xml.

    Verifies that the cross-collection endpoint with both tfidf and graphml format
    parameters returns a valid XML response with Content-Type: application/xml.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        for col_name in ("col-cc-gml-a", "col-cc-gml-b"):
            col_file = tmp_path / f"{col_name}.txt"
            col_file.write_text(f"tfidf graphml cross collection test for {col_name}", encoding="utf-8")
            ingest_file_via_path(client, col_name, str(col_file), api_key=api_key)

        response = client.get(
            "/graph/cross-collection?collections=col-cc-gml-a,col-cc-gml-b"
            "&salience=tfidf&format=graphml",
            headers=_auth(api_key),
        )

        assert response.status_code == 200, (
            f"Expected 200 for cross-collection tfidf graphml, "
            f"got {response.status_code}: {response.text}"
        )
        content_type = response.headers.get("content-type", "")
        assert "application/xml" in content_type, (
            f"Expected application/xml content-type for graphml, got: {content_type!r}"
        )


# ---------------------------------------------------------------------------
# T-2 tests: Cross-collection endpoint — salience parameter route integration
# ---------------------------------------------------------------------------


def test_cross_collection_tfidf_echoes_salience_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/cross-collection?salience=tfidf → 200, salience_mode == 'tfidf'.

    Verifies the route echoes the salience mode in the cross-collection JSON body
    regardless of whether the collections contain graph data.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        for col_name in ("t2-tfidf-echo-a", "t2-tfidf-echo-b"):
            col_file = tmp_path / f"{col_name}.txt"
            col_file.write_text(f"tfidf echo test content for {col_name}", encoding="utf-8")
            ingest_file_via_path(client, col_name, str(col_file), api_key=api_key)

        response = client.get(
            "/graph/cross-collection?collections=t2-tfidf-echo-a,t2-tfidf-echo-b&salience=tfidf",
            headers=_auth(api_key),
        )

        assert response.status_code == 200, (
            f"Expected 200 for cross-collection ?salience=tfidf, "
            f"got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "salience_mode" in data, "Expected salience_mode in cross-collection response body"
        assert data["salience_mode"] == "tfidf", (
            f"Expected salience_mode='tfidf', got {data['salience_mode']!r}"
        )


def test_cross_collection_invalid_salience_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/cross-collection?salience=bm25 → 422.

    FastAPI enum validation rejects any value not in {'frequency', 'tfidf'}.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        for col_name in ("t2-inv-a", "t2-inv-b"):
            col_file = tmp_path / f"{col_name}.txt"
            col_file.write_text(f"invalid salience test for {col_name}", encoding="utf-8")
            ingest_file_via_path(client, col_name, str(col_file), api_key=api_key)

        response = client.get(
            "/graph/cross-collection?collections=t2-inv-a,t2-inv-b&salience=bm25",
            headers=_auth(api_key),
        )

        assert response.status_code == 422, (
            f"Expected 422 for ?salience=bm25 on cross-collection, "
            f"got {response.status_code}: {response.text}"
        )
        error = response.json()
        assert "detail" in error
        detail = error["detail"]
        # FastAPI enum validation produces a list of errors with location info
        if isinstance(detail, list):
            locs = [e.get("loc", []) for e in detail]
            assert any("salience" in loc for loc in locs), (
                f"Expected salience in error locs, got: {locs}"
            )
        else:
            assert "salience" in str(detail), (
                f"Expected salience in error detail, got: {detail!r}"
            )


def test_cross_collection_frequency_default_echoes_salience_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/cross-collection (no ?salience=) → 200, salience_mode == 'frequency'.

    Verifies that omitting ?salience= applies the 'frequency' default and the
    cross-collection response body echoes it.
    """
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        for col_name in ("t2-freq-def-a", "t2-freq-def-b"):
            col_file = tmp_path / f"{col_name}.txt"
            col_file.write_text(f"frequency default test for {col_name}", encoding="utf-8")
            ingest_file_via_path(client, col_name, str(col_file), api_key=api_key)

        response = client.get(
            "/graph/cross-collection?collections=t2-freq-def-a,t2-freq-def-b",
            headers=_auth(api_key),
        )

        assert response.status_code == 200, (
            f"Expected 200 for cross-collection default (no ?salience=), "
            f"got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "salience_mode" in data, "Expected salience_mode in cross-collection response body"
        assert data["salience_mode"] == "frequency", (
            f"Expected salience_mode='frequency' for default (no ?salience=), "
            f"got {data['salience_mode']!r}"
        )


def test_cross_collection_explicit_frequency_identical_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/cross-collection?salience=frequency → response body identical to omitting ?salience=.

    Both explicit and implicit frequency must produce exactly the same response for the
    cross-collection endpoint. A shared entity with mentions in both collections ensures
    the body comparison exercises actual node salience values, not just empty graphs.
    """
    from archon_search.graph_types import EntityType, make_stable_entity_id

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Seed both collections with real chunk data and a shared entity so the
        # frequency-mode node list is non-empty and the body comparison is non-vacuous.
        asyncio.run(_seed_namespace_collection(cfg.db_path, "t2-expl-freq-a", "default", chunk_count=10))
        asyncio.run(_seed_namespace_collection(cfg.db_path, "t2-expl-freq-b", "default", chunk_count=10))

        entity_id = make_stable_entity_id(EntityType.concept.value, "SharedEntity")
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "t2-expl-freq-a", entity_id, "SharedEntity",
            ["chunk-ef-a-000", "chunk-ef-a-001", "chunk-ef-a-002"],
        ))
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "t2-expl-freq-b", entity_id, "SharedEntity",
            ["chunk-ef-b-000", "chunk-ef-b-001"],
        ))

        resp_default = client.get(
            "/graph/cross-collection?collections=t2-expl-freq-a,t2-expl-freq-b",
            headers=_auth(api_key),
        )
        resp_explicit = client.get(
            "/graph/cross-collection?collections=t2-expl-freq-a,t2-expl-freq-b&salience=frequency",
            headers=_auth(api_key),
        )

        assert resp_default.status_code == 200
        assert resp_explicit.status_code == 200

        default_data = resp_default.json()
        explicit_data = resp_explicit.json()

        # Confirm nodes are non-empty so this is a real comparison, not a vacuous {} == {} check
        assert default_data.get("nodes"), (
            "Expected non-empty nodes in default response — SharedEntity seeding must have worked"
        )
        assert default_data == explicit_data, (
            "Explicit ?salience=frequency on cross-collection should produce identical "
            "body to omitting the param"
        )


def test_cross_collection_graph_disabled_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /graph/cross-collection → 422 when graph.enabled=false.

    The existing graph-disabled guard fires regardless of salience param.
    No spaCy stub is required here — create_app skips the spaCy check when
    graph.enabled=false.
    """
    # graph_enabled=False (default) — no spaCy stub needed
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        response = client.get(
            "/graph/cross-collection?collections=any-a,any-b",
            headers=_auth(api_key),
        )

        assert response.status_code == 422, (
            f"Expected 422 when graph disabled, got {response.status_code}: {response.text}"
        )
        assert "graph inspection requires [graph] enabled=true" in response.json()["detail"], (
            f"Expected graph-disabled error message, got: {response.json()['detail']!r}"
        )


def test_cross_collection_tfidf_idf_denominator_is_all_namespace_collections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDF denominator uses ALL 4 namespace collections, verified via absolute salience value.

    Setup: 4-collection namespace (A, B, C, D). Only A and B are listed in ?collections=.
      - Entity X: present in all 4 collections (df=4)
          IDF(X) = log((4+1)/4) = log(5/4) ≈ 0.223
      - Entity Y: present ONLY in collection A (df=1)
          IDF(Y) = log((4+1)/1) = log(5) ≈ 1.609

    Under tfidf entity Y's salience must exceed entity X's salience because
    IDF(Y) >> IDF(X).

    Absolute salience assertion for Y (pins the N=4 formula):
      chunk_count(Y in A) = 3, total_chunks(A) = 20
      TF(Y) = 3/20 = 0.15
      expected_salience_Y = (3/20) × log(5)

    If the handler incorrectly used only the 2 listed collections for the IDF denominator
    (N=2 bug):
      df(Y) in listed only = 1  → IDF(Y) = log(3/1) = log(3) ≈ 1.099
      wrong_salience_Y = (3/20) × log(3) ≈ 0.165

    The two values (≈0.241 vs ≈0.165) are far enough apart that rel_tol=1e-6 catches the bug.
    """
    from archon_search.graph_types import EntityType, make_stable_entity_id

    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Create 4 collections with precise chunk counts (default namespace)
        # A: 20 chunks (TF denominator for Y and X)
        # B: 5 chunks (listed, no Y → contributes to X df and N count)
        # C: 1 chunk (unlisted; contributes to N and X df)
        # D: 1 chunk (unlisted; contributes to N and X df)
        for col, count in [
            ("t2-idf-a", 20),
            ("t2-idf-b", 5),
            ("t2-idf-c", 1),
            ("t2-idf-d", 1),
        ]:
            asyncio.run(_seed_namespace_collection(cfg.db_path, col, "default", chunk_count=count))

        entity_x_id = make_stable_entity_id(EntityType.concept.value, "EntityX4")
        entity_y_id = make_stable_entity_id(EntityType.concept.value, "EntityY4")

        # Entity X: 10 mentions in A (high chunk_count to beat Y in frequency mode),
        # 1 mention each in B, C, D (to achieve df=4 across all namespace collections)
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "t2-idf-a", entity_x_id, "EntityX4",
            [f"chunk-x4-a-{i:06d}" for i in range(10)],
        ))
        for col in ("t2-idf-b", "t2-idf-c", "t2-idf-d"):
            asyncio.run(_seed_node_with_mentions(
                cfg.db_path, col, entity_x_id, "EntityX4",
                [f"chunk-x4-{col}-000000"],
            ))

        # Entity Y: 3 mentions only in A (df=1 across all namespace collections)
        asyncio.run(_seed_node_with_mentions(
            cfg.db_path, "t2-idf-a", entity_y_id, "EntityY4",
            [f"chunk-y4-{i:06d}" for i in range(3)],
        ))

        # Request only A and B; C and D are unlisted but still in the namespace
        response = client.get(
            "/graph/cross-collection?collections=t2-idf-a,t2-idf-b&salience=tfidf",
            headers=_auth(api_key),
        )

        assert response.status_code == 200, (
            f"Expected 200 for cross-collection tfidf (4-collection namespace), "
            f"got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert data["salience_mode"] == "tfidf", (
            f"Expected salience_mode='tfidf', got {data['salience_mode']!r}"
        )

        nodes = data["nodes"]
        assert nodes, "Expected non-empty node list"

        node_y = next((n for n in nodes if n["entity_name"] == "EntityY4"), None)
        node_x = next((n for n in nodes if n["entity_name"] == "EntityX4"), None)
        assert node_y is not None, (
            f"EntityY4 not found in nodes: {[n['entity_name'] for n in nodes]}"
        )
        assert node_x is not None, (
            f"EntityX4 not found in nodes: {[n['entity_name'] for n in nodes]}"
        )

        # Ranking assertion: Y (unique → high IDF) must outrank X (ubiquitous → low IDF)
        node_names = [n["entity_name"] for n in nodes]
        assert node_names.index("EntityY4") < node_names.index("EntityX4"), (
            f"Expected EntityY4 (df=1, IDF=log(5)≈{math.log(5):.4f}) to rank above "
            f"EntityX4 (df=4, IDF=log(5/4)≈{math.log(5/4):.4f}); "
            f"actual order: {node_names}"
        )

        # Absolute salience assertion for EntityY4 (pins N=4 denominator):
        #   chunk_count(Y in A) = 3, total_chunks(A) = 20 → TF = 3/20
        #   IDF(Y) = log((4+1)/1) = log(5)  [N=4 all-namespace denominator]
        #   expected_salience_Y = (3/20) × log(5)
        #
        # If N=2 (only listed collections) was incorrectly used:
        #   df(Y in listed) = 1 → IDF(Y) = log(3/1) = log(3)
        #   wrong_salience_Y = (3/20) × log(3) ≈ 0.165  (differs from ≈0.241)
        expected_salience_y = (3 / 20) * math.log(5)
        actual_salience_y = node_y["salience"]
        assert math.isclose(actual_salience_y, expected_salience_y, rel_tol=1e-6), (
            f"EntityY4 salience mismatch — expected (3/20)×log(5) = {expected_salience_y:.6f} "
            f"(N=4 all-namespace), got {actual_salience_y:.6f}. "
            f"A value near {(3/20)*math.log(3):.6f} indicates N=2 was used (only listed "
            f"collections counted instead of all 4 namespace collections)."
        )
