"""Standalone config loader for archon-search."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

_logger = logging.getLogger("archon.search")


class ConfigError(Exception):
    """Raised on invalid configuration values."""


@dataclass
class TelemetryConfig:
    enabled: bool = False
    retention_days: int = 30
    export_enabled: bool = False
    log_dir: str = "~/.archon/search-logs"


@dataclass
class SearchConfig:
    # [server]
    host: str = "127.0.0.1"
    port: int = 8765
    # [database]
    db_path: str = "~/.archon/search"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    chunk_size: int = 512
    auto_reindex_on_chunk_size_change: bool = True
    providers: list[str] = field(default_factory=list)
    # [routing]
    routing_shortlist_size: int = 8
    routing_confidence_threshold: float = 0.30
    max_parallel_collections: int = 3
    # [collections]
    pinned_collections: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    watch: bool = False
    # [logging]
    level: str = "INFO"
    log_file: str = "~/.archon/logs/archon-search.log"
    # [telemetry]
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)


def save_config(config: SearchConfig, path: Path | str) -> None:
    """Write collections and pinned_collections back to the TOML file.

    Uses tomlkit for round-trip editing (preserves comments/formatting).
    If the file does not exist yet, creates it with the two arrays.
    """
    path = Path(path)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
    else:
        doc = tomlkit.document()

    if "collections" not in doc:
        doc.add("collections", tomlkit.table())  # type: ignore[arg-type]
    col_section = doc["collections"]
    col_section["collections"] = tomlkit.array()
    col_section["collections"].extend(config.collections)
    col_section["pinned_collections"] = tomlkit.array()
    col_section["pinned_collections"].extend(config.pinned_collections)

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def get_default_config_path() -> Path:
    return Path.home() / ".archon" / "archon-search.toml"


def _coerce_int(value: object, field_name: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Expected integer for '{field_name}', got {type(value).__name__}") from exc


def _coerce_float(value: object, field_name: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Expected float for '{field_name}', got {type(value).__name__}") from exc


def _coerce_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"Expected boolean for '{field_name}', got {type(value).__name__}")
    return value


def load_config(path: Path | None = None) -> SearchConfig:
    """Load SearchConfig from a TOML file. Missing file returns all defaults."""
    if path is None:
        path = get_default_config_path()

    config = SearchConfig()

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return config

    try:
        doc = tomlkit.parse(text)
    except Exception as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc

    server = doc.get("server", {})
    if "host" in server:
        config.host = str(server["host"])
    if "port" in server:
        port = _coerce_int(server["port"], "port")
        if not 1 <= port <= 65535:
            raise ConfigError(f"port must be between 1 and 65535, got {port}")
        config.port = port

    database = doc.get("database", {})
    if "db_path" in database:
        config.db_path = str(database["db_path"])
    if "embedding_model" in database:
        config.embedding_model = str(database["embedding_model"])
    if "reranker_model" in database:
        config.reranker_model = str(database["reranker_model"])
    if "chunk_size" in database:
        chunk_size = _coerce_int(database["chunk_size"], "chunk_size")
        if chunk_size <= 0:
            raise ConfigError(f"chunk_size must be > 0, got {chunk_size}")
        config.chunk_size = chunk_size
    if "auto_reindex_on_chunk_size_change" in database:
        config.auto_reindex_on_chunk_size_change = _coerce_bool(
            database["auto_reindex_on_chunk_size_change"], "auto_reindex_on_chunk_size_change"
        )
    if "providers" in database:
        config.providers = list(database["providers"])

    routing = doc.get("routing", {})
    if "routing_shortlist_size" in routing:
        routing_shortlist_size = _coerce_int(routing["routing_shortlist_size"], "routing_shortlist_size")
        if routing_shortlist_size <= 0:
            raise ConfigError(f"routing_shortlist_size must be > 0, got {routing_shortlist_size}")
        config.routing_shortlist_size = routing_shortlist_size
    if "routing_confidence_threshold" in routing:
        threshold = _coerce_float(routing["routing_confidence_threshold"], "routing_confidence_threshold")
        if not 0.0 <= threshold <= 1.0:
            raise ConfigError(f"routing_confidence_threshold must be in [0.0, 1.0], got {threshold}")
        config.routing_confidence_threshold = threshold
    if "max_parallel_collections" in routing:
        max_parallel = _coerce_int(routing["max_parallel_collections"], "max_parallel_collections")
        if max_parallel <= 0:
            raise ConfigError(f"max_parallel_collections must be > 0, got {max_parallel}")
        config.max_parallel_collections = max_parallel

    collections = doc.get("collections", {})
    if "pinned_collections" in collections:
        config.pinned_collections = list(collections["pinned_collections"])
    if "collections" in collections:
        config.collections = list(collections["collections"])
    if "watch" in collections:
        config.watch = _coerce_bool(collections["watch"], "watch")

    log_cfg = doc.get("logging", {})
    if "level" in log_cfg:
        config.level = str(log_cfg["level"])
    if "log_file" in log_cfg:
        config.log_file = str(log_cfg["log_file"])

    telemetry_cfg = doc.get("telemetry", {})
    telemetry = TelemetryConfig()
    if "enabled" in telemetry_cfg:
        telemetry.enabled = _coerce_bool(telemetry_cfg["enabled"], "[telemetry].enabled")
    if "retention_days" in telemetry_cfg:
        retention_days = _coerce_int(telemetry_cfg["retention_days"], "[telemetry].retention_days")
        if retention_days < 1:
            raise ConfigError("[telemetry].retention_days must be >= 1")
        telemetry.retention_days = retention_days
    if "export_enabled" in telemetry_cfg:
        export_enabled = _coerce_bool(
            telemetry_cfg["export_enabled"], "[telemetry].export_enabled"
        )
        if export_enabled:
            _logger.warning("telemetry: export attempt rejected")
            raise ConfigError(
                "[telemetry].export_enabled is reserved for FEAT-039c and must be false in v1"
            )
        telemetry.export_enabled = export_enabled
    if "log_dir" in telemetry_cfg:
        log_dir = str(telemetry_cfg["log_dir"])
        if not log_dir:
            raise ConfigError("[telemetry].log_dir must be a non-empty string")
        telemetry.log_dir = log_dir
    config.telemetry = telemetry

    return config
