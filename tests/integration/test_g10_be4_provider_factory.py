"""BE-4 — Provider factory wiring and rate-limit skip for Ollama.

Tests:
- S7: HyDE with Ollama falls back gracefully on timeout → hyde_applied=False
- S8: RAG Fusion with Ollama falls back gracefully on timeout → rag_fusion_applied=False
- S6: Hyde=Ollama, RAG Fusion=Anthropic operate independently with no cross-contamination
- C1-B-8: factory injects the shared LlmClient (HyDE) and a QueryExpansionProvider (RAG Fusion)
- S6 (rate-limit): Ollama provider skips rate limiting; non-Ollama provider respects it
- C1-T-3: ollama_base_url is the host the HyDE client calls
- C1-T-4: non-Ollama (Anthropic) provider IS blocked when token bucket exhausted

Run with:
    uv run pytest tests/integration/test_g10_be4_provider_factory.py --no-cov -v
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.llm_client.protocol import LlmCallRequest
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_SLOW_ANSWER_SECONDS = 3.0
_HYDE_TIMEOUT_SECONDS = 0.5
_REQUEST_TIMEOUT_SECONDS = 10.0
_MAX_OUTPUT_TOKENS = 200


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests of the fake model server."""
    keep_loopback_out_of_proxies(monkeypatch)


def _hyde_request() -> LlmCallRequest:
    return LlmCallRequest(
        prompt="write a passage", max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=_REQUEST_TIMEOUT_SECONDS
    )

# anthropic is stubbed for all integration tests by the autouse fixture in
# tests/integration/conftest.py (create_app's provider guard imports it).


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


def _toml_hyde_ollama(
    hyde_enabled: bool = True, base_url: str = "http://localhost:11434", timeout_seconds: float | None = None
) -> str:
    toml = (
        f"[hyde]\n"
        f"enabled = {'true' if hyde_enabled else 'false'}\n"
        f'provider = "ollama"\n'
        f'model = "llama3.2"\n'
        f'ollama_base_url = "{base_url}"\n'
    )
    if timeout_seconds is not None:
        toml += f"timeout_seconds = {timeout_seconds}\n"
    return toml


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
    """S7: Ollama HyDE times out → hyde_applied=False (graceful fallback).

    The fake Ollama server answers later than ``[hyde].timeout_seconds``: the shared client raises a timeout,
    the search still returns 200 with hyde_applied=False (not a 500 error).
    """
    doc = tmp_path / "hyde_test.md"
    doc.write_text("# HyDE Test\n\nHypothetical document embedding test content.\n" * 4)

    with FakeModelServer(response_delay_seconds=_SLOW_ANSWER_SECONDS) as server:
        toml = _toml_hyde_ollama(hyde_enabled=True, base_url=server.base_url, timeout_seconds=_HYDE_TIMEOUT_SECONDS)

        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
            assert cfg.hyde.provider == "ollama", "expected provider=ollama from TOML"
            assert cfg.hyde.enabled, "expected hyde.enabled=True"

            col = "hyde-ollama-timeout"
            ingest_file_via_path(client, col, str(doc), api_key=api_key)

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
            assert len(server.requests_to("/api/chat")) == 1, "the call must have reached the slow provider"


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
        mock_client.chat = AsyncMock(side_effect=TimeoutError("simulated timeout"))
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
    - HyDE uses an OllamaClient on its own fake server
    - RAG Fusion uses AnthropicQueryExpansionProvider (or the Anthropic default)
    They must not share state or interfere with each other.

    Also verifies behavioral independence: Ollama HyDE actually calls through
    to produce hyde_applied=True, proving the Ollama path works end-to-end.
    """
    from archon_search.llm_client.ollama_client import OllamaClient

    doc = tmp_path / "independence_test.md"
    doc.write_text("# Independence Test\n\nBehavioral independence between providers.\n" * 4)

    with FakeModelServer(reply_text="A short hypothetical document.") as server:
        toml = (
            "[hyde]\n"
            "enabled = true\n"
            'provider = "ollama"\n'
            'model = "llama3.2"\n'
            f'ollama_base_url = "{server.base_url}"\n'
            "\n"
            "[rag_fusion]\n"
            "enabled = true\n"
            'provider = "anthropic"\n'
            'model = "claude-haiku-4-5"\n'
        )

        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
            assert cfg.hyde.provider == "ollama", "expected hyde provider=ollama"
            assert cfg.rag_fusion.provider == "anthropic", "expected rag_fusion provider=anthropic"

            # Verify the two generators sit on different kinds of caller (construction wiring)
            hyde_client = client.app.state.hyde_client
            rag_provider = client.app.state.rag_fusion_generator._provider
            assert isinstance(hyde_client, OllamaClient), (
                f"HyDE should use an OllamaClient, got: {type(hyde_client)}"
            )
            from archon_search.providers.anthropic_provider import (
                AnthropicQueryExpansionProvider,
            )
            assert isinstance(rag_provider, AnthropicQueryExpansionProvider), (
                f"RAG Fusion should use AnthropicQueryExpansionProvider, got: {type(rag_provider)}"
            )

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
    assert len(server.requests_to("/api/chat")) == 1


# ---------------------------------------------------------------------------
# C1-B-8 — Factory injects correct provider type (QueryExpansionProvider protocol)
# ---------------------------------------------------------------------------

def test_factory_injects_correct_provider_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-B-8: HyDE gets a shared ``LlmClient`` of its own provider; RAG Fusion keeps a QueryExpansionProvider.

    isinstance checks work because both protocols are @runtime_checkable.
    Tests both the default (anthropic) and ollama paths.
    """
    from archon_search.llm_client.anthropic_client import AnthropicClient
    from archon_search.llm_client.ollama_client import OllamaClient
    from archon_search.llm_client.protocol import LlmClient
    from archon_search.query_expansion_protocol import QueryExpansionProvider

    # --- Default (anthropic) provider path ---
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _api_key):
        hyde_client = client.app.state.hyde_client
        rag_provider = client.app.state.rag_fusion_generator._provider

        assert isinstance(hyde_client, AnthropicClient)
        assert isinstance(hyde_client, LlmClient)
        assert client.app.state.hyde_generator._client is hyde_client
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
    with make_real_app(ollama_tmp, monkeypatch, toml_content=toml) as (client2, _cfg2, _api_key2):
        ollama_hyde_client = client2.app.state.hyde_client
        ollama_rag_provider = client2.app.state.rag_fusion_generator._provider

        assert isinstance(ollama_hyde_client, OllamaClient)
        assert isinstance(ollama_hyde_client, LlmClient)
        assert isinstance(ollama_rag_provider, QueryExpansionProvider), (
            f"Ollama RAG Fusion provider must satisfy QueryExpansionProvider protocol, got: {type(ollama_rag_provider)}"
        )


