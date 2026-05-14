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


_ARCHON_SEARCH_SRC = Path(__file__).parent.parent / "archon_search"


def test_notification_monitor_deleted() -> None:
    """Task 2.4: notification_monitor.py must be deleted from archon_search/ (had archon.config import)."""
    assert not (_ARCHON_SEARCH_SRC / "notification_monitor.py").exists(), (
        "notification_monitor.py still exists in archon_search/ — delete it (Task 2.4)"
    )


def test_install_py_has_no_archon_imports() -> None:
    """Task 2.3: install.py must have zero archon.* imports after platform extraction."""
    source = (_ARCHON_SEARCH_SRC / "install.py").read_text(encoding="utf-8")
    lines = [ln for ln in source.splitlines() if "from archon." in ln or "import archon." in ln]
    assert not lines, f"install.py still has archon.* imports: {lines}"
