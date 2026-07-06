""" contract tests for the committed synthetic eval corpus.

These tests verify that the data files under tests/eval/ satisfy the
 corpus requirements before any live search infrastructure is needed.
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
    """Corpus must contain between 50 and 200 documents (inclusive).

    Upper bound raised to 200 in E2e BE-9 to accommodate 100 new HotpotQA
    distractor corpus documents for the negative control eval gate.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)
    count = len(corpus.documents)
    assert 50 <= count <= 200, (
        f"Expected 50–200 documents, got {count}. "
        "Add or remove documents to satisfy the corpus size requirement."
    )


def test_eval_corpus_query_count_range() -> None:
    """Corpus must contain between 25 and 150 queries (inclusive).

    Upper bound raised from 30 to 40 in B4 to accommodate 4 new routing
    queries added for the expanded routing fixture (Task 1.3).
    Upper bound raised from 40 to 45 in E1a BE-9 to accommodate 2 new
    graph-mode retrieval queries.
    Upper bound raised from 45 to 55 in E2e BE-7 to accommodate 2 new
    multihop-2wiki queries (plus future BE-9 HotpotQA queries).
    Upper bound raised from 55 to 150 in E2e BE-9 to accommodate 100 new
    HotpotQA distractor-setting queries for the negative control eval gate.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)
    count = len(corpus.queries)
    assert 25 <= count <= 150, (
        f"Expected 25–150 queries, got {count}. "
        "Add or remove queries to satisfy the benchmark size requirement."
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


# ---------------------------------------------------------------------------
# BE-3: MuSiQue-Ans corpus tests
# ---------------------------------------------------------------------------


def test_corpus_contract_multihop_musique() -> None:
    """load_eval_corpus loads all MuSiQue documents without error.

    All naive-mode query entries have correct schema fields.
    Corresponding labels present.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)

    # Verify MuSiQue documents exist and have correct collection name
    musique_docs = [d for d in corpus.documents if d.collection == "multihop-musique"]
    assert len(musique_docs) > 0, "No MuSiQue documents found in corpus"

    # Verify MuSiQue queries exist and have correct schema
    musique_queries = [q for q in corpus.queries if q.collection == "multihop-musique"]
    assert len(musique_queries) > 0, "No MuSiQue queries found in corpus"

    for query in musique_queries:
        assert query.graph_mode == "naive", (
            f"Query {query.query_id} should have graph_mode='naive', "
            f"got {query.graph_mode!r}"
        )
        assert query.metric_scope == "retrieval", (
            f"Query {query.query_id} should have metric_scope='retrieval', "
            f"got {query.metric_scope!r}"
        )

    # Verify all MuSiQue queries have at least one positive label
    musique_query_ids = {q.query_id for q in musique_queries}
    musique_labels = {lbl.query_id for lbl in corpus.labels if lbl.query_id in musique_query_ids and lbl.grade > 0}
    assert musique_labels == musique_query_ids, (
        f"MuSiQue queries missing positive labels: "
        f"{musique_query_ids - musique_labels}"
    )


def test_musique_queries_are_naive_mode() -> None:
    """All MuSiQue query entries have graph_mode='naive' and collection='multihop-musique'."""
    corpus = load_eval_corpus(CORPUS_ROOT)
    musique_queries = [q for q in corpus.queries if q.collection == "multihop-musique"]

    assert len(musique_queries) > 0, "No MuSiQue queries found"

    for query in musique_queries:
        assert query.graph_mode == "naive", (
            f"Query {query.query_id}: expected graph_mode='naive', got {query.graph_mode!r}"
        )
        assert query.collection == "multihop-musique", (
            f"Query {query.query_id}: expected collection='multihop-musique', got {query.collection!r}"
        )


def test_license_datasets_file_exists() -> None:
    """tests/eval/LICENSE-DATASETS exists and contains MuSiQue and CC BY 4.0."""
    license_file = CORPUS_ROOT / "LICENSE-DATASETS"
    assert license_file.exists(), (
        f"LICENSE-DATASETS file not found at {license_file}"
    )

    content = license_file.read_text(encoding="utf-8")
    assert "MuSiQue" in content, "LICENSE-DATASETS must mention 'MuSiQue'"
    assert "CC BY 4.0" in content, "LICENSE-DATASETS must mention 'CC BY 4.0'"


