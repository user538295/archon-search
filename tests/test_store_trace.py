"""Tests for SearchStore.hybrid_search_with_trace and _hybrid_search_with_trace."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search.store import SearchStore, _hybrid_search_with_trace


def test_lance_store_hybrid_search_with_trace_delegates_to_module_function() -> None:
    """hybrid_search_with_trace instance method must delegate to module-level function."""
    import asyncio

    store = SearchStore("/tmp/fake_db")

    fake_result: list[ScoredSearchCandidate] = []

    async def _fake_fn(s, col, vec, text, depth):  # noqa: ANN001
        assert s is store
        assert col == "my-col"
        assert vec == [1.0, 0.0]
        assert text == "query"
        assert depth == 10
        return fake_result

    with patch(
        "archon_search.store._hybrid_search_with_trace",
        side_effect=_fake_fn,
    ) as mock_fn:
        result = asyncio.run(
            store.hybrid_search_with_trace("my-col", [1.0, 0.0], "query", 10)
        )

    mock_fn.assert_called_once_with(store, "my-col", [1.0, 0.0], "query", 10)
    assert result is fake_result
