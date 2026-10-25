"""archon-search serve subcommand.

Foreground-blocking uvicorn entry point used by the Docker container and any
direct-run scenario where launchd/systemd service management is undesirable.

`serve` calls `load_config(path, serve=True)`: the host defaults to loopback, or to `0.0.0.0` inside the container
(`ARCHON_SEARCH_CONTAINER=1`, set by the Dockerfile), where the published port needs a non-loopback interface.
TOML `[server].host` or `ARCHON_SEARCH_HOST` override either; then it invokes
`run_server(config, config_path)`. It never touches platform service management.

Without `--config` or `ARCHON_SEARCH_CONFIG`, the config file is
`<data dir>/archon-search.toml`, where the data dir is `get_data_dir()`
(`ARCHON_SEARCH_DATA_DIR`, else `~/.archon-search`) — so inside a container it
lives on the mounted data volume alongside the rest of the runtime state.
"""
from __future__ import annotations

from pathlib import Path

import click

from archon_search.cli._helpers import _exit_on_config_error
from archon_search.config import load_config


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
    # Forward --config as given: run_server persists collection add/remove to that
    # file, and with None resolves the same default load_config read (data dir /
    # ARCHON_SEARCH_CONFIG) and emits the ignored-legacy-config warning once
    # logging is configured. load_config(None) already exits cleanly on a bad data dir.
    with _exit_on_config_error():
        config = load_config(config_path, serve=True)

    from archon_search.server.app import run_server

    run_server(config, config_path)
