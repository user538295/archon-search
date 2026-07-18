"""archon-search serve subcommand.

Foreground-blocking uvicorn entry point used by the Docker container and any
direct-run scenario where launchd/systemd service management is undesirable.

`serve` calls `load_config(path, serve=True)` so the host default is `0.0.0.0`
(overridable by TOML `[server].host` or `ARCHON_SEARCH_HOST`), then invokes
`run_server(config)`. It never touches platform service management.

When `ARCHON_SEARCH_DATA_DIR` is set but `ARCHON_SEARCH_CONFIG` is not, a
startup warning is emitted: `collection add/remove` commands write to the TOML
config file at `~/.archon-search/archon-search.toml`, which is outside the
mounted data volume — they will fail inside a container unless
`ARCHON_SEARCH_CONFIG` points to a writable path under `/data`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from archon_search.config import ConfigError, load_config

logger = logging.getLogger(__name__)

_CONTAINER_COLLECTION_WARNING = (
    "ARCHON_SEARCH_DATA_DIR is set but ARCHON_SEARCH_CONFIG is not — "
    "'collection add/remove' commands will fail; set "
    "ARCHON_SEARCH_CONFIG=/data/archon-search.toml to enable collection "
    "management inside the container."
)


@click.command()
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Path to archon-search.toml",
)
def serve(config_path: Path | None) -> None:
    """Start the archon-search server in the foreground (container / direct-run mode)."""
    try:
        config = load_config(config_path, serve=True)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if os.environ.get("ARCHON_SEARCH_DATA_DIR") and not os.environ.get("ARCHON_SEARCH_CONFIG"):
        logger.warning(_CONTAINER_COLLECTION_WARNING)

    from archon_search.server.app import run_server  # noqa: PLC0415

    run_server(config)
