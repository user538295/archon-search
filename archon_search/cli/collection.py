"""archon-search collection subcommands: list, add, remove, info, set-description, clear-description, reindex."""
from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from urllib.parse import quote

import click
import httpx

from archon_search.cli._helpers import (
    _CONNECT_FAIL,
    _DESCRIPTION_SET_REQUEST_TIMEOUT_S,
    _LOCK_WAITING_REQUEST_TIMEOUT_S,
    _clean,
    _poll_job,
    _server_connect_fail_msg,
)
from archon_search.description_write import check_description_text
from archon_search.key_manager import load_key
from archon_search.paths import get_data_dir

_DEFAULT_API_URL = "http://localhost:8765"


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    key = load_key()
    if key:
        return key
    click.echo(
        "No API key found. Pass --api-key, set ARCHON_SEARCH_API_KEY, or run the server "
        "once to auto-generate a key file.",
        err=True,
    )
    raise SystemExit(1)

logger = logging.getLogger(__name__)

_INFO_OMIT_WHEN_EMPTY = frozenset({
    "pending_embedding_model", "reindex_job_id", "description",
    "description_source", "description_status",
})

# Pydantic prefixes the message of a validator's ValueError with this; the CLI prints the message alone.
_PYDANTIC_VALUE_ERROR_PREFIX = "Value error, "
# The ``code`` of the 503 the server answers when the collection lock is not free in time.
_STORE_BUSY_CODE = "store_busy"
_EMPTY_SERVER_MESSAGE = "the server refused the description"
_NOT_UTF8_MESSAGE = "the description is not valid UTF-8 text"


@click.group()
def collection() -> None:
    """Manage collections."""


