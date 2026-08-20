"""Construction-time spaCy guard for graph.enabled (2026-08-19-030).

A missing ``[graph]`` extra is an operator misconfiguration, so it must fail
when the pipeline is BUILT — not once per file, pre-persist, inside every
ingest. `create_app` already guarded this; `create_pipeline` did not, so
non-server callers (`install/installer.py`) built pipelines whose every graph
ingest aborted before persist. Both now share one implementation.

`create_pipeline` is public API (re-exported at ``install/__init__.py``), so
the callers this protects include library embedders outside this repo — not
just ``install/installer.py``, whose own ``_bootstrap_collections`` call turns
out to be unreachable (2026-08-19-030 C2-I-7).
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

    Asserting only that it raises would pass with the guard moved to the
    BOTTOM of the function, which is the regression itself — a doomed pipeline
    gets fully built first. Assert nothing was constructed (C2-T-6).
    """
    from unittest.mock import MagicMock, patch

    from archon_search.config import SearchConfig
    from archon_search.pipeline import create_pipeline

    cfg = SearchConfig()
    cfg.graph = GraphConfig(enabled=True)

    with (
        _spacy_absent(),
        patch("archon_search.pipeline.SearchStore", MagicMock()) as mock_store,
        patch("archon_search.pipeline.ModelEmbedder", MagicMock()) as mock_embedder,
        pytest.raises(ConfigError),
    ):
        create_pipeline(cfg)

    mock_store.assert_not_called()
    mock_embedder.assert_not_called()


def test_app_and_pipeline_guards_share_one_implementation() -> None:
    """Two copies of this check would drift; one of them would then be wrong.

    Behavioural, not a source-text scan: `"ensure_spacy_importable" in
    getsource(...)` passes when the call is deleted but a comment mentioning it
    survives, and says nothing about `create_pipeline` — the half that actually
    regressed (C2-T-7). Both call sites import the name function-locally, so a
    module-attribute patch takes effect at call time.
    """
    from unittest.mock import MagicMock, patch

    from archon_search.config import SearchConfig
    from archon_search.jobs import JobStore
    from archon_search.pipeline import create_pipeline
    from archon_search.server.app import create_app

    class _Sentinel(Exception):
        pass

    cfg = SearchConfig()
    cfg.graph = GraphConfig(enabled=True)

    def _raise(_config: GraphConfig) -> None:
        raise _Sentinel

    with patch("archon_search.graph_extractor.ensure_spacy_importable", _raise):
        with pytest.raises(_Sentinel):
            create_pipeline(cfg)
        with pytest.raises(_Sentinel):
            create_app(cfg, MagicMock(spec=JobStore))


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