# ---------------------------------------------------------------------------
# S6 (rate-limit) — Ollama skips rate limit; non-Ollama respects it
# ---------------------------------------------------------------------------

def test_ollama_rate_limit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """S6 (rate-limit): Ollama HyDE bypasses the token bucket.

    Unit-style test: creates a HyDEGenerator with provider='ollama' over a scripted client, exhausts
    _rpm_tokens=0, and verifies the client IS called (rate limit was skipped) and generate() returns an embedding.
    """
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator
    from tests._hyde_stubs import ScriptedLlmClient

    mock_embedder = MagicMock()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 384)

    client = ScriptedLlmClient(reply="A short hypothetical document.")
    config = HyDEConfig(provider="ollama", model="llama3.2")
    generator = HyDEGenerator(config=config, client=client)

    # Exhaust the token bucket
    generator._rpm_tokens = 0

    # generate() must succeed: Ollama skips the rate-limit check
    result = asyncio.run(generator.generate("test query", embedder=mock_embedder))

    assert result is not None, (
        "Ollama HyDE should bypass the token bucket and return an embedding vector"
    )
    assert len(client.requests) == 1
    # Token remains at 0 — Ollama never decrements the bucket
    assert generator._rpm_tokens == 0, (
        "Ollama path must not decrement _rpm_tokens (no API cap)"
    )


def test_non_ollama_provider_respects_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1-T-4: Non-Ollama (Anthropic) HyDE IS blocked by the token bucket.

    Unit-style test: creates a HyDEGenerator with provider='anthropic' over a client that would succeed, sets
    _rpm_tokens=0, and asserts the client is NOT called (rate limit fired first) and generate() returns None.
    """
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator
    from tests._hyde_stubs import ScriptedLlmClient

    # Ensure the key is set — without it a None result could be a missing key, not the rate limit.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-rate-limit-test")

    mock_embedder = MagicMock()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 384)

    client = ScriptedLlmClient(reply="This would be a hypothesis if not rate-limited.")
    config = HyDEConfig(provider="anthropic", model="claude-haiku-4-5")
    generator = HyDEGenerator(config=config, client=client)

    # Exhaust the token bucket
    generator._rpm_tokens = 0

    # generate() must return None: rate limit fires before calling the client
    result = asyncio.run(generator.generate("test query", embedder=mock_embedder))

    assert result is None, (
        f"Non-Ollama HyDE must return None when _rpm_tokens=0 (rate limited), got: {result}"
    )
    assert client.requests == []


def test_ollama_base_url_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-T-3: ``[hyde].ollama_base_url`` is the host the HyDE client calls, not the default localhost:11434."""
    with FakeModelServer(reply_text="A short hypothetical document.") as server:
        toml = _toml_hyde_ollama(hyde_enabled=True, base_url=server.base_url)

        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, _api_key):
            reply = asyncio.run(client.app.state.hyde_client.call(_hyde_request()))

    assert reply.text == "A short hypothetical document."
    assert len(server.requests_to("/api/chat")) == 1


# ---------------------------------------------------------------------------
# C2-M-1 — provider='openai' starts the app successfully (BE-6 implemented)
# ---------------------------------------------------------------------------


def test_openai_provider_starts_app_successfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2-M-1 (updated BE-6): create_app() with provider='openai' and a valid model starts OK.

    BE-6 implemented OpenAIQueryExpansionProvider; the old placeholder ConfigError
    guard no longer applies when the openai package is installed (which it is as a
    dev dependency). The app must start and respond to GET /health.
    """
    toml = (
        "[hyde]\n"
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
        'ollama_base_url = "http://localhost:11434"\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, _api_key):
        resp = client.get("/health")
        assert resp.status_code == 200


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
    from archon_search.config import RAGFusionConfig
    from archon_search.providers.anthropic_provider import (
        AnthropicQueryExpansionProvider,
    )
    from archon_search.rag_fusion import RAGFusionGenerator

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
