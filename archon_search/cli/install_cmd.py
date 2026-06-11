"""archon-search install and uninstall subcommands."""
from __future__ import annotations

import sys
from pathlib import Path
from shutil import rmtree

import click

from archon_search.cli._helpers import _get_service
from archon_search.install import SearchInstaller


def _get_db_path(config_path: Path | None = None) -> Path:
    """Return the expanded database path from config."""
    from archon_search.config import load_config
    cfg = load_config(config_path)
    return Path(cfg.db_path).expanduser()


def _install_options(f: click.decorators.FC) -> click.decorators.FC:
    for decorator in reversed([
        click.option("--profile", type=click.Choice(["minimal", "balanced", "max"]), default=None, help="Install profile"),
        click.option("--multilingual", is_flag=True, default=False, help="Use multilingual models"),
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
def wizard(
    profile: str | None,
    multilingual: bool,
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
) -> None:
    """Interactive setup wizard: choose a profile, download models, start service."""
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
