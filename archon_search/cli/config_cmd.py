"""archon-search config subcommands: show, get, set."""
from __future__ import annotations

from pathlib import Path

import click
import tomlkit

from archon_search.cli._helpers import _exit_on_config_error
from archon_search.config import SearchConfig, get_default_config_path, warn_if_legacy_config_ignored


def _default_toml() -> str:
    """Return the ``SearchConfig`` defaults that ``config show``/``get`` fill in for keys a file omits."""
    cfg = SearchConfig()
    doc = tomlkit.document()

    server = tomlkit.table()
    server.add("host", cfg.host)
    server.add("port", cfg.port)
    doc.add("server", server)

    database = tomlkit.table()
    database.add("db_path", cfg.db_path)
    database.add("embedding_model", cfg.embedding_model)
    database.add("reranker_model", cfg.reranker_model)
    database.add("chunk_size", cfg.chunk_size)
    database.add("auto_reindex_on_chunk_size_change", cfg.auto_reindex_on_chunk_size_change)
    database.add("eager_load_embedders", cfg.eager_load_embedders)
    doc.add("database", database)

    routing = tomlkit.table()
    routing.add("routing_shortlist_size", cfg.routing_shortlist_size)
    routing.add("routing_confidence_threshold", cfg.routing_confidence_threshold)
    routing.add("routing_strategy", cfg.routing_strategy)
    doc.add("routing", routing)

    collections = tomlkit.table()
    collections.add("pinned_collections", cfg.pinned_collections)
    collections.add("collections", cfg.collections)
    collections.add("watch", cfg.watch)
    doc.add("collections", collections)

    logging_table = tomlkit.table()
    logging_table.add("level", cfg.level)
    logging_table.add("log_file", cfg.log_file)
    logging_table.add("format", cfg.log_format)
    logging_table.add("backup_count", cfg.backup_count)
    doc.add("logging", logging_table)

    for section, feature in (("telemetry", cfg.telemetry), ("hyde", cfg.hyde), ("rag_fusion", cfg.rag_fusion)):
        feature_table = tomlkit.table()
        feature_table.add("enabled", feature.enabled)
        doc.add(section, feature_table)

    return tomlkit.dumps(doc)


def _resolve_config_path(config_path: Path | None) -> Path:
    """Return ``config_path`` or the default, exiting cleanly on a misconfigured data dir.

    Without ``--config``, first warns when an upgrade left the config at the legacy
    ``~/.archon-search`` location — otherwise ``config set`` would silently start a
    fresh, divergent config in the data dir.
    """
    with _exit_on_config_error():
        warn_if_legacy_config_ignored(config_path)
        return config_path or get_default_config_path()


def _effective_toml(path: Path) -> str:
    """Return the file's TOML with every missing default section/key filled in.

    The wizard deliberately omits unanswered keys from the written file, so a
    raw echo would show an incomplete config.
    """
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    defaults = tomlkit.parse(_default_toml())

    for section, table in defaults.items():
        if section not in doc:
            doc[section] = table
            continue
        for key, value in table.items():
            if key not in doc[section]:
                doc[section][key] = value

    return tomlkit.dumps(doc)


@click.group()
def config() -> None:
    """Show or edit configuration."""


@config.command("show")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def show(config_path: Path | None) -> None:
    """Show current configuration."""
    path = _resolve_config_path(config_path)
    click.echo(_effective_toml(path))


@config.command("get")
@click.argument("key")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def get(key: str, config_path: Path | None) -> None:
    """Get a configuration value by dotted key (e.g. server.port)."""
    path = _resolve_config_path(config_path)
    doc = tomlkit.parse(_effective_toml(path))

    parts = key.split(".")
    if len(parts) != 2:
        click.echo(f"Error: key must be in section.field format, got '{key}'", err=True)
        raise SystemExit(1)

    section, field = parts
    section_data = doc.get(section)
    if section_data is None or field not in section_data:
        click.echo(f"Error: key '{key}' not found in config", err=True)
        raise SystemExit(1)

    click.echo(section_data[field])


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def set_cmd(key: str, value: str, config_path: Path | None) -> None:
    """Set a configuration value by dotted key (e.g. server.port 9000)."""
    path = _resolve_config_path(config_path)

    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    parts = key.split(".")
    if len(parts) != 2:
        click.echo(f"Error: key must be in section.field format, got '{key}'", err=True)
        raise SystemExit(1)

    section, field = parts

    if section not in doc:
        doc.add(section, tomlkit.table())

    # Try to coerce to bool, int, or float if possible
    coerced: bool | str | int | float = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    else:
        try:
            coerced = int(value)
        except ValueError:
            try:
                coerced = float(value)
            except ValueError:
                pass

    doc[section][field] = coerced  # type: ignore[index]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")  # noqa: durable-write
    click.echo(f"Set {key} = {coerced}")
