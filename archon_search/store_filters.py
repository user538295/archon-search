"""SQL literal-quoting helpers for LanceDB predicate construction.

The single source of truth for escaping string values that are interpolated into
LanceDB (DataFusion) SQL ``where`` / ``delete`` / ``count_rows`` predicates. Used by
``store.py``'s ``_where_eq`` / ``_where_in`` helpers as the defense-in-depth boundary
behind the upstream identifier regex gates.
"""
from __future__ import annotations


def _sql_quote_str(value: str) -> str:
    """Return ``value`` as a SQL single-quoted string literal.

    Embedded single quotes are doubled per the SQL standard, so
    ``_sql_quote_str("O'Brien") == "'O''Brien'"``. DataFusion (LanceDB's SQL engine)
    uses single-quote doubling for string-literal escaping.
    """
    return "'" + value.replace("'", "''") + "'"
