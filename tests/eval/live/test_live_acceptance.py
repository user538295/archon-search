"""Live-backend acceptance tests (brief tests 1, 3, 8, 9, 10).

Requires real fastembed + cross-encoder model weights.
Run with: uv run pytest -m live_eval tests/eval/live/test_live_acceptance.py -v --no-cov
"""
from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.eval.backends import EvalEmbedderBackend
from archon_search.eval.live_report import build_live_report
from archon_search.eval.runner import (
    _BASELINE_MODEL_VERSION_FIELDS,
    _build_pipeline_with_eval_backends,
    load_baseline,
    run_eval_suite,
)


# ---------------------------------------------------------------------------
# Test 1 — live backend uses real models
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_live_backend_uses_real_models(
    tmp_path: Path,
    live_corpus_root: Path,
) -> None:
    """Real models produce 384-dim embeddings, not the stub's 128-dim."""
    pipeline = await _build_pipeline_with_eval_backends(tmp_path, backend="live")
    try:
        vecs = pipeline._embedder._backend.encode(["hello world"])
        assert len(vecs[0]) == 384, f"expected 384-dim vectors, got {len(vecs[0])}"
        assert pipeline._embedder.model_name == "BAAI/bge-small-en-v1.5"
    finally:
        await pipeline.store.disconnect()


# ---------------------------------------------------------------------------
# Test 3 — model versions recorded in baseline
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_model_versions_recorded_in_baseline(
    tmp_path: Path,
    live_corpus_root: Path,
    live_runtime_cfg_path: Path,
) -> None:
    """importlib.metadata APIs are accessible; all 6 model-version fields round-trip."""
    fastembed_version = importlib.metadata.version("fastembed")
    assert fastembed_version, "fastembed version must be non-empty"
    assert re.match(r"^\d+\.\d+", fastembed_version), (
        f"fastembed version {fastembed_version!r} does not match semver pattern"
    )

    archon_version = importlib.metadata.version("archon-search")
    assert archon_version, "archon-search version must be non-empty"

    report = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")
    build_live_report(report)  # must not raise

    baseline_dict = {
        "eval_hash": "test-hash",
        "metrics": dataclasses.asdict(report.metrics),
        "runtime_config_hash": "test-runtime-hash",
        "command": "uv run pytest -m live_eval",
        "embedding_model_id": "BAAI/bge-small-en-v1.5",
        "embedding_model_version": fastembed_version,
        "reranker_model_id": "Xenova/ms-marco-MiniLM-L-6-v2",
        "reranker_model_version": fastembed_version,
        "archon_search_version": archon_version,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    tmp_json = tmp_path / "baseline.json"
    tmp_json.write_text(json.dumps(baseline_dict))

    baseline = load_baseline(tmp_json)
    for field_name in _BASELINE_MODEL_VERSION_FIELDS:
        val = getattr(baseline, field_name)
        assert val and isinstance(val, str), (
            f"{field_name} must be a non-empty string, got {val!r}"
        )


# ---------------------------------------------------------------------------
# Test 8 — calibration procedure
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_calibration_procedure(
    tmp_path: Path,
    live_corpus_root: Path,
    live_runtime_cfg_path: Path,
) -> None:
    """Full calibration workflow: run eval, build baseline dict, round-trip via load_baseline."""
    report = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")

    fastembed_version = importlib.metadata.version("fastembed")
    archon_version = importlib.metadata.version("archon-search")
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    baseline_dict = {
        "eval_hash": "calibration-test",
        "metrics": dataclasses.asdict(report.metrics),
        "runtime_config_hash": "calibration-runtime",
        "command": "uv run pytest -m live_eval",
        "embedding_model_id": "BAAI/bge-small-en-v1.5",
        "embedding_model_version": fastembed_version,
        "reranker_model_id": "Xenova/ms-marco-MiniLM-L-6-v2",
        "reranker_model_version": fastembed_version,
        "archon_search_version": archon_version,
        "captured_at": captured_at,
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline_dict))

    baseline = load_baseline(baseline_path)
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", baseline.captured_at), (
        f"captured_at format invalid: {baseline.captured_at!r}"
    )
    for field_name in _BASELINE_MODEL_VERSION_FIELDS:
        val = getattr(baseline, field_name)
        assert val and isinstance(val, str), (
            f"{field_name} must be a non-empty string, got {val!r}"
        )


# ---------------------------------------------------------------------------
# Test 9 — fixture isolation
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_fixture_isolation(
    tmp_path: Path,
    live_corpus_root: Path,
    live_runtime_cfg_path: Path,
) -> None:
    """The no-op autouse shadow ensures ARCHON_SEARCH_EVAL_BACKENDS is never set to '1'."""
    assert os.environ.get("ARCHON_SEARCH_EVAL_BACKENDS") != "1", (
        "ARCHON_SEARCH_EVAL_BACKENDS must not be '1' — parent autouse must be shadowed"
    )

    # Deterministic stubs still work in the live directory
    pipeline = await _build_pipeline_with_eval_backends(tmp_path, backend="deterministic")
    try:
        assert isinstance(pipeline._embedder._backend, EvalEmbedderBackend)
    finally:
        await pipeline.store.disconnect()

    # Run live eval once
    await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")

    # Env var still not set after live eval
    assert os.environ.get("ARCHON_SEARCH_EVAL_BACKENDS") != "1", (
        "ARCHON_SEARCH_EVAL_BACKENDS must not be '1' after live eval"
    )


# ---------------------------------------------------------------------------
# Test 10 — latency stability
# ---------------------------------------------------------------------------


@pytest.mark.live_eval
async def test_latency_stability(
    live_corpus_root: Path,
    live_runtime_cfg_path: Path,
) -> None:
    """Two consecutive live eval runs have latency_p95_ms within 50% of each other."""
    r1 = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")
    r2 = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")

    assert r1.metrics.latency_p95_ms > 0.0
    assert r2.metrics.latency_p95_ms > 0.0

    relative_diff = abs(r2.metrics.latency_p95_ms - r1.metrics.latency_p95_ms) / max(
        r1.metrics.latency_p95_ms, 1.0
    )
    assert relative_diff < 0.5, (
        f"latency_p95_ms diverged by {relative_diff:.1%}: "
        f"r1={r1.metrics.latency_p95_ms:.1f}ms, r2={r2.metrics.latency_p95_ms:.1f}ms"
    )