@collection.command("list")
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
def list_cmd(api_url: str, api_key: str | None) -> None:
    """List all collections (proxies GET /collections/; requires server running)."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/collections/"

    try:
        resp = httpx.get(url, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    try:
        collections = resp.json()
    except ValueError as exc:
        click.echo(f"Error: server returned unexpected response: {exc}", err=True)
        raise SystemExit(1)
    if not collections:
        click.echo("No collections found.")
    else:
        any_empty = False
        for c in collections:
            doc_count = c.get("doc_count", 0)
            any_empty = any_empty or doc_count == 0
            click.echo(f"{c['name']}  docs={doc_count}  chunks={c.get('chunk_count', 0)}")
        if any_empty:
            log_path = get_data_dir() / "logs" / "archon-search.log"
            click.echo(
                "\nSome collections show docs=0. If ingestion failed, run "
                "'archon-search jobs list' (then 'archon-search jobs status <id>') "
                f"and check the server log at {log_path}."
            )


@collection.command("add")
@click.argument("path")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the ingest job completes")
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
def add(path: str, wait_flag: bool, api_url: str, api_key: str | None) -> None:
    """Add a path to collections and ingest it."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}", "X-Ingested-By": "cli"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/collections/"

    try:
        resp = httpx.post(post_url, json={"path": path}, headers=headers, timeout=_LOCK_WAITING_REQUEST_TIMEOUT_S)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        try:
            detail = resp.json().get("detail", "collection already registered")
        except (ValueError, AttributeError):
            detail = "collection already registered"
        click.echo(f"Error: {detail}", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    collection_name: str = job_data["collection"]
    click.echo(f"Add collection job submitted: {job_id}. Collection: '{collection_name}'")
    click.echo(f"Track progress with: archon-search jobs status {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            click.echo(f"Collection '{collection_name}' ingested successfully.")


@collection.command("remove")
@click.argument("name")
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
def remove(name: str, api_url: str, api_key: str | None) -> None:
    """Remove a collection (proxies DELETE /collections/{name}; requires server running)."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    delete_url = f"{base_url}/collections/{name}"

    try:
        resp = httpx.delete(delete_url, headers=headers, timeout=_LOCK_WAITING_REQUEST_TIMEOUT_S)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        click.echo(
            f"Cannot remove '{name}': collection is pinned-only. Un-pin it first.",
            err=True,
        )
        raise SystemExit(1)

    if resp.status_code == 503:
        click.echo(
            f"Cannot remove '{name}': the server has a write in progress on this collection."
            " Retry after the active job completes.",
            err=True,
        )
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    click.echo(f"Removed collection '{name}'.")


@collection.command("info")
@click.argument("collection_name")
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
def info(collection_name: str, api_url: str, api_key: str | None) -> None:
    """Show details for a collection (proxies GET /collections/{name}; requires server running)."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/collections/{collection_name}"

    try:
        resp = httpx.get(url, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(api_url.rstrip('/')), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    data = resp.json()
    fields = [
        "name", "description", "description_source", "description_status",
        "namespace", "doc_count", "chunk_count",
        "active_embedding_model", "pending_embedding_model", "needs_reindex",
        "reindex_job_id", "last_indexed", "default_ttl_seconds", "schema_version",
        "centroid_present",
    ]
    for field in fields:
        val = data.get(field)
        if field == "last_indexed" and val is None:
            val = "never"
        # Every value is server data and description is derived from document content: strip terminal controls.
        text = _clean(str(val))
        if (val is None or not text.strip()) and field in _INFO_OMIT_WHEN_EMPTY:
            continue
        click.echo(f"{field}: {text}")


def _json_body(resp: httpx.Response) -> dict:
    """The JSON object of *resp*, or ``{}`` when the body is not JSON or not an object (a proxy's error page)."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _description_error(name: str, resp: httpx.Response) -> str:
    """The stderr line for a failed description request (404, 422, 503 or any other status).

    Server text is stripped of terminal control characters: the 503 ``service not ready`` and the 422 messages are the
    server's words, not ours.
    """
    body = _json_body(resp)
    if resp.status_code == 404:
        return f"Error: collection '{name}' not found."
    if resp.status_code == 503 and body.get("code") == _STORE_BUSY_CODE:
        return (
            f"Cannot change the description of '{name}': the server has a write in progress on this collection."
            " Retry after the active job completes."
        )
    if resp.status_code in (422, 503):
        return f"Error: {_server_message(body.get('detail'), resp.text)}"
    return f"Error: server returned {resp.status_code}: {_clean(resp.text)}"


def _server_message(detail: object, fallback: str) -> str:
    """The words of a ``detail``: FastAPI's list of ``{"msg": ...}`` items (the validator prefix dropped), a plain
    string, or *fallback* when the body had none."""
    if isinstance(detail, list):
        messages = [str(item.get("msg", "")) for item in detail if isinstance(item, dict)]
        detail = "; ".join(message.removeprefix(_PYDANTIC_VALUE_ERROR_PREFIX) for message in messages if message)
    text = _clean(str(detail)) if detail else _clean(fallback)
    return text or _EMPTY_SERVER_MESSAGE


def _call_description_route(
    name: str, api_url: str, api_key: str | None, send: Callable[[str, dict[str, str]], httpx.Response]
) -> httpx.Response:
    """Call ``/collections/{name}/description`` through *send(url, headers)* and return the 200 response.

    Exits 1 with the mapped message on a connect failure or any other answer. The name is one URL path segment: ``quote``
    leaves ``.`` alone and httpx drops dot segments from the path, so the dots are encoded too, and a name holding
    ``#``, ``?``, ``/`` or only dots can never reach another route (the clear is a DELETE).
    """
    if not name:
        click.echo("Error: the collection name cannot be empty.", err=True)
        raise SystemExit(1)
    headers = {"Authorization": f"Bearer {_resolve_api_key(api_key)}"}
    base_url = api_url.rstrip("/")
    segment = quote(name, safe="").replace(".", "%2E")
    url = f"{base_url}/collections/{segment}/description"
    try:
        resp = send(url, headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(_description_error(name, resp), err=True)
        raise SystemExit(1)
    return resp


@collection.command("set-description", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("text")
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
def set_description(name: str, text: str, api_url: str, api_key: str | None) -> None:
    """Set a collection's description by hand (proxies PUT /collections/{name}/description).

    TEXT is one line of plain text of at most 600 characters; any other text is refused and never trimmed. A manual
    description is never overwritten by generation; to let generation write a new one, clear it first with
    `collection clear-description`.
    """
    try:
        check_description_text(text)
        text.encode("utf-8")  # a stray non-UTF-8 byte in argv is a lone surrogate the JSON body cannot carry
    except ValueError as exc:  # UnicodeEncodeError is a ValueError too
        click.echo(f"Error: {_NOT_UTF8_MESSAGE if isinstance(exc, UnicodeEncodeError) else exc}", err=True)
        raise SystemExit(1)
    resp = _call_description_route(
        name,
        api_url,
        api_key,
        lambda url, headers: httpx.put(
            url, json={"description": text}, headers=headers, timeout=_DESCRIPTION_SET_REQUEST_TIMEOUT_S
        ),
    )
    data = _json_body(resp)
    source = _clean(str(data.get("description_source")))
    status = _clean(str(data.get("description_status")))
    click.echo(f"Set the description of '{_clean(name)}' (description_source: {source}, description_status: {status}).")


@collection.command("clear-description")
@click.argument("name")
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
def clear_description(name: str, api_url: str, api_key: str | None) -> None:
    """Clear a collection's description (proxies DELETE /collections/{name}/description).

    Nothing is generated by a clear: the next qualifying ingest, an enabled startup backfill or `collection describe`
    writes a new description.
    """
    _call_description_route(
        name,
        api_url,
        api_key,
        lambda url, headers: httpx.delete(url, headers=headers, timeout=_LOCK_WAITING_REQUEST_TIMEOUT_S),
    )
    shown = _clean(name)
    click.echo(f"Cleared the description of '{shown}'.")
    click.echo(f"Run `archon-search collection describe {shlex.quote(shown)}` to generate a new one.")


@collection.command("reindex-metadata")
@click.argument("collection_name")
@click.option("--dry-run", is_flag=True, default=False, help="Report counts without writing")
@click.option(
    "--normalize-timestamps/--no-normalize-timestamps",
    default=True,
    help="Rewrite indexed_at/updated_at to fixed-width UTC (YYYY-MM-DDTHH:MM:SS.ffffffZ)",
)
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the reindex-metadata job completes")
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
def reindex_metadata_cmd(
    collection_name: str,
    dry_run: bool,
    normalize_timestamps: bool,
    wait_flag: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Backfill metadata fields (file_type, updated_at, ingested_by) on an existing collection.

    Routes through the archon-search server (POST /collections/{name}/reindex-metadata).
    Use --wait to poll until the job completes.
    """
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/collections/{collection_name}/reindex-metadata"
    body = {"dry_run": dry_run, "normalize_timestamps": normalize_timestamps}

    try:
        resp = httpx.post(post_url, json=body, headers=headers, timeout=_LOCK_WAITING_REQUEST_TIMEOUT_S)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code == 409:
        try:
            detail = resp.json().get("detail", "metadata reindex already in progress")
        except (ValueError, AttributeError):
            detail = "metadata reindex already in progress"
        click.echo(f"Error: {detail}", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    click.echo(f"Reindex-metadata job submitted: {job_id}. Track progress with: archon-search jobs status {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            result = job.get("result") or {}
            click.echo(
                f"Reindex-metadata complete for '{collection_name}'. "
                f"processed={result.get('processed', 0)}, "
                f"updated={result.get('updated', 0)}, "
                f"skipped={result.get('skipped', 0)}, "
                f"ts_normalized={result.get('ts_normalized', 0)}"
            )
            warnings = result.get("warnings") or []
            if warnings:
                click.echo("warnings:")
                for w in warnings:
                    click.echo(f"  - {w}")


@collection.command("migrate")
@click.argument("collection_name")
@click.option("--dry-run", is_flag=True, default=False, help="Print pending migrations without applying (default behaviour)")
@click.option("--apply", "apply_flag", is_flag=True, default=False, help="Apply migrations (synchronous for in-place; async job for rewrite)")
@click.option("--backup-first", "backup_first", is_flag=True, default=False, help="Confirm you have a backup (required for rewrite migrations)")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="When a rewrite job is created (202), poll until done and print progress")
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
def migrate_cmd(
    collection_name: str,
    dry_run: bool,
    apply_flag: bool,
    backup_first: bool,
    wait_flag: bool,
    api_url: str,
    api_key: str | None,
) -> None:
    """Show pending schema migrations for a collection.

    Without flags (or with --dry-run) prints the list of pending migrations
    and exits without modifying anything. Use --apply to apply in-place
    migrations synchronously. Use --apply --backup-first for rewrite migrations.
    Add --wait to poll the rewrite job until completion.
    """
    if dry_run and apply_flag:
        click.echo("Error: --dry-run and --apply are mutually exclusive.", err=True)
        raise SystemExit(1)

    if dry_run and backup_first:
        click.echo("Error: --backup-first has no effect with --dry-run (use --apply --backup-first).", err=True)
        raise SystemExit(1)

    if backup_first and not apply_flag:
        click.echo("Error: --backup-first requires --apply.", err=True)
        raise SystemExit(1)

    if wait_flag and not apply_flag:
        click.echo("Error: --wait requires --apply.", err=True)
        raise SystemExit(1)

    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    if apply_flag:
        post_url = f"{base_url}/collections/{collection_name}/migrate"
        body: dict = {"dry_run": False, "backup_confirmed": backup_first}
        try:
            resp = httpx.post(post_url, json=body, headers=headers, timeout=_LOCK_WAITING_REQUEST_TIMEOUT_S)
        except _CONNECT_FAIL:
            click.echo(_server_connect_fail_msg(base_url), err=True)
            raise SystemExit(1)
        except httpx.HTTPError as exc:
            click.echo(f"Error contacting server: {exc}", err=True)
            raise SystemExit(1)

        if resp.status_code == 404:
            click.echo(f"Error: collection '{collection_name}' not found.", err=True)
            raise SystemExit(1)

        if resp.status_code == 202:
            # Rewrite job created — optionally poll
            job_data = resp.json()
            job_id: str = job_data["job_id"]
            click.echo(f"Migration job submitted: {job_id}")
            if wait_flag:
                _poll_migration_job(job_id, base_url, headers)
            return

        if resp.status_code != 200:
            click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
            raise SystemExit(1)

        data = resp.json()
        applied = data.get("migrations_applied", [])
        if not applied:
            click.echo(f"Collection '{collection_name}' is up to date — no migrations applied.")
        else:
            click.echo(f"Applied {len(applied)} migration(s) to '{collection_name}':")
            for name in applied:
                click.echo(f"  - {name}")
        return

    url = f"{base_url}/collections/{collection_name}/migrations/pending"

    try:
        resp = httpx.get(url, headers=headers)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 200:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    data = resp.json()
    pending = data.get("pending", [])

    if not pending:
        click.echo(f"Collection '{collection_name}' is up to date — no pending migrations.")
        return

    click.echo(f"Pending migrations for '{collection_name}' (schema_version={data.get('schema_version', 0)}):")
    for spec in pending:
        click.echo(f"  - {spec['name']}  kind={spec['kind']}  {spec['description']}")


def _poll_migration_job(job_id: str, base_url: str, headers: dict) -> None:
    """Poll GET /jobs/{job_id} until terminal, printing progress. Exits 1 on FAILED/CANCELLED."""
    job = _poll_job(job_id, base_url, headers)
    if not job:
        # KeyboardInterrupt path — _poll_job already printed the message
        return
    click.echo("Migration complete.")


@collection.command("reindex")
@click.argument("collection_name")
@click.option("--wait", "wait_flag", is_flag=True, default=False, help="Poll until the reindex job completes")
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
def reindex(collection_name: str, wait_flag: bool, api_url: str, api_key: str | None) -> None:
    """Force full reindex of a collection."""
    key = _resolve_api_key(api_key)
    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")
    post_url = f"{base_url}/collections/{collection_name}/reindex"

    try:
        resp = httpx.post(post_url, headers=headers, timeout=_LOCK_WAITING_REQUEST_TIMEOUT_S)
    except _CONNECT_FAIL:
        click.echo(_server_connect_fail_msg(base_url), err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1)

    if resp.status_code == 404:
        click.echo(f"Error: collection '{collection_name}' not found.", err=True)
        raise SystemExit(1)

    if resp.status_code != 202:
        click.echo(f"Error: server returned {resp.status_code}: {resp.text}", err=True)
        raise SystemExit(1)

    job_data = resp.json()
    job_id: str = job_data["job_id"]
    click.echo(f"Reindex job submitted: {job_id}")

    if wait_flag:
        job = _poll_job(job_id, base_url, headers)
        if job:
            click.echo(f"Reindex complete for '{collection_name}'.")
