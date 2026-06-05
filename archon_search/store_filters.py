"""SQL predicate builder for LanceDB WHERE clauses derived from SearchFilters.

This module is the single place where SearchFilters → SQL translation happens.
No f-string SQL anywhere else in the codebase — all SQL literals go through
``_sql_quote_str`` to prevent injection via user-supplied filter values.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.filters import SearchFilters

# glob × ACL attrition stack requires 5× over-fetch to maintain top_k candidates
GLOB_OVERFETCH_FACTOR: int = 5


def _sql_quote_str(s: str) -> str:
    """Wrap *s* in single quotes, doubling any internal single quotes.

    Example: ``"O'Reilly"`` → ``"'O''Reilly'"``.
    """
    return "'" + s.replace("'", "''") + "'"


def escape_like(s: str) -> str:
    """Escape SQL LIKE metacharacters (``%``, ``_``, ``\\``) with a backslash.

    The result is safe to embed as a LIKE operand when paired with
    ``ESCAPE '\\'`` in the query (single backslash escape char).
    """
    # Order matters: escape backslash first so we don't double-escape later replacements.
    s = s.replace("\\", "\\\\")
    s = s.replace("%", "\\%")
    s = s.replace("_", "\\_")
    return s


def build_where(filters: "SearchFilters") -> str:
    """Compile *filters* into a SQL WHERE predicate string.

    Returns ``""`` when no SQL-expressible filters are set (caller should skip
    ``.where()`` entirely in that case).

    Fields handled:
    - ``file_type``           → ``file_type = '<value>'``
    - ``source_path_prefix``  → ``source_path LIKE '<escaped>%' ESCAPE '\\'``
    - ``indexed_after``       → ``indexed_at >= '<fixed-width UTC>'``
    - ``indexed_before``      → ``indexed_at <= '<fixed-width UTC>'``
    - ``language``            → ``language = '<code>'``

    Fields deliberately NOT emitted as SQL:
    - ``source_path_glob``  — post-RRF Python-side filter
    - ``include_metadata``  — response-shaping flag
    """
    from archon_search._types import normalize_iso_utc  # lazy import

    clauses: list[str] = []

    if filters.file_type is not None:
        clauses.append("file_type = " + _sql_quote_str(filters.file_type))

    if filters.source_path_prefix is not None:
        escaped = escape_like(filters.source_path_prefix)
        pattern = _sql_quote_str(escaped + "%")
        clauses.append("source_path LIKE " + pattern + " ESCAPE '\\'")

    if filters.indexed_after is not None:
        clauses.append("indexed_at >= " + _sql_quote_str(normalize_iso_utc(filters.indexed_after)))

    if filters.indexed_before is not None:
        clauses.append("indexed_at <= " + _sql_quote_str(normalize_iso_utc(filters.indexed_before)))

    if filters.language is not None:
        clauses.append("language = " + _sql_quote_str(filters.language))

    return " AND ".join(clauses)


def _compute_fetch(top_k: int, *, has_glob: bool) -> int:
    """Compute how many rows to fetch from LanceDB before post-processing.

    - ``has_glob=False``: ``max(top_k * 3, 20)`` — standard over-fetch for RRF.
    - ``has_glob=True``: ``max(top_k * GLOB_OVERFETCH_FACTOR, 60)`` — extra
      headroom because glob filtering happens after retrieval and ACL gating
      can further reduce the candidate set.
    """
    if has_glob:
        return max(top_k * GLOB_OVERFETCH_FACTOR, 60)
    return max(top_k * 3, 20)
