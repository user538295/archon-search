"""Unit tests for the lazy fasttext models directory path (C9 Task 2.5).

Replaces the old module-level ``FASTTEXT_MODELS_DIR`` constant with
``get_fasttext_models_dir()`` so ``ARCHON_SEARCH_DATA_DIR`` redirects the
fasttext model cache at call time, not at import time. Also removes the
module-level ``_MULTILINGUAL_MODEL_PATH`` in ``server/app.py`` so the
multilingual model lookup is lazy.

The autouse fixture in ``tests/conftest.py`` clears
``ARCHON_SEARCH_DATA_DIR`` between tests, so each test can assume a clean
environment.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.archon_unset_data_dir
def test_get_fasttext_models_dir_default() -> None:
    """No env vars set → fall back to ``~/.archon-search/models``."""
    from archon_search.language_detector import get_fasttext_models_dir

    assert get_fasttext_models_dir() == Path.home() / ".archon-search" / "models"


def test_get_fasttext_models_dir_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARCHON_SEARCH_DATA_DIR="/data"`` → ``/data/models``."""
    from archon_search.language_detector import get_fasttext_models_dir

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
    assert get_fasttext_models_dir() == Path("/data/models")


def test_no_module_level_fasttext_models_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Setting ``ARCHON_SEARCH_DATA_DIR`` AFTER import must still redirect the
    models directory — pins the "resolved fresh on every call" contract for
    ``get_fasttext_models_dir()``."""
    from archon_search.language_detector import get_fasttext_models_dir

    data_dir = tmp_path / "guard-test"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    assert get_fasttext_models_dir() == data_dir / "models"


def test_no_module_level_multilingual_model_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full multilingual model path must also be resolved lazily —
    callers should compose ``get_fasttext_models_dir() / FASTTEXT_MODEL_FILENAME``
    at call time, not capture a module-level path constant."""
    from archon_search.language_detector import (
        FASTTEXT_MODEL_FILENAME,
        get_fasttext_models_dir,
    )

    data_dir = tmp_path / "guard-test"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    path = get_fasttext_models_dir() / FASTTEXT_MODEL_FILENAME
    assert path == data_dir / "models" / FASTTEXT_MODEL_FILENAME


def test_get_fasttext_models_dir_reflects_env_change_between_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two sequential calls return different paths when ``ARCHON_SEARCH_DATA_DIR``
    changes between them — parity with Task 2.4 ``get_jobs_file`` laziness
    contract."""
    from archon_search.language_detector import get_fasttext_models_dir

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(first_dir))
    first = get_fasttext_models_dir()
    assert first == first_dir / "models"

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(second_dir))
    second = get_fasttext_models_dir()
    assert second == second_dir / "models"
    assert first != second


def test_get_fasttext_models_dir_propagates_invalid_env_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid ``ARCHON_SEARCH_DATA_DIR`` (e.g. relative path) propagates as
    ``ValueError`` from ``get_data_dir()`` — parity with Task 2.4
    ``get_jobs_file`` error-propagation contract."""
    from archon_search.language_detector import get_fasttext_models_dir

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "relative/not/absolute")
    with pytest.raises(ValueError, match="must be an absolute path"):
        get_fasttext_models_dir()


def test_create_pipeline_uses_lazy_fasttext_models_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``create_pipeline()`` with ``multilingual=True`` must read
    ``ARCHON_SEARCH_DATA_DIR`` at runtime — pins the lazy resolution contract
    for the second factory call site (alongside ``server/app.py::create_app``).
    Added in the iterative review of Task 2.5 to close a TDD gap."""
    from unittest.mock import patch
    from archon_search.config import SearchConfig

    data_dir = tmp_path / "lazy-data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))

    cfg = SearchConfig()
    cfg.multilingual = True
    cfg.reranker_model = ""  # avoid touching ModelReranker

    captured_paths: list[Path] = []

    def _capture_init(self: object, model_path: Path) -> None:
        captured_paths.append(model_path)

    with (
        patch("archon_search.pipeline.SearchStore"),
        patch("archon_search.pipeline.ModelEmbedder"),
        patch(
            "archon_search.language_detector.LanguageDetector.__init__",
            _capture_init,
        ),
    ):
        from archon_search.pipeline import create_pipeline

        create_pipeline(cfg)

    assert captured_paths, "LanguageDetector was never constructed"
    assert captured_paths[0] == data_dir / "models" / "lid.176.ftz"
