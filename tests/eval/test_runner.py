"""Tests for EvalThresholds dataclasses and load_thresholds() — FEAT-039 Task 1.4."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from archon_search.eval.runner import (
    EvalLatencyCeilings,
    EvalQualityFloors,
    EvalThresholds,
    load_thresholds,
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
