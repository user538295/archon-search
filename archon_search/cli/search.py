"""archon-search search: query one or more collections through the running server (POST /search)."""
from __future__ import annotations

import json

import click
import httpx

from archon_search.cli._helpers import (
    _DEFAULT_API_URL,
    _clean,
    _json_body,
    _resolve_api_key,
    _send_or_exit,
    _server_message,
)
from archon_search.constants import (
    CLI_SEARCH_REQUEST_TIMEOUT_SECONDS,
    VALID_GRAPH_MODES,
)

# Longest excerpt, in characters, printed per result line (after whitespace is collapsed).
_EXCERPT_MAX_CHARS = 200


# What a bare 401 (the API-key middleware answers it with no body) and any other body-less error say.
_BARE_401_MESSAGE = (
    "the server rejected the API key. Check --api-key, ARCHON_SEARCH_API_KEY or the key file."
)
_UNEXPLAINED_ERROR_MESSAGE = "the server answered {status} without an error message."


def _retry_after_note(resp: httpx.Response) -> str:
    """``" (retry after Ns)"`` for a ``Retry-After`` of whole seconds, else ``""`` (never echoes the raw header)."""
    seconds = resp.headers.get("Retry-After", "").strip()
    return f" (retry after {seconds}s)" if seconds.isascii() and seconds.isdigit() else ""


def _error_message(resp: httpx.Response) -> str:
    """The ``Error: ...`` line for a non-200 answer: the server's detail (any REST shape), cleaned of controls."""
    empty_text = (
        _BARE_401_MESSAGE
        if resp.status_code == 401
        else _UNEXPLAINED_ERROR_MESSAGE.format(status=resp.status_code)
    )
    message = _server_message(_json_body(resp).get("detail"), resp.text, empty_text=empty_text)
    return f"Error: {message.strip()}{_retry_after_note(resp)}"


def _excerpt(text: str) -> str:
    """*text* on one line: controls cleaned, whitespace collapsed, cut at ``_EXCERPT_MAX_CHARS``."""
    return " ".join(_clean(text).split())[:_EXCERPT_MAX_CHARS]


def _result_line(rank: int, result: dict) -> str:
    score = result.get("score", 0.0)
    fields = (
        f"{rank}.",
        f"{score:.4f}",
        _clean(str(result.get("collection", ""))),
        _clean(str(result.get("source_path", ""))),
        _excerpt(str(result.get("text", ""))),
    )
    return "  ".join(fields)


def _echo_notes(payload: dict) -> None:
    """Write one note on stderr for a step that did not run and one per skipped collection."""
    warning = payload.get("expansion_warning")
    if warning:
        click.echo(f"Note: {_clean(str(warning))}", err=True)
    for excluded in payload.get("excluded_collections") or []:
        name = _clean(str(excluded.get("name", "")))
        reason = _clean(str(excluded.get("reason", "")))
        click.echo(f"Note: collection '{name}' skipped: {reason}", err=True)


def _given(**options: object) -> dict[str, object]:
    """The options the caller gave: a value of ``None`` means "not given" and is left out of the request body."""
    return {name: value for name, value in options.items() if value is not None}


@click.command("search")
@click.argument("query")
@click.option(
    "--collection",
    "collections",
    multiple=True,
    required=True,
    help="Collection to search (repeat for several).",
)
@click.option("--file-type", default=None, help="Only files of this type.")
@click.option("--source-path-prefix", default=None, help="Only sources whose path starts with this prefix.")
@click.option("--source-path-glob", default=None, help="Only sources whose path matches this glob.")
@click.option("--indexed-after", default=None, help="Only documents indexed after this date or timestamp.")
@click.option("--indexed-before", default=None, help="Only documents indexed before this date or timestamp.")
@click.option("--language", default=None, help="Only documents in this language code.")
@click.option(
    "--include-metadata",
    is_flag=True,
    default=False,
    help="Include each result's metadata (visible with --json).",
)
@click.option("--hyde", is_flag=True, default=False, help="Expand the query with a hypothetical document (HyDE).")
@click.option("--rag-fusion", is_flag=True, default=False, help="Fuse the results of several rewritten queries.")
@click.option(
    "--graph-mode",
    default=None,
    help=f"Graph retrieval mode: {', '.join(VALID_GRAPH_MODES)} (the server validates the value).",
)
@click.option(
    "--scope-filter",
    default=None,
    help="Scope tag to match, exact ('user:alice') or one trailing-'*' prefix ('user:alice*'); not with --graph-mode.",
)
@click.option(
    "--acl-context",
    is_flag=True,
    default=False,
    help="Include the ACL gate on every result (visible with --json).",
)
@click.option("--json", "json_flag", is_flag=True, default=False, help="Print the full response as JSON.")
@click.option(
    "--api-url",
    default=_DEFAULT_API_URL,
    show_default=True,
    help="Base URL of the archon-search server.",
)
@click.option(
    "--api-key",
    default=None,
    help="API key (falls back to ARCHON_SEARCH_API_KEY env var or the key file).",
)
def search(
    query: str,
    collections: tuple[str, ...],
    file_type: str | None,
    source_path_prefix: str | None,
    source_path_glob: str | None,
    indexed_after: str | None,
    indexed_before: str | None,
    language: str | None,
    include_metadata: bool,
    hyde: bool,
    rag_fusion: bool,
    graph_mode: str | None,
    scope_filter: str | None,
    acl_context: bool,
    json_flag: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Search collections (proxies POST /search; requires server running)."""
    key = _resolve_api_key(api_key)
    base_url = api_url.rstrip("/")
    body: dict = {"query": query}
    if len(collections) == 1:
        body["collection"] = collections[0]
    else:
        body["collections"] = list(collections)
    filters = _given(
        file_type=file_type,
        source_path_prefix=source_path_prefix,
        source_path_glob=source_path_glob,
        indexed_after=indexed_after,
        indexed_before=indexed_before,
        language=language,
    )
    if filters:
        body["filters"] = filters
    body |= _given(
        include_metadata=include_metadata or None,
        hyde=hyde or None,
        rag_fusion=rag_fusion or None,
        acl_context=acl_context or None,
        graph_mode=graph_mode,
        scope_filter=scope_filter,
    )

    resp = _send_or_exit(
        base_url,
        lambda: httpx.post(
            f"{base_url}/search",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=CLI_SEARCH_REQUEST_TIMEOUT_SECONDS,
        ),
    )
    if resp.status_code != 200:
        click.echo(_error_message(resp), err=True)
        raise SystemExit(1)

    try:
        payload = resp.json()
    except ValueError as exc:
        click.echo(f"Error: server returned unexpected response: {_clean(str(exc))}", err=True)
        raise SystemExit(1)
    if not isinstance(payload, dict):
        click.echo("Error: server returned unexpected response: expected a JSON object", err=True)
        raise SystemExit(1)

    if json_flag:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    results = payload.get("results") or []
    if not results:
        click.echo("No results found.")
    for rank, result in enumerate(results, start=1):
        click.echo(_result_line(rank, result))
    _echo_notes(payload)
