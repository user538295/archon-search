"""BE-4 — Provider factory wiring and rate-limit skip for Ollama.

Tests:
- S7: HyDE with Ollama falls back gracefully on timeout → hyde_applied=False
- S8: RAG Fusion with Ollama falls back gracefully on timeout → rag_fusion_applied=False
- S6: Hyde=Ollama, RAG Fusion=Anthropic operate independently with no cross-contamination
- C1-B-8: factory injects the correct provider type (QueryExpansionProvider protocol)
- S6 (rate-limit): Ollama provider skips rate limiting; non-Ollama provider respects it
- C1-T-3: ollama_base_url passthrough to OllamaQueryExpansionProvider._base_url
- C1-T-4: non-Ollama (Anthropic) provider IS blocked when token bucket exhausted

Run with:
    uv run pytest tests/integration/test_g10_be4_provider_factory.py --no-cov -v
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_ollama_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ollama module in sys.modules.

    Must be called BEFORE entering make_real_app because _check_provider_deps
    imports ollama synchronously at create_app() time.

    Returns the fake ollama module so tests can configure async client mocks.
    """
    fake_ollama = types.ModuleType("ollama")

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.message = _FakeMessage(content)

    class _FakeAsyncClient:
        def __init__(self, host: str = "http://localhost:11434") -> None:
            self._host = host

        async def chat(self, **kwargs):
            return _FakeResponse("A short hypothetical document.\nVariant one.\nVariant two.\nVariant three.")

    fake_ollama.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
    monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider", raising=False)
    return fake_ollama


def _toml_hyde_ollama(hyde_enabled: bool = True) -> str:
    return (
        f"[hyde]\n"
        f"enabled = {'true' if hyde_enabled else 'false'}\n"
        f'provider = "ollama"\n'
        f'model = "llama3.2"\n'
        f'ollama_base_url = "http://localhost:11434"\n'
    )


def _toml_rag_fusion_ollama(rag_fusion_enabled: bool = True) -> str:
    return (
        f"[rag_fusion]\n"
        f"enabled = {'true' if rag_fusion_enabled else 'false'}\n"
        f'provider = "ollama"\n'
        f'model = "llama3.2"\n'
        f'ollama_base_url = "http://localhost:11434"\n'
    )


# ---------------------------------------------------------------------------
# S7 — HyDE with Ollama falls back on timeout
# ---------------------------------------------------------------------------

def test_search_hyde_ollama_fallback_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7: Ollama HyDE provider times out → hyde_applied=False (graceful fallback).

    Verifies that when the Ollama AsyncClient.chat() raises TimeoutError,
    the search still returns 200 with hyde_applied=False (not a 500 error).
    """
    doc = tmp_path / "hyde_test.md"
    doc.write_text("# HyDE Test\n\nHypothetical document embedding test content.\n" * 4)

    _install_ollama_stub(monkeypatch)

    toml = _toml_hyde_ollama(hyde_enabled=True)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "ollama", "expected provider=ollama from TOML"
        assert cfg.hyde.enabled, "expected hyde.enabled=True"

        col = "hyde-ollama-timeout"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # After startup, replace the provider's _get_client to raise TimeoutError
        provider = client.app.state.hyde_generator._provider
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError("simulated timeout"))
        monkeypatch.setattr(provider, "_get_client", lambda: mock_client)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "hypothetical document", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data
        assert data["hyde_applied"] is False, (
            f"expected hyde_applied=False on Ollama timeout, got: {data['hyde_applied']}"
        )


# ---------------------------------------------------------------------------
# S8 — RAG Fusion with Ollama falls back on timeout
# ---------------------------------------------------------------------------

def test_search_rag_fusion_ollama_fallback_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8: Ollama RAG Fusion provider times out → rag_fusion_applied=False (graceful fallback).

    Verifies that when the Ollama AsyncClient.chat() raises TimeoutError for
    RAG Fusion, the search returns 200 with rag_fusion_applied=False.
    """
    doc = tmp_path / "rag_fusion_test.md"
    doc.write_text("# RAG Fusion Test\n\nQuery decomposition test content.\n" * 4)

    _install_ollama_stub(monkeypatch)

    toml = _toml_rag_fusion_ollama(rag_fusion_enabled=True)

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.rag_fusion.provider == "ollama", "expected provider=ollama from TOML"
        assert cfg.rag_fusion.enabled, "expected rag_fusion.enabled=True"

        col = "rag-fusion-ollama-timeout"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # After startup, replace the provider's _get_client to raise TimeoutError
        provider = client.app.state.rag_fusion_generator._provider
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError("simulated timeout"))
        monkeypatch.setattr(provider, "_get_client", lambda: mock_client)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "query decomposition test", "rag_fusion": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data
        assert data["rag_fusion_applied"] is False, (
            f"expected rag_fusion_applied=False on Ollama timeout, got: {data['rag_fusion_applied']}"
        )


