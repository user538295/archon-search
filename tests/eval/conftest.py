"""Pytest fixtures and CLI options for the eval slice.

This module:

- Registers the ``--thresholds-path`` pytest CLI option used by +
  gated smoke tests.
- Provides module-scoped fixtures for the eval corpus and a temporary
  LanceDB root.
- Activates the deterministic eval backends from
  :mod:`archon_search.eval.backends` for every eval test so the suite never
  needs to download real embedding or reranker models.
- Generates the three-page PDF eval corpus fixture (Task 5.2, C3b) at
  session start so corpus contract tests and the eval runner both find it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
from archon_search.eval.fixtures import EvalCorpus, load_eval_corpus


CORPUS_ROOT = Path(__file__).resolve().parent

_EVAL_PDF_PATH = CORPUS_ROOT / "corpus" / "pdf-fixtures" / "three_page.pdf"

# Ensure tests/_pdf_fixture.py is importable from within the eval conftest.
_TESTS_DIR = CORPUS_ROOT.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# PDF corpus fixture (autouse, session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _generate_eval_corpus_pdf() -> None:
    """Generate tests/eval/corpus/pdf-fixtures/three_page.pdf before any test.

    Session-scoped and autouse so the PDF exists before corpus contract tests
    (tests/eval/test_corpus_contract.py) call load_eval_corpus() and before
    the eval runner ingests the corpus.  Reuses the shared generator from
    tests/_pdf_fixture.py so the textual content is identical across fixture
    copies.
    """
    from _pdf_fixture import generate_three_page_pdf  # noqa: PLC0415

    generate_three_page_pdf(_EVAL_PDF_PATH)


# ---------------------------------------------------------------------------
# --thresholds-path CLI option
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--thresholds-path",
        action="store",
        default=None,
        help=(
            "Path to thresholds.toml for gated eval smoke tests. "
            "Must be passed explicitly by CI; not auto-discovered."
        ),
    )


@pytest.fixture(scope="session")
def thresholds_path(pytestconfig: pytest.Config) -> Path:
    """Return the path passed via ``--thresholds-path``.

    - In CI (``CI`` env var set): ``pytest.fail`` so misconfiguration is loud.
    - Locally: ``pytest.skip`` with guidance.
    """
    raw = pytestconfig.getoption("--thresholds-path")
    if raw is None:
        if os.environ.get("CI"):
            pytest.fail(
                "thresholds-path not provided in CI — pass --thresholds-path explicitly"
            )
        pytest.skip(
            "thresholds-path not provided; use -k 'not gated' for report-only mode "
            "or pass --thresholds-path for gated mode."
        )
    return Path(raw)


# ---------------------------------------------------------------------------
# Corpus + LanceDB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def eval_corpus() -> EvalCorpus:
    """Load the committed eval corpus once per module."""
    return load_eval_corpus(CORPUS_ROOT)


@pytest.fixture(scope="module")
def eval_tmp_lancedb_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-scoped temporary directory for a fresh LanceDB store."""
    return tmp_path_factory.mktemp("eval_lancedb")


# ---------------------------------------------------------------------------
# Deterministic backend activation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _activate_deterministic_eval_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate deterministic eval backends for every eval test.

    Exposes the backend instances via env-var sentinels so callers that build
    pipelines can pick them up without importing test code, and also installs
    them on a shared registry attribute used by the harness when present.
    """
    embedder = EvalEmbedderBackend()
    reranker = EvalRerankerBackend()

    monkeypatch.setenv("ARCHON_SEARCH_EVAL_BACKENDS", "1")

    # Best-effort: if the eval backends module exposes a registry hook, set it.
    import archon_search.eval.backends as backends_mod

    monkeypatch.setattr(
        backends_mod, "_ACTIVE_EMBEDDER", embedder, raising=False
    )
    monkeypatch.setattr(
        backends_mod, "_ACTIVE_RERANKER", reranker, raising=False
    )
