"""Contract tests for tests/eval/README.md .

These tests assert that the eval README documents the key concepts a
maintainer needs in order to refresh thresholds, interpret baselines,
and understand latency caveats. They read README.md as text and assert
substring presence (case-insensitive).
"""
from __future__ import annotations

from pathlib import Path

README = Path(__file__).parent / "README.md"


def _read() -> str:
    assert README.exists(), f"Missing eval README: {README}"
    return README.read_text(encoding="utf-8").lower()


def test_eval_readme_mentions_threshold_baselines() -> None:
    text = _read()
    assert "baseline" in text
    assert "threshold" in text
    # Relationship between thresholds and baselines must be discussed
    # (floors at or below baseline values).
    assert ("at or below" in text) or ("from" in text and "baseline" in text)


def test_eval_readme_mentions_machine_readable_baseline_metadata() -> None:
    text = _read()
    assert "baseline.json" in text


def test_eval_readme_requires_threshold_lowering_rationale() -> None:
    text = _read()
    assert "rationale" in text
    assert "lower" in text


def test_eval_readme_mentions_floor_drop_waiver_policy() -> None:
    text = _read()
    assert ("waiver_ids" in text) or ("waiver" in text)


def test_eval_readme_mentions_document_level_metrics() -> None:
    text = _read()
    assert ("document-level" in text) or ("deduplicat" in text)


def test_eval_readme_mentions_eval_backend_latency_limits() -> None:
    text = _read()
    assert "deterministic" in text
    assert (
        "regression guard" in text
        or "not a production sla" in text
        or "not production slas" in text
        or "sla" in text
    )


# -------------------- package README --------------------


PACKAGE_README = Path(__file__).resolve().parents[2] / "README.md"


def test_package_readme_mentions_eval_command() -> None:
    assert PACKAGE_README.exists(), f"Missing package README: {PACKAGE_README}"
    text = PACKAGE_README.read_text(encoding="utf-8").lower()
    assert "pytest -m eval" in text
    assert "--thresholds-path" in text
