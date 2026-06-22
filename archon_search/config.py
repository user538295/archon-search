"""Standalone config loader for archon-search."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from archon_search.constants import DEFAULT_FAST_MODEL, DEFAULT_ROUTING_DESCRIPTION_WEIGHT
from archon_search.paths import get_data_dir

_logger = logging.getLogger(__name__)

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(Exception):
    """Raised on invalid configuration values."""


@dataclass
class HyDEConfig:
    enabled: bool = False
    model: str = field(default_factory=lambda: DEFAULT_FAST_MODEL)
    timeout_seconds: float = 5.0
    max_requests_per_minute: int = 60


@dataclass
class RAGFusionConfig:
    enabled: bool = False
    model: str = field(default_factory=lambda: DEFAULT_FAST_MODEL)
    timeout_seconds: float = 5.0
    max_requests_per_minute: int = 60
    num_queries: int = 2


@dataclass
class TelemetryConfig:
    enabled: bool = False
    retention_days: int = 30
    export_enabled: bool = False
    log_dir: str = "~/.archon-search/search-logs"


@dataclass
class ObservabilityConfig:
    stage_timings_enabled: bool = True
    request_id_header: str = "X-Request-ID"


@dataclass
class JobsConfig:
    max_concurrent_bulk: int = 1
    checkpoint_interval: int = 100


_BACKUP_OUTPUT_DIR_MIN_PARTS: int = 3


@dataclass
class BackupConfig:
    interval_hours: int = 0
    keep: int = 7
    exclude: list[str] = field(default_factory=list)
    output_dir: str = ""  # empty → resolved to get_data_dir() / "backups" at load time


@dataclass
class MaintenanceConfig:
    interval_hours: int = 0
    fts_optimize: bool = True
    orphan_cleanup: bool = True
    failed_ingest_retry: bool = True
    retry_max_attempts: int = 3
    retry_max_age_hours: int = 72
    exclude: list[str] = field(default_factory=list)


@dataclass
class AuthConfig:
    rotate_grace_seconds: int = 0


@dataclass
class SearchConfig:
    # [server]
    host: str = "127.0.0.1"
    port: int = 8765
    # [database]
    db_path: str = "~/.archon-search/search"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    chunk_size: int = 512
    auto_reindex_on_chunk_size_change: bool = True
    providers: list[str] = field(default_factory=list)
    top_k_retrieve: int = 15
    top_k_return: int = 5
    # [database] — D6 install-time / background provider validation
    validation_timeout_seconds: int = 60
    # [search] — multi-collection fan-out execution bounds (B3)
    max_fanout: int = 8
    fanout_leg_trim: int = 40
    fanout_timeout_seconds: float = 30.0
    # [routing]
    routing_shortlist_size: int = 8
    routing_confidence_threshold: float = 0.30
    max_parallel_collections: int = 3
    routing_strategy: str = "centroid"
    routing_description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT
    # [database] — B5 incremental centroid
    centroid_recompute_threshold: int = 10_000
    # [database] — C0 tiered install profiles
    profile: str = ""
    multilingual: bool = False
    # [database] — C2 multilingual language detection
    language_detection_confidence_threshold: float = 0.7
    # [database] — C1 per-collection embedding model
    embedder_cache_size: int = 3
    eager_load_embedders: bool = False
    # [collections]
    pinned_collections: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    watch: bool = False
    # [logging]
    level: str = "INFO"
    log_file: str = "~/.archon-search/logs/archon-search.log"
    log_format: str = "text"
    backup_count: int = 7
    # [telemetry]
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    # [observability]
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    # [namespaces]
    namespaces: dict[str, str] = field(default_factory=dict)
    # [hyde]
    hyde: HyDEConfig = field(default_factory=HyDEConfig)
    # [rag_fusion]
    rag_fusion: RAGFusionConfig = field(default_factory=RAGFusionConfig)
    # [jobs]
    jobs: JobsConfig = field(default_factory=JobsConfig)
    # [backup]
    backup: BackupConfig = field(default_factory=BackupConfig)
    # [maintenance]
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    # [auth]
    auth: AuthConfig = field(default_factory=AuthConfig)


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

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")  # noqa: durable-write


def get_default_config_path() -> Path:
    env_val = os.environ.get("ARCHON_SEARCH_CONFIG")
    if env_val:
        expanded = os.path.expanduser(env_val)
        path = Path(expanded)
        # Relative paths are resolved against cwd (not the home directory).
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path
    return Path.home() / ".archon-search" / "archon-search.toml"


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


def _coerce_str(value: object, field_name: str) -> str:
    try:
        return str(value)
    except Exception as exc:
        raise ConfigError(f"Expected string for '{field_name}', got {type(value).__name__}") from exc


def load_config(path: Path | None = None, *, serve: bool = False) -> SearchConfig:
    """Load SearchConfig from a TOML file. Missing file returns all defaults.

    Args:
        path: Path to the TOML config file. Defaults to `get_default_config_path()`.
        serve: When True, set `host` default to `"0.0.0.0"` BEFORE TOML/env processing
            so foreground/container deployments bind to all interfaces by default.
            TOML `[server].host` and `ARCHON_SEARCH_HOST` env var still override it.

    Env var overrides applied after TOML parsing:
        ARCHON_SEARCH_HOST: overrides `config.host` (any non-empty string).
        ARCHON_SEARCH_PORT: overrides `config.port` (validated int in 1..65535).
        ARCHON_SEARCH_DATA_DIR: when set, overrides `config.db_path`,
            `config.log_file`, and `config.telemetry.log_dir` (derived under
            the data directory). Wins over any TOML-sourced values for those
            three fields.
    """
    if path is None:
        path = get_default_config_path()

    config = SearchConfig()
    if serve:
        # Set the `serve` mode default BEFORE TOML/env processing so they can override.
        config.host = "0.0.0.0"

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Fall through to the env var application block — the container/serve
        # deployment path typically has no TOML file mounted.
        text = None

    if text is not None:
        try:
            doc = tomlkit.parse(text)
        except Exception as exc:
            raise ConfigError(f"Failed to parse {path}: {exc}") from exc

        _apply_toml(config, doc)

    _apply_env_overrides(config)
    _post_process_backup(config)
    _post_process_maintenance(config)
    return config


def _apply_toml(config: SearchConfig, doc: tomlkit.TOMLDocument) -> None:
    """Apply TOML document values onto `config` in place."""
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
    if "top_k_retrieve" in database:
        top_k_retrieve = _coerce_int(database["top_k_retrieve"], "top_k_retrieve")
        if top_k_retrieve <= 0:
            raise ConfigError(f"top_k_retrieve must be > 0, got {top_k_retrieve}")
        config.top_k_retrieve = top_k_retrieve
    if "top_k_return" in database:
        top_k_return = _coerce_int(database["top_k_return"], "top_k_return")
        if top_k_return <= 0:
            raise ConfigError(f"top_k_return must be > 0, got {top_k_return}")
        config.top_k_return = top_k_return
    if "validation_timeout_seconds" in database:
        validation_timeout = _coerce_int(
            database["validation_timeout_seconds"], "validation_timeout_seconds"
        )
        if validation_timeout > 0:
            config.validation_timeout_seconds = validation_timeout
        else:
            _logger.warning(
                "validation_timeout_seconds must be > 0, got %s; falling back to default %s",
                validation_timeout,
                config.validation_timeout_seconds,
            )
    if "centroid_recompute_threshold" in database:
        threshold = _coerce_int(database["centroid_recompute_threshold"], "centroid_recompute_threshold")
        if threshold < 1:
            raise ConfigError("centroid_recompute_threshold must be >= 1")
        config.centroid_recompute_threshold = threshold
    if "centroid_incremental_enabled" in database:
        _logger.warning(
            "centroid_incremental_enabled is deprecated and ignored; "
            "the B5 incremental centroid path is always used"
        )
    if "profile" in database:
        config.profile = str(database["profile"])
    if "multilingual" in database:
        config.multilingual = _coerce_bool(database["multilingual"], "multilingual")
    if "language_detection_confidence_threshold" in database:
        ldc_threshold = _coerce_float(
            database["language_detection_confidence_threshold"],
            "language_detection_confidence_threshold",
        )
        # 0.0 is excluded: a zero threshold would accept all detections, making it meaningless.
        if not (0.0 < ldc_threshold <= 1.0):
            raise ConfigError(
                f"language_detection_confidence_threshold must be in (0.0, 1.0], got {ldc_threshold}"
            )
        config.language_detection_confidence_threshold = ldc_threshold
    if "embedder_cache_size" in database:
        embedder_cache_size = _coerce_int(database["embedder_cache_size"], "embedder_cache_size")
        if embedder_cache_size < 1:
            raise ConfigError("embedder_cache_size must be >= 1")
        config.embedder_cache_size = embedder_cache_size
    if "eager_load_embedders" in database:
        config.eager_load_embedders = _coerce_bool(database["eager_load_embedders"], "eager_load_embedders")

    search = doc.get("search", {})
    if "max_fanout" in search:
        max_fanout = _coerce_int(search["max_fanout"], "max_fanout")
        if max_fanout < 1:
            raise ConfigError(f"max_fanout must be >= 1, got {max_fanout}")
        config.max_fanout = max_fanout
    if "fanout_leg_trim" in search:
        fanout_leg_trim = _coerce_int(search["fanout_leg_trim"], "fanout_leg_trim")
        if fanout_leg_trim < 1:
            raise ConfigError(f"fanout_leg_trim must be >= 1, got {fanout_leg_trim}")
        config.fanout_leg_trim = fanout_leg_trim
    if "fanout_timeout_seconds" in search:
        fanout_timeout_seconds = _coerce_float(
            search["fanout_timeout_seconds"], "fanout_timeout_seconds"
        )
        if fanout_timeout_seconds <= 0:
            raise ConfigError(
                f"fanout_timeout_seconds must be > 0, got {fanout_timeout_seconds}"
            )
        config.fanout_timeout_seconds = fanout_timeout_seconds

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
    if "routing_strategy" in routing:
        strategy = str(routing["routing_strategy"])
        if strategy not in {"centroid", "hybrid"}:
            raise ConfigError(f"routing_strategy must be 'centroid' or 'hybrid', got {strategy!r}")
        config.routing_strategy = strategy
    if "routing_description_weight" in routing:
        description_weight = _coerce_float(routing["routing_description_weight"], "routing_description_weight")
        if not 0.0 <= description_weight <= 1.0:
            raise ConfigError(f"routing_description_weight must be in [0.0, 1.0], got {description_weight}")
        config.routing_description_weight = description_weight

    collections = doc.get("collections", {})
    if "pinned_collections" in collections:
        config.pinned_collections = list(collections["pinned_collections"])
    if "collections" in collections:
        config.collections = list(collections["collections"])
    if "watch" in collections:
        config.watch = _coerce_bool(collections["watch"], "watch")

    log_cfg = doc.get("logging", {})
    if "level" in log_cfg:
        raw_level = str(log_cfg["level"]).upper()
        if raw_level == "WARN":
            raw_level = "WARNING"
        if raw_level not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"[logging].level must be one of {sorted(_VALID_LOG_LEVELS)}, got {str(log_cfg['level'])!r}"
            )
        config.level = raw_level
    if "log_file" in log_cfg:
        config.log_file = str(log_cfg["log_file"])
    if "format" in log_cfg:
        fmt = str(log_cfg["format"])
        if fmt not in {"text", "json"}:
            raise ConfigError(f"[logging].format must be 'text' or 'json', got {fmt!r}")
        config.log_format = fmt
    if "backup_count" in log_cfg:
        bc = _coerce_int(log_cfg["backup_count"], "[logging].backup_count")
        if bc < 0:
            raise ConfigError(f"[logging].backup_count must be >= 0, got {bc}")
        config.backup_count = bc

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
            _logger.warning("telemetry: export_enabled is reserved for a future release and will be ignored")
            telemetry.export_enabled = False
        else:
            telemetry.export_enabled = export_enabled
    if "log_dir" in telemetry_cfg:
        log_dir = str(telemetry_cfg["log_dir"])
        if not log_dir:
            raise ConfigError("[telemetry].log_dir must be a non-empty string")
        telemetry.log_dir = log_dir
    config.telemetry = telemetry

    obs_cfg = doc.get("observability", {})
    observability = ObservabilityConfig()
    if "stage_timings_enabled" in obs_cfg:
        observability.stage_timings_enabled = _coerce_bool(
            obs_cfg["stage_timings_enabled"], "[observability].stage_timings_enabled"
        )
    if "request_id_header" in obs_cfg:
        header = str(obs_cfg["request_id_header"])
        if not header:
            raise ConfigError("[observability].request_id_header must be a non-empty string")
        observability.request_id_header = header
    config.observability = observability

    raw_ns = doc.get("namespaces", {})
    namespaces: dict[str, str] = {}
    for k, v in raw_ns.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(
                f"[namespaces] entries must be string key = string value; got {k!r} = {v!r}"
            )
        namespaces[k] = v
    config.namespaces = namespaces

    hyde_cfg = doc.get("hyde", {})
    hyde = HyDEConfig()
    if "enabled" in hyde_cfg:
        hyde.enabled = _coerce_bool(hyde_cfg["enabled"], "[hyde].enabled")
    if "model" in hyde_cfg:
        model = _coerce_str(hyde_cfg["model"], "[hyde].model")
        if not model:
            raise ConfigError("[hyde].model must be a non-empty string")
        hyde.model = model
    if "timeout_seconds" in hyde_cfg:
        timeout_seconds = _coerce_float(hyde_cfg["timeout_seconds"], "[hyde].timeout_seconds")
        if timeout_seconds <= 0:
            raise ConfigError(f"[hyde].timeout_seconds must be > 0, got {timeout_seconds}")
        hyde.timeout_seconds = timeout_seconds
    if "max_requests_per_minute" in hyde_cfg:
        max_rpm = _coerce_int(hyde_cfg["max_requests_per_minute"], "[hyde].max_requests_per_minute")
        if max_rpm < 1:
            raise ConfigError(f"[hyde].max_requests_per_minute must be >= 1, got {max_rpm}")
        hyde.max_requests_per_minute = max_rpm
    config.hyde = hyde

    rag_fusion_cfg = doc.get("rag_fusion", {})
    rag_fusion = RAGFusionConfig()
    if "enabled" in rag_fusion_cfg:
        rag_fusion.enabled = _coerce_bool(rag_fusion_cfg["enabled"], "[rag_fusion].enabled")
    if "model" in rag_fusion_cfg:
        model = _coerce_str(rag_fusion_cfg["model"], "[rag_fusion].model")
        if not model:
            raise ConfigError("[rag_fusion].model must be a non-empty string")
        rag_fusion.model = model
    if "timeout_seconds" in rag_fusion_cfg:
        timeout_seconds = _coerce_float(rag_fusion_cfg["timeout_seconds"], "[rag_fusion].timeout_seconds")
        if timeout_seconds <= 0:
            raise ConfigError(f"[rag_fusion].timeout_seconds must be > 0, got {timeout_seconds}")
        rag_fusion.timeout_seconds = timeout_seconds
    if "max_requests_per_minute" in rag_fusion_cfg:
        max_rpm = _coerce_int(rag_fusion_cfg["max_requests_per_minute"], "[rag_fusion].max_requests_per_minute")
        if max_rpm < 1:
            raise ConfigError(f"[rag_fusion].max_requests_per_minute must be >= 1, got {max_rpm}")
        rag_fusion.max_requests_per_minute = max_rpm
    if "num_queries" in rag_fusion_cfg:
        num_queries = _coerce_int(rag_fusion_cfg["num_queries"], "[rag_fusion].num_queries")
        if num_queries < 1 or num_queries > 5:
            raise ConfigError(f"[rag_fusion].num_queries must be between 1 and 5, got {num_queries}")
        if num_queries == 1:
            _logger.warning(
                "[rag_fusion].num_queries = 1: LLM overhead rarely justifies a single variant; consider num_queries >= 2"
            )
        rag_fusion.num_queries = num_queries
    config.rag_fusion = rag_fusion

    jobs_cfg = doc.get("jobs", {})
    jobs = JobsConfig()
    if "max_concurrent_bulk" in jobs_cfg:
        max_concurrent_bulk = _coerce_int(jobs_cfg["max_concurrent_bulk"], "[jobs].max_concurrent_bulk")
        if max_concurrent_bulk <= 0:
            raise ConfigError(f"[jobs].max_concurrent_bulk must be > 0, got {max_concurrent_bulk}")
        jobs.max_concurrent_bulk = max_concurrent_bulk
    if "checkpoint_interval" in jobs_cfg:
        checkpoint_interval = _coerce_int(jobs_cfg["checkpoint_interval"], "[jobs].checkpoint_interval")
        if checkpoint_interval <= 0:
            raise ConfigError(f"[jobs].checkpoint_interval must be > 0, got {checkpoint_interval}")
        jobs.checkpoint_interval = checkpoint_interval
    config.jobs = jobs

    backup_cfg = doc.get("backup", {})
    backup = BackupConfig()
    if "interval_hours" in backup_cfg:
        interval_hours = _coerce_int(backup_cfg["interval_hours"], "[backup].interval_hours")
        if interval_hours < 0:
            raise ConfigError(f"[backup].interval_hours must be >= 0, got {interval_hours}")
        backup.interval_hours = interval_hours
    if "keep" in backup_cfg:
        keep = _coerce_int(backup_cfg["keep"], "[backup].keep")
        if keep < 0:
            raise ConfigError(f"[backup].keep must be >= 0, got {keep}")
        backup.keep = keep
    if "exclude" in backup_cfg:
        backup.exclude = [str(p) for p in backup_cfg["exclude"]]
    if "output_dir" in backup_cfg:
        backup.output_dir = _coerce_str(backup_cfg["output_dir"], "[backup].output_dir")
    config.backup = backup

    maintenance_cfg = doc.get("maintenance", {})
    maintenance = MaintenanceConfig()
    if "interval_hours" in maintenance_cfg:
        maint_interval_hours = _coerce_int(maintenance_cfg["interval_hours"], "[maintenance].interval_hours")
        if maint_interval_hours < 0:
            raise ConfigError(f"[maintenance].interval_hours must be >= 0, got {maint_interval_hours}")
        maintenance.interval_hours = maint_interval_hours
    if "fts_optimize" in maintenance_cfg:
        maintenance.fts_optimize = _coerce_bool(maintenance_cfg["fts_optimize"], "[maintenance].fts_optimize")
    if "orphan_cleanup" in maintenance_cfg:
        maintenance.orphan_cleanup = _coerce_bool(maintenance_cfg["orphan_cleanup"], "[maintenance].orphan_cleanup")
    if "failed_ingest_retry" in maintenance_cfg:
        maintenance.failed_ingest_retry = _coerce_bool(
            maintenance_cfg["failed_ingest_retry"], "[maintenance].failed_ingest_retry"
        )
    if "retry_max_attempts" in maintenance_cfg:
        retry_max_attempts = _coerce_int(maintenance_cfg["retry_max_attempts"], "[maintenance].retry_max_attempts")
        if retry_max_attempts < 1:
            raise ConfigError(f"[maintenance].retry_max_attempts must be >= 1, got {retry_max_attempts}")
        maintenance.retry_max_attempts = retry_max_attempts
    if "retry_max_age_hours" in maintenance_cfg:
        retry_max_age_hours = _coerce_int(
            maintenance_cfg["retry_max_age_hours"], "[maintenance].retry_max_age_hours"
        )
        if retry_max_age_hours < 0:
            raise ConfigError(f"[maintenance].retry_max_age_hours must be >= 0, got {retry_max_age_hours}")
        maintenance.retry_max_age_hours = retry_max_age_hours
    if "exclude" in maintenance_cfg:
        maintenance.exclude = [str(p) for p in maintenance_cfg["exclude"]]
    config.maintenance = maintenance

    auth_cfg = doc.get("auth", {})
    auth = AuthConfig()
    if "rotate_grace_seconds" in auth_cfg:
        grace = _coerce_int(auth_cfg["rotate_grace_seconds"], "[auth].rotate_grace_seconds")
        if grace < 0:
            raise ConfigError(f"[auth].rotate_grace_seconds must be >= 0, got {grace}")
        auth.rotate_grace_seconds = grace
    config.auth = auth


def _post_process_maintenance(config: SearchConfig) -> None:
    """Validate and warn on maintenance config after TOML + env overrides are applied."""
    if config.maintenance.retry_max_age_hours == 0:
        _logger.warning(
            "[maintenance].retry_max_age_hours = 0: all failed ingest jobs will be immediately "
            "eligible for retry regardless of age; this may cause excessive retry churn"
        )


def _post_process_backup(config: SearchConfig) -> None:
    """Resolve and validate backup config after TOML + env overrides are applied."""
    default_output_dir = str(get_data_dir() / "backups")

    # Resolve empty output_dir to the default path.
    if not config.backup.output_dir:
        config.backup.output_dir = default_output_dir

    # Guard against near-root paths that could cause rotation to scan root-level dirs.
    output_path = Path(config.backup.output_dir)
    if len(output_path.parts) < _BACKUP_OUTPUT_DIR_MIN_PARTS:
        _logger.error(
            "[backup].output_dir %r has fewer than %d path components; "
            "falling back to default %r to prevent near-root directory scanning",
            config.backup.output_dir,
            _BACKUP_OUTPUT_DIR_MIN_PARTS,
            default_output_dir,
        )
        config.backup.output_dir = default_output_dir

    # Warn when rotation is effectively disabled while backups are enabled.
    if config.backup.interval_hours > 0 and config.backup.keep == 0:
        _logger.warning(
            "[backup] interval_hours=%d but keep=0: rotation is disabled; "
            "backup archives will accumulate without limit (unbounded disk growth risk)",
            config.backup.interval_hours,
        )


def _apply_env_overrides(config: SearchConfig) -> None:
    """Apply `ARCHON_SEARCH_*` env var overrides onto `config` in place.

    Precedence: env > TOML > dataclass default.

    Raises:
        ConfigError: on non-int or out-of-range `ARCHON_SEARCH_PORT`,
            or on misconfigured `ARCHON_SEARCH_DATA_DIR` (empty, relative,
            or otherwise rejected by `get_data_dir()`).
    """
    host_env = os.environ.get("ARCHON_SEARCH_HOST")
    if host_env:
        # Any non-empty string is a valid host at config time; empty string is
        # treated as "not set" (skip override) and preserves the existing value.
        config.host = host_env

    port_env = os.environ.get("ARCHON_SEARCH_PORT")
    if port_env:
        try:
            port = int(port_env)
        except ValueError as exc:
            raise ConfigError(
                f"ARCHON_SEARCH_PORT must be an integer, got {port_env!r}"
            ) from exc
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"ARCHON_SEARCH_PORT must be between 1 and 65535, got {port}"
            )
        config.port = port

    # ARCHON_SEARCH_DATA_DIR override. Only the three TOML-backed config
    # fields are overridden here. The other four runtime-state paths in the
    # plan's "Path derivations" table (key file, jobs file, fasttext models,
    # ingest history) are non-config: their domain modules call
    # `get_data_dir()` lazily at use sites in Tasks 2.3–2.6, not here.
    #
    # The env var name is duplicated as a literal string — `paths.py` defines
    # the same string as a private `_ENV_VAR` constant, but env var names are
    # the operator-facing public contract and the codebase consistently
    # hardcodes them rather than centralising. Keep both in sync if you ever
    # rename — `ARCHON_SEARCH_DATA_DIR` is the canonical operator-facing name.
    #
    # `get_data_dir()` reads the env var itself and raises `ValueError` on
    # misconfiguration (empty, relative, HOME-unset with `~`). The
    # `is not None` check distinguishes "unset" (skip override, fall back to
    # TOML/default) from "set to empty" (loud error). DATA_DIR's strictness
    # is intentionally asymmetric with HOST/PORT (which silently skip empty
    # strings): an empty DATA_DIR in a container is almost certainly an
    # operator error worth surfacing. We translate `ValueError` →
    # `ConfigError` so callers of `load_config()` only catch one type.
    if os.environ.get("ARCHON_SEARCH_DATA_DIR") is not None:
        try:
            data_dir = get_data_dir()
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        # Path suffixes must match `SearchConfig`'s dataclass defaults
        # (search / logs/archon-search.log / search-logs); the plan's
        # "Path derivations" table is the canonical source of truth.
        config.db_path = str(data_dir / "search")
        config.log_file = str(data_dir / "logs" / "archon-search.log")
        config.telemetry.log_dir = str(data_dir / "search-logs")
