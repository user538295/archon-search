"""Baseline contract tests — FEAT-039 Task 4.3.

Verifies that committed thresholds, baseline metadata, and the rendered baseline
report stay in sync with eval-determinism-defining inputs.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from archon_search.eval._hashing import (
    compute_eval_hash,
    compute_runtime_config_hash,
    compute_thresholds_hash,
)

EVAL_DIR = Path(__file__).resolve().parent
BASELINES_DIR = EVAL_DIR / "baselines"
BASELINE_JSON = BASELINES_DIR / "baseline.json"
BASELINE_MD = BASELINES_DIR / "baseline.md"
THRESHOLDS_TOML = EVAL_DIR / "thresholds.toml"
RUNTIME_TOML = EVAL_DIR / "runtime.toml"

_QUALITY_FIELDS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "ndcg_at_10",
    "routing_accuracy",
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _load_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text())


# ---------------------------------------------------------------------------
# Co-presence + metadata
# ---------------------------------------------------------------------------


def test_thresholds_have_matching_baseline_report() -> None:
    if not THRESHOLDS_TOML.exists():
        pytest.skip("thresholds.toml not committed yet")
    assert BASELINE_JSON.exists(), "thresholds.toml committed without baseline.json"
    assert BASELINE_MD.exists(), "thresholds.toml committed without baseline.md"


def test_baseline_metadata_hashes_match_benchmark_inputs() -> None:
    if not BASELINE_JSON.exists():
        pytest.skip("baseline.json not committed yet")
    data = _load_json(BASELINE_JSON)

    expected_eval = compute_eval_hash(EVAL_DIR)
    expected_runtime = compute_runtime_config_hash(RUNTIME_TOML)
    assert data["eval_hash"] == expected_eval, (
        "eval_hash drift — refresh baseline (inputs changed)"
    )
    assert data["runtime_config_hash"] == expected_runtime, (
        "runtime_config_hash drift — refresh baseline (runtime.toml changed)"
    )
    if THRESHOLDS_TOML.exists() and data.get("thresholds_hash") is not None:
        expected_thresholds = compute_thresholds_hash(THRESHOLDS_TOML)
        assert data["thresholds_hash"] == expected_thresholds, (
            "thresholds_hash drift — refresh baseline (thresholds.toml changed)"
        )


def test_baseline_metadata_records_eval_hash_and_command() -> None:
    if not BASELINE_JSON.exists():
        pytest.skip("baseline.json not committed yet")
    data = _load_json(BASELINE_JSON)
    assert isinstance(data.get("eval_hash"), str) and data["eval_hash"], (
        "baseline.json missing non-empty eval_hash"
    )
    assert isinstance(data.get("command"), str) and data["command"], (
        "baseline.json missing non-empty command"
    )
    assert isinstance(data.get("runtime_config_hash"), str) and data["runtime_config_hash"]
    if THRESHOLDS_TOML.exists():
        assert isinstance(data.get("thresholds_hash"), str) and data["thresholds_hash"]


# ---------------------------------------------------------------------------
# Quality floor contracts
# ---------------------------------------------------------------------------


def test_quality_floors_never_exceed_baseline() -> None:
    if not (THRESHOLDS_TOML.exists() and BASELINE_JSON.exists()):
        pytest.skip("thresholds + baseline not both committed yet")
    thresholds = _load_toml(THRESHOLDS_TOML).get("quality_floors", {})
    metrics = _load_json(BASELINE_JSON)["metrics"]
    for field in _QUALITY_FIELDS:
        if field not in thresholds:
            continue
        baseline_v = metrics.get(field)
        if baseline_v is None:
            continue
        floor_v = thresholds[field]
        assert floor_v <= baseline_v + 1e-9, (
            f"floor {field}={floor_v} exceeds baseline={baseline_v}"
        )


def test_quality_floor_below_baseline_requires_rationale() -> None:
    if not (THRESHOLDS_TOML.exists() and BASELINE_JSON.exists()):
        pytest.skip("thresholds + baseline not both committed yet")
    thresholds_raw = THRESHOLDS_TOML.read_text()
    baseline_md_raw = BASELINE_MD.read_text() if BASELINE_MD.exists() else ""
    thresholds = _load_toml(THRESHOLDS_TOML).get("quality_floors", {})
    metrics = _load_json(BASELINE_JSON)["metrics"]

    lowered: list[str] = []
    for field in _QUALITY_FIELDS:
        if field not in thresholds:
            continue
        baseline_v = metrics.get(field)
        if baseline_v is None:
            continue
        if baseline_v - thresholds[field] > 0.005:
            lowered.append(field)

    if not lowered:
        return
    combined = thresholds_raw + "\n" + baseline_md_raw
    assert ("rationale" in combined.lower()), (
        f"floors lowered below baseline ({lowered}) but no rationale found"
    )


def test_quality_floor_drop_beyond_policy_requires_waiver() -> None:
    if not (THRESHOLDS_TOML.exists() and BASELINE_JSON.exists()):
        pytest.skip("thresholds + baseline not both committed yet")
    thresholds_data = _load_toml(THRESHOLDS_TOML)
    policy_drop = float(
        thresholds_data.get("policy", {}).get("max_floor_drop_without_waiver", 0.05)
    )
    floors = thresholds_data.get("quality_floors", {})
    baseline = _load_json(BASELINE_JSON)
    metrics = baseline["metrics"]
    waivers = baseline.get("waiver_ids", {})

    for field in _QUALITY_FIELDS:
        if field not in floors:
            continue
        baseline_v = metrics.get(field)
        if baseline_v is None:
            continue
        if baseline_v - floors[field] > policy_drop:
            assert field in waivers, (
                f"floor for {field} drops more than {policy_drop} below baseline "
                f"({baseline_v} -> {floors[field]}) without a waiver_ids entry"
            )


# ---------------------------------------------------------------------------
# Hash helper unit tests
# ---------------------------------------------------------------------------


def _copy_eval_dir(dst: Path) -> Path:
    """Copy the minimal eval-input tree (excluding caches/test code) into dst."""
    shutil.copy2(EVAL_DIR / "documents.jsonl", dst / "documents.jsonl")
    shutil.copy2(EVAL_DIR / "queries.jsonl", dst / "queries.jsonl")
    shutil.copy2(EVAL_DIR / "labels.jsonl", dst / "labels.jsonl")
    shutil.copytree(EVAL_DIR / "corpus", dst / "corpus")
    if (EVAL_DIR / "routing").exists():
        shutil.copytree(EVAL_DIR / "routing", dst / "routing")
    return dst


def test_eval_hash_is_stable_for_same_inputs() -> None:
    h1 = compute_eval_hash(EVAL_DIR)
    h2 = compute_eval_hash(EVAL_DIR)
    assert h1 == h2


def test_eval_hash_changes_when_document_manifest_changes(tmp_path: Path) -> None:
    _copy_eval_dir(tmp_path)
    h_before = compute_eval_hash(tmp_path)
    docs = tmp_path / "documents.jsonl"
    docs.write_text(docs.read_text() + '{"doc_id":"tmp_extra","collection":"docs","relative_path":"docs/api_reference.md"}\n')
    h_after = compute_eval_hash(tmp_path)
    assert h_before != h_after


def test_eval_hash_changes_when_corpus_file_changes(tmp_path: Path) -> None:
    _copy_eval_dir(tmp_path)
    h_before = compute_eval_hash(tmp_path)
    # Mutate any corpus file.
    target = tmp_path / "corpus" / "docs" / "api_reference.md"
    target.write_text(target.read_text() + "\nextra line\n")
    h_after = compute_eval_hash(tmp_path)
    assert h_before != h_after


def test_eval_hash_changes_when_backends_py_changes(tmp_path: Path) -> None:
    _copy_eval_dir(tmp_path)
    # Make two copies of backends.py and verify hash changes when content differs.
    from archon_search.eval import _hashing

    real_backends = Path(_hashing.__file__).parent / "backends.py"
    fake_backends = tmp_path / "backends_modified.py"
    fake_backends.write_text(real_backends.read_text() + "\n# mutated for test\n")
    h_real = compute_eval_hash(tmp_path, backends_path=real_backends)
    h_mut = compute_eval_hash(tmp_path, backends_path=fake_backends)
    assert h_real != h_mut


def test_eval_hash_excludes_metrics_py(tmp_path: Path) -> None:
    """Mutating metrics.py must NOT affect compute_eval_hash output."""
    _copy_eval_dir(tmp_path)
    from archon_search.eval import _hashing

    eval_pkg_dir = Path(_hashing.__file__).parent
    real_backends = eval_pkg_dir / "backends.py"
    metrics_py = eval_pkg_dir / "metrics.py"

    h_before = compute_eval_hash(tmp_path, backends_path=real_backends)
    original = metrics_py.read_bytes()
    try:
        metrics_py.write_bytes(original + b"\n# transient mutation for test\n")
        h_after = compute_eval_hash(tmp_path, backends_path=real_backends)
    finally:
        metrics_py.write_bytes(original)
    assert h_before == h_after, "metrics.py changes must not change eval_hash"


def test_runtime_config_hash_changes_when_runtime_toml_changes(tmp_path: Path) -> None:
    f = tmp_path / "runtime.toml"
    f.write_text(RUNTIME_TOML.read_text())
    h_before = compute_runtime_config_hash(f)
    f.write_text(f.read_text() + "\n# trailing comment\n")
    h_after = compute_runtime_config_hash(f)
    assert h_before != h_after


def test_thresholds_hash_changes_when_thresholds_toml_changes(tmp_path: Path) -> None:
    f = tmp_path / "thresholds.toml"
    f.write_text("[quality_floors]\nrecall_at_1 = 0.5\n")
    h_before = compute_thresholds_hash(f)
    f.write_text(f.read_text() + "# trailing\n")
    h_after = compute_thresholds_hash(f)
    assert h_before != h_after
