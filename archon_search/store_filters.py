"""store_filters — pure SQL predicate builders for SearchFilters (A2).

All functions are pure (no I/O, no async) so they are easily unit-tested
and importable without a live LanceDB connection.
"""
from __future__ import annotations

GLOB_OVERFETCH_FACTOR: int = 5
# Attrition stack: glob typically matches ~20% of results; combined with ACL,
# overfetching by 5× keeps the post-filter pool above top_k in most deployments.


def _sql_quote_str(s: str) -> str:
    """Wrap *s* in single quotes, doubling any embedded single quotes (SQL standard)."""
    return "'" + s.replace("'", "''") + "'"


def escape_like(s: str) -> str:
    """Escape LIKE metacharacters (%, _, \\) with backslash."""
    s = s.replace("\\", "\\\\")
    s = s.replace("%", "\\%")
    s = s.replace("_", "\\_")
    return s


def build_where(filters: "SearchFilters") -> str:  # type: ignore[name-defined]
    """Build a SQL WHERE predicate string from *filters*.

    Returns ``''`` when no SQL-level filter is set.

    ``source_path_glob`` and ``include_metadata`` are NOT emitted as SQL
    (handled as post-RRF step and response-shaping respectively).
    ``language`` is never reachable — rejected at validation by ``SearchFilters``.
    """
    from archon_search.filters import SearchFilters  # noqa: PLC0415 — lazy to avoid circular
    from archon_search._types import normalize_iso_utc  # noqa: PLC0415

    clauses: list[str] = []

    if filters.file_type is not None:
        clauses.append(f"file_type = {_sql_quote_str(filters.file_type)}")

    if filters.source_path_prefix is not None:
        escaped = escape_like(filters.source_path_prefix)
        # ESCAPE '\\' means backslash is the escape character (SQL standard).
        # Four Python backslashes → two in the string literal → one SQL escape char.
        clauses.append(
            f"source_path LIKE {_sql_quote_str(escaped + '%')} ESCAPE '\\\\'"
        )

    if filters.indexed_after is not None:
        clauses.append(
            f"indexed_at >= {_sql_quote_str(normalize_iso_utc(filters.indexed_after))}"
        )

    if filters.indexed_before is not None:
        clauses.append(
            f"indexed_at <= {_sql_quote_str(normalize_iso_utc(filters.indexed_before))}"
        )

    if filters.language is not None:
        raise RuntimeError(
            "language filter must be rejected by SearchFilters validator before reaching build_where"
        )

    return " AND ".join(clauses)


def _compute_fetch(top_k: int, *, has_glob: bool) -> int:
    """Single source of truth for over-fetch multipliers.

    When a glob is present, overfetch by ``GLOB_OVERFETCH_FACTOR`` (default 5×)
    to absorb attrition from the post-RRF glob filter.  Otherwise use 3×.
    A floor of 20 (no-glob) or 60 (glob) avoids degenerate tiny fetches.
    """
    if has_glob:
        return max(top_k * GLOB_OVERFETCH_FACTOR, 60)
    return max(top_k * 3, 20)
