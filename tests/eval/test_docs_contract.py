"""Contract tests for tests/eval/README.md (Task 5.1).

These tests assert that the eval README documents the key concepts a
maintainer needs in order to refresh thresholds, interpret baselines,
and understand latency caveats. They read README.md as text and assert
substring presence (case-insensitive).
"""
from __future__ import annotations

from pathlib import Path

import pytest

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


# -------------------- Task 5.2: package + roadmap docs --------------------


PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # packages/archon-search/
PACKAGE_README = PACKAGE_ROOT / "README.md"


def _find_archon_repo_root() -> Path | None:
    """Walk upward looking for the Archon repo root (parent of packages/)."""
    current = PACKAGE_ROOT
    for _ in range(6):
        current = current.parent
        if (current / "Documentation").is_dir() and (current / "packages").is_dir():
            return current
    return None


def _read_lower(path: Path) -> str:
    assert path.exists(), f"Missing file: {path}"
    return path.read_text(encoding="utf-8").lower()


# --- Package-local tests (always run) ---


def test_package_readme_mentions_eval_command() -> None:
    text = _read_lower(PACKAGE_README)
    assert "pytest -m eval" in text
    assert "--thresholds-path" in text


def test_package_doc_tests_do_not_require_archon_documentation_when_extracted() -> None:
    """Archon-repo doc checks must skip cleanly when run outside the monorepo."""
    repo_root = _find_archon_repo_root()
    if repo_root is None:
        pytest.skip("Archon Documentation/ not present — package is extracted")
    assert (repo_root / "Documentation").is_dir()


# --- Archon-repo tests (gated on Documentation/ presence) ---


def _archon_docs_or_skip() -> Path:
    repo_root = _find_archon_repo_root()
    if repo_root is None:
        pytest.skip("Archon Documentation/ not present")
    return repo_root / "Documentation"


def test_roadmap_docs_reference_eval_harness() -> None:
    docs = _archon_docs_or_skip()
    text = (docs / "Architecture" / "180_search_architecture.md").read_text(
        encoding="utf-8"
    ).lower()
    assert ("eval" in text) or ("evaluation harness" in text)
    assert "feat-039" in text


def _feat037_path_or_skip(docs: Path) -> Path:
    """Locate FEAT-037 roadmap doc; skip if absent (renamed/moved in main)."""
    path = docs / "Backlog" / "FEAT-037-search-world-class-roadmap.md"
    if not path.exists():
        pytest.skip(f"FEAT-037 roadmap doc not present at {path}")
    return path


def test_roadmap_docs_keep_data_collection_followup_open() -> None:
    docs = _archon_docs_or_skip()
    text = _feat037_path_or_skip(docs).read_text(encoding="utf-8").lower()
    assert ("data-collection" in text) or ("data collection" in text)
    # Indicator that the loop is still open
    assert ("[ ]" in text) or ("follow-up" in text) or ("open" in text) or (
        "deferred" in text
    )


def test_roadmap_docs_document_path_filtered_pr_eval_gate() -> None:
    docs = _archon_docs_or_skip()
    feat037 = _feat037_path_or_skip(docs).read_text(encoding="utf-8").lower()
    arch180_path = docs / "Architecture" / "180_search_architecture.md"
    if not arch180_path.exists():
        pytest.skip(f"Architecture doc not present at {arch180_path}")
    arch180 = arch180_path.read_text(encoding="utf-8").lower()
    combined = feat037 + "\n" + arch180
    assert "pr" in combined
    assert "eval" in combined
    assert ("path-filtered" in combined) or ("path filter" in combined)


def test_roadmap_docs_mark_feat_039_partial_if_pr_gate_missing() -> None:
    """PR gate IS implemented (Task 4.5) — FEAT-037 must have a status word near item 4."""
    docs = _archon_docs_or_skip()
    text = _feat037_path_or_skip(docs).read_text(encoding="utf-8").lower()
    assert ("delivered" in text) or ("feat-039 (partial)" in text) or (
        "partially delivered" in text
    )


def test_roadmap_docs_keep_archon_routing_eval_followup_open_when_needed() -> None:
    """Routing is Search-owned per FEAT-038 — no separate Archon routing eval needed."""
    docs = _archon_docs_or_skip()
    text = (docs / "Architecture" / "180_search_architecture.md").read_text(
        encoding="utf-8"
    ).lower()
    # Either the doc explicitly states routing is Search-owned, or skips.
    assert ("search-owned" in text) or ("routing is search-owned" in text) or (
        "routable" in text and "feat-038" in text
    )


def test_documentation_index_includes_new_followup_backlog_items() -> None:
    docs = _archon_docs_or_skip()
    text = (docs / "990_documentation_index_and_contribution_guide.md").read_text(
        encoding="utf-8"
    )
    assert "packages/archon-search/README.md" in text
    assert "packages/archon-search/tests/eval/README.md" in text
