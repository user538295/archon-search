"""Eval fixture dataclasses and loader for FEAT-039.

No runtime search dependencies (no LanceDB imports).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_VALID_METRIC_SCOPES = ("retrieval", "routing")

# Same pattern as archon_search.store._COLLECTION_RE
_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass
class EvalDocument:
    """A document in the eval corpus.

    Attributes:
        doc_id: Stable fixture ID used in relevance labels.
        relative_path: Path to the document under the ``corpus/`` directory.
        collection: Collection this document belongs to.  Empty string when not
            set (legacy construction without collection).
    """

    doc_id: str
    relative_path: str
    collection: str = ""


@dataclass
class EvalQuery:
    """A query in the eval corpus.

    Attributes:
        query_id: Stable query ID.
        text: Query text.
        collection: Target collection name. Must be ``None`` only when
            ``metric_scope="routing"``.  Retrieval-scope queries always require
            an explicit collection.
        metric_scope: Either ``"retrieval"`` or ``"routing"``.
    """

    query_id: str
    text: str
    collection: str | None
    metric_scope: Literal["retrieval", "routing"]

    def __post_init__(self) -> None:
        if self.metric_scope not in _VALID_METRIC_SCOPES:
            raise ValueError(
                f"metric_scope must be one of {_VALID_METRIC_SCOPES!r}, "
                f"got {self.metric_scope!r}"
            )


@dataclass
class RelevanceLabel:
    """A relevance label linking a query to a document.

    Attributes:
        query_id: ID of the query being judged.
        doc_id: ID of the document being judged.
        grade: Relevance grade (>= 0). Defaults to ``1``.
    """

    query_id: str
    doc_id: str
    grade: int = 1

    def __post_init__(self) -> None:
        if self.grade < 0:
            raise ValueError(
                f"grade must be >= 0, got {self.grade!r}"
            )


@dataclass
class EvalCorpus:
    """Container holding eval documents, queries, and relevance labels.

    Attributes:
        documents: List of :class:`EvalDocument` instances.
        queries: List of :class:`EvalQuery` instances.
        labels: List of :class:`RelevanceLabel` instances.
    """

    documents: list[EvalDocument] = field(default_factory=list)
    queries: list[EvalQuery] = field(default_factory=list)
    labels: list[RelevanceLabel] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file and return a list of dicts. Raises ValueError on malformed lines."""
    rows = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSONL in {path} at line {lineno}: {exc}") from exc
    return rows


# ---------------------------------------------------------------------------
# load_eval_corpus
# ---------------------------------------------------------------------------


