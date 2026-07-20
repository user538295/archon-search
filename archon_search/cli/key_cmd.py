"""``archon-search key`` CLI group (D7 FE-1, FE-2, FE-3).

Provides:

* ``archon-search key create --namespace NS [--label LABEL] [--expires EXPR]``
  Issue a new managed API key.  The raw token is printed to stdout exactly once;
  a warning banner is printed to stderr.
* ``archon-search key list [--namespace NS] [--status active|revoked|all]``
  List managed API keys.  Active keys only by default; revoked count hint shown
  when hidden revoked keys exist.
* ``archon-search key revoke <ID>``
  Revoke a managed API key immediately.
* ``archon-search key rotate [--grace DURATION]``
  Rotate the default API key.  Generates a new key, writes it to ``.search.env``,
  and revokes (or grace-expires) the old key.  The new raw token is printed to
  stdout exactly once; a warning banner is printed to stderr.

Duration strings accepted by ``--expires``:
  ``30d``, ``12h``, ``3600s`` (relative to now, UTC) or an ISO-8601 datetime
  with a timezone offset (e.g. ``2025-12-31T23:59:59Z`` or
  ``2025-12-31T23:59:59+05:30``).  Naive datetimes (no timezone) are rejected
  with a clear error message.

Duration strings accepted by ``--grace``:
  ``30d``, ``12h``, ``3600s`` (converted to an integer number of seconds).
  The ``POST /keys/rotate`` API accepts grace as an integer ``grace_seconds``.
"""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

import click
import httpx

from archon_search.key_manager import load_or_generate_key
from archon_search.cli._helpers import _SERVER_NOT_RUNNING_MSG

_DEFAULT_API_URL = "http://localhost:8765"

_DURATION_RE = re.compile(r"^(\d+)([dhDH]|[sS])$")


def _resolve_api_key(api_key: str | None) -> str:
    """Return the API key from the option, env var, or the key file."""
    if api_key:
        return api_key
    env_key = os.environ.get("ARCHON_SEARCH_API_KEY")
    if env_key:
        return env_key
    key, _ = load_or_generate_key()
    return key


def _parse_expires(value: str) -> datetime:
    """Parse a duration or ISO-8601 datetime string to a timezone-aware datetime.

    Accepted formats:
    - ``<N>d`` — N days from now (UTC)
    - ``<N>h`` — N hours from now (UTC)
    - ``<N>s`` — N seconds from now (UTC)
    - ISO-8601 datetime with timezone (e.g. ``2025-12-31T23:59:59Z``)

    Raises click.BadParameter for naive datetimes or unrecognised strings.
    """
    # Try relative duration first
    m = _DURATION_RE.match(value)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "d":
            return datetime.now(UTC) + timedelta(days=n)
        if unit == "h":
            return datetime.now(UTC) + timedelta(hours=n)
        if unit == "s":
            return datetime.now(UTC) + timedelta(seconds=n)

    # Try ISO-8601 datetime
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise click.BadParameter(
                f"{value!r} is a naive datetime (no timezone). "
                "Add a timezone offset, e.g. '2025-12-31T23:59:59Z'."
            )
        return dt
    except ValueError:
        pass

    raise click.BadParameter(
        f"{value!r} is not a valid duration (e.g. '30d', '12h', '3600s') "
        "or an ISO-8601 datetime with timezone."
    )


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("key")
def key_cmd() -> None:
    """Manage API keys (issue, list, revoke, rotate)."""


# ---------------------------------------------------------------------------
# create subcommand
# ---------------------------------------------------------------------------


