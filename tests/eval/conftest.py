"""Pytest fixtures and CLI options for the eval slice.

This module:

- Registers the ``--thresholds-path`` pytest CLI option used by +
  gated smoke tests.
- Provides module-scoped fixtures for the eval corpus and a temporary
  LanceDB root.
- Activates the deterministic eval backends from
  :mod:`archon_search.eval.backends` for every eval test so the suite never
  needs to download real embedding or reranker models.
- Generates the three-page PDF eval corpus fixture (Task 5.2, C3b) at
  session start so corpus contract tests and the eval runner both find it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
from archon_search.eval.fixtures import EvalCorpus, load_eval_corpus


CORPUS_ROOT = Path(__file__).resolve().parent

_EVAL_PDF_PATH = CORPUS_ROOT / "corpus" / "pdf-fixtures" / "three_page.pdf"

# Ensure tests/_pdf_fixture.py is importable from within the eval conftest.
_TESTS_DIR = CORPUS_ROOT.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# PDF corpus fixture (autouse, session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _generate_eval_corpus_pdf() -> None:
    """Generate tests/eval/corpus/pdf-fixtures/three_page.pdf before any test.

    Session-scoped and autouse so the PDF exists before corpus contract tests
    (tests/eval/test_corpus_contract.py) call load_eval_corpus() and before
    the eval runner ingests the corpus.  Reuses the shared generator from
    tests/_pdf_fixture.py so the textual content is identical across fixture
    copies.

    Under xdist, multiple workers run this session fixture concurrently. To
    avoid a race where one worker truncates the file while another reads it for
    compute_eval_hash, we write to a temp file and rename atomically.  If the
    file already exists (committed to git or previously generated), we skip
    regeneration entirely — the PDF is byte-deterministic so the committed copy
    is identical to what generate_three_page_pdf would produce.
    """
    if _EVAL_PDF_PATH.exists():
        return  # already present; skip to avoid concurrent-write race with xdist workers

    from _pdf_fixture import generate_three_page_pdf  # noqa: PLC0415

    import tempfile  # noqa: PLC0415

    _EVAL_PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=_EVAL_PDF_PATH.parent, suffix=".pdf.tmp", delete=False
    ) as tmp:
        tmp_path_obj = Path(tmp.name)
    try:
        generate_three_page_pdf(tmp_path_obj)
        # Atomic rename: only one writer wins; others see a complete file.
        tmp_path_obj.replace(_EVAL_PDF_PATH)
    except Exception:
        tmp_path_obj.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# --thresholds-path CLI option
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--thresholds-path",
        action="store",
        default=None,
        help=(
            "Path to thresholds.toml for gated eval smoke tests. "
            "Must be passed explicitly by CI; not auto-discovered."
        ),
    )


@pytest.fixture(scope="session")
def thresholds_path(pytestconfig: pytest.Config) -> Path:
    """Return the path passed via ``--thresholds-path``.

    - In CI (``CI`` env var set): ``pytest.fail`` so misconfiguration is loud.
    - Locally: ``pytest.skip`` with guidance.
    """
    raw = pytestconfig.getoption("--thresholds-path")
    if raw is None:
        if os.environ.get("CI"):
            pytest.fail(
                "thresholds-path not provided in CI — pass --thresholds-path explicitly"
            )
        pytest.skip(
            "thresholds-path not provided; use -k 'not gated' for report-only mode "
            "or pass --thresholds-path for gated mode."
        )
    return Path(raw)


# ---------------------------------------------------------------------------
# Corpus + LanceDB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def eval_corpus() -> EvalCorpus:
    """Load the committed eval corpus once per module."""
    return load_eval_corpus(CORPUS_ROOT)


@pytest.fixture(scope="module")
def eval_tmp_lancedb_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-scoped temporary directory for a fresh LanceDB store."""
    return tmp_path_factory.mktemp("eval_lancedb")


