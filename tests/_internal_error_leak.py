"""Shared fixtures for the no-internal-error-leak regression tests (MCP, REST, jobs).

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``detail`` or job
``error`` field for a caught exception whose message embeds internals. The tests raise
exceptions carrying ``LEAK_NEEDLE`` (an internal-looking absolute path) and assert it
never reaches the wire, while the cause is still logged with its traceback.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search import uv_tool
from archon_search.chunker import DocumentChunker
from archon_search.collection_meta import CollectionMeta
from archon_search.config import GraphConfig, HyDEConfig, RAGFusionConfig
from archon_search.constants import DEFAULT_CONTEXT_WINDOW_MAX
from archon_search.defref_extractor import DefRefExtractor
from archon_search.embedder import Embedder
from archon_search.hyde import (
    HYDE_DEPENDENCY_DETAIL,
    HyDEDependencyError,
    HyDEGenerator,
)
from archon_search.parser import DocumentParser
from archon_search.pipeline import MetadataLookupError, SearchPipeline
from archon_search.rag_fusion import RAGFusionGenerator
from tests._hyde_stubs import ScriptedLlmClient

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

# What a tree-sitter failure inside the def/ref walk looks like.
DEFREF_PARSE_MSG = f"tree_sitter: failed to load language from {LEAK_NEEDLE}/.archon-search/grammars/python.so"
_DEFREF_SOURCE_NAME = "module.py"
_DEFREF_SOURCE = "def caller():\n    return callee()\n\n\ndef callee():\n    return 1\n"
_STUB_EMBEDDING_DIM = 4

# Making the provider module unimportable is how a missing `hyde` / `rag_fusion`
# extra reaches RAGFusionGenerator __init__ (HyDEGenerator no longer builds a provider: BE-11).
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


def metadata_lookup_error() -> MetadataLookupError:
    """A store error wrapped the way the pipeline's fan-out raises it
    (``raise MetadataLookupError(exc) from exc``), returned once caught, cause chained."""
    try:
        try:
            raise RuntimeError(LANCE_MSG)
        except RuntimeError as exc:
            raise MetadataLookupError(exc) from exc
    except MetadataLookupError as err:
        return err


def mock_pipeline() -> MagicMock:
    """A SearchPipeline stand-in that is warm and resolves the default collection."""
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline.embedder_is_warm = True
    pipeline.context_window_max = DEFAULT_CONTEXT_WINDOW_MAX
    pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", namespace="default"))
    return pipeline


def _embedder(embed_error: BaseException) -> tuple[MagicMock, AsyncMock]:
    embed_one = AsyncMock(side_effect=embed_error)
    embedder = MagicMock()
    embedder.embed_one = embed_one
    return embedder, embed_one


def hyde_generator(config: HyDEConfig, embed_error: BaseException) -> tuple[HyDEGenerator, AsyncMock]:
    """A real HyDEGenerator whose shared client answers and whose hypothesis embed fails like ONNX.

    ``generate`` embeds with the embedder its caller passes (the searched collection's);
    this stand-in swaps in one whose ``embed_one`` raises *embed_error*, whatever the
    caller passed, so the failure lands in the real generator's embed step.
    """
    failing_embedder, embed_one = _embedder(embed_error)
    generator = HyDEGenerator(config, ScriptedLlmClient(reply="a hypothetical answer"))
    real_generate = generator.generate

    async def _generate(query: str, *, embedder: Any) -> list[float] | None:
        return await real_generate(query, embedder=failing_embedder)

    generator.generate = _generate  # type: ignore[method-assign]
    return generator, embed_one


def hyde_generator_missing_extra() -> MagicMock:
    """A ``HyDEGenerator`` stand-in whose ``generate`` raises ``HyDEDependencyError`` like a missing ``hyde`` extra.

    The message carries the install command from ``uv_tool.extra_install_hint`` (patched by
    ``patch_install_hint(monkeypatch, "archon_search.uv_tool")``); the transports must log it and send only the
    fixed ``HYDE_DEPENDENCY_DETAIL``. The shared-client generator reports a missing package as a call failure, so
    this stand-in serves the transport-mapping tests until BE-13 decides whether the error stays reachable.
    """

    async def _generate(*_args: Any, **_kwargs: Any) -> None:
        raise HyDEDependencyError(f"{HYDE_DEPENDENCY_DETAIL} ({uv_tool.extra_install_hint('hyde')})")

    return MagicMock(spec=HyDEGenerator, generate=AsyncMock(side_effect=_generate))


def rag_fusion_generator_without_provider(config: RAGFusionConfig) -> RAGFusionGenerator:
    """A real RAGFusionGenerator built while the `rag_fusion` extra's provider module is absent."""
    with patch.dict(sys.modules, _NO_ANTHROPIC_PROVIDER):
        return RAGFusionGenerator(config)


class _StubEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * _STUB_EMBEDDING_DIM for _ in texts]


def write_defref_source(directory: Path) -> Path:
    """A Python file with a def and a call, so ingest runs the def/ref walk on it."""
    source = directory / _DEFREF_SOURCE_NAME
    source.write_text(_DEFREF_SOURCE, encoding="utf-8")
    return source


def defref_pipeline(store: Any) -> SearchPipeline:
    """A real pipeline over a real store with a real DefRefExtractor (graph store stubbed)."""
    graph_store = MagicMock()
    graph_store.ensure_graph_tables = AsyncMock()
    graph_store.delete_defref_graph_by_doc = AsyncMock()
    graph_store.write_graph = AsyncMock()
    return SearchPipeline(
        store=store,
        embedder=Embedder(_StubEmbedderBackend()),
        reranker=None,
        chunker=DocumentChunker(),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_store=graph_store,
        graph_config=GraphConfig(enabled=True),
        defref_extractor=DefRefExtractor(graph_store),
    )
