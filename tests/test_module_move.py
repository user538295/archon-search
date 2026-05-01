"""Tests verifying that archon/search/ modules are importable from archon_search (Task 2.1)."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_chunker_importable_from_archon_search() -> None:
    """Chunker must be importable from archon_search after the module move."""
    from archon_search.chunker import DocumentChunker  # noqa: PLC0415

    assert DocumentChunker is not None


def test_store_importable_from_archon_search() -> None:
    """SearchStore must be importable from archon_search after the module move."""
    from archon_search.store import SearchStore  # noqa: PLC0415

    assert SearchStore is not None


# ---------------------------------------------------------------------------
# Parametrized import tests for all independently-importable modules
# ---------------------------------------------------------------------------

_IMPORTABLE_MODULES = [
    ("archon_search._types", "ChunkRecord"),
    ("archon_search.types", "JobStatus"),
    ("archon_search.config", "SearchConfig"),
    ("archon_search.collection_meta", "CollectionMeta"),
    ("archon_search.embedder", "Embedder"),
    ("archon_search.parser", "DocumentParser"),
    ("archon_search.progress", "IndexingStateStore"),
    ("archon_search.reranker", "Reranker"),
    ("archon_search.router", "MultiCollectionRouter"),
    ("archon_search.watcher", "CollectionWatcher"),
    ("archon_search.sync", "SearchCollectionSync"),
]


@pytest.mark.parametrize("module_path,class_name", _IMPORTABLE_MODULES)
def test_importable_modules(module_path: str, class_name: str) -> None:
    """All standalone modules must be importable from archon_search without cross-package deps."""
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    assert cls is not None


# ---------------------------------------------------------------------------
# Verify cross-package imports were NOT rewritten to archon_search.*
# ---------------------------------------------------------------------------

_CROSS_PACKAGE_IMPORTS = [
    # (filename, expected import string that must still exist)
    ("notification_monitor.py", "from archon.config.loader import NotificationsConfig"),
    ("description_generator.py", "from archon.ai.claude_session import _get_env_lock"),
    ("install.py", "from archon.cli.console import Console"),
]

_ARCHON_SEARCH_SRC = Path(__file__).parent.parent / "archon_search"


@pytest.mark.parametrize("filename,expected_import", _CROSS_PACKAGE_IMPORTS)
def test_cross_package_imports_preserved(filename: str, expected_import: str) -> None:
    """Cross-package 'from archon.' imports must NOT have been rewritten to 'from archon_search.'."""
    source = (_ARCHON_SEARCH_SRC / filename).read_text(encoding="utf-8")
    assert expected_import in source, (
        f"{filename}: expected cross-package import not found: {expected_import!r}"
    )
