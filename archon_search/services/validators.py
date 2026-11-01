"""Shared request-parameter validators for REST routes and MCP tools."""


def validate_scope_filter(scope_filter: str | None) -> str | None:
    """Return an error message if *scope_filter* is syntactically invalid, else ``None``.

    Valid values: no wildcard (exact match) or exactly one trailing ``*`` with a
    non-empty prefix (e.g. ``"user:*"``).
    Invalid: empty string, bare ``*``, leading ``*``, mid-string ``*``, multiple ``*``.
    """
    if scope_filter is None:
        return None
    if not scope_filter:
        return "scope_filter must not be empty"
    if "*" not in scope_filter:
        return None
    star_count = scope_filter.count("*")
    if star_count > 1:
        return "scope_filter contains multiple '*' characters; only a single trailing '*' is permitted"
    if not scope_filter.endswith("*"):
        return "scope_filter wildcard '*' must appear only at the end of the string"
    if not scope_filter[:-1]:
        return "bare '*' is not a valid scope_filter; use a prefix followed by '*' for wildcard matching"
    return None
