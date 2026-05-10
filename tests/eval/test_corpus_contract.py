"""Task 1.3 — contract tests for the committed synthetic eval corpus.

These tests verify that the data files under tests/eval/ satisfy the
FEAT-039 corpus requirements before any live search infrastructure is needed.
"""
from __future__ import annotations

import json
from pathlib import Path

from archon_search.eval.fixtures import load_eval_corpus

EVAL_DIR = Path(__file__).parent
CORPUS_ROOT = EVAL_DIR  # documents.jsonl, queries.jsonl, labels.jsonl live here


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_eval_corpus_document_count_range() -> None:
    """Corpus must contain between 50 and 100 documents (inclusive)."""
    corpus = load_eval_corpus(CORPUS_ROOT)
    count = len(corpus.documents)
    assert 50 <= count <= 100, (
        f"Expected 50–100 documents, got {count}. "
        "Add or remove documents to satisfy the FEAT-039 corpus size requirement."
    )


def test_eval_corpus_query_count_range() -> None:
    """Corpus must contain between 25 and 30 queries (inclusive)."""
    corpus = load_eval_corpus(CORPUS_ROOT)
    count = len(corpus.queries)
    assert 25 <= count <= 30, (
        f"Expected 25–30 queries, got {count}. "
        "Add or remove queries to satisfy the FEAT-039 benchmark size requirement."
    )


def test_every_query_has_positive_relevance_label() -> None:
    """Every query must have at least one label with grade > 0.

    load_eval_corpus() already enforces this — but this test makes the
    contract explicit and gives a cleaner failure message.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)
    positives: dict[str, int] = {q.query_id: 0 for q in corpus.queries}
    for lbl in corpus.labels:
        if lbl.grade > 0:
            positives[lbl.query_id] = positives.get(lbl.query_id, 0) + 1

    missing = [qid for qid, cnt in positives.items() if cnt == 0]
    assert not missing, (
        f"Queries missing positive labels: {missing}. "
        "Each query must have at least one grade > 0 label."
    )


def test_positive_relevance_labels_are_reachable_from_query_collection() -> None:
    """For retrieval-scope queries, every positive label must be in the query's collection.

    This mirrors the loader validation but makes corpus-level reachability explicit.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)
    doc_col = {doc.doc_id: doc.collection for doc in corpus.documents}
    query_col = {q.query_id: q.collection for q in corpus.queries}
    query_scope = {q.query_id: q.metric_scope for q in corpus.queries}

    violations = []
    for lbl in corpus.labels:
        if lbl.grade > 0 and query_scope.get(lbl.query_id) == "retrieval":
            expected = query_col.get(lbl.query_id)
            actual = doc_col.get(lbl.doc_id)
            if expected is not None and actual != expected:
                violations.append(
                    f"query={lbl.query_id!r} expects collection={expected!r} "
                    f"but doc={lbl.doc_id!r} is in {actual!r}"
                )

    assert not violations, "Unreachable positive labels:\n" + "\n".join(violations)


def test_collections_cover_multiple_domains() -> None:
    """Corpus must span at least 2 distinct collection names."""
    corpus = load_eval_corpus(CORPUS_ROOT)
    collections = {doc.collection for doc in corpus.documents}
    assert len(collections) >= 2, (
        f"Only {len(collections)} collection(s) found: {collections}. "
        "At least 2 distinct collections are required (e.g. 'code', 'docs', 'mixed')."
    )


def test_manifest_doc_ids_are_stable_and_unique() -> None:
    """Every document in documents.jsonl must have a unique, stable doc_id.

    Stability means the id is an explicit opaque string, not derived from a
    path component that might change.  Uniqueness is enforced by load_eval_corpus()
    but we also check against the raw JSONL to catch any pre-load issues.
    """
    rows = _read_jsonl(CORPUS_ROOT / "documents.jsonl")
    ids = [r["doc_id"] for r in rows]
    assert len(ids) == len(set(ids)), (
        f"Duplicate doc_ids found in documents.jsonl: "
        f"{[x for x in ids if ids.count(x) > 1]}"
    )
    # Every id must be a non-empty string
    empties = [r for r in rows if not r.get("doc_id")]
    assert not empties, f"Empty or missing doc_id in {len(empties)} row(s)"

    # Load through the fixture to confirm uniqueness survives the full validation
    corpus = load_eval_corpus(CORPUS_ROOT)
    loaded_ids = [doc.doc_id for doc in corpus.documents]
    assert len(loaded_ids) == len(set(loaded_ids))


def test_routing_collections_manifest_exists() -> None:
    """routing/collections.jsonl must exist and list at least the corpus collections."""
    collections_file = CORPUS_ROOT / "routing" / "collections.jsonl"
    assert collections_file.exists(), (
        f"routing/collections.jsonl not found at {collections_file}. "
        "This file must list at least the collections used in the corpus."
    )
    rows = _read_jsonl(collections_file)
    assert len(rows) >= 2, (
        f"routing/collections.jsonl has only {len(rows)} entry/entries; "
        "it must list at least 2 collections."
    )
    for row in rows:
        assert "name" in row, f"Missing 'name' field in routing/collections.jsonl row: {row}"
