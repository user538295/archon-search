"""Eval runner types and config loaders — FEAT-039.

Provides threshold and runtime config dataclasses with their loaders.
Further runner functionality belongs in later tasks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

_REQUIRED_QUALITY_KEYS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
)


@dataclass
class EvalQualityFloors:
    """Minimum acceptable quality metric thresholds."""

    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    ndcg_at_5: float
    ndcg_at_10: float
    routing_accuracy: float | None = None


@dataclass
class EvalLatencyCeilings:
    """Maximum acceptable latency thresholds (None = not gated)."""

    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None


@dataclass
class EvalThresholds:
    """Combined eval thresholds loaded from thresholds.toml."""

    quality_floors: EvalQualityFloors
    latency_ceilings: EvalLatencyCeilings = field(default_factory=EvalLatencyCeilings)
    max_floor_drop_without_waiver: float = 0.05


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_thresholds(config_path: Path) -> EvalThresholds:
    """Parse *config_path* (a TOML file) into :class:`EvalThresholds`.

    Raises :class:`ValueError` on:
    - Invalid TOML syntax
    - Missing required quality floor keys
    - Wrong type for any quality floor value
    """
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception as exc:
        raise ValueError(f"Invalid TOML in {config_path}: {exc}") from exc

    # --- quality_floors section -----------------------------------------------
    raw_floors = data.get("quality_floors", {})

    for key in _REQUIRED_QUALITY_KEYS:
        if key not in raw_floors:
            raise ValueError(
                f"Missing required key in [quality_floors]: {key!r}"
            )
        if not isinstance(raw_floors[key], (int, float)):
            raise ValueError(
                f"[quality_floors].{key} must be a float, got {type(raw_floors[key]).__name__!r}"
            )

    routing_accuracy = raw_floors.get("routing_accuracy")
    if routing_accuracy is not None and not isinstance(routing_accuracy, (int, float)):
        raise ValueError(
            f"[quality_floors].routing_accuracy must be a float, "
            f"got {type(routing_accuracy).__name__!r}"
        )

    quality_floors = EvalQualityFloors(
        recall_at_1=float(raw_floors["recall_at_1"]),
        recall_at_3=float(raw_floors["recall_at_3"]),
        recall_at_5=float(raw_floors["recall_at_5"]),
        mrr=float(raw_floors["mrr"]),
        ndcg_at_5=float(raw_floors["ndcg_at_5"]),
        ndcg_at_10=float(raw_floors["ndcg_at_10"]),
        routing_accuracy=float(routing_accuracy) if routing_accuracy is not None else None,
    )

    # --- latency_ceilings section (optional) ----------------------------------
    raw_latency = data.get("latency_ceilings", {})
    latency_ceilings = EvalLatencyCeilings(
        latency_p50_ms=float(raw_latency["latency_p50_ms"]) if "latency_p50_ms" in raw_latency else None,
        latency_p95_ms=float(raw_latency["latency_p95_ms"]) if "latency_p95_ms" in raw_latency else None,
    )

    # --- policy section -------------------------------------------------------
    raw_policy = data.get("policy", {})
    max_floor_drop = float(raw_policy.get("max_floor_drop_without_waiver", 0.05))

    return EvalThresholds(
        quality_floors=quality_floors,
        latency_ceilings=latency_ceilings,
        max_floor_drop_without_waiver=max_floor_drop,
    )


# ---------------------------------------------------------------------------
# EvalRuntimeConfig
# ---------------------------------------------------------------------------

_METRIC_K = 10  # nDCG@10 requires at least this depth


@dataclass
class EvalRuntimeConfig:
    """Eval runtime settings loaded from runtime.toml."""

    candidate_depth: int
    return_depth: int
    metric_depth: int
    routing_contract_enabled: bool


def load_runtime_config(config_path: Path) -> EvalRuntimeConfig:
    """Parse *config_path* (a TOML file) into :class:`EvalRuntimeConfig`.

    Raises :class:`ValueError` on:
    - Invalid TOML syntax
    - Missing [search] section
    - Wrong type for any depth field
    - Constraint violations (metric_depth >= 10, return_depth >= metric_depth,
      candidate_depth > return_depth)
    """
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception as exc:
        raise ValueError(f"Invalid TOML in {config_path}: {exc}") from exc

    if "search" not in data:
        raise ValueError("Missing required [search] section in runtime config")

    raw_search = data["search"]

    for key in ("candidate_depth", "return_depth", "metric_depth"):
        if key not in raw_search:
            raise ValueError(f"Missing required key in [search]: {key!r}")
        if not isinstance(raw_search[key], int):
            raise ValueError(
                f"[search].{key} must be an integer, got {type(raw_search[key]).__name__!r}"
            )

    candidate_depth: int = raw_search["candidate_depth"]
    return_depth: int = raw_search["return_depth"]
    metric_depth: int = raw_search["metric_depth"]

    if metric_depth < _METRIC_K:
        raise ValueError(
            f"metric_depth must be >= {_METRIC_K} (required for nDCG@10), got {metric_depth}"
        )
    if return_depth < metric_depth:
        raise ValueError(
            f"return_depth ({return_depth}) must be >= metric_depth ({metric_depth})"
        )
    if candidate_depth <= return_depth:
        raise ValueError(
            f"candidate_depth ({candidate_depth}) must be > return_depth ({return_depth})"
        )

    raw_routing = data.get("routing", {})
    routing_contract_enabled: bool = bool(raw_routing.get("contract_enabled", False))

    return EvalRuntimeConfig(
        candidate_depth=candidate_depth,
        return_depth=return_depth,
        metric_depth=metric_depth,
        routing_contract_enabled=routing_contract_enabled,
    )


def validate_routing_contract(
    runtime_cfg: EvalRuntimeConfig,
    thresholds: EvalThresholds,
) -> None:
    """Validate that routing_accuracy threshold is set when routing contract is enabled.

    Raises :class:`ValueError` if ``runtime_cfg.routing_contract_enabled`` is True
    but ``thresholds.quality_floors.routing_accuracy`` is None.
    """
    if runtime_cfg.routing_contract_enabled and thresholds.quality_floors.routing_accuracy is None:
        raise ValueError(
            "routing_contract_enabled=True requires a numeric routing_accuracy floor "
            "in thresholds config, but routing_accuracy is None"
        )
