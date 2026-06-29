"""Unit tests for BE-6: routes_search.py graph_mode Literal extension.

Tests:
- SearchRequest validates "naive", "local", "global" as valid graph_mode values
- SearchRequest rejects invalid graph_mode values with ValidationError
- "None" is still valid (graph_mode is optional)
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search.server.routes_search import SearchRequest


# ---------------------------------------------------------------------------
# test_search_request_graph_mode_validation
# ---------------------------------------------------------------------------


class TestSearchRequestGraphModeValidation:
    """SearchRequest.graph_mode Literal extension covers naive, local, global."""

    def test_graph_mode_none_is_valid(self) -> None:
        """None is valid — graph_mode is optional."""
        req = SearchRequest(collection="col", query="hello", graph_mode=None)
        assert req.graph_mode is None

    def test_graph_mode_naive_is_valid(self) -> None:
        """'naive' was valid before BE-6 and must remain valid."""
        req = SearchRequest(collection="col", query="hello", graph_mode="naive")
        assert req.graph_mode == "naive"

    def test_graph_mode_local_is_valid(self) -> None:
        """'local' is newly accepted in BE-6."""
        req = SearchRequest(collection="col", query="hello", graph_mode="local")
        assert req.graph_mode == "local"

    def test_graph_mode_global_is_valid(self) -> None:
        """'global' is newly accepted in BE-6."""
        req = SearchRequest(collection="col", query="hello", graph_mode="global")
        assert req.graph_mode == "global"

    def test_invalid_graph_mode_raises(self) -> None:
        """Unrecognised value (e.g. 'naive2') → ValidationError."""
        with pytest.raises(ValidationError):
            SearchRequest(collection="col", query="hello", graph_mode="naive2")  # type: ignore[arg-type]

    def test_empty_string_graph_mode_raises(self) -> None:
        """Empty string is not a valid graph_mode."""
        with pytest.raises(ValidationError):
            SearchRequest(collection="col", query="hello", graph_mode="")  # type: ignore[arg-type]

    def test_case_sensitive_graph_mode(self) -> None:
        """Graph mode is case-sensitive — 'Naive', 'LOCAL' etc. are invalid."""
        with pytest.raises(ValidationError):
            SearchRequest(collection="col", query="hello", graph_mode="Local")  # type: ignore[arg-type]

    def test_default_graph_mode_is_none(self) -> None:
        """graph_mode defaults to None when not provided."""
        req = SearchRequest(collection="col", query="hello")
        assert req.graph_mode is None