# ---------------------------------------------------------------------------
# S6 — HyDE=Ollama and RAG Fusion=Anthropic operate independently
# ---------------------------------------------------------------------------

def test_search_hyde_ollama_rag_fusion_anthropic_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: Hyde=ollama, RAG Fusion=anthropic; both work without interference.

    Verifies that each generator uses its configured client independently:
    - HyDE uses OllamaQueryExpansionProvider
    - RAG Fusion uses AnthropicQueryExpansionProvider (or the Anthropic default)
    They must not share state or interfere with each other.

    Also verifies behavioral independence: Ollama HyDE actually calls through
    to produce hyde_applied=True, proving the Ollama path works end-to-end.
    """
    doc = tmp_path / "independence_test.md"
    doc.write_text("# Independence Test\n\nBehavioral independence between providers.\n" * 4)

    _install_ollama_stub(monkeypatch)

    toml = (
        "[hyde]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "llama3.2"\n'
        "\n"
        "[rag_fusion]\n"
        "enabled = true\n"
        'provider = "anthropic"\n'
        'model = "claude-haiku-4-5"\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "ollama", "expected hyde provider=ollama"
        assert cfg.rag_fusion.provider == "anthropic", "expected rag_fusion provider=anthropic"

        # Verify generators have different provider types (construction wiring)
        hyde_provider = client.app.state.hyde_generator._provider
        rag_provider = client.app.state.rag_fusion_generator._provider

        # HyDE should use OllamaQueryExpansionProvider
        from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider
        assert isinstance(hyde_provider, OllamaQueryExpansionProvider), (
            f"HyDE should use OllamaQueryExpansionProvider, got: {type(hyde_provider)}"
        )

        # RAG Fusion should use AnthropicQueryExpansionProvider
        from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider
        assert isinstance(rag_provider, AnthropicQueryExpansionProvider), (
            f"RAG Fusion should use AnthropicQueryExpansionProvider, got: {type(rag_provider)}"
        )

        # Providers must be independent instances (no shared state)
        assert hyde_provider is not rag_provider, "generators must use independent provider instances"

        # Behavioral independence: ingest a document and run Ollama HyDE search
        col = "s6-independence"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "independence test", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["hyde_applied"] is True, (
            f"Ollama HyDE should succeed independently of the Anthropic RAG Fusion provider, "
            f"got hyde_applied={data['hyde_applied']}"
        )


# ---------------------------------------------------------------------------
# C1-B-8 — Factory injects correct provider type (QueryExpansionProvider protocol)
# ---------------------------------------------------------------------------

def test_factory_injects_correct_provider_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-B-8: The factory must inject a QueryExpansionProvider-conforming provider.

    isinstance check works because QueryExpansionProvider is @runtime_checkable.
    Tests both the default (anthropic) and ollama paths.
    """
    from archon_search.query_expansion_protocol import QueryExpansionProvider

    # --- Default (anthropic) provider path ---
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        hyde_provider = client.app.state.hyde_generator._provider
        rag_provider = client.app.state.rag_fusion_generator._provider

        assert isinstance(hyde_provider, QueryExpansionProvider), (
            f"HyDE provider must satisfy QueryExpansionProvider protocol, got: {type(hyde_provider)}"
        )
        assert isinstance(rag_provider, QueryExpansionProvider), (
            f"RAG Fusion provider must satisfy QueryExpansionProvider protocol, got: {type(rag_provider)}"
        )

    # --- Ollama provider path ---
    _install_ollama_stub(monkeypatch)
    toml = (
        "[hyde]\n"
        'provider = "ollama"\n'
        'model = "llama3.2"\n'
        "\n"
        "[rag_fusion]\n"
        'provider = "ollama"\n'
        'model = "llama3.2"\n'
    )
    ollama_tmp = tmp_path / "ollama_sub"
    ollama_tmp.mkdir()
    with make_real_app(ollama_tmp, monkeypatch, toml_content=toml) as (client2, cfg2, api_key2):
        ollama_hyde_provider = client2.app.state.hyde_generator._provider
        ollama_rag_provider = client2.app.state.rag_fusion_generator._provider

        assert isinstance(ollama_hyde_provider, QueryExpansionProvider), (
            f"Ollama HyDE provider must satisfy QueryExpansionProvider protocol, got: {type(ollama_hyde_provider)}"
        )
        assert isinstance(ollama_rag_provider, QueryExpansionProvider), (
            f"Ollama RAG Fusion provider must satisfy QueryExpansionProvider protocol, got: {type(ollama_rag_provider)}"
        )


