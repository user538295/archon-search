"""archon-search config subcommands: show, get, set."""
from __future__ import annotations

from pathlib import Path

import click
import tomlkit

from archon_search.config import SearchConfig, get_default_config_path


def _default_toml() -> str:
    """Return a minimal default TOML string with SearchConfig defaults."""
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
    doc.add("database", database)

    return tomlkit.dumps(doc)


@click.group()
def config() -> None:
    """Show or edit configuration."""


@config.command("show")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def show(config_path: Path | None) -> None:
    """Show current configuration."""
    path = config_path or get_default_config_path()
    if path.exists():
        click.echo(path.read_text(encoding="utf-8"))
    else:
        click.echo(_default_toml())


@config.command("get")
@click.argument("key")
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Path to archon-search.toml")
def get(key: str, config_path: Path | None) -> None:
    """Get a configuration value by dotted key (e.g. server.port)."""
    path = config_path or get_default_config_path()
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.parse(_default_toml())

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
    path = config_path or get_default_config_path()

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

    # Try to coerce to int or float if possible
    coerced: str | int | float = value
    try:
        coerced = int(value)
    except ValueError:
        try:
            coerced = float(value)
        except ValueError:
            pass

    doc[section][field] = coerced  # type: ignore[index]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    click.echo(f"Set {key} = {coerced}")
