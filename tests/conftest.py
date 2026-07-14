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
from typing import Generator
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# C18 Fix 2 — session-level Anthropic token-burn prevention
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _block_anthropic_key_at_session() -> None:
    """Remove ANTHROPIC_API_KEY before any session-scoped fixture can fire.

    The function-scoped autouse _archon_isolated_data_dir clears the key per
    test body, but session fixtures run before it. A session fixture that calls
    ingest_directory would see the key live unless this fixture removes it first.
    Not restored after the session — tests that need the key use monkeypatch.setenv.
    """
    os.environ.pop("ANTHROPIC_API_KEY", None)


@pytest.fixture(autouse=True, scope="session")
def _block_anthropic_client() -> Generator[None, None, None]:
    """Mock anthropic.Anthropic and anthropic.AsyncAnthropic to raise if instantiated.

    Guards against token burns even when the env-var guard is bypassed — e.g. a
    future code path that constructs the client without checking ANTHROPIC_API_KEY.
    Only active when the `anthropic` package is installed (hyde/rag_fusion extras).
    Existing tests that patch sys.modules["anthropic"] with a mock module are
    unaffected: patch.dict replaces the module object entirely, so the lazy
    `import anthropic` inside generators gets the mock, not the real module.
    """
    try:
        import anthropic as _anthropic_mod  # noqa: PLC0415
    except ImportError:
        yield  # anthropic extra not installed — nothing to block
        return

    def _raise(*a: object, **kw: object) -> None:
        raise RuntimeError(
            "Test suite attempted to instantiate the Anthropic client — "
            "this would burn real tokens. Add monkeypatch.setenv('ANTHROPIC_API_KEY', ...) "
            "and patch the client in your test instead."
        )

    with (
        patch.object(_anthropic_mod, "Anthropic", side_effect=_raise),
        patch.object(_anthropic_mod, "AsyncAnthropic", side_effect=_raise),
    ):
        yield


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


@pytest.fixture(scope="session")
def substantial_three_page_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Three-page PDF with enough content per page for docling to emit page breaks.

    The sparse single-line fixture (three_page_pdf) can be merged by docling's
    segmentation heuristics into one section with zero page-break markers. This
    fixture uses paragraph-length text per page so the page boundary is reliably
    detected. Use this fixture (not three_page_pdf) when testing PAGE_BREAK_MARKER
    emission.
    """
    pytest.importorskip("reportlab")
    from reportlab.pdfgen.canvas import Canvas  # noqa: PLC0415

    _PARA = (
        "This is a paragraph of text that provides enough content for the PDF parser "
        "to treat each page as a structurally distinct section. "
        "Adding several sentences ensures the page is not silently merged with adjacent "
        "pages by the document segmentation heuristic. "
        "The content on this page is unique so pages are distinguishable."
    )
    pages = [f"Page one. {_PARA}", f"Page two. {_PARA}", f"Page three. {_PARA}"]

    pdf_path = tmp_path_factory.mktemp("pdfs") / "substantial_three_page.pdf"
    c = Canvas(str(pdf_path), pagesize=(612, 792))
    for text in pages:
        y = 700
        for line in [text[i : i + 80] for i in range(0, len(text), 80)]:
            c.drawString(72, y, line)
            y -= 15
        c.showPage()
    c.save()
    return pdf_path


# ---------------------------------------------------------------------------
# MockGraphStore fixture for unit testing graph_inspector.py (E2b)
# ---------------------------------------------------------------------------


class MockGraphStore:
    """Mock GraphStore for unit testing graph inspection logic without LanceDB."""

    def __init__(self) -> None:
        """Initialize with empty node/edge/mention collections per collection."""
        self.nodes: dict[str, list] = {}
        self.edges: dict[str, list] = {}
        self.mentions: dict[str, list] = {}

    async def get_all_nodes(self, collection: str, *, ns: str = "default"):  # type: ignore[no-untyped-def]
        """Return all nodes for *collection*; empty list if not in store."""
        return self.nodes.get(collection, [])

    async def get_all_edges(self, collection: str, *, ns: str = "default"):  # type: ignore[no-untyped-def]
        """Return all edges for *collection*; empty list if not in store."""
        return self.edges.get(collection, [])

    async def get_all_mentions(self, collection: str, limit: int | None = None, *, ns: str = "default"):  # type: ignore[no-untyped-def]
        """Return mentions for *collection* up to *limit*; empty list if not in store."""
        mentions = self.mentions.get(collection, [])
        if limit is not None:
            return mentions[:limit]
        return mentions


@pytest.fixture
def mock_graph_store() -> MockGraphStore:
    """Fixture providing a MockGraphStore instance for unit tests."""
    return MockGraphStore()
