"""Pure-logic acceptance tests for the live eval lane (marked `eval`).

These tests do not require model weights and run in the PR eval suite.
"""

import pytest
import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"
CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CFG_PATH = CORPUS_ROOT / "runtime.toml"


@pytest.mark.eval
def test_live_eval_marker_excluded_from_default_run() -> None:
    """live_eval must appear in both markers list and addopts exclusion expression."""
    with PYPROJECT_PATH.open("rb") as f:
        config = tomllib.load(f)

    pytest_cfg = config["tool"]["pytest"]["ini_options"]
    markers: list[str] = pytest_cfg["markers"]
    addopts: str = pytest_cfg["addopts"]

    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "live_eval" in marker_names, "live_eval marker must be registered in pyproject.toml [markers]"
    assert "not live_eval" in addopts, "live_eval must be negated in the addopts exclusion expression"


@pytest.mark.eval
async def test_deterministic_backend_uses_stubs(tmp_path: Path) -> None:
    """Default backend must use EvalEmbedderBackend and EvalRerankerBackend (brief test 2)."""
    from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
    from archon_search.eval.runner import _build_pipeline_with_eval_backends

    pipeline = await _build_pipeline_with_eval_backends(tmp_path)
    assert isinstance(pipeline._embedder._backend, EvalEmbedderBackend)
    assert isinstance(pipeline._reranker._backend, EvalRerankerBackend)


@pytest.mark.eval
async def test_live_backend_guard_fires_when_eval_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """live backend must raise RuntimeError when ARCHON_SEARCH_EVAL_BACKENDS=1 is set."""
    from archon_search.eval.runner import _build_pipeline_with_eval_backends

    monkeypatch.setenv("ARCHON_SEARCH_EVAL_BACKENDS", "1")
    with pytest.raises(RuntimeError, match="ARCHON_SEARCH_EVAL_BACKENDS"):
        await _build_pipeline_with_eval_backends(tmp_path, backend="live")


@pytest.mark.eval
async def test_run_eval_suite_default_backend_is_deterministic() -> None:
    """run_eval_suite() with no backend kwarg uses deterministic backend and returns EvalReport."""
    from archon_search.eval.runner import EvalReport, run_eval_suite

    report = await run_eval_suite(CORPUS_ROOT, RUNTIME_CFG_PATH)
    assert isinstance(report, EvalReport)
    assert report.metrics.recall_at_1 >= 0.0


_LIVE_THRESHOLDS_TOML = CORPUS_ROOT / "live_thresholds.toml"

_VALID_THRESHOLDS_CONTENT = """\
[quality_floors]
recall_at_1 = 0.5
recall_at_3 = 0.7
recall_at_5 = 0.8
mrr = 0.6
ndcg_at_5 = 0.65
ndcg_at_10 = 0.7

[latency_ceilings]
latency_p50_ms = 200.0
latency_p95_ms = 500.0
"""


@pytest.mark.eval
def test_load_live_thresholds_with_valid_file(tmp_path: Path) -> None:
    """Valid TOML with [quality_floors] returns EvalThresholds (brief test 4, with-section case)."""
    from archon_search.eval.live_report import load_live_thresholds
    from archon_search.eval.runner import EvalThresholds

    p = tmp_path / "live_thresholds.toml"
    p.write_text(_VALID_THRESHOLDS_CONTENT)
    result = load_live_thresholds(p)
    assert isinstance(result, EvalThresholds)
    assert result.quality_floors.recall_at_1 == 0.5


@pytest.mark.eval
def test_load_live_thresholds_empty_stub() -> None:
    """The comment-only stub returns None without raising (brief test 4, without-section case)."""
    from archon_search.eval.live_report import load_live_thresholds

    result = load_live_thresholds(_LIVE_THRESHOLDS_TOML)
    assert result is None


@pytest.mark.eval
def test_load_live_thresholds_missing_file(tmp_path: Path) -> None:
    """Non-existent path returns None without raising."""
    from archon_search.eval.live_report import load_live_thresholds

    result = load_live_thresholds(tmp_path / "nonexistent.toml")
    assert result is None


@pytest.mark.eval
def test_load_live_thresholds_malformed_toml(tmp_path: Path) -> None:
    """Invalid TOML syntax returns None without raising."""
    from archon_search.eval.live_report import load_live_thresholds

    p = tmp_path / "bad.toml"
    p.write_text("[[[\n")
    result = load_live_thresholds(p)
    assert result is None
