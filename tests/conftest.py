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
# Module-scoped store fixture — one LanceDB connection per test module to
# avoid spawning a new Tokio thread pool for every test.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connected_store(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One shared SearchStore per test module (sync connect/disconnect via asyncio.run).

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


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer auth headers using the test API key."""
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def col_name() -> str:
    """Unique LanceDB collection name per test (avoids cross-test pollution)."""
    return f"test-{uuid.uuid4().hex[:8]}"
