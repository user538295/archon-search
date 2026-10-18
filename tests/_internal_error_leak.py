"""Shared fixtures for the no-internal-error-leak regression tests (MCP, REST, jobs).

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``detail`` or job
``error`` field for a caught exception whose message embeds internals. The tests raise
exceptions carrying ``LEAK_NEEDLE`` (an internal-looking absolute path) and assert it
never reaches the wire, while the cause is still logged with its traceback.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.config import HyDEConfig, RAGFusionConfig
from archon_search.hyde import HyDEGenerator
from archon_search.rag_fusion import RAGFusionGenerator

# Internal-looking marker: an absolute path under the runtime data dir.
LEAK_NEEDLE = "/Users/secret"
LANCE_PATH = f"{LEAK_NEEDLE}/.archon-search/lancedb/chunks.lance"
LANCE_MSG = (
    "lance error: LanceError(IO): Generic LocalFileSystem error: Unable to open file "
    f"{LANCE_PATH}/data/0a1b.lance: Permission denied (os error 13)"
)
ONNX_MSG = (
    "[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : Non-zero status code returned while running "
    f"Gather node. Model path: {LEAK_NEEDLE}/.archon-search/models/bge-small/model_optimized.onnx"
)

# Making the provider module unimportable is how a missing `hyde` / `rag_fusion`
# extra reaches HyDEGenerator / RAGFusionGenerator __init__.
_NO_ANTHROPIC_PROVIDER = {"archon_search.providers.anthropic_provider": None}


def assert_logged_with_traceback(caplog: pytest.LogCaptureFixture, message: str) -> None:
    """The sanitized wire error must not lose the cause: it is logged with its traceback."""
    assert any(
        r.exc_info is not None and message in str(r.exc_info[1]) for r in caplog.records
    ), f"exception not logged with exc_info: {[r.getMessage() for r in caplog.records]!r}"
    assert message in caplog.text and "Traceback" in caplog.text


def permission_error(name: str) -> PermissionError:
    """An OSError whose ``str()`` echoes a key/job file path under the data dir."""
    return PermissionError(13, "Permission denied", f"{LEAK_NEEDLE}/.archon-search/{name}")


def mock_pipeline() -> MagicMock:
    """A SearchPipeline stand-in that is warm and resolves the default collection."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = True
    pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", namespace="default"))
    return pipeline


def _embedder(embed_error: BaseException) -> tuple[MagicMock, AsyncMock]:
    embed_one = AsyncMock(side_effect=embed_error)
    embedder = MagicMock()
    embedder.embed_one = embed_one
    return embedder, embed_one


def hyde_generator(config: HyDEConfig, embed_error: BaseException) -> tuple[HyDEGenerator, AsyncMock]:
    """A real HyDEGenerator whose provider answers and whose embedder fails like ONNX."""
    provider = MagicMock()
    provider.generate_hypothetical_doc = AsyncMock(return_value="a hypothetical answer")
    embedder, embed_one = _embedder(embed_error)
    return HyDEGenerator(embedder, config, provider=provider), embed_one


def hyde_generator_without_provider(config: HyDEConfig) -> HyDEGenerator:
    """A real HyDEGenerator built while the `hyde` extra's provider module is absent."""
    embedder, _ = _embedder(AssertionError("embed_one must not be reached"))
    with patch.dict(sys.modules, _NO_ANTHROPIC_PROVIDER):
        return HyDEGenerator(embedder, config)


def rag_fusion_generator_without_provider(config: RAGFusionConfig) -> RAGFusionGenerator:
    """A real RAGFusionGenerator built while the `rag_fusion` extra's provider module is absent."""
    with patch.dict(sys.modules, _NO_ANTHROPIC_PROVIDER):
        return RAGFusionGenerator(config)
