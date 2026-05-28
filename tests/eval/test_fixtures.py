"""Tests for eval fixture dataclasses ."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.eval.fixtures import (
    EvalCorpus,
    EvalDocument,
    EvalQuery,
    RelevanceLabel,
    build_doc_collection_map,
    load_eval_corpus,
)


# ---------------------------------------------------------------------------
# RelevanceLabel
# ---------------------------------------------------------------------------


def test_relevance_label_default_grade() -> None:
    label = RelevanceLabel(query_id="q1", doc_id="d1")
    assert label.grade == 1


def test_relevance_label_rejects_negative_grade() -> None:
    with pytest.raises(ValueError):
        RelevanceLabel(query_id="q1", doc_id="d1", grade=-1)


# ---------------------------------------------------------------------------
# EvalDocument
# ---------------------------------------------------------------------------


def test_eval_document_requires_relative_path() -> None:
    doc = EvalDocument(doc_id="doc-001", relative_path="corpus/intro.md")
    assert doc.doc_id == "doc-001"
    assert doc.relative_path == "corpus/intro.md"


# ---------------------------------------------------------------------------
# EvalQuery
# ---------------------------------------------------------------------------


def test_eval_query_supports_optional_collection() -> None:
    q = EvalQuery(
        query_id="q1",
        text="What is X?",
        collection=None,
        metric_scope="routing",
    )
    assert q.collection is None


def test_eval_query_collection_none_is_routing_only() -> None:
    q = EvalQuery(
        query_id="q1",
        text="What is X?",
        collection=None,
        metric_scope="routing",
    )
    assert q.metric_scope == "routing"


def test_eval_query_metric_scope_separates_retrieval_from_routing() -> None:
    retrieval_q = EvalQuery(
        query_id="q-ret",
        text="Find doc about Y",
        collection="docs",
        metric_scope="retrieval",
    )
    routing_q = EvalQuery(
        query_id="q-rout",
        text="General question",
        collection=None,
        metric_scope="routing",
    )
    assert retrieval_q.metric_scope == "retrieval"
    assert retrieval_q.collection == "docs"
    assert routing_q.metric_scope == "routing"
    assert routing_q.collection is None


def test_eval_query_rejects_invalid_metric_scope() -> None:
    with pytest.raises(ValueError):
        EvalQuery(
            query_id="q1",
            text="Some query",
            collection=None,
            metric_scope="unknown",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# EvalCorpus
# ---------------------------------------------------------------------------


def test_eval_corpus_holds_documents_and_queries() -> None:
    docs = [EvalDocument(doc_id="d1", relative_path="corpus/a.md")]
    queries = [
        EvalQuery(
            query_id="q1",
            text="What is A?",
            collection="col",
            metric_scope="retrieval",
        )
    ]
    corpus = EvalCorpus(documents=docs, queries=queries)
    assert len(corpus.documents) == 1
    assert len(corpus.queries) == 1


# ---------------------------------------------------------------------------
# Helpers for building minimal valid fixture directories
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _make_valid_fixture(
    root: Path,
    *,
    doc_id: str = "doc-1",
    collection: str = "col1",
    filename: str = "a.md",
    query_id: str = "q1",
    query_text: str = "What is A?",
    metric_scope: str = "retrieval",
    grade: int | None = None,
) -> None:
    """Write a minimal valid fixture directory under *root*."""
    corpus_dir = root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / filename).write_text("content")

    _write_jsonl(
        root / "documents.jsonl",
        [{"doc_id": doc_id, "collection": collection, "relative_path": filename}],
    )
    _write_jsonl(
        root / "queries.jsonl",
        [
            {
                "query_id": query_id,
                "text": query_text,
                "collection": collection,
                "metric_scope": metric_scope,
            }
        ],
    )
    label_row: dict = {"query_id": query_id, "doc_id": doc_id}
    if grade is not None:
        label_row["grade"] = grade
    _write_jsonl(root / "labels.jsonl", [label_row])


# ---------------------------------------------------------------------------
# load_eval_corpus — happy path
# ---------------------------------------------------------------------------


def test_load_eval_corpus_reads_documents_queries_and_labels(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    corpus = load_eval_corpus(tmp_path)

    assert len(corpus.documents) == 1
    assert corpus.documents[0].doc_id == "doc-1"
    assert len(corpus.queries) == 1
    assert corpus.queries[0].query_id == "q1"
    assert len(corpus.labels) == 1
    assert corpus.labels[0].grade == 1  # default grade normalisation


# ---------------------------------------------------------------------------
# load_eval_corpus — validation failures
# ---------------------------------------------------------------------------


def test_load_eval_corpus_rejects_unknown_label_doc_id(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    # Overwrite labels with an unknown doc_id
    _write_jsonl(tmp_path / "labels.jsonl", [{"query_id": "q1", "doc_id": "ghost"}])
    with pytest.raises(ValueError, match="Unknown doc_id"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_duplicate_document_ids(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    docs = [
        {"doc_id": "doc-1", "collection": "col1", "relative_path": "a.md"},
        {"doc_id": "doc-1", "collection": "col1", "relative_path": "b.md"},
    ]
    (tmp_path / "corpus" / "b.md").write_text("content b")
    _write_jsonl(tmp_path / "documents.jsonl", docs)
    with pytest.raises(ValueError, match="Duplicate doc_id"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_duplicate_query_ids(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    queries = [
        {"query_id": "q1", "text": "Q1", "collection": "col1", "metric_scope": "retrieval"},
        {"query_id": "q1", "text": "Q1 dup", "collection": "col1", "metric_scope": "retrieval"},
    ]
    _write_jsonl(tmp_path / "queries.jsonl", queries)
    with pytest.raises(ValueError, match="Duplicate query_id"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_unknown_query_id_in_labels(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    _write_jsonl(tmp_path / "labels.jsonl", [{"query_id": "ghost", "doc_id": "doc-1"}])
    with pytest.raises(ValueError, match="Unknown query_id"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_query_without_positive_label(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path, grade=0)
    with pytest.raises(ValueError, match="no positive"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_positive_label_outside_query_collection(
    tmp_path: Path,
) -> None:
    """A retrieval query pointing at collection col1 must not have a positive label
    from a document in a different collection."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "a.md").write_text("content a")
    (corpus_dir / "b.md").write_text("content b")

    _write_jsonl(
        tmp_path / "documents.jsonl",
        [
            {"doc_id": "doc-a", "collection": "col1", "relative_path": "a.md"},
            {"doc_id": "doc-b", "collection": "col2", "relative_path": "b.md"},
        ],
    )
    _write_jsonl(
        tmp_path / "queries.jsonl",
        [{"query_id": "q1", "text": "Q", "collection": "col1", "metric_scope": "retrieval"}],
    )
    # doc-b is in col2 but the query targets col1 → unreachable positive
    _write_jsonl(tmp_path / "labels.jsonl", [{"query_id": "q1", "doc_id": "doc-b", "grade": 1}])
    with pytest.raises(ValueError, match="unreachable"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_missing_manifest_file(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    # Remove the corpus file referenced by documents.jsonl
    (tmp_path / "corpus" / "a.md").unlink()
    with pytest.raises(ValueError, match="Missing corpus file"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_invalid_collection_name(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "a.md").write_text("content")
    _write_jsonl(
        tmp_path / "documents.jsonl",
        [{"doc_id": "doc-1", "collection": "invalid name!", "relative_path": "a.md"}],
    )
    _write_jsonl(
        tmp_path / "queries.jsonl",
        [
            {
                "query_id": "q1",
                "text": "Q",
                "collection": "invalid name!",
                "metric_scope": "retrieval",
            }
        ],
    )
    _write_jsonl(tmp_path / "labels.jsonl", [{"query_id": "q1", "doc_id": "doc-1"}])
    with pytest.raises(ValueError, match="Invalid collection name"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_path_escape(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _write_jsonl(
        tmp_path / "documents.jsonl",
        [{"doc_id": "doc-1", "collection": "col1", "relative_path": "../escape.md"}],
    )
    _write_jsonl(
        tmp_path / "queries.jsonl",
        [{"query_id": "q1", "text": "Q", "collection": "col1", "metric_scope": "retrieval"}],
    )
    _write_jsonl(tmp_path / "labels.jsonl", [{"query_id": "q1", "doc_id": "doc-1"}])
    with pytest.raises(ValueError, match="path traversal"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_duplicate_relative_path(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "a.md").write_text("content")
    docs = [
        {"doc_id": "doc-1", "collection": "col1", "relative_path": "a.md"},
        {"doc_id": "doc-2", "collection": "col1", "relative_path": "a.md"},
    ]
    _write_jsonl(tmp_path / "documents.jsonl", docs)
    _write_jsonl(
        tmp_path / "queries.jsonl",
        [{"query_id": "q1", "text": "Q", "collection": "col1", "metric_scope": "retrieval"}],
    )
    _write_jsonl(
        tmp_path / "labels.jsonl",
        [{"query_id": "q1", "doc_id": "doc-1"}],
    )
    with pytest.raises(ValueError, match="Duplicate relative_path"):
        load_eval_corpus(tmp_path)


def test_load_eval_corpus_rejects_orphan_corpus_files(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path)
    # Add an extra file not referenced by documents.jsonl
    (tmp_path / "corpus" / "orphan.md").write_text("orphan content")
    with pytest.raises(ValueError, match="[Oo]rphan"):
        load_eval_corpus(tmp_path)


# ---------------------------------------------------------------------------
# Runtime path → fixture doc_id mapping
# ---------------------------------------------------------------------------


def test_runtime_doc_ids_map_to_fixture_doc_ids(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path, doc_id="doc-1", filename="a.md")
    corpus = load_eval_corpus(tmp_path)

    # The corpus/ subdirectory is the root for relative paths
    runtime_path = tmp_path / "corpus" / "a.md"
    doc_map = build_doc_collection_map(corpus)
    # relative_path "a.md" maps to doc-1
    assert "a.md" in doc_map
    assert doc_map["a.md"] == ("doc-1", "col1")

    _ = runtime_path  # used for conceptual clarity


# ---------------------------------------------------------------------------
# build_doc_collection_map
# ---------------------------------------------------------------------------


def test_build_doc_collection_map_returns_correct_mapping(tmp_path: Path) -> None:
    _make_valid_fixture(tmp_path, doc_id="doc-1", collection="col1", filename="a.md")
    corpus = load_eval_corpus(tmp_path)
    mapping = build_doc_collection_map(corpus)
    assert mapping == {"a.md": ("doc-1", "col1")}


def test_build_doc_collection_map_with_multiple_collections(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "a.md").write_text("a")
    (corpus_dir / "b.md").write_text("b")

    _write_jsonl(
        tmp_path / "documents.jsonl",
        [
            {"doc_id": "doc-a", "collection": "col1", "relative_path": "a.md"},
            {"doc_id": "doc-b", "collection": "col2", "relative_path": "b.md"},
        ],
    )
    _write_jsonl(
        tmp_path / "queries.jsonl",
        [
            {"query_id": "q1", "text": "Q1", "collection": "col1", "metric_scope": "retrieval"},
            {"query_id": "q2", "text": "Q2", "collection": "col2", "metric_scope": "retrieval"},
        ],
    )
    _write_jsonl(
        tmp_path / "labels.jsonl",
        [
            {"query_id": "q1", "doc_id": "doc-a"},
            {"query_id": "q2", "doc_id": "doc-b"},
        ],
    )
    corpus = load_eval_corpus(tmp_path)
    mapping = build_doc_collection_map(corpus)
    assert mapping == {
        "a.md": ("doc-a", "col1"),
        "b.md": ("doc-b", "col2"),
    }


def test_build_doc_collection_map_with_empty_corpus() -> None:
    corpus = EvalCorpus(documents=[], queries=[], labels=[])
    assert build_doc_collection_map(corpus) == {}


# ---------------------------------------------------------------------------
# B4 Task 1.3 — FAQ collection fixture expansion
# ---------------------------------------------------------------------------

_EVAL_DIR = Path(__file__).parent
_CORPUS_ROOT = _EVAL_DIR


def test_fixture_faq_collection_in_routing_collections() -> None:
    """routing/collections.jsonl must contain a collection named 'faq'."""
    import json
    cols_file = _CORPUS_ROOT / "routing" / "collections.jsonl"
    assert cols_file.exists(), f"routing/collections.jsonl not found at {cols_file}"
    names = [json.loads(line)["name"] for line in cols_file.read_text().splitlines() if line.strip()]
    assert "faq" in names, f"'faq' not found in routing collections: {names}"


def test_fixture_faq_documents_present() -> None:
    """documents.jsonl must have at least 5 entries with collection == 'faq'."""
    import json
    docs_file = _CORPUS_ROOT / "documents.jsonl"
    faq_docs = [
        json.loads(line)
        for line in docs_file.read_text().splitlines()
        if line.strip() and json.loads(line).get("collection") == "faq"
    ]
    assert len(faq_docs) >= 5, f"Expected ≥5 faq documents, got {len(faq_docs)}"


def test_fixture_routing_queries_expanded() -> None:
    """queries.jsonl must have ≥6 routing-scope entries after B4 expansion."""
    import json
    queries_file = _CORPUS_ROOT / "queries.jsonl"
    routing = [
        json.loads(line)
        for line in queries_file.read_text().splitlines()
        if line.strip() and json.loads(line).get("metric_scope") == "routing"
    ]
    assert len(routing) >= 6, f"Expected ≥6 routing queries, got {len(routing)}"


def test_fixture_all_routing_collections_have_scorable_centroids() -> None:
    """Every collection in routing/collections.jsonl must have a non-None, non-zero centroid.

    Computes centroids via the deterministic SHA-256 eval embedder over corpus files.
    This is the precondition that ensures no unscored-fallback entries contaminate
    the ranked list during eval runs.
    """
    import json
    import math
    from archon_search.eval.backends import EvalEmbedderBackend

    cols_file = _CORPUS_ROOT / "routing" / "collections.jsonl"
    collections = [json.loads(l) for l in cols_file.read_text().splitlines() if l.strip()]
    embedder = EvalEmbedderBackend()

    for col in collections:
        name = col["name"]
        corpus_dir = _CORPUS_ROOT / "corpus" / name
        assert corpus_dir.exists(), f"corpus/{name}/ directory missing"
        files = list(corpus_dir.iterdir())
        assert files, f"corpus/{name}/ has no files"

        texts = [f.read_text(encoding="utf-8") for f in files]
        vecs = embedder.encode(texts)

        dim = len(vecs[0])
        centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
        norm = math.sqrt(sum(c * c for c in centroid))
        assert norm > 0, f"collection '{name}' has a zero-norm centroid"


def test_fixture_faq_vocab_distinct_from_other_collections() -> None:
    """FAQ queries must have higher centroid similarity to 'faq' than to 'code' or 'docs'.

    Also verifies the FAQ description has higher cosine similarity to the FAQ
    corpus centroid than to 'code'/'docs' corpus centroids.
    Uses the SHA-256 bag-of-words deterministic embedder.
    """
    import json
    import math
    from archon_search.eval.backends import EvalEmbedderBackend

    embedder = EvalEmbedderBackend()

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _centroid(texts: list[str]) -> list[float]:
        vecs = embedder.encode(texts)
        dim = len(vecs[0])
        c = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
        return c

    def _corpus_texts(name: str) -> list[str]:
        corpus_dir = _CORPUS_ROOT / "corpus" / name
        return [f.read_text(encoding="utf-8") for f in sorted(corpus_dir.iterdir())]

    faq_centroid = _centroid(_corpus_texts("faq"))
    code_centroid = _centroid(_corpus_texts("code"))
    docs_centroid = _centroid(_corpus_texts("docs"))

    # Get FAQ routing queries
    queries_file = _CORPUS_ROOT / "queries.jsonl"
    routing_queries = [
        json.loads(l)
        for l in queries_file.read_text().splitlines()
        if l.strip()
    ]
    faq_queries = [q["text"] for q in routing_queries if q.get("query_id") in {"q-route-03", "q-route-04"}]
    assert faq_queries, "No FAQ routing queries (q-route-03, q-route-04) found"

    # Note: docs/troubleshooting.md contains "troubleshoot" which overlaps with FAQ
    # vocabulary. Under the bag-of-words SHA-256 embedder, centroid-level similarity
    # is sufficient to discriminate because the FAQ corpus has many more troubleshoot-
    # related tokens spread across 5 files vs the single mention in docs. This is
    # accepted risk; production-model validation belongs to B6.
    for qtext in faq_queries:
        qvec = embedder.encode([qtext])[0]
        sim_faq = _cosine(qvec, faq_centroid)
        sim_code = _cosine(qvec, code_centroid)
        sim_docs = _cosine(qvec, docs_centroid)
        assert sim_faq > sim_code, (
            f"FAQ query '{qtext}': sim_faq={sim_faq:.4f} not > sim_code={sim_code:.4f}"
        )
        assert sim_faq > sim_docs, (
            f"FAQ query '{qtext}': sim_faq={sim_faq:.4f} not > sim_docs={sim_docs:.4f}"
        )

    # Verify FAQ description aligns more with FAQ centroid than code/docs
    cols_file = _CORPUS_ROOT / "routing" / "collections.jsonl"
    faq_col = next(
        json.loads(l) for l in cols_file.read_text().splitlines()
        if l.strip() and json.loads(l).get("name") == "faq"
    )
    desc = faq_col.get("description", "")
    assert desc, "FAQ collection must have a 'description' field"
    desc_vec = embedder.encode([desc])[0]
    assert _cosine(desc_vec, faq_centroid) > _cosine(desc_vec, code_centroid), (
        "FAQ description not more similar to faq centroid than code centroid"
    )
    assert _cosine(desc_vec, faq_centroid) > _cosine(desc_vec, docs_centroid), (
        "FAQ description not more similar to faq centroid than docs centroid"
    )
