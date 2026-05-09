"""Eval fixture dataclasses for FEAT-039.

No runtime search dependencies (no LanceDB imports).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

_VALID_METRIC_SCOPES = ("retrieval", "routing")


@dataclass
class EvalDocument:
    """A document in the eval corpus.

    Attributes:
        doc_id: Stable fixture ID used in relevance labels.
        relative_path: Path to the document under the ``corpus/`` directory.
    """

    doc_id: str
    relative_path: str


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
    """Container holding eval documents and queries together.

    Attributes:
        documents: List of :class:`EvalDocument` instances.
        queries: List of :class:`EvalQuery` instances.
    """

    documents: list[EvalDocument] = field(default_factory=list)
    queries: list[EvalQuery] = field(default_factory=list)
