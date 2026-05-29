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
