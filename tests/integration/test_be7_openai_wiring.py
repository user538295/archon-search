"""BE-7 (G10) — OpenAI provider wired into HyDE and RAG Fusion generators.

Tests:
  - test_search_hyde_openai_provider_mocked: make_real_app with provider="openai";
    mock AsyncOpenAI.chat.completions.create; search with hyde=true; assert hyde_applied=True
  - test_search_rag_fusion_openai_provider_mocked: same for RAG Fusion

Also verifies:
  - DA-TEST-C1-I-4: lazy import of 'openai' preserved end-to-end (no module-level import)
  - is_key_available() returns True when OPENAI_API_KEY is set, False otherwise
  - OpenAIQueryExpansionProvider satisfies the QueryExpansionProvider protocol

Run with:
    uv run pytest tests/integration/test_be7_openai_wiring.py --no-cov -v
"""
from __future__ import annotations

import types
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
    (DA-ARCH-C1-I-8 normalization; matches OpenAIQueryExpansionProvider._normalize_text)
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

    Wires the OpenAI provider into HyDEGenerator via the factory in app.py,
    mocks AsyncOpenAI.chat.completions.create, searches with hyde=true, and
    asserts hyde_applied=True — proving the wiring is end-to-end.

    OPENAI_API_KEY must be set or the key-check guard in the provider returns None
    before the mock is reached.
    """
    doc = tmp_path / "openai_hyde_test.md"
    doc.write_text(
        "# OpenAI HyDE Test\n\nHypothetical document embedding via OpenAI provider.\n" * 4
    )

    # Set API key so the provider's is_key_available() guard passes
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-be7-hyde")

    toml = _toml_hyde_openai(hyde_enabled=True)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "openai", f"expected provider=openai, got: {cfg.hyde.provider}"
        assert cfg.hyde.enabled, "expected hyde.enabled=True"

        col = "be7-hyde-openai-mocked"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Mock AsyncOpenAI.chat.completions.create on the provider's client
        provider = client.app.state.hyde_generator._provider

        from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider
        assert isinstance(provider, OpenAIQueryExpansionProvider), (
            f"HyDE generator must use OpenAIQueryExpansionProvider, got: {type(provider)}"
        )

        mock_response = _make_openai_mock_completion(
            "A short hypothetical document about OpenAI-powered HyDE search."
        )
        mock_openai_client = MagicMock()
        mock_openai_client.chat = MagicMock()
        mock_openai_client.chat.completions = MagicMock()
        mock_openai_client.chat.completions.create = AsyncMock(return_value=mock_response)

        monkeypatch.setattr(provider, "_get_client", lambda: mock_openai_client)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "OpenAI hypothetical document", "hyde": True},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data, "response must contain 'results'"
        assert data["hyde_applied"] is True, (
            f"expected hyde_applied=True with mocked OpenAI provider, got: {data['hyde_applied']}"
        )
        mock_openai_client.chat.completions.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# test_search_rag_fusion_openai_provider_mocked
# ---------------------------------------------------------------------------


def test_search_rag_fusion_openai_provider_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5/C2: RAG Fusion with OpenAI provider mocked → rag_fusion_applied=True.

    Wires the OpenAI provider into RAGFusionGenerator via the factory in app.py,
    mocks AsyncOpenAI.chat.completions.create to return multiple query variants,
    searches with rag_fusion=true, and asserts rag_fusion_applied=True.

    OPENAI_API_KEY must be set or the key-check guard in the provider returns []
    before the mock is reached.
    """
    doc = tmp_path / "openai_rag_fusion_test.md"
    doc.write_text(
        "# OpenAI RAG Fusion Test\n\nQuery decomposition via OpenAI provider.\n" * 4
    )

    # Set API key so the provider's is_key_available() guard passes
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-be7-rag-fusion")

    toml = _toml_rag_fusion_openai(rag_fusion_enabled=True)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.rag_fusion.provider == "openai", (
            f"expected provider=openai, got: {cfg.rag_fusion.provider}"
        )
        assert cfg.rag_fusion.enabled, "expected rag_fusion.enabled=True"

        col = "be7-rag-fusion-openai-mocked"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Mock AsyncOpenAI.chat.completions.create on the provider's client
        provider = client.app.state.rag_fusion_generator._provider

        from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider
        assert isinstance(provider, OpenAIQueryExpansionProvider), (
            f"RAG Fusion generator must use OpenAIQueryExpansionProvider, got: {type(provider)}"
        )

        # RAG Fusion decompose_query expects newline-separated variant queries
        mock_response = _make_openai_mock_completion(
            "OpenAI query variant one\nOpenAI query variant two\nOpenAI query variant three"
        )
        mock_openai_client = MagicMock()
        mock_openai_client.chat = MagicMock()
        mock_openai_client.chat.completions = MagicMock()
        mock_openai_client.chat.completions.create = AsyncMock(return_value=mock_response)

        monkeypatch.setattr(provider, "_get_client", lambda: mock_openai_client)

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
            f"expected rag_fusion_applied=True with mocked OpenAI provider, "
            f"got: {data['rag_fusion_applied']}"
        )
        mock_openai_client.chat.completions.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# DA-TEST-C1-I-4: Lazy import preserved end-to-end
# ---------------------------------------------------------------------------


def test_openai_provider_lazy_import_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DA-TEST-C1-I-4: 'openai' is imported lazily inside __init__, not at module level.

    Simulates absent 'openai' package via sys.modules injection; verifies that
    importing the provider module itself succeeds (no module-level import),
    and that instantiating the provider with openai absent sets _openai_available=False.
    """
    import sys

    # Remove cached provider module so it reimports fresh
    monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider", raising=False)

    # Simulate openai package being absent
    monkeypatch.setitem(sys.modules, "openai", None)

    # Module-level import must succeed (no top-level 'import openai' in the file)
    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider

    # Instantiation must succeed; _openai_available must be False
    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    assert provider._openai_available is False, (
        "provider._openai_available must be False when openai is absent"
    )


# ---------------------------------------------------------------------------
# is_key_available delegates to OPENAI_API_KEY env var
# ---------------------------------------------------------------------------


def test_openai_provider_is_key_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_key_available() returns True when OPENAI_API_KEY is set, False otherwise.

    Verifies the provider's key-check is wired to the correct env var,
    and that provider_key_available('openai') in the protocol delegates correctly.
    """
    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider
    from archon_search.query_expansion_protocol import provider_key_available

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")

    # Without key
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert provider.is_key_available() is False, (
        "is_key_available() must return False when OPENAI_API_KEY is unset"
    )
    assert provider_key_available("openai") is False, (
        "provider_key_available('openai') must return False when OPENAI_API_KEY is unset"
    )

    # With key
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    assert provider.is_key_available() is True, (
        "is_key_available() must return True when OPENAI_API_KEY is set"
    )
    assert provider_key_available("openai") is True, (
        "provider_key_available('openai') must return True when OPENAI_API_KEY is set"
    )


# ---------------------------------------------------------------------------
# OpenAIQueryExpansionProvider satisfies the QueryExpansionProvider protocol
# ---------------------------------------------------------------------------


def test_openai_provider_satisfies_protocol() -> None:
    """OpenAIQueryExpansionProvider satisfies the QueryExpansionProvider protocol.

    isinstance check works because QueryExpansionProvider is @runtime_checkable.
    """
    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider
    from archon_search.query_expansion_protocol import QueryExpansionProvider

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    assert isinstance(provider, QueryExpansionProvider), (
        f"OpenAIQueryExpansionProvider must satisfy QueryExpansionProvider protocol, "
        f"got: {type(provider)}"
    )