# ---------------------------------------------------------------------------
# S6 (rate-limit) — Ollama skips rate limit; non-Ollama respects it
# ---------------------------------------------------------------------------

def test_ollama_rate_limit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """S6 (rate-limit): Ollama HyDE provider bypasses the token bucket.

    Unit-style test: creates a HyDEGenerator with provider='ollama' and a mock
    Ollama provider, exhausts _rpm_tokens=0, and verifies the provider IS called
    (rate limit was skipped) and generate() returns a non-None embedding.
    """
    _install_ollama_stub(monkeypatch)

    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator
    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider

    # Mock embedder that returns a fixed vector
    mock_embedder = MagicMock()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 384)

    # Mock Ollama provider that returns a fixed hypothesis text
    mock_provider = MagicMock(spec=OllamaQueryExpansionProvider)
    mock_provider.generate_hypothetical_doc = AsyncMock(
        return_value="A short hypothetical document."
    )

    config = HyDEConfig(provider="ollama", model="llama3.2")
    generator = HyDEGenerator(embedder=mock_embedder, config=config, provider=mock_provider)

    # Exhaust the token bucket
    generator._rpm_tokens = 0

    # generate() must succeed: Ollama skips the rate-limit check
    result = asyncio.run(generator.generate("test query"))

    assert result is not None, (
        "Ollama HyDE should bypass the token bucket and return an embedding vector"
    )
    mock_provider.generate_hypothetical_doc.assert_called_once()
    # Token remains at 0 — Ollama never decrements the bucket
    assert generator._rpm_tokens == 0, (
        "Ollama path must not decrement _rpm_tokens (no API cap)"
    )


