"""Default-suite guard for ``tests/_live_model_cache.py``.

Both real-model lanes (``tests/eval/live/``, ``tests/eval/live_benchmark/``) are excluded
from the default run, so a conftest that stops importing the pin fixture would only show up
as a silent re-download in a lane nobody runs locally. This module checks the imports
structurally and exercises the helper's behaviour here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from archon_search import model_store
from archon_search.model_specs import (
    EmbedderSpec,
    RerankerSpec,
    get_embedder_spec,
    get_reranker_spec,
)
from archon_search.paths import get_models_dir
from tests import _live_model_cache
from tests._live_model_cache import (
    live_specs,
    missing_live_models,
    prefetch_live_models,
)

_TESTS_DIR = Path(__file__).parent
_HELPER_MODULE = "tests._live_model_cache"
_REQUIRED_FIXTURES = {"_pin_live_models_dir", "live_models_dir"}


@pytest.fixture
def live_models_dir(tmp_path: Path) -> Path:
    """Overrides the helper's fixture (not imported here): a throwaway cache per test, so
    the default suite never creates ``~/.archon-search/models``."""
    return tmp_path / "live_models"


@pytest.fixture(autouse=True)
def _pin_live_models_dir(
    _archon_isolated_data_dir: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_models_dir: Path
) -> None:
    """The lanes' autouse pin, on a data dir private to the test. The per-worker dir is shared
    by every test of the worker, and whatever an earlier test stored in its ``models/`` would make
    the pin's ``rmdir`` fail here."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    _live_model_cache.pin_models_dir(live_models_dir)


def _install_snapshot(models_dir: Path, spec: EmbedderSpec | RerankerSpec, revision: str | None = None) -> Path:
    snapshot = models_dir / f"models--{spec.repo.replace('/', '--')}" / "snapshots" / (revision or spec.revision)
    snapshot.mkdir(parents=True)
    for name in model_store.MODEL_FILES:
        (snapshot / name).write_bytes(b"x")
    return snapshot


def _names_imported_from_helper(conftest: Path) -> set[str]:
    tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _HELPER_MODULE
        for alias in node.names
    }


@pytest.mark.parametrize("lane", ["live", "live_benchmark"])
def test_real_model_lane_conftest_imports_the_cache_pin(lane: str) -> None:
    conftest = _TESTS_DIR / "eval" / lane / "conftest.py"

    assert _names_imported_from_helper(conftest) >= _REQUIRED_FIXTURES


def test_live_specs_are_the_benchmarked_embedder_and_reranker() -> None:
    embedder, reranker = live_specs()

    assert embedder == get_embedder_spec("BAAI/bge-small-en-v1.5")
    assert reranker == get_reranker_spec("Xenova/ms-marco-MiniLM-L-6-v2")


def test_pin_makes_the_backends_resolve_the_session_cache_after_the_root_isolation(
    live_models_dir: Path,
) -> None:
    """The root ``_archon_isolated_data_dir`` points the data dir at an empty per-worker
    directory; the autouse pin links its ``models/`` at the session's cache, so a snapshot
    there is found by the model store and a download would land there."""
    embedder, _ = live_specs()
    snapshot = _install_snapshot(live_models_dir, embedder)

    assert get_models_dir().resolve() == live_models_dir.resolve()
    assert model_store.model_path(embedder).resolve() == snapshot.resolve()


def test_pin_is_idempotent_and_replaces_an_empty_models_dir(live_models_dir: Path) -> None:
    link = get_models_dir()
    assert link.is_symlink()

    _live_model_cache.pin_models_dir(live_models_dir)  # next test of the same worker: link already there
    assert link.is_symlink() and link.resolve() == live_models_dir.resolve()

    link.unlink()
    link.mkdir()  # something created an empty real models/ before the pin ran
    _live_model_cache.pin_models_dir(live_models_dir)
    assert link.is_symlink() and link.resolve() == live_models_dir.resolve()


def test_missing_live_models_names_every_repo_without_a_pinned_snapshot(
    live_models_dir: Path,
) -> None:
    embedder, reranker = live_specs()
    _install_snapshot(live_models_dir, embedder)

    assert missing_live_models() == [reranker.repo]


def test_missing_live_models_is_empty_when_both_are_cached(
    live_models_dir: Path,
) -> None:
    embedder, reranker = live_specs()
    _install_snapshot(live_models_dir, embedder)
    _install_snapshot(live_models_dir, reranker)

    assert missing_live_models() == []


def test_a_snapshot_at_another_revision_counts_as_missing(
    live_models_dir: Path,
) -> None:
    embedder, reranker = live_specs()
    _install_snapshot(live_models_dir, embedder, revision="0" * 40)
    _install_snapshot(live_models_dir, reranker)

    assert missing_live_models() == [embedder.repo]


def test_prefetch_ensures_every_live_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[EmbedderSpec | RerankerSpec] = []
    monkeypatch.setattr(_live_model_cache, "ensure_model", lambda spec: fetched.append(spec) or Path("/unused"))

    prefetch_live_models()

    assert fetched == list(live_specs())
