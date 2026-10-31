"""BE-17 integration (re-pointed by LCD BE-16): the surviving community-summarisation path.

S24 — with a graph provider configured, abstractive summaries are still produced, now through the shared client the
app composes (``_build_graph_client``) against a real HTTP model server.
S25 — with no provider configured, composing the client raises nothing and simply yields no client (no summaries).
"""
from __future__ import annotations

import pytest

from archon_search.community_builder import CommunityBuilder
from archon_search.config import GraphConfig
from archon_search.server.app import _build_graph_client
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies

pytestmark = pytest.mark.integration

_SUMMARY = "A summary of the community."


async def test_summaries_still_produced_with_a_provider_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    with FakeModelServer(reply_text=_SUMMARY) as server:
        config = GraphConfig(
            enabled=True,
            provider="llama_cpp",
            extraction_model="local-model",
            llama_cpp_base_url=server.base_url,
        )
        client = _build_graph_client(config, rate_limit_rpm=None)
        assert client is not None
        builder = CommunityBuilder(graph_store=None, config=config, graph_client=client)  # type: ignore[arg-type]

        summary = await builder._generate_llm_summary(client, ["chunk one", "chunk two"], ["Alpha", "Beta"])

        assert summary == _SUMMARY
        (request,) = server.requests_to("/v1/chat/completions")
        assert "chunk one" in request.body["messages"][0]["content"]


async def test_no_provider_configured_raises_nothing_and_omits_summaries() -> None:
    config = GraphConfig(enabled=True, provider=None)
    # No provider means no client, and no configuration error is raised.
    assert _build_graph_client(config, rate_limit_rpm=None) is None
