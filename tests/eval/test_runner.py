"""Tests for EvalThresholds dataclasses and load_thresholds() — FEAT-039 Task 1.4."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from archon_search.eval.runner import (
    EvalLatencyCeilings,
    EvalQualityFloors,
    EvalRuntimeConfig,
    EvalThresholds,
    load_runtime_config,
    load_thresholds,
    validate_routing_contract,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "thresholds.toml"
    p.write_text(textwrap.dedent(content))
    return p


_FULL_QUALITY = """
[quality_floors]
recall_at_1 = 0.60
recall_at_3 = 0.75
recall_at_5 = 0.80
mrr = 0.65
ndcg_at_5 = 0.70
ndcg_at_10 = 0.72
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_thresholds_reads_all_metrics(tmp_path: Path) -> None:
    """Valid TOML with all quality fields parses into EvalThresholds correctly."""
    path = _write_toml(tmp_path, _FULL_QUALITY)
    result = load_thresholds(path)

    assert isinstance(result, EvalThresholds)
    floors = result.quality_floors
    assert isinstance(floors, EvalQualityFloors)
    assert floors.recall_at_1 == pytest.approx(0.60)
    assert floors.recall_at_3 == pytest.approx(0.75)
    assert floors.recall_at_5 == pytest.approx(0.80)
    assert floors.mrr == pytest.approx(0.65)
    assert floors.ndcg_at_5 == pytest.approx(0.70)
    assert floors.ndcg_at_10 == pytest.approx(0.72)
    assert floors.routing_accuracy is None


def test_load_thresholds_allows_omitted_routing_accuracy(tmp_path: Path) -> None:
    """Omitting routing_accuracy results in None — no error raised."""
    path = _write_toml(tmp_path, _FULL_QUALITY)
    result = load_thresholds(path)
    assert result.quality_floors.routing_accuracy is None


def test_load_thresholds_accepts_optional_routing_floor_shape(tmp_path: Path) -> None:
    """routing_accuracy = 0.8 is accepted as a valid float."""
    content = _FULL_QUALITY + "routing_accuracy = 0.8\n"
    path = _write_toml(tmp_path, content)
    result = load_thresholds(path)
    assert result.quality_floors.routing_accuracy == pytest.approx(0.8)


def test_load_thresholds_rejects_missing_metric(tmp_path: Path) -> None:
    """Missing ndcg_at_10 raises ValueError."""
    content = """
[quality_floors]
recall_at_1 = 0.60
recall_at_3 = 0.75
recall_at_5 = 0.80
mrr = 0.65
ndcg_at_5 = 0.70
"""
    path = _write_toml(tmp_path, content)
    with pytest.raises(ValueError, match="ndcg_at_10"):
        load_thresholds(path)


def test_load_thresholds_reads_floor_drop_policy(tmp_path: Path) -> None:
    """max_floor_drop_without_waiver parses correctly and defaults to 0.05."""
    # Default — no [policy] section
    path = _write_toml(tmp_path, _FULL_QUALITY)
    result = load_thresholds(path)
    assert result.max_floor_drop_without_waiver == pytest.approx(0.05)

    # Explicit value
    content = _FULL_QUALITY + "\n[policy]\nmax_floor_drop_without_waiver = 0.10\n"
    sub = tmp_path / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    path2 = _write_toml(sub, content)
    result2 = load_thresholds(path2)
    assert result2.max_floor_drop_without_waiver == pytest.approx(0.10)


def test_load_thresholds_rejects_malformed_toml_syntax(tmp_path: Path) -> None:
    """Invalid TOML syntax raises ValueError."""
    path = tmp_path / "thresholds.toml"
    path.write_text("this is not valid toml ===\n[broken\n")
    with pytest.raises(ValueError, match="[Ii]nvalid|[Pp]arse|TOML"):
        load_thresholds(path)


def test_load_thresholds_rejects_wrong_type_for_routing_floor(tmp_path: Path) -> None:
    """routing_accuracy = 'high' (string) raises ValueError."""
    content = _FULL_QUALITY + 'routing_accuracy = "high"\n'
    path = _write_toml(tmp_path, content)
    with pytest.raises(ValueError, match="routing_accuracy"):
        load_thresholds(path)


