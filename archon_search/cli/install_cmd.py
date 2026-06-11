"""archon-search install and uninstall subcommands."""
from __future__ import annotations

import sys
from pathlib import Path
from shutil import rmtree

import click

from archon_search.cli._helpers import _get_service
from archon_search.install import SearchInstaller

_TOP_K_MAX = 100
_TOP_K_MIN = 1


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
@click.option("--log-to-stderr", is_flag=True, default=False,
              help="Log to stderr only (sets log_file=''); canonical container combo: --log-format json --log-to-stderr")
@click.option("--top-k", type=int, default=None, callback=_validate_top_k, is_eager=False,
              help="Number of results to return per query (default: 5; valid: 1–100)")
@click.option("--telemetry-retention-days", type=click.IntRange(min=1), default=None,
              help="Number of days to retain telemetry logs (requires --telemetry)")
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
    log_to_stderr: bool,
    top_k: int | None,
    telemetry_retention_days: int | None,
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
        SearchInstaller(
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
            log_to_stderr=log_to_stderr,
            top_k=top_k,
            telemetry_retention_days=telemetry_retention_days,
        )
    )


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Print actions without executing")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def install(dry_run: bool, config_path: Path | None) -> None:
    """Register and start the archon-search service.

    Run 'archon-search wizard' first to choose a profile and download models.
    """
    import sys
    rc = SearchInstaller(
        config_file=str(config_path) if config_path else None,
        dry_run=dry_run,
    ).run_register_and_start()
    sys.exit(rc)


@click.command()
@click.option("--delete-db", is_flag=True, default=False, help="Also delete the search database directory")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def uninstall(delete_db: bool, config_path: Path | None) -> None:
    """Uninstall archon-search service."""
    try:
        service = _get_service()
        service.stop()
        service.unregister()
    except Exception as exc:
        click.echo(f"Error during service teardown: {exc}", err=True)
        raise SystemExit(1)

    if delete_db:
        db_path = _get_db_path(config_path)
        if db_path.exists():
            rmtree(db_path)
            click.echo(f"Deleted search database at {db_path}.")

    click.echo("archon-search uninstalled.")
