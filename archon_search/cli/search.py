"""archon-search search: query one or more collections through the running server (POST /search)."""
from __future__ import annotations

import json

import click
import httpx

from archon_search.cli._helpers import (
    _DEFAULT_API_URL,
    _clean,
    _resolve_api_key,
    _send_or_exit,
)
from archon_search.constants import CLI_SEARCH_REQUEST_TIMEOUT_SECONDS

# Longest excerpt, in characters, printed per result line (after whitespace is collapsed).
_EXCERPT_MAX_CHARS = 200


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


@click.command("search")
@click.argument("query")
@click.option(
    "--collection",
    "collections",
    multiple=True,
    required=True,
    help="Collection to search (repeat for several).",
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
def search(query: str, collections: tuple[str, ...], json_flag: bool, api_url: str, api_key: str | None) -> None:
    """Search collections (proxies POST /search; requires server running)."""
    key = _resolve_api_key(api_key)
    base_url = api_url.rstrip("/")
    body: dict = {"query": query}
    if len(collections) == 1:
        body["collection"] = collections[0]
    else:
        body["collections"] = list(collections)

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
        click.echo(f"Error: server returned {resp.status_code}: {_clean(resp.text)}", err=True)
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