@key_cmd.command("create")
@click.option(
    "--namespace",
    required=True,
    help="Namespace this key grants access to.",
)
@click.option(
    "--label",
    default=None,
    help="Optional human-readable label for the key.",
)
@click.option(
    "--expires",
    "expires_str",
    default=None,
    help=(
        "Expiry as a duration ('30d', '12h', '3600s') or ISO-8601 datetime "
        "with timezone ('2025-12-31T23:59:59Z'). Omit for no expiry."
    ),
)
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
def create_subcommand(
    namespace: str,
    label: str | None,
    expires_str: str | None,
    api_url: str,
    api_key: str | None,
) -> None:
    """Issue a new managed API key.

    The raw bearer token is printed to stdout exactly once.
    Store it securely — the server does not retain the plaintext.
    """
    try:
        key = _resolve_api_key(api_key)
    except Exception as exc:
        click.echo(f"Error resolving API key: {exc}", err=True)
        raise SystemExit(1) from exc

    expires_at_str: str | None = None
    if expires_str is not None:
        try:
            expires_dt = _parse_expires(expires_str)
        except click.BadParameter as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1) from exc
        expires_at_str = expires_dt.isoformat()

    body: dict[str, object] = {"namespace": namespace}
    if label is not None:
        body["label"] = label
    if expires_at_str is not None:
        body["expires_at"] = expires_at_str

    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/keys"

    try:
        with httpx.Client() as client:
            resp = client.post(url, headers=headers, json=body)
    except httpx.ConnectError:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 201:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    try:
        data = resp.json()
    except ValueError:
        click.echo("Error: server returned invalid JSON", err=True)
        raise SystemExit(1)

    token: str = data.get("token", "")
    key_id: str = data.get("id", "")
    created_at: str = data.get("created_at", "")

    # S22: warning banner on stderr first, then metadata on stderr, token on stdout only.
    # A script capturing output via $() or pipe gets the clean token; metadata goes to stderr.
    click.echo(
        "WARNING: Store this token safely — it will not be shown again.",
        err=True,
    )
    click.echo(f"Key created:", err=True)
    click.echo(f"  id:         {key_id}", err=True)
    click.echo(f"  namespace:  {namespace}", err=True)
    if label:
        click.echo(f"  label:      {label}", err=True)
    click.echo(f"  created_at: {created_at}", err=True)
    if expires_at_str:
        click.echo(f"  expires_at: {expires_at_str}", err=True)

    # Raw token to stdout only (S22).
    click.echo(token)


# ---------------------------------------------------------------------------
# list subcommand
# ---------------------------------------------------------------------------


@key_cmd.command("list")
@click.option(
    "--namespace",
    default=None,
    help="Filter keys by namespace.",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    type=click.Choice(["active", "revoked", "all"]),
    help="Show active (default), revoked, or all keys.",
)
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
def list_subcommand(
    namespace: str | None,
    status_filter: str | None,
    api_url: str,
    api_key: str | None,
) -> None:
    """List managed API keys.

    Active keys are shown by default.  When revoked keys exist but are hidden,
    a hint count is displayed.  Use ``--status all`` to show all keys.
    """
    try:
        key = _resolve_api_key(api_key)
    except Exception as exc:
        click.echo(f"Error resolving API key: {exc}", err=True)
        raise SystemExit(1) from exc

    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/keys"

    params: dict[str, str] = {}
    if status_filter is not None:
        params["status"] = status_filter
    if namespace is not None:
        params["namespace"] = namespace

    try:
        with httpx.Client() as client:
            resp = client.get(url, headers=headers, params=params if params else None)
    except httpx.ConnectError:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 200:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    try:
        data = resp.json()
    except ValueError:
        click.echo("Error: server returned invalid JSON", err=True)
        raise SystemExit(1)

    keys = data.get("keys", [])
    hidden_revoked_count: int = data.get("hidden_revoked_count", 0)

    if not keys:
        click.echo("No keys found.")
    else:
        for record in keys:
            key_id = record.get("id") or "null"
            ns = record.get("namespace", "")
            status = record.get("status", "")
            label = record.get("label") or ""
            created_at = record.get("created_at", "")
            expires_at = record.get("expires_at") or ""
            label_part = f"  label: {label}" if label else ""
            expires_part = f"  expires_at: {expires_at}" if expires_at else ""
            click.echo(
                f"id: {key_id}  namespace: {ns}  status: {status}"
                f"  created_at: {created_at}{label_part}{expires_part}"
            )

    if hidden_revoked_count > 0:
        click.echo(
            f"\n({hidden_revoked_count} revoked key(s) hidden — use --status all or --status revoked to show)"
        )


# ---------------------------------------------------------------------------
# revoke subcommand
# ---------------------------------------------------------------------------


def _lookup_key_label(base_url: str, headers: dict[str, str], key_id: str) -> str | None:
    """Best-effort fetch of a key's label for the confirmation prompt.

    Returns the label if the key is found and has one, else ``None``.  Never
    raises: a network error, non-200 response, invalid JSON, missing key, or
    absent label all fall back to ``None`` so the caller prompts with the raw
    ID.  ``status=all`` is used so revoked keys are still matched.
    """
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{base_url}/keys", headers=headers, params={"status": "all"}
            )
        if resp.status_code != 200:
            return None
        for record in resp.json().get("keys", []):
            if record.get("id") == key_id:
                return record.get("label") or None
    except (httpx.HTTPError, ValueError):
        return None
    return None


