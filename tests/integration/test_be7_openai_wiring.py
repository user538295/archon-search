"""BE-7 (G10) — OpenAI provider wired into HyDE and RAG Fusion generators.

Tests:
  - test_search_hyde_openai_provider_mocked: make_real_app with provider="openai";
    mock AsyncOpenAI.chat.completions.create; search with hyde=true; assert hyde_applied=True
  - test_search_rag_fusion_openai_provider_mocked: same for RAG Fusion

The lazy import of the SDK, the key check and the protocol fit of the OpenAI wrapper are tested in
tests/test_llm_client_cloud.py and tests/test_llm_client_interface.py.

Run with:
    uv run pytest tests/integration/test_be7_openai_wiring.py --no-cov -v
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _make_openai_mock_completion(content: str) -> MagicMock:
    """Build a mock AsyncOpenAI chat completion response object.

    Response shape: response.choices[0].message.content == content
    (the shape the OpenAI SDK returns and OpenAIClient reads)
    """
    message = MagicMock()
    message.content = content

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    return response


def _toml_hyde_openai(hyde_enabled: bool = True) -> str:
    return (
        f"[hyde]\n"
        f"enabled = {'true' if hyde_enabled else 'false'}\n"
        f'provider = "openai"\n'
        f'model = "gpt-4o-mini"\n'
    )


def _toml_rag_fusion_openai(rag_fusion_enabled: bool = True) -> str:
    return (
        f"[rag_fusion]\n"
        f"enabled = {'true' if rag_fusion_enabled else 'false'}\n"
        f'provider = "openai"\n'
        f'model = "gpt-4o-mini"\n'
    )


# ---------------------------------------------------------------------------
# test_search_hyde_openai_provider_mocked
# ---------------------------------------------------------------------------


def test_search_hyde_openai_provider_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4/C2: HyDE with OpenAI provider mocked → hyde_applied=True.

    Wires the shared ``OpenAIClient`` into HyDEGenerator through ``create_app``, mocks the SDK's
    ``chat.completions.create``, searches with hyde=true, and asserts hyde_applied=True — proving the wiring is
    end-to-end and the request carries the HyDE template and the 200-token ceiling.

    OPENAI_API_KEY must be set or the client's key guard raises before the mock is reached.
    """
    from archon_search.llm_client.openai_client import OpenAIClient

    doc = tmp_path / "openai_hyde_test.md"
    doc.write_text(
        "# OpenAI HyDE Test\n\nHypothetical document embedding via OpenAI provider.\n" * 4
    )

    # Set API key so the client's is_key_available() guard passes
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-be7-hyde")

    toml = _toml_hyde_openai(hyde_enabled=True)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "openai", f"expected provider=openai, got: {cfg.hyde.provider}"
        assert cfg.hyde.enabled, "expected hyde.enabled=True"

        col = "be7-hyde-openai-mocked"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        hyde_client = client.app.state.hyde_client
        assert isinstance(hyde_client, OpenAIClient), (
            f"HyDE generator must use OpenAIClient, got: {type(hyde_client)}"
        )

        mock_response = _make_openai_mock_completion(
            "A short hypothetical document about OpenAI-powered HyDE search."
        )
        mock_openai_client = MagicMock()
        mock_openai_client.chat = MagicMock()
        mock_openai_client.chat.completions = MagicMock()
        mock_openai_client.chat.completions.create = AsyncMock(return_value=mock_response)

        # The wrapper caches its SDK client on first call; seed it with the mock.
        monkeypatch.setattr(hyde_client, "_client", mock_openai_client)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "OpenAI hypothetical document", "hyde": True},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data, "response must contain 'results'"
        assert data["hyde_applied"] is True, (
            f"expected hyde_applied=True with mocked OpenAI client, got: {data['hyde_applied']}"
        )
        mock_openai_client.chat.completions.create.assert_awaited_once()
        sent = mock_openai_client.chat.completions.create.await_args.kwargs
        assert sent["max_completion_tokens"] == 200
        [message] = sent["messages"]
        assert message["role"] == "user"
        assert "OpenAI hypothetical document" in message["content"]


# ---------------------------------------------------------------------------
# test_search_rag_fusion_openai_provider_mocked
# ---------------------------------------------------------------------------


def test_search_rag_fusion_openai_provider_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5/C2: RAG Fusion with OpenAI provider mocked → rag_fusion_applied=True.

    Wires the shared ``OpenAIClient`` into RAGFusionGenerator through ``create_app``, mocks the SDK's
    ``chat.completions.create`` to return multiple query variants, searches with rag_fusion=true, and asserts
    rag_fusion_applied=True — the request carries the decomposition template and the 150-per-query ceiling.

    OPENAI_API_KEY must be set or the client's key guard raises before the mock is reached.
    """
    from archon_search.llm_client.openai_client import OpenAIClient

    doc = tmp_path / "openai_rag_fusion_test.md"
    doc.write_text(
        "# OpenAI RAG Fusion Test\n\nQuery decomposition via OpenAI provider.\n" * 4
    )

    # Set API key so the client's key guard passes
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-be7-rag-fusion")

    toml = _toml_rag_fusion_openai(rag_fusion_enabled=True)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.rag_fusion.provider == "openai", (
            f"expected provider=openai, got: {cfg.rag_fusion.provider}"
        )
        assert cfg.rag_fusion.enabled, "expected rag_fusion.enabled=True"

        col = "be7-rag-fusion-openai-mocked"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        rag_client = client.app.state.rag_fusion_client
        assert isinstance(rag_client, OpenAIClient), (
            f"RAG Fusion generator must use OpenAIClient, got: {type(rag_client)}"
        )

        # RAG Fusion expects newline-separated variant queries
        mock_response = _make_openai_mock_completion(
            "OpenAI query variant one\nOpenAI query variant two\nOpenAI query variant three"
        )
        mock_openai_client = MagicMock()
        mock_openai_client.chat = MagicMock()
        mock_openai_client.chat.completions = MagicMock()
        mock_openai_client.chat.completions.create = AsyncMock(return_value=mock_response)

        # The wrapper caches its SDK client on first call; seed it with the mock.
        monkeypatch.setattr(rag_client, "_client", mock_openai_client)

        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "OpenAI query decomposition test",
                "rag_fusion": True,
            },
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data, "response must contain 'results'"
        assert data["rag_fusion_applied"] is True, (
            f"expected rag_fusion_applied=True with mocked OpenAI client, "
            f"got: {data['rag_fusion_applied']}"
        )
        # num_queries defaults to 2: the third line is surplus, the ceiling is 150 per requested variant.
        assert data["rag_fusion_queries_used"] == 2
        mock_openai_client.chat.completions.create.assert_awaited_once()
        sent = mock_openai_client.chat.completions.create.await_args.kwargs
        assert sent["max_completion_tokens"] == 300
        [message] = sent["messages"]
        assert message["role"] == "user"
        assert "OpenAI query decomposition test" in message["content"]
        assert "generate 2 alternative" in message["content"]