def test_non_ollama_provider_respects_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1-T-4: Non-Ollama (Anthropic) HyDE provider IS blocked by the token bucket.

    Unit-style test: creates a HyDEGenerator with provider='anthropic' and a mock
    Anthropic provider that would succeed, sets _rpm_tokens=0, sets ANTHROPIC_API_KEY
    so the key guard inside the provider passes, and asserts the provider is NOT
    called (rate limit fired first) and generate() returns None.
    """
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator
    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider

    # Ensure the API key guard in the provider passes — without this, the result
    # would be None because of the missing key, not because of the rate limit.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-rate-limit-test")

    # Mock embedder
    mock_embedder = MagicMock()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 384)

    # Mock Anthropic provider that would return content if called
    mock_provider = MagicMock(spec=AnthropicQueryExpansionProvider)
    mock_provider.generate_hypothetical_doc = AsyncMock(
        return_value="This would be a hypothesis if not rate-limited."
    )

    config = HyDEConfig(provider="anthropic", model="claude-haiku-4-5")
    generator = HyDEGenerator(embedder=mock_embedder, config=config, provider=mock_provider)

    # Exhaust the token bucket
    generator._rpm_tokens = 0

    # generate() must return None: rate limit fires before calling the provider
    result = asyncio.run(generator.generate("test query"))

    assert result is None, (
        f"Non-Ollama HyDE must return None when _rpm_tokens=0 (rate limited), got: {result}"
    )
    mock_provider.generate_hypothetical_doc.assert_not_called()


def test_ollama_base_url_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-T-3: Custom ollama_base_url is forwarded to OllamaQueryExpansionProvider._base_url.

    Verifies that the factory wires the configured base URL into the provider
    rather than using the default 'http://localhost:11434'.
    """
    _install_ollama_stub(monkeypatch)

    custom_url = "http://custom-ollama-host:9999"
    toml = (
        "[hyde]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "llama3.2"\n'
        f'ollama_base_url = "{custom_url}"\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        hyde_provider = client.app.state.hyde_generator._provider

        from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider
        assert isinstance(hyde_provider, OllamaQueryExpansionProvider), (
            f"Expected OllamaQueryExpansionProvider, got: {type(hyde_provider)}"
        )
        assert hyde_provider._base_url == custom_url, (
            f"Expected ollama_base_url='{custom_url}' to be forwarded to provider._base_url, "
            f"got: '{hyde_provider._base_url}'"
        )


# ---------------------------------------------------------------------------
# C2-M-1 — provider='openai' raises ConfigError at startup
# ---------------------------------------------------------------------------


def test_openai_provider_raises_config_error_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2-M-1: create_app() with provider='openai' raises ConfigError (not yet supported).

    _build_query_expansion_provider raises ConfigError for provider='openai'
    regardless of whether the openai package is installed. This guard must stay
    in place — an untested guard is silently deletable by future refactors.
    """
    from archon_search.config import ConfigError  # noqa: PLC0415

    toml = (
        "[hyde]\n"
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
        'ollama_base_url = "http://localhost:11434"\n'
    )

    with pytest.raises(ConfigError):
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
            pass


# ---------------------------------------------------------------------------
# C2-M-3 — RAG Fusion non-Ollama provider IS blocked by the token bucket
# ---------------------------------------------------------------------------


def test_rag_fusion_non_ollama_provider_respects_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2-M-3: Non-Ollama RAG Fusion provider IS blocked when _rpm_tokens=0.

    Covers the `if not _is_ollama:` branch in rag_fusion.py that would be
    bypassed if _is_ollama were ever erroneously True for Anthropic providers.
    """
    from archon_search.config import RAGFusionConfig  # noqa: PLC0415
    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415
    from archon_search.rag_fusion import RAGFusionGenerator  # noqa: PLC0415

    mock_provider = MagicMock(spec=AnthropicQueryExpansionProvider)
    mock_provider.decompose_query = AsyncMock(return_value=["q1", "q2"])

    config = RAGFusionConfig(provider="anthropic", model="claude-haiku-4-5")
    generator = RAGFusionGenerator(config=config, provider=mock_provider)

    # Exhaust the token bucket
    generator._rpm_tokens = 0

    result = asyncio.run(generator.generate_variants("test query"))

    assert result == [], (
        f"Non-Ollama RAG Fusion must return [] when _rpm_tokens=0 (rate limited), got: {result}"
    )
    mock_provider.decompose_query.assert_not_called()
