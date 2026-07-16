"""Integration tests — claude_cli provider factory wiring and rate-limit skip.

Mirrors the Ollama coverage in test_g10_be4_provider_factory.py:
- factory injects a ClaudeCLIQueryExpansionProvider for provider='claude_cli'
- claude_cli skips the token bucket (local/free path, like Ollama)
- app starts with provider='claude_cli' and a blank model (no ConfigError)

Run with:
    uv run pytest tests/integration/test_claude_cli_provider_factory.py --no-cov -v
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _mock_claude_proc(stdout: bytes) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


def test_factory_injects_claude_cli_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider='claude_cli' → generators use ClaudeCLIQueryExpansionProvider."""
    from archon_search.providers.claude_cli_provider import ClaudeCLIQueryExpansionProvider
    from archon_search.query_expansion_protocol import QueryExpansionProvider

    toml = (
        "[hyde]\n"
        'provider = "claude_cli"\n'
        "\n"
        "[rag_fusion]\n"
        'provider = "claude_cli"\n'
    )
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, _api_key):
        assert cfg.hyde.provider == "claude_cli"
        assert cfg.rag_fusion.provider == "claude_cli"

        hyde_provider = client.app.state.hyde_generator._provider
        rag_provider = client.app.state.rag_fusion_generator._provider

        assert isinstance(hyde_provider, ClaudeCLIQueryExpansionProvider)
        assert isinstance(rag_provider, ClaudeCLIQueryExpansionProvider)
        assert isinstance(hyde_provider, QueryExpansionProvider)
        assert hyde_provider is not rag_provider


def test_claude_cli_blank_model_starts_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider='claude_cli' with no model must start (no ConfigError) — S: guides-not-blocks."""
    toml = "[hyde]\nenabled = true\nprovider = \"claude_cli\"\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, _api_key):
        assert client.get("/health").status_code == 200


def test_claude_cli_rate_limit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude_cli HyDE provider bypasses the token bucket (like Ollama)."""
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator
    from archon_search.providers.claude_cli_provider import ClaudeCLIQueryExpansionProvider

    mock_embedder = MagicMock()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 384)

    mock_provider = MagicMock(spec=ClaudeCLIQueryExpansionProvider)
    mock_provider.generate_hypothetical_doc = AsyncMock(return_value="A hypothetical document.")

    config = HyDEConfig(provider="claude_cli", model="haiku")
    generator = HyDEGenerator(embedder=mock_embedder, config=config, provider=mock_provider)

    generator._rpm_tokens = 0  # exhaust the bucket

    result = asyncio.run(generator.generate("test query"))

    assert result is not None, "claude_cli HyDE should bypass the token bucket"
    mock_provider.generate_hypothetical_doc.assert_called_once()
    assert generator._rpm_tokens == 0, "claude_cli must not decrement the bucket"


def test_claude_cli_rag_fusion_rate_limit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude_cli RAG Fusion provider bypasses the token bucket."""
    from archon_search.config import RAGFusionConfig
    from archon_search.providers.claude_cli_provider import ClaudeCLIQueryExpansionProvider
    from archon_search.rag_fusion import RAGFusionGenerator

    mock_provider = MagicMock(spec=ClaudeCLIQueryExpansionProvider)
    mock_provider.decompose_query = AsyncMock(return_value=["q1", "q2"])

    config = RAGFusionConfig(provider="claude_cli", model="haiku")
    generator = RAGFusionGenerator(config=config, provider=mock_provider)

    generator._rpm_tokens = 0

    result = asyncio.run(generator.generate_variants("test query"))

    assert result == ["q1", "q2"], "claude_cli RAG Fusion should bypass the token bucket"
    mock_provider.decompose_query.assert_called_once()


def test_search_hyde_claude_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: HTTP /search with hyde=True and provider=claude_cli → hyde_applied=True.

    The `claude` subprocess is mocked (canned stdout) so the test is fast and
    environment-independent, but the full path runs for real: HTTP → pipeline →
    HyDEGenerator → ClaudeCLIQueryExpansionProvider → embedding → search.
    """
    import archon_search.providers.claude_cli_provider as ccp

    doc = tmp_path / "claude_cli_e2e.md"
    doc.write_text("# Claude CLI E2E\n\nHypothetical document embedding via the Claude CLI.\n" * 4)

    toml = "[hyde]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "claude_cli"

        # Force the provider to see `claude` as present and drive a canned response.
        provider = client.app.state.hyde_generator._provider
        monkeypatch.setattr(provider, "_claude_available", True)
        monkeypatch.setattr(provider, "_claude_path", "/usr/bin/claude")
        monkeypatch.setattr(
            ccp.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=_mock_claude_proc(b"A hypothetical answer passage.")),
        )

        col = "claude-cli-e2e"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "hypothetical document", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["hyde_applied"] is True


def test_search_hyde_claude_cli_timeout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: claude subprocess times out → search still 200 with hyde_applied=False."""
    import archon_search.providers.claude_cli_provider as ccp

    doc = tmp_path / "claude_cli_timeout.md"
    doc.write_text("# Timeout\n\nGraceful fallback content.\n" * 4)

    toml = "[hyde]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        provider = client.app.state.hyde_generator._provider
        monkeypatch.setattr(provider, "_claude_available", True)
        monkeypatch.setattr(provider, "_claude_path", "/usr/bin/claude")

        timed_out = MagicMock()
        timed_out.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        timed_out.kill = MagicMock()
        timed_out.wait = AsyncMock(return_value=None)
        monkeypatch.setattr(
            ccp.asyncio, "create_subprocess_exec", AsyncMock(return_value=timed_out)
        )

        col = "claude-cli-timeout"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "graceful fallback", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["hyde_applied"] is False