# ---------------------------------------------------------------------------
# Deterministic backend activation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _activate_deterministic_eval_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate deterministic eval backends for every eval test.

    Exposes the backend instances via env-var sentinels so callers that build
    pipelines can pick them up without importing test code, and also installs
    them on a shared registry attribute used by the harness when present.
    """
    embedder = EvalEmbedderBackend()
    reranker = EvalRerankerBackend()

    monkeypatch.setenv("ARCHON_SEARCH_EVAL_BACKENDS", "1")

    # Best-effort: if the eval backends module exposes a registry hook, set it.
    import archon_search.eval.backends as backends_mod

    monkeypatch.setattr(
        backends_mod, "_ACTIVE_EMBEDDER", embedder, raising=False
    )
    monkeypatch.setattr(
        backends_mod, "_ACTIVE_RERANKER", reranker, raising=False
    )


# ---------------------------------------------------------------------------
# Community builder fixture for multi-hop eval collections (BE-6)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def build_communities_for_eval(eval_tmp_lancedb_root: Path):
    """Build communities for multi-hop eval collections in the shared LanceDB store.

    Skips if leidenalg is not installed (graph extras absent).

    Pre-builds communities for multi-hop collections by:
    1. Ingesting documents from corpus/multihop-musique and corpus/multihop-2wiki
    2. Running graph extraction (via spaCy stub)
    3. Building communities via CommunityBuilder with seed=42

    Module-scoped so it runs once per test module before any test that imports it.

    Yields: (lancedb_root, dict[collection_name, GraphStore])
    """
    import asyncio
    import hashlib

    pytest.importorskip("leidenalg")  # Skip if graph extras absent

    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    async def _build_communities():
        # Create GraphStore for the eval temp directory
        graph_store = GraphStore(db_path=str(eval_tmp_lancedb_root))
        await graph_store.connect()

        try:
            ns = "default"
            graph_stores = {}  # collection_name -> GraphStore

            # Process each multihop collection
            for collection_name in ["multihop-musique", "multihop-2wiki"]:
                corpus_dir = CORPUS_ROOT / "corpus" / collection_name
                if not corpus_dir.exists():
                    continue

                # Ensure graph tables exist
                await graph_store.ensure_graph_tables(collection_name, ns=ns)

                # Ingest documents from corpus
                all_nodes = {}  # entity_id -> node (dedup across documents)
                all_edges = []
                for txt_file in sorted(corpus_dir.glob("*.txt")):
                    text = txt_file.read_text(encoding="utf-8")
                    # Compute source_doc_id using the same SHA-256 hash that
                    # pipeline.ingest_file uses (line 377 of pipeline.py), so
                    # CommunityBuilder.get_chunks_for_doc finds the right chunks.
                    source_doc_id = hashlib.sha256(
                        str(txt_file.resolve()).encode()
                    ).hexdigest()
                    # Extract entity names from text (basic approach: capitalized words)
                    words = text.split()
                    entity_names = [
                        w.rstrip(":.,'\"") for w in words if w and w[0].isupper()
                    ]

                    # Create graph nodes for entities found in the text
                    doc_nodes = []
                    for entity_name in entity_names[:5]:  # limit to 5 per document for speed
                        entity_id = make_stable_entity_id("concept", entity_name)
                        if entity_id not in all_nodes:
                            node = GraphNode(
                                id=entity_id,
                                entity_name=entity_name,
                                entity_type=EntityType.concept,
                                source_doc_id=source_doc_id,
                                collection_name=collection_name,
                            )
                            all_nodes[entity_id] = node
                        doc_nodes.append(all_nodes[entity_id])

                    # Create some edges between consecutive nodes
                    for i in range(len(doc_nodes) - 1):
                        edge = GraphEdge(
                            id=make_stable_edge_id(
                                doc_nodes[i].id, doc_nodes[i + 1].id, "related_to"
                            ),
                            source_node_id=doc_nodes[i].id,
                            target_node_id=doc_nodes[i + 1].id,
                            relationship_type=RelationshipType.related_to,
                            source_doc_id=source_doc_id,
                        )
                        all_edges.append(edge)

                # Write all collected nodes and edges to graph store
                if all_nodes:
                    await graph_store.write_graph(
                        collection_name, list(all_nodes.values()), all_edges, ns=ns
                    )

                # NOTE: Communities are NOT built here. Building requires chunks in the
                # search_store (for MMR representative selection), but chunks are only
                # available after _ingest_corpus runs inside run_eval_suite. Community
                # building is deferred to run_eval_suite, after _ingest_corpus completes.

                graph_stores[collection_name] = graph_store

            return eval_tmp_lancedb_root, graph_stores, graph_store

        except Exception:
            await graph_store.disconnect()
            raise

    # Run async code via asyncio.run (synchronous wrapper)
    lancedb_root, graph_stores, graph_store = asyncio.run(_build_communities())

    yield lancedb_root, graph_stores

    # Cleanup
    try:
        asyncio.run(graph_store.disconnect())
    except Exception:
        pass
