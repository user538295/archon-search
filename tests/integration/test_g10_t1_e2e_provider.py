"""T-1 (G10) — E2e: Ollama fallback (mocked) and GET /status shows correct provider.

Tests:
  - test_e2e_hyde_ollama_timeout_fallback: HyDE with Ollama times out → hyde_applied=False,
    plain search result returned (status 200)
  - test_e2e_status_both_providers_shown: hyde=ollama / rag_fusion=openai wired (BE-6/BE-7),
    GET /status shows both provider fields present and correct.

Run with:
    uv run pytest tests/integration/test_g10_t1_e2e_provider.py --no-cov -v
"""
from __future__ import annotations

import asyncio
import sys
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


def _install_ollama_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ollama module in sys.modules.

    Must be called BEFORE entering make_real_app because _check_provider_deps
    imports ollama synchronously at create_app() time.
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
            # Response content is irrelevant — _FakeAsyncClient.chat() is never called in either
            # test in this file. The stub is only needed to satisfy the import check in
            # _check_provider_deps at create_app() time.
            return _FakeResponse("")

    fake_ollama.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
    monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider", raising=False)
    return fake_ollama


# ---------------------------------------------------------------------------
# test_e2e_hyde_ollama_timeout_fallback (S7, S8, completes S7)
# ---------------------------------------------------------------------------


def test_e2e_hyde_ollama_timeout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7: HyDE with Ollama provider times out → hyde_applied=False, 200 returned.

    Verifies end-to-end that when the Ollama AsyncClient.chat() raises
    asyncio.TimeoutError, the search still returns HTTP 200 with hyde_applied=False
    (graceful fallback to plain hybrid search) and results are returned.
    """
    doc = tmp_path / "e2e_hyde_test.md"
    doc.write_text("# E2e HyDE Test\n\nHypothetical document embedding fallback content.\n" * 4)

    # Install ollama stub BEFORE entering make_real_app
    _install_ollama_stub(monkeypatch)

    toml = (
        "[hyde]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "llama3.2"\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "ollama", "expected provider=ollama from TOML"
        assert cfg.hyde.enabled, "expected hyde.enabled=True"

        col = "e2e-hyde-ollama-timeout"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Replace the provider's _get_client to raise asyncio.TimeoutError
        provider = client.app.state.hyde_generator._provider
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError("simulated timeout"))
        monkeypatch.setattr(provider, "_get_client", lambda: mock_client)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "hypothetical document fallback", "hyde": True},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "results" in data, "response must contain 'results'"
        assert data["hyde_applied"] is False, (
            f"expected hyde_applied=False on Ollama TimeoutError, got: {data['hyde_applied']}"
        )
        assert data["results"], "expected non-empty results from plain search fallback"


# ---------------------------------------------------------------------------
# test_e2e_status_both_providers_shown (S13, completes S13)
# ---------------------------------------------------------------------------


def test_e2e_status_both_providers_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S13: GET /status shows provider fields for both HyDE and RAG Fusion.

    Wires hyde=ollama / rag_fusion=openai (BE-6/BE-7 implemented).
    Asserts that:
      - data["hyde"]["provider"] == "ollama"
      - data["rag_fusion"]["provider"] == "openai"
    """
    # Install ollama stub BEFORE entering make_real_app (needed for hyde path)
    _install_ollama_stub(monkeypatch)

    # OPENAI_API_KEY needed so RAG Fusion key_available check passes in /status
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-t1-status")

    toml = (
        "[hyde]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "llama3.2"\n'
        "\n"
        "[rag_fusion]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "ollama", "expected hyde.provider=ollama"
        assert cfg.rag_fusion.provider == "openai", "expected rag_fusion.provider=openai"

        resp = client.get("/status", headers=_auth(api_key))

        assert resp.status_code == 200, f"GET /status failed: {resp.status_code}: {resp.text}"
        data = resp.json()

        assert "hyde" in data, "response must contain 'hyde' field"
        hyde = data["hyde"]
        assert hyde is not None, "'hyde' must not be null when hyde.enabled=True"
        assert "provider" in hyde, "'hyde' sub-object must contain 'provider'"
        assert hyde["provider"] == "ollama", (
            f"expected hyde.provider='ollama', got: {hyde['provider']!r}"
        )

        assert "rag_fusion" in data, "response must contain 'rag_fusion' field"
        rag_fusion = data["rag_fusion"]
        assert rag_fusion is not None, "'rag_fusion' must not be null when rag_fusion.enabled=True"
        assert "provider" in rag_fusion, "'rag_fusion' sub-object must contain 'provider'"
        assert rag_fusion["provider"] == "openai", (
            f"expected rag_fusion.provider='openai', got: {rag_fusion['provider']!r}"
        )
        assert rag_fusion["key_available"] is True, (
            f"expected rag_fusion.key_available=True (OPENAI_API_KEY is set), got: {rag_fusion.get('key_available')!r}"
        )
