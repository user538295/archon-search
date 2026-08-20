"""Construction-time spaCy guard for graph.enabled (2026-08-19-030).

A missing ``[graph]`` extra is an operator misconfiguration, so it must fail
when the pipeline is BUILT — not once per file, pre-persist, inside every
ingest. `create_app` already guarded this; `create_pipeline` did not, so
non-server callers (`install/installer.py`) built pipelines whose every graph
ingest aborted before persist. Both now share one implementation.
"""
from __future__ import annotations

import sys

import pytest

from archon_search.config import ConfigError, GraphConfig
from archon_search.graph_extractor import ensure_spacy_importable


def test_guard_noops_when_graph_disabled() -> None:
    """The overwhelmingly common config must not pay for the graph check."""
    with _spacy_absent():
        ensure_spacy_importable(GraphConfig(enabled=False))


def test_guard_passes_when_spacy_importable() -> None:
    ensure_spacy_importable(GraphConfig(enabled=True))


def test_guard_raises_config_error_when_spacy_absent() -> None:
    with _spacy_absent(), pytest.raises(ConfigError) as excinfo:
        ensure_spacy_importable(GraphConfig(enabled=True))
    message = str(excinfo.value)
    assert "graph.enabled=true" in message
    assert "archon-search[graph]" in message, (
        f"the error must name the remedy, got: {message!r}"
    )


def test_create_pipeline_raises_before_building_anything() -> None:
    """The regression: `create_pipeline` used to build a doomed pipeline.

    Guards at construction, so `install/installer.py`'s direct `create_pipeline`
    call can no longer produce an object whose every graph ingest fails.
    """
    from archon_search.config import SearchConfig
    from archon_search.pipeline import create_pipeline

    cfg = SearchConfig()
    cfg.graph = GraphConfig(enabled=True)

    with _spacy_absent(), pytest.raises(ConfigError):
        create_pipeline(cfg)


def test_app_and_pipeline_guards_share_one_implementation() -> None:
    """Two copies of this check would drift; one of them would then be wrong."""
    import inspect

    from archon_search.server.app import _check_graph_deps

    source = inspect.getsource(_check_graph_deps)
    assert "ensure_spacy_importable" in source, (
        "_check_graph_deps must delegate to the shared guard, not re-implement it"
    )


class _spacy_absent:
    """Bind ``sys.modules['spacy']`` to None — the suite's absent-module idiom."""

    def __enter__(self) -> None:
        self._saved = sys.modules.get("spacy", ...)
        sys.modules["spacy"] = None  # type: ignore[assignment]

    def __exit__(self, *exc: object) -> None:
        if self._saved is ...:
            sys.modules.pop("spacy", None)
        else:
            sys.modules["spacy"] = self._saved  # type: ignore[assignment]