@key_cmd.command("revoke")
@click.argument("key_id")
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt (for non-interactive/scripted use).",
)
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
def revoke_subcommand(
    key_id: str, assume_yes: bool, api_url: str, api_key: str | None
) -> None:
    """Revoke a managed API key immediately.

    Prompts for confirmation before deleting (showing the key's label when it
    can be looked up).  Pass ``--yes``/``-y`` to skip the prompt in scripts.
    Idempotent: revoking an already-revoked key returns success.
    """
    try:
        key = _resolve_api_key(api_key)
    except Exception as exc:
        click.echo(f"Error resolving API key: {exc}", err=True)
        raise SystemExit(1) from exc

    headers = {"Authorization": f"Bearer {key}"}
    base_url = api_url.rstrip("/")

    if not assume_yes:
        label = _lookup_key_label(base_url, headers, key_id)
        target = f'"{label}" (id: {key_id})' if label else key_id
        # Plain confirm (not abort=True): an interactive "no" returns False and
        # exits 0; a non-interactive stdin (pipe/CI) raises Abort on EOF and
        # exits non-zero, so silent revocation in automation still fails.
        if not click.confirm(f"Revoke key {target}? This cannot be undone."):
            click.echo("Aborted.")
            return

    url = f"{base_url}/keys/{key_id}"

    try:
        with httpx.Client() as client:
            resp = client.delete(url, headers=headers)
    except httpx.ConnectError:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code not in (200, 204):
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    click.echo(f"Key {key_id} revoked.")


# ---------------------------------------------------------------------------
# rotate subcommand
# ---------------------------------------------------------------------------


def _parse_grace(value: str) -> int:
    """Parse a grace duration string to an integer number of seconds.

    Accepted formats:
    - ``<N>d`` — N days (N * 86400 seconds)
    - ``<N>h`` — N hours (N * 3600 seconds)
    - ``<N>s`` — N seconds

    Raises click.BadParameter for unrecognised strings.
    """
    m = _DURATION_RE.match(value)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "d":
            return n * 86400
        if unit == "h":
            return n * 3600
        if unit == "s":
            return n
    raise click.BadParameter(
        f"{value!r} is not a valid grace duration. "
        "Use a relative duration like '30d', '12h', or '3600s'."
    )


@key_cmd.command("rotate")
@click.option(
    "--grace",
    "grace_str",
    default=None,
    help=(
        "Grace period before the old key expires ('30d', '12h', '3600s'). "
        "Omit for immediate revocation of the old key."
    ),
)
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
def rotate_subcommand(
    grace_str: str | None,
    api_url: str,
    api_key: str | None,
) -> None:
    """Rotate the default API key.

    Generates a new managed API key, writes the new raw token to .search.env,
    and revokes (or grace-expires) the old default key.

    The new raw bearer token is printed to stdout exactly once.
    Store it securely — the server does not retain the plaintext.
    """
    try:
        key = _resolve_api_key(api_key)
    except Exception as exc:
        click.echo(f"Error resolving API key: {exc}", err=True)
        raise SystemExit(1) from exc

    body: dict[str, object] = {}
    if grace_str is not None:
        try:
            grace_seconds = _parse_grace(grace_str)
        except click.BadParameter as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1) from exc
        body["grace_seconds"] = grace_seconds

    headers = {"Authorization": f"Bearer {key}"}
    url = f"{api_url.rstrip('/')}/keys/rotate"

    try:
        with httpx.Client() as client:
            resp = client.post(url, headers=headers, json=body)
    except httpx.ConnectError:
        click.echo(_SERVER_NOT_RUNNING_MSG, err=True)
        raise SystemExit(1)
    except httpx.HTTPError as exc:
        click.echo(f"Error contacting server: {exc}", err=True)
        raise SystemExit(1) from exc

    if resp.status_code != 200:
        click.echo(
            f"Error: server returned {resp.status_code}: {resp.text}", err=True
        )
        raise SystemExit(1)

    try:
        data = resp.json()
    except ValueError:
        click.echo("Error: server returned invalid JSON", err=True)
        raise SystemExit(1)

    new_token: str = data.get("token", "")
    new_key_id: str = data.get("new_key_id", "")
    old_key_id: str | None = data.get("old_key_id")
    old_key_status: str | None = data.get("old_key_status")
    old_key_expires_at: str | None = data.get("old_key_expires_at")

    # S22: warning banner on stderr first, then metadata on stderr, token on stdout only.
    click.echo(
        "WARNING: Store this token safely — it will not be shown again.",
        err=True,
    )
    click.echo("Key rotated:", err=True)
    click.echo(f"  new_key_id: {new_key_id}", err=True)
    if old_key_id:
        click.echo(f"  old_key_id: {old_key_id}", err=True)
    if old_key_status:
        click.echo(f"  old_key_status: {old_key_status}", err=True)
    if old_key_expires_at:
        click.echo(f"  old_key_expires_at: {old_key_expires_at}", err=True)

    # Raw token to stdout only (S22).
    click.echo(new_token)
