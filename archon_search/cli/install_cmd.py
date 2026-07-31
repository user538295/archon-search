"""archon-search install and uninstall subcommands."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import rmtree

import click

from archon_search.cli._helpers import _CONTAINER_MSG, _get_service
from archon_search.install import create_installer
from archon_search.key_manager import _HEX_RE

_TOP_K_MAX = 100
_TOP_K_MIN = 1
_SERVER_KEY_MIN_LEN = 32


class _HexKeyParamType(click.ParamType):
    """Click param type that validates a lowercase hex string of minimum 32 chars."""

    name = "HEX_KEY"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        if not value or not _HEX_RE.fullmatch(value):
            self.fail(
                '--server-key must be a lowercase hex string '
                '(e.g., generated with: python -c "import secrets; print(secrets.token_hex(32))")',
                param,
                ctx,
            )
        if len(value) < _SERVER_KEY_MIN_LEN:
            self.fail(
                f"--server-key must be at least {_SERVER_KEY_MIN_LEN} hex characters for adequate security.",
                param,
                ctx,
            )
        return value


def _get_db_path(config_path: Path | None = None) -> Path:
    """Return the expanded database path from config."""
    from archon_search.config import load_config
    cfg = load_config(config_path)
    return Path(cfg.db_path).expanduser()


def _validate_host(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """Reject empty string for --host."""
    if value is not None and value == "":
        raise click.BadParameter("--host must not be empty.", param=param, ctx=ctx)
    return value


def _validate_top_k(ctx: click.Context, param: click.Parameter, value: int | None) -> int | None:
    """Reject values outside 1–100 for --top-k."""
    if value is None:
        return value
    if value < _TOP_K_MIN:
        raise click.BadParameter(
            f"--top-k must be at least {_TOP_K_MIN}.",
            param=param,
            ctx=ctx,
        )
    if value > _TOP_K_MAX:
        raise click.BadParameter(
            f"top_k > {_TOP_K_MAX} is likely to cause performance problems; "
            "edit archon-search.toml directly if you need a higher value.",
            param=param,
            ctx=ctx,
        )
    return value


def _install_options(f: click.decorators.FC) -> click.decorators.FC:
    for decorator in reversed([
        click.option("--profile", type=click.Choice(["minimal", "balanced", "max"]), default=None, help="Install profile"),
        click.option("--multilingual/--no-multilingual", default=None, help="Use multilingual models (--no-multilingual forces English)"),
        click.option("--skip-preload", is_flag=True, default=False, help="Skip model pre-download"),
        click.option("--force", is_flag=True, default=False, help="Force reinstall"),
        click.option("--delete-db", is_flag=True, default=False, help="Delete database on reinstall"),
        click.option("--dry-run", is_flag=True, default=False, help="Print actions without executing"),
        click.option("--non-interactive", is_flag=True, default=False, help="Skip interactive prompts"),
        click.option("--accept-jina-license", is_flag=True, default=False, help="Accept Jina CC-BY-NC-4.0 license"),
        click.option("--accept-fasttext-license", is_flag=True, default=False, help="Accept fasttext lid.176.ftz CC-BY-SA 3.0 license"),
        click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml"),
    ]):
        f = decorator(f)
    return f



@click.command()
@_install_options
@click.option("--code/--no-code", default=None, help="Install tree-sitter code enrichment packages")
@click.option("--watch/--no-watch", default=None, help="Enable filesystem watcher for auto-reindex")
@click.option("--telemetry/--no-telemetry", default=None, help="Enable local query telemetry")
@click.option("--eager-load/--no-eager-load", default=None, help="Pre-load embedding models at startup")
@click.option("--no-reranker", is_flag=True, default=False, help="Disable reranker (lower latency, less precision)")
@click.option("--routing-strategy", type=click.Choice(["centroid", "hybrid"]), default=None, help="Routing strategy")
@click.option("--log-format", type=click.Choice(["text", "json"]), default=None, help="Log format")
@click.option("--disable-gpu", is_flag=True, default=False, help="Force CPU execution; skip GPU acceleration")
@click.option("--host", default=None, callback=_validate_host, is_eager=False,
              help="Bind address (default: 127.0.0.1); use 0.0.0.0 for remote access")
@click.option("--port", type=click.IntRange(1, 65535), default=None,
              help="HTTP port (default: 8765; valid: 1–65535)")
@click.option("--db-path", "db_path", type=click.Path(), default=None,
              help="Database directory (default: ~/.archon-search/search); write path as-is")
@click.option("--log-level",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=True),
              default=None, help="Log level")
@click.option("--top-k", type=int, default=None, callback=_validate_top_k, is_eager=False,
              help="Number of results to return per query (default: 5; valid: 1–100)")
@click.option("--telemetry-retention-days", type=click.IntRange(min=1), default=None,
              help="Number of days to retain telemetry logs (requires --telemetry)")
@click.option("--enable-hyde", is_flag=True, default=False,
              help="Enable HyDE query expansion")
@click.option("--enable-rag-fusion", is_flag=True, default=False,
              help="Enable RAG Fusion query expansion")
@click.option("--server-key", type=_HexKeyParamType(), default=None,
              help="Custom server API key (lowercase hex, min 32 chars). Sets the archon-search Bearer token.")
def wizard(
    profile: str | None,
    multilingual: bool | None,
    skip_preload: bool,
    force: bool,
    delete_db: bool,
    dry_run: bool,
    non_interactive: bool,
    accept_jina_license: bool,
    accept_fasttext_license: bool,
    config_path: Path | None,
    code: bool | None,
    watch: bool | None,
    telemetry: bool | None,
    eager_load: bool | None,
    no_reranker: bool,
    routing_strategy: str | None,
    log_format: str | None,
    disable_gpu: bool,
    host: str | None,
    port: int | None,
    db_path: str | None,
    log_level: str | None,
    top_k: int | None,
    telemetry_retention_days: int | None,
    enable_hyde: bool,
    enable_rag_fusion: bool,
    server_key: str | None,
) -> None:
    """Interactive setup wizard: choose a profile, download models, start service."""
    # Warn if --telemetry-retention-days is given without --telemetry
    if telemetry_retention_days is not None and not telemetry:
        click.echo(
            "Warning: --telemetry-retention-days has no effect because telemetry is not enabled. "
            "Pass --telemetry to enable it.",
            err=True,
        )
        # Clear retention_days so it is not written to TOML
        telemetry_retention_days = None

    sys.exit(
        create_installer(
            config_file=str(config_path) if config_path else None,
            dry_run=dry_run,
        ).run(
            non_interactive=non_interactive,
            profile=profile,
            multilingual=multilingual,
            skip_preload=skip_preload,
            force=force,
            delete_db=delete_db,
            accept_jina_license=accept_jina_license,
            accept_fasttext_license=accept_fasttext_license,
            install_code=code,
            disable_reranker=no_reranker if no_reranker else None,
            enable_watch=watch,
            enable_telemetry=telemetry,
            eager_load=eager_load,
            routing_strategy=routing_strategy,
            log_format=log_format,
            disable_gpu=disable_gpu,
            host=host,
            port=port,
            db_path=db_path,
            log_level=log_level,
            top_k=top_k,
            telemetry_retention_days=telemetry_retention_days,
            enable_hyde=enable_hyde,
            enable_rag_fusion=enable_rag_fusion,
            server_key=server_key,
        )
    )


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Print actions without executing")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def install(dry_run: bool, config_path: Path | None) -> None:
    """Register and start the archon-search service.

    Run 'archon-search wizard' first to choose a profile and download models.
    """
    if os.environ.get("ARCHON_SEARCH_CONTAINER") == "1":
        click.echo(_CONTAINER_MSG, err=True)
        raise SystemExit(1)
    try:
        rc = create_installer(
            config_file=str(config_path) if config_path else None,
            dry_run=dry_run,
        ).run_register_and_start()
    except RuntimeError as exc:
        if "systemctl binary not found" in str(exc):
            click.echo(_CONTAINER_MSG, err=True)
        else:
            click.echo(f"Error during install: {exc}", err=True)
        raise SystemExit(1)
    sys.exit(rc)


@click.command()
@click.option("--delete-db", is_flag=True, default=False, help="Also delete the search database directory")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def uninstall(delete_db: bool, config_path: Path | None) -> None:
    """Uninstall archon-search service."""
    if os.environ.get("ARCHON_SEARCH_CONTAINER") == "1":
        click.echo(_CONTAINER_MSG, err=True)
        raise SystemExit(1)
    try:
        service = _get_service()
        service.stop()
        service.unregister()
    except RuntimeError as exc:
        if "systemctl binary not found" in str(exc):
            click.echo(_CONTAINER_MSG, err=True)
        else:
            click.echo(f"Error during service teardown: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error during service teardown: {exc}", err=True)
        raise SystemExit(1)

    if delete_db:
        db_path = _get_db_path(config_path)
        if db_path.exists():
            rmtree(db_path)
            click.echo(f"Deleted search database at {db_path}.")

    click.echo("archon-search uninstalled.")
