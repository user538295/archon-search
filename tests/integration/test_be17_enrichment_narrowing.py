"""BE-17 integration: the surviving community-summarisation path.

S24 — with an enrichment provider configured, abstractive summaries are still
produced through the one surviving protocol method.
S25 — with no provider configured, the factory and provider registry survive:
building a client raises nothing and simply yields no client (no summaries).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.enrichment.factory import EnrichmentClientFactory

pytestmark = pytest.mark.integration


def _make_response(json_body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(return_value=json_body)
    response.raise_for_status = MagicMock()
    return response


async def test_summaries_still_produced_with_a_provider_configured() -> None:
    config = GraphConfig(enabled=True, provider="llama_cpp", extraction_model="local-model")
    client = EnrichmentClientFactory.build(config)
    assert client is not None

    response = _make_response(
        {"choices": [{"message": {"content": "A summary of the community."}}]}
    )
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = _AsyncReturn(_make_client_with_post(response))
    mock_ctx.__aexit__ = _AsyncReturn(None)

    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", return_value=mock_ctx):
        summary = await client.summarize_community(
            ["chunk one", "chunk two"], ["Alpha", "Beta"]
        )

    assert summary == "A summary of the community."


async def test_no_provider_configured_raises_nothing_and_omits_summaries() -> None:
    config = GraphConfig(enabled=True, provider=None)
    # The factory and provider registry survive the narrowing: no provider
    # means no client, and no configuration error is raised.
    client = EnrichmentClientFactory.build(config)
    assert client is None


class _AsyncReturn:
    """A callable returning an awaitable that yields a fixed value (for __aenter__/__aexit__)."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def __call__(self, *_args: object, **_kwargs: object) -> object:
        return self._value


def _make_client_with_post(response: MagicMock) -> MagicMock:
    inner = MagicMock()

    async def _post(*_args: object, **_kwargs: object) -> MagicMock:
        return response

    inner.post = _post
    return inner