# ---------------------------------------------------------------------------
# EvalRuntimeConfig / load_runtime_config tests — Task 1.5
# ---------------------------------------------------------------------------

_VALID_RUNTIME_TOML = """
[search]
candidate_depth = 40
return_depth = 20
metric_depth = 10

[routing]
contract_enabled = true
"""

_COMMITTED_RUNTIME_TOML = Path(__file__).parent / "runtime.toml"


def _write_runtime_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "runtime.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_load_runtime_config_reads_search_depths(tmp_path: Path) -> None:
    """Valid TOML parses correctly into EvalRuntimeConfig."""
    path = _write_runtime_toml(tmp_path, _VALID_RUNTIME_TOML)
    cfg = load_runtime_config(path)

    assert isinstance(cfg, EvalRuntimeConfig)
    assert cfg.candidate_depth == 40
    assert cfg.return_depth == 20
    assert cfg.metric_depth == 10
    assert cfg.routing_contract_enabled is True


def test_committed_runtime_toml_exists_and_loads() -> None:
    """The committed tests/eval/runtime.toml loads without error."""
    assert _COMMITTED_RUNTIME_TOML.exists(), "tests/eval/runtime.toml must be committed"
    cfg = load_runtime_config(_COMMITTED_RUNTIME_TOML)
    assert isinstance(cfg, EvalRuntimeConfig)


def test_committed_runtime_toml_uses_eval_depth_names() -> None:
    """The committed runtime.toml uses the correct eval-specific depth key names."""
    cfg = load_runtime_config(_COMMITTED_RUNTIME_TOML)
    assert cfg.candidate_depth >= 1
    assert cfg.return_depth >= 1
    assert cfg.metric_depth >= 10


def test_load_runtime_config_rejects_metric_depth_below_metric_k(tmp_path: Path) -> None:
    """metric_depth < 10 raises ValueError (nDCG@10 requires depth >= 10)."""
    content = """
[search]
candidate_depth = 40
return_depth = 20
metric_depth = 9

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="metric_depth"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_return_depth_below_metric_depth(tmp_path: Path) -> None:
    """return_depth < metric_depth raises ValueError."""
    content = """
[search]
candidate_depth = 40
return_depth = 9
metric_depth = 10

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="return_depth"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_candidate_depth_not_greater_than_return_depth(tmp_path: Path) -> None:
    """candidate_depth <= return_depth raises ValueError."""
    content = """
[search]
candidate_depth = 20
return_depth = 20
metric_depth = 10

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="candidate_depth"):
        load_runtime_config(path)


def test_runner_requires_routing_floor_when_routing_contract_enabled(tmp_path: Path) -> None:
    """When routing_contract_enabled=True and thresholds.routing_accuracy is None, raise ValueError."""
    runtime_path = _write_runtime_toml(tmp_path, _VALID_RUNTIME_TOML)
    runtime_cfg = load_runtime_config(runtime_path)

    # Thresholds with routing_accuracy=None (not set)
    threshold_path = _write_toml(tmp_path, _FULL_QUALITY)
    thresholds = load_thresholds(threshold_path)

    assert thresholds.quality_floors.routing_accuracy is None
    assert runtime_cfg.routing_contract_enabled is True

    with pytest.raises(ValueError, match="routing_accuracy"):
        validate_routing_contract(runtime_cfg, thresholds)


def test_load_runtime_config_rejects_malformed_toml_syntax(tmp_path: Path) -> None:
    """Invalid TOML syntax raises ValueError."""
    path = tmp_path / "runtime.toml"
    path.write_text("this is not valid toml ===\n[broken\n")
    with pytest.raises(ValueError, match="[Ii]nvalid|[Pp]arse|TOML"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_missing_search_table(tmp_path: Path) -> None:
    """Missing [search] section raises ValueError."""
    content = """
[routing]
contract_enabled = true
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="[Ss]earch"):
        load_runtime_config(path)


def test_load_runtime_config_rejects_wrong_type_for_depth_field(tmp_path: Path) -> None:
    """Non-integer candidate_depth raises ValueError."""
    content = """
[search]
candidate_depth = "forty"
return_depth = 20
metric_depth = 10

[routing]
contract_enabled = false
"""
    path = _write_runtime_toml(tmp_path, content)
    with pytest.raises(ValueError, match="candidate_depth"):
        load_runtime_config(path)
