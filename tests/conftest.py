"""
packages/archon-search/tests/conftest.py — ML-model isolation and shared store fixture.

ALL sys.modules injections run at module level (import time), before pytest
discovers any test file.  This prevents ONNX model downloads and the
HuggingFace-tokenizers Rust-library process explosion.
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import os
import sys
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from tests._entrypoint_harness import Container

# Cap native-runtime thread counts BEFORE ML libs / Tokio runtimes initialize.
# onnxruntime, OpenMP, OpenBLAS and LanceDB's Tokio runtime each default to
# spawning cpu_count() threads per process; with `-n auto` workers that yields
# 14×N threads competing for ~14 cores. Measured impact: −33% wall time.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("ORT_NUM_THREADS", "1")
os.environ.setdefault("TOKIO_WORKER_THREADS", "2")

# pyarrow 25's mimalloc allocator segfaults on macOS/arm64 during per-thread
# heap init (`mi_thread_init`, apache/arrow #37010/#41696/#44342) — this is
# where the 7 crashes landed (test-worker subprocesses). Route Arrow through
# the system allocator on macOS before any direct `import lancedb`/`pyarrow`.
# macOS-only mirrors archon_search/__init__.py; Linux keeps mimalloc.
if sys.platform == "darwin":
    os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from _search_stubs import install_stubs

install_stubs()

# Captured before the override below, so a genuinely shell-exported key is still
# distinguishable from this suite-wide default — see test_multi_instance_e2e.py's
# compose_stack fixture, which checks this instead of the live env var (the live
# one is always this suite's own TEST_API_KEY by the time that fixture runs).
ORIGINAL_ARCHON_SEARCH_API_KEY = os.environ.get("ARCHON_SEARCH_API_KEY")

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
        import anthropic as _anthropic_mod
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
    elif "graph_real_artifact" not in request.keywords:
        # The lane owns its own data dir. Being function-scoped, this fixture runs AFTER
        # the lane's module-scoped `_lane_data_dir`, so redirecting here would win — and
        # `GLiNER.from_pretrained` reads `get_graph_models_dir()` lazily inside the test
        # body (prose_extraction_backend.py:272), i.e. after that override. The lane would
        # then re-download ~1.2 GB past the CI cache on every run, which is exactly what
        # the cache and prefetch steps exist to prevent.
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(_archon_worker_data_dir))


_REAL_BACKEND_MARKERS = frozenset({"real_torch_backends", "live_eval", "live_benchmark", "runtime_parity"})


@pytest.fixture(autouse=True)
def _stub_model_backends(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the default suite free of PyTorch, transformers and model weights.

    ``ModelEmbedder.encode`` returns zero vectors of the spec's dimension and
    ``ModelReranker.predict`` returns uniform 0.5 scores. Both mark the backend warm on
    first use, as loading the real weights would, so warm-up and the warm/cold search
    gates behave as in production. Construction is untouched: a model name outside the
    registry still raises ``UnsupportedModelError``, so tests pick registered models.

    Tests of the real backends opt out with ``pytestmark = pytest.mark.real_torch_backends``;
    the real-weights lanes (``live_eval``, ``live_benchmark``, ``runtime_parity``) are opted
    out by their own marker.
    """
    if _REAL_BACKEND_MARKERS & set(request.keywords):
        return
    from archon_search.embedder import ModelEmbedder
    from archon_search.reranker import ModelReranker

    loaded = object()

    def encode(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._encoder = loaded  # type: ignore[assignment]
        return [[0.0] * self._spec.dim for _ in texts]

    def predict(self: ModelReranker, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        self._cross_encoder = loaded  # type: ignore[assignment]
        return [0.5] * len(pairs)

    monkeypatch.setattr(ModelEmbedder, "encode", encode)
    monkeypatch.setattr(ModelReranker, "predict", predict)


@pytest.fixture(autouse=True)
def _isolate_archon_logging() -> Generator[None, None, None]:
    """Isolate the `archon_search` logger's state per test.

    `logging_setup.configure_logging()` sets `logger.propagate = False` and attaches
    a file/stderr handler whenever `log_file` or container mode is configured. Under
    xdist that state LEAKS across tests on the same worker: once any test disables
    propagation, later caplog-based warning assertions (store/sync/watcher/backfill)
    capture nothing — because child records (`archon_search.*`) no longer reach the
    root handler pytest's `caplog` installs — and fail non-deterministically depending
    on which tests happen to share a worker. Snapshot at setup, force `propagate=True`
    so the test-under-observation always reaches caplog, and fully restore at teardown
    so no test leaks logging state into the next.
    """
    import logging

    logger = logging.getLogger("archon_search")
    saved_handlers = logger.handlers[:]
    saved_propagate = logger.propagate
    saved_level = logger.level
    logger.propagate = True
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            if handler not in saved_handlers:
                logger.removeHandler(handler)
                with contextlib.suppress(Exception):
                    handler.close()
        logger.handlers[:] = saved_handlers
        logger.propagate = saved_propagate
        logger.setLevel(saved_level)


@pytest.fixture
def real_mcp_auth_context() -> None:
    """Opt a test out of ``_mcp_default_request_context``: its tool calls see the real request context."""


@pytest.fixture(autouse=True)
def _mcp_default_request_context(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give MCP tool functions called directly (no HTTP request) an authenticated default-namespace admin caller.

    ``archon_search.server.mcp._get_request_identity`` fails closed: outside an authenticated HTTP request it
    refuses the call. Tests that invoke the tool functions directly (``StubFastMCP``) have no request, so
    ``get_http_request`` falls back to a fake request that ``APIKeyMiddleware`` would have stamped for the
    default key. A real request (``TestClient`` against the mounted ``/mcp`` app) is untouched: the real
    ``get_http_request`` answers first. Request the ``real_mcp_auth_context`` fixture to opt out.
    """
    if "real_mcp_auth_context" in request.fixturenames:
        return
    import importlib
    from types import SimpleNamespace

    mcp_module = importlib.import_module("archon_search.server.mcp")
    real_get_http_request = mcp_module.get_http_request
    default_request = SimpleNamespace(state=SimpleNamespace(namespace="default", admin=True))

    def _request_or_default_caller() -> object:
        try:
            return real_get_http_request()
        except RuntimeError:
            return default_request

    monkeypatch.setattr(mcp_module, "get_http_request", _request_or_default_caller)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer auth headers using the test API key."""
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def col_name() -> str:
    """Unique LanceDB collection name per test (avoids cross-test pollution)."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def off_swap_platform() -> Generator[None, None, None]:
    """Run as macOS/arm64, so a CUDA install never takes the linux/x86_64 torch-swap path.

    On linux/x86_64 the wizard's CUDA step swaps in the ``+cu126`` torch and asks a fresh interpreter whether CUDA
    is visible, instead of calling ``_accelerator_validated``. A test that stubs only the latter would pass on a
    macOS or ARM host and probe the real machine on an x86_64 Linux CI runner.
    """
    with (
        patch("archon_search.install.installer.sys.platform", "darwin"),
        patch("archon_search.install.installer.platform.machine", return_value="arm64"),
    ):
        yield


# ---------------------------------------------------------------------------
# Install-method prefixes: what `sys.prefix` looks like for a `uv tool install
# archon-search` environment versus any other (pip venv, pipx, checkout).
# ---------------------------------------------------------------------------

UV_TOOL_RECEIPT = '[tool]\nrequirements = [{ name = "archon-search" }]\n'


@pytest.fixture
def uv_tool_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run as if from a uv tool environment: ``sys.prefix`` is
    ``<tmp>/tools/archon-search`` holding a plain ``uv-receipt.toml``.

    torch reads as not installed, so commands and hints derived from the
    receipt carry no ``--torch-backend`` regardless of the test venv's torch.
    Tests may overwrite the receipt (``prefix / "uv-receipt.toml"``).
    """
    prefix = tmp_path / "tools" / "archon-search"
    prefix.mkdir(parents=True)
    (prefix / "uv-receipt.toml").write_text(UV_TOOL_RECEIPT)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    real_version = importlib.metadata.version

    def _version(name: str) -> str:
        if name == "torch":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", _version)
    return prefix


@pytest.fixture
def pip_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run as if from a plain virtualenv (no uv tool receipt)."""
    prefix = tmp_path / ".venv"
    prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    return prefix


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
    from _pdf_fixture import generate_three_page_pdf

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
    from reportlab.pdfgen.canvas import Canvas

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


@pytest.fixture(scope="session")
def scanned_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped fixture returning the path to a one-page, image-only PDF.

    Unlike three_page_pdf / substantial_three_page_pdf (real PDF text layers, extracted
    directly — OCR contributes nothing), this PDF's page is a rendered bitmap with no text
    layer at all, so recovering the words requires docling's OCR path specifically.
    """
    pytest.importorskip("reportlab")
    pytest.importorskip("PIL")
    from _pdf_fixture import generate_scanned_pdf

    pdf_path = tmp_path_factory.mktemp("pdfs") / "scanned.pdf"
    generate_scanned_pdf(pdf_path)
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


# ---------------------------------------------------------------------------
# docker-entrypoint.sh harness (tests/_entrypoint_harness.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def entrypoint_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The offline wheelhouse every entrypoint :class:`Container` resolves from; read-only once built."""
    from tests._entrypoint_harness import build_wheelhouse

    directory = tmp_path_factory.mktemp("entrypoint-wheelhouse")
    build_wheelhouse(directory)
    return directory


@pytest.fixture
def entrypoint_container(tmp_path: Path, entrypoint_wheelhouse: Path) -> Container:
    """A fresh image plus an empty ``/pip-packages`` volume that runs the real entrypoint."""
    from tests import _entrypoint_harness

    return _entrypoint_harness.Container(tmp_path, entrypoint_wheelhouse)
