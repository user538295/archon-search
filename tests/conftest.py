"""
packages/archon-search/tests/conftest.py — ML-model isolation and shared store fixture.

ALL sys.modules injections run at module level (import time), before pytest
discovers any test file.  This prevents ONNX model downloads and the
HuggingFace-tokenizers Rust-library process explosion.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# Cap native-runtime thread counts BEFORE ML libs / Tokio runtimes initialize.
# onnxruntime, OpenMP, OpenBLAS and LanceDB's Tokio runtime each default to
# spawning cpu_count() threads per process; with `-n auto` workers that yields
# 14×N threads competing for ~14 cores. Measured impact: −33% wall time.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("ORT_NUM_THREADS", "1")
os.environ.setdefault("TOKIO_WORKER_THREADS", "2")

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _search_stubs import install_stubs  # noqa: E402

install_stubs()

# Fixed test API key injected into all tests so create_app() uses a known key.
TEST_API_KEY = "0" * 64
os.environ["ARCHON_SEARCH_API_KEY"] = TEST_API_KEY

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-openapi-snapshot",
        action="store_true",
        default=False,
        help="Regenerate the OpenAPI spec snapshot baseline",
    )


# ---------------------------------------------------------------------------
# Session-scoped store fixture — one LanceDB connection per xdist worker session
# to avoid spawning a new Tokio thread pool for every test module.
# Under --dist=loadgroup each worker shares one SearchStore for its entire session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def connected_store(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One shared SearchStore per xdist worker session (sync connect/disconnect via asyncio.run).

    LanceDB's Rust/Tokio runtime is independent of the Python asyncio event loop,
    so the connected store is safely reusable across test-function event loops.
    """
    import asyncio

    from archon_search.store import SearchStore

    tmp_path = tmp_path_factory.mktemp("rag_db")
    store = SearchStore(tmp_path)
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


@pytest.fixture(scope="session")
def _archon_worker_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Per-worker isolated DATA_DIR. One per pytest session per xdist worker.

    Mirrors the connected_store and three_page_pdf session-scoped fixtures that
    use tmp_path_factory.mktemp to give each worker a private scratch directory.
    """
    return tmp_path_factory.mktemp("archon-data")


@pytest.fixture(autouse=True)
def _archon_isolated_data_dir(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _archon_worker_data_dir: Path,
) -> None:
    """Replaces _clear_archon_env_vars. Sets ARCHON_SEARCH_DATA_DIR per-worker
    unless the test is marked @pytest.mark.archon_unset_data_dir, in which case
    the env var is unset so the Path.home() default-fallback is exercised.

    `ARCHON_SEARCH_API_KEY` is intentionally NOT cleared — it is set globally above
    for auth test infrastructure and must remain set for all tests.
    `ANTHROPIC_API_KEY` IS cleared by a separate block below — see the inline
    comment for the SDK-timeout motivation.
    """
    for var in (
        "ARCHON_SEARCH_HOST",
        "ARCHON_SEARCH_PORT",
        "ARCHON_SEARCH_CONTAINER",
        "ARCHON_SEARCH_KEY_FILE",
        "ARCHON_SEARCH_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)

    # ANTHROPIC_API_KEY is a third-party vendor key (not archon-namespace).
    # When developers have it exported in their shell, every test that calls
    # `ingest_directory` on a new collection triggers `generate_description`,
    # which then sits in a 30 s asyncio.wait_for around the Claude SDK.
    # Clearing it here lets the SDK-calling modules that have early-exit guards
    # (`description_generator.generate_description`, `hyde.HyDEGenerator.generate`,
    # `rag_fusion.RAGFusionGenerator.generate_variants`) short-circuit on their
    # `os.environ.get("ANTHROPIC_API_KEY")` check. Other production paths that
    # read this key (e.g., `install.py` wizard prompts, `cli/install_cmd.py`) are
    # safely tested either in branches that are only entered when the key IS set
    # (so clearing it harmlessly skips those branches) or via explicit
    # `patch.dict("os.environ", {"ANTHROPIC_API_KEY": ...})` overrides, which
    # operate independently of monkeypatch and override the cleared state.
    # Tests that need the key set use `monkeypatch.setenv` themselves —
    # setenv on the same monkeypatch instance overwrites the cleared state, so
    # the per-test override wins. Uses raising=False so this is a no-op when the
    # key is absent (CI, fresh shells, tests that already cleared it themselves).
    # Side-effect: `live`/`live_eval` tests that gate on ANTHROPIC_API_KEY (e.g.,
    # `test_live_rag_fusion.py`) will always skip in the default suite because
    # this fixture clears the key before _skip_if_no_api_key() runs. There is
    # currently no supported way to run those tests within the standard test tree
    # while this autouse fixture is active; they must be invoked via a separate
    # mechanism that sets the key after fixture setup (e.g., a live conftest that
    # re-injects it via monkeypatch.setenv).
    # Downside: if the SDK ever renames ANTHROPIC_API_KEY upstream, this line
    # becomes dead and the 30 s floor returns — the guard-existence regression test
    # in tests/test_anthropic_key_guards.py mitigates that risk.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    if "archon_unset_data_dir" in request.keywords:
        monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(_archon_worker_data_dir))


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer auth headers using the test API key."""
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def col_name() -> str:
    """Unique LanceDB collection name per test (avoids cross-test pollution)."""
    return f"test-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Three-page PDF fixture (Task 5.1 — C3b)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def three_page_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped fixture returning the path to a deterministic three-page PDF.

    Generates a three-page PDF in a temporary directory on first use via reportlab.
    Page contents are pinned to "alpha content", "beta content", "gamma content".

    The PDF is byte-deterministic (invariant=True suppresses reportlab timestamps).
    Uses tmp_path_factory so each xdist worker gets its own isolated copy.
    """
    from _pdf_fixture import generate_three_page_pdf  # noqa: PLC0415

    pdf_path = tmp_path_factory.mktemp("pdfs") / "three_page.pdf"
    generate_three_page_pdf(pdf_path)
    return pdf_path