def test_all_graph_queries_have_labels() -> None:
    """Every query entry with collection in multi-hop collections has ≥1 positive label.

    This guards against silent scoring over an empty label set, which returns 0.0 with no error.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)

    multihop_collections = {"multihop-musique", "multihop-2wiki", "hotpotqa"}
    multihop_queries = [q for q in corpus.queries if q.collection in multihop_collections]

    # Build positive label mapping
    positives_by_query: dict[str, int] = {}
    for lbl in corpus.labels:
        if lbl.query_id in {q.query_id for q in multihop_queries} and lbl.grade > 0:
            positives_by_query[lbl.query_id] = positives_by_query.get(lbl.query_id, 0) + 1

    # Check every multihop query has at least one positive label
    missing = [q.query_id for q in multihop_queries if positives_by_query.get(q.query_id, 0) == 0]
    assert not missing, (
        f"Multi-hop queries missing positive labels: {missing}. "
        "Each query must have ≥1 label with grade > 0."
    )


# ---------------------------------------------------------------------------
# BE-7: 2WikiMultiHopQA corpus tests
# ---------------------------------------------------------------------------


def test_corpus_contract_multihop_2wiki() -> None:
    """load_eval_corpus loads all 2Wiki documents without error.

    Local and global query entries have correct schema.
    Corresponding labels present.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)

    # Verify 2Wiki documents exist and have correct collection name
    wiki2_docs = [d for d in corpus.documents if d.collection == "multihop-2wiki"]
    assert len(wiki2_docs) > 0, "No 2WikiMultiHopQA documents found in corpus"

    # Verify 2Wiki queries exist and have correct schema
    wiki2_queries = [q for q in corpus.queries if q.collection == "multihop-2wiki"]
    assert len(wiki2_queries) > 0, "No 2WikiMultiHopQA queries found in corpus"

    for query in wiki2_queries:
        assert query.graph_mode in ("local", "global"), (
            f"Query {query.query_id} should have graph_mode in ['local', 'global'], "
            f"got {query.graph_mode!r}"
        )
        assert query.metric_scope == "retrieval", (
            f"Query {query.query_id} should have metric_scope='retrieval', "
            f"got {query.metric_scope!r}"
        )
        assert query.collection == "multihop-2wiki", (
            f"Query {query.query_id} should have collection='multihop-2wiki', "
            f"got {query.collection!r}"
        )

    # Verify all 2Wiki queries have at least one positive label
    wiki2_query_ids = {q.query_id for q in wiki2_queries}
    wiki2_labels = {lbl.query_id for lbl in corpus.labels if lbl.query_id in wiki2_query_ids and lbl.grade > 0}
    assert wiki2_labels == wiki2_query_ids, (
        f"2Wiki queries missing positive labels: "
        f"{wiki2_query_ids - wiki2_labels}"
    )


def test_2wiki_queries_have_local_and_global_modes() -> None:
    """At least one graph_mode='local' and one graph_mode='global' entry present for multihop-2wiki."""
    corpus = load_eval_corpus(CORPUS_ROOT)
    wiki2_queries = [q for q in corpus.queries if q.collection == "multihop-2wiki"]

    assert len(wiki2_queries) > 0, "No 2WikiMultiHopQA queries found"

    has_local = any(q.graph_mode == "local" for q in wiki2_queries)
    has_global = any(q.graph_mode == "global" for q in wiki2_queries)

    assert has_local, (
        "At least one 2Wiki query with graph_mode='local' is required"
    )
    assert has_global, (
        "At least one 2Wiki query with graph_mode='global' is required"
    )


def test_license_datasets_includes_2wiki() -> None:
    """LICENSE-DATASETS contains '2WikiMultiHopQA' and 'Apache-2.0'."""
    license_file = CORPUS_ROOT / "LICENSE-DATASETS"
    assert license_file.exists(), (
        f"LICENSE-DATASETS file not found at {license_file}"
    )

    content = license_file.read_text(encoding="utf-8")
    assert "2WikiMultiHopQA" in content, "LICENSE-DATASETS must mention '2WikiMultiHopQA'"
    assert "Apache-2.0" in content or "Apache License 2.0" in content, (
        "LICENSE-DATASETS must mention 'Apache-2.0' or 'Apache License 2.0'"
    )