def load_eval_corpus(root: Path) -> EvalCorpus:
    """Load and validate an eval corpus from *root*.

    Expected layout::

        root/
          documents.jsonl   # {doc_id, collection, relative_path}
          queries.jsonl     # {query_id, text, collection?, metric_scope}
          labels.jsonl      # {query_id, doc_id, grade?}
          corpus/           # actual document files

    Raises :class:`ValueError` on any validation failure (fail-fast).
    """
    # --- documents -----------------------------------------------------------
    doc_rows = _read_jsonl(root / "documents.jsonl")
    doc_ids: set[str] = set()
    rel_paths: set[str] = set()
    documents: list[EvalDocument] = []

    for row in doc_rows:
        doc_id = row["doc_id"]
        collection = row["collection"]
        relative_path = row["relative_path"]

        if doc_id in doc_ids:
            raise ValueError(f"Duplicate doc_id: {doc_id!r}")
        doc_ids.add(doc_id)

        if relative_path in rel_paths:
            raise ValueError(f"Duplicate relative_path: {relative_path!r}")
        rel_paths.add(relative_path)

        # Path safety checks
        p = Path(relative_path)
        if p.is_absolute():
            raise ValueError(f"Absolute path not allowed in relative_path: {relative_path!r}")
        if ".." in p.parts:
            raise ValueError(f"path traversal (..) not allowed in relative_path: {relative_path!r}")

        # Collection name validation
        if not _COLLECTION_RE.match(collection):
            raise ValueError(
                f"Invalid collection name: {collection!r} — "
                "must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}$"
            )

        documents.append(EvalDocument(doc_id=doc_id, collection=collection, relative_path=relative_path))

    # --- corpus file checks --------------------------------------------------
    corpus_dir = root / "corpus"
    declared_paths = {doc.relative_path for doc in documents}

    # Missing manifest files
    for relative_path in declared_paths:
        if not (corpus_dir / relative_path).exists():
            raise ValueError(f"Missing corpus file: corpus/{relative_path}")

    # Orphan corpus files
    if corpus_dir.exists():
        actual_files = {
            str(f.relative_to(corpus_dir))
            for f in corpus_dir.rglob("*")
            if f.is_file()
        }
        orphans = actual_files - declared_paths
        if orphans:
            raise ValueError(f"Orphan corpus files not in documents.jsonl: {sorted(orphans)}")

    # --- queries -------------------------------------------------------------
    query_rows = _read_jsonl(root / "queries.jsonl")
    query_ids: set[str] = set()
    queries: list[EvalQuery] = []

    for row in query_rows:
        query_id = row["query_id"]
        if query_id in query_ids:
            raise ValueError(f"Duplicate query_id: {query_id!r}")
        query_ids.add(query_id)

        collection = row.get("collection")
        if collection is not None and not _COLLECTION_RE.match(collection):
            raise ValueError(
                f"Invalid collection name: {collection!r} — "
                "must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}$"
            )

        queries.append(
            EvalQuery(
                query_id=query_id,
                text=row["text"],
                collection=collection,
                metric_scope=row["metric_scope"],
            )
        )

    # --- labels --------------------------------------------------------------
    label_rows = _read_jsonl(root / "labels.jsonl")
    labels: list[RelevanceLabel] = []
    doc_collection: dict[str, str] = {doc.doc_id: doc.collection for doc in documents}
    query_collection: dict[str, str | None] = {q.query_id: q.collection for q in queries}
    query_scope: dict[str, str] = {q.query_id: q.metric_scope for q in queries}

    for row in label_rows:
        query_id = row["query_id"]
        doc_id = row["doc_id"]
        grade = row.get("grade")

        # Normalize missing grade to 1
        if grade is None:
            grade = 1

        if doc_id not in doc_ids:
            raise ValueError(f"Unknown doc_id in labels: {doc_id!r}")
        if query_id not in query_ids:
            raise ValueError(f"Unknown query_id in labels: {query_id!r}")

        # Retrieval scope: positive labels must belong to the query's collection
        if grade > 0 and query_scope.get(query_id) == "retrieval":
            expected_col = query_collection.get(query_id)
            actual_col = doc_collection.get(doc_id)
            if expected_col is not None and actual_col != expected_col:
                raise ValueError(
                    f"unreachable positive: label query_id={query_id!r} doc_id={doc_id!r} "
                    f"is in collection {actual_col!r} but query targets {expected_col!r}"
                )

        labels.append(RelevanceLabel(query_id=query_id, doc_id=doc_id, grade=grade))

    # --- per-query label checks ----------------------------------------------
    # Every query must have at least one positive (grade > 0) label
    positives_by_query: dict[str, int] = {q.query_id: 0 for q in queries}
    for lbl in labels:
        if lbl.grade > 0:
            positives_by_query[lbl.query_id] = positives_by_query.get(lbl.query_id, 0) + 1

    for query_id, pos_count in positives_by_query.items():
        if pos_count == 0:
            raise ValueError(
                f"Query {query_id!r} has no positive labels (all grades are 0 or missing)"
            )

    return EvalCorpus(documents=documents, queries=queries, labels=labels)


# ---------------------------------------------------------------------------
# build_doc_collection_map
# ---------------------------------------------------------------------------


def build_doc_collection_map(corpus: EvalCorpus) -> dict[str, tuple[str, str]]:
    """Return a mapping from ``relative_path`` to ``(doc_id, collection)``.

    This lets callers translate a runtime file path (relative to the corpus/
    directory) back to the stable fixture doc_id and its collection name.
    """
    return {doc.relative_path: (doc.doc_id, doc.collection) for doc in corpus.documents}
