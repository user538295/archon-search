"""``archon-search key`` CLI group (D7 FE-1).

Provides:

* ``archon-search key create --namespace NS [--label LABEL] [--expires EXPR]``
  Issue a new managed API key.  The raw token is printed to stdout exactly once;
  a warning banner is printed to stderr.

Duration strings accepted by ``--expires``:
  ``30d``, ``12h``, ``3600s`` (relative to now, UTC) or an ISO-8601 datetime
  with a timezone offset (e.g. ``2025-12-31T23:59:59Z`` or
  ``2025-12-31T23:59:59+05:30``).  Naive datetimes (no timezone) are rejected
  with a clear error message.
"""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

import click
import httpx

from archon_search.key_manager import load_or_generate_key

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