def test_all_2wiki_queries_have_labels() -> None:
    """Every multihop-2wiki query entry has ≥1 positive label in labels.jsonl."""
    corpus = load_eval_corpus(CORPUS_ROOT)

    wiki2_queries = [q for q in corpus.queries if q.collection == "multihop-2wiki"]
    assert len(wiki2_queries) > 0, "No 2WikiMultiHopQA queries found in corpus"

    # Build positive label mapping for 2Wiki queries
    wiki2_query_ids = {q.query_id for q in wiki2_queries}
    positives_by_query: dict[str, int] = {}
    for lbl in corpus.labels:
        if lbl.query_id in wiki2_query_ids and lbl.grade > 0:
            positives_by_query[lbl.query_id] = positives_by_query.get(lbl.query_id, 0) + 1

    # Check every 2Wiki query has at least one positive label
    missing = [q.query_id for q in wiki2_queries if positives_by_query.get(q.query_id, 0) == 0]
    assert not missing, (
        f"2Wiki queries missing positive labels: {missing}. "
        "Each query must have ≥1 label with grade > 0."
    )


# ---------------------------------------------------------------------------
# BE-9: HotpotQA distractor corpus tests
# ---------------------------------------------------------------------------


def test_corpus_contract_hotpotqa() -> None:
    """load_eval_corpus loads all HotpotQA documents without error.

    All query entries have graph_mode='naive' and collection='hotpotqa'.
    Corresponding labels present.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)

    # Verify HotpotQA documents exist and have correct collection name
    hotpotqa_docs = [d for d in corpus.documents if d.collection == "hotpotqa"]
    assert len(hotpotqa_docs) > 0, "No HotpotQA documents found in corpus"

    # Verify HotpotQA queries exist and have correct schema
    hotpotqa_queries = [q for q in corpus.queries if q.collection == "hotpotqa"]
    assert len(hotpotqa_queries) > 0, "No HotpotQA queries found in corpus"

    for query in hotpotqa_queries:
        assert query.graph_mode == "naive", (
            f"Query {query.query_id} should have graph_mode='naive', "
            f"got {query.graph_mode!r}"
        )
        assert query.metric_scope == "retrieval", (
            f"Query {query.query_id} should have metric_scope='retrieval', "
            f"got {query.metric_scope!r}"
        )
        assert query.collection == "hotpotqa", (
            f"Query {query.query_id} should have collection='hotpotqa', "
            f"got {query.collection!r}"
        )

    # Verify all HotpotQA queries have at least one positive label
    hotpotqa_query_ids = {q.query_id for q in hotpotqa_queries}
    hotpotqa_labels = {lbl.query_id for lbl in corpus.labels if lbl.query_id in hotpotqa_query_ids and lbl.grade > 0}
    assert hotpotqa_labels == hotpotqa_query_ids, (
        f"HotpotQA queries missing positive labels: "
        f"{hotpotqa_query_ids - hotpotqa_labels}"
    )


def test_license_datasets_includes_hotpotqa() -> None:
    """LICENSE-DATASETS contains 'HotpotQA' and 'CC BY 4.0'."""
    license_file = CORPUS_ROOT / "LICENSE-DATASETS"
    assert license_file.exists(), (
        f"LICENSE-DATASETS file not found at {license_file}"
    )

    content = license_file.read_text(encoding="utf-8")
    assert "HotpotQA" in content, "LICENSE-DATASETS must mention 'HotpotQA'"
    assert "CC BY 4.0" in content, "LICENSE-DATASETS must mention 'CC BY 4.0'"


def test_all_hotpotqa_queries_have_labels() -> None:
    """Every hotpotqa query entry has ≥1 positive label in labels.jsonl."""
    corpus = load_eval_corpus(CORPUS_ROOT)

    hotpotqa_queries = [q for q in corpus.queries if q.collection == "hotpotqa"]
    assert len(hotpotqa_queries) > 0, "No HotpotQA queries found in corpus"

    # Build positive label mapping for HotpotQA queries
    hotpotqa_query_ids = {q.query_id for q in hotpotqa_queries}
    positives_by_query: dict[str, int] = {}
    for lbl in corpus.labels:
        if lbl.query_id in hotpotqa_query_ids and lbl.grade > 0:
            positives_by_query[lbl.query_id] = positives_by_query.get(lbl.query_id, 0) + 1

    # Check every HotpotQA query has at least one positive label
    missing = [q.query_id for q in hotpotqa_queries if positives_by_query.get(q.query_id, 0) == 0]
    assert not missing, (
        f"HotpotQA queries missing positive labels: {missing}. "
        "Each query must have ≥1 label with grade > 0."
    )
