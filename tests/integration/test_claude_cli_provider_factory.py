"""Integration tests — claude_cli provider factory wiring and rate-limit skip.

Mirrors the Ollama coverage in test_g10_be4_provider_factory.py:
- factory injects a ClaudeCliClient into HyDE and into RAG Fusion for provider='claude_cli'
- RAG Fusion over the fake ``claude`` binary: prompt on stdin, one transport retry, timeout fallback
- claude_cli skips the token bucket (local/free path, like Ollama)
- app starts with provider='claude_cli' and a blank model (no ConfigError)

Run with:
    uv run pytest tests/integration/test_claude_cli_provider_factory.py --no-cov -v
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.llm_client import claude_cli_client
from tests import _fake_claude
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_HANG_SECONDS = 30.0
_HYDE_TIMEOUT_SECONDS = 3.0  # covers the fake binary's interpreter start-up before it records its invocation


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **behaviour: object
) -> _fake_claude.FakeClaude:
    """Put the fake ``claude`` first on ``PATH``, so the app composes its ``ClaudeCliClient`` on it."""
    fake = _fake_claude.install(tmp_path / "fake-claude-bin", **behaviour)  # type: ignore[arg-type]
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    return fake


def test_factory_injects_claude_cli_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider='claude_cli' → HyDE and RAG Fusion each get a ClaudeCliClient, handed to their generator."""
    from archon_search.llm_client.claude_cli_client import ClaudeCliClient

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

        hyde_client = client.app.state.hyde_client
        rag_client = client.app.state.rag_fusion_client

        assert isinstance(hyde_client, ClaudeCliClient)
        assert client.app.state.hyde_generator._client is hyde_client
        assert isinstance(rag_client, ClaudeCliClient)
        assert client.app.state.rag_fusion_generator._client is rag_client
        assert rag_client is not hyde_client


def test_claude_cli_blank_model_starts_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider='claude_cli' with no model must start (no ConfigError) — S: guides-not-blocks."""
    toml = "[hyde]\nenabled = true\nprovider = \"claude_cli\"\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, _api_key):
        assert client.get("/health").status_code == 200


def test_claude_cli_rate_limit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude_cli HyDE bypasses the token bucket (like Ollama)."""
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator
    from tests._hyde_stubs import ScriptedLlmClient

    mock_embedder = MagicMock()
    mock_embedder.embed_one = AsyncMock(return_value=[0.1] * 384)

    client = ScriptedLlmClient(reply="A hypothetical document.")
    config = HyDEConfig(provider="claude_cli", model="haiku")
    generator = HyDEGenerator(config=config, client=client)

    generator._rpm_tokens = 0  # exhaust the bucket

    result = asyncio.run(generator.generate("test query", embedder=mock_embedder))

    assert result is not None, "claude_cli HyDE should bypass the token bucket"
    assert len(client.requests) == 1
    assert generator._rpm_tokens == 0, "claude_cli must not decrement the bucket"


def test_claude_cli_rag_fusion_rate_limit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude_cli RAG Fusion bypasses the token bucket."""
    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator
    from tests._hyde_stubs import ScriptedLlmClient

    client = ScriptedLlmClient(reply="q1\nq2")
    config = RAGFusionConfig(provider="claude_cli", model="haiku")
    generator = RAGFusionGenerator(config=config, client=client)

    generator._rpm_tokens = 0

    result = asyncio.run(generator.generate_variants("test query"))

    assert result == ["q1", "q2"], "claude_cli RAG Fusion should bypass the token bucket"
    assert len(client.requests) == 1
    assert generator._rpm_tokens == 0, "claude_cli must not decrement the bucket"


def test_search_hyde_claude_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: HTTP /search with hyde=True and provider=claude_cli → hyde_applied=True.

    The ``claude`` on ``PATH`` is the real-subprocess fake of ``tests/_fake_claude.py``, so the full path runs:
    HTTP → pipeline → HyDEGenerator → ClaudeCliClient → fake binary (prompt on standard input) → embedding → search.
    """
    fake = _install_fake_claude(tmp_path, monkeypatch, reply="A hypothetical answer passage.")
    doc = tmp_path / "claude_cli_e2e.md"
    doc.write_text("# Claude CLI E2E\n\nHypothetical document embedding via the Claude CLI.\n" * 4)

    toml = "[hyde]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.hyde.provider == "claude_cli"

        col = "claude-cli-e2e"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "hypothetical document", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["hyde_applied"] is True

    [invocation] = fake.invocations()
    assert "hypothetical document" in invocation.stdin
    assert "hypothetical document" not in " ".join(invocation.argv)


def test_search_hyde_claude_cli_timeout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: claude subprocess outlives ``[hyde].timeout_seconds`` → search still 200 with hyde_applied=False."""
    fake = _install_fake_claude(tmp_path, monkeypatch, hang_seconds=_HANG_SECONDS)
    doc = tmp_path / "claude_cli_timeout.md"
    doc.write_text("# Timeout\n\nGraceful fallback content.\n" * 4)

    toml = (
        "[hyde]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\n"
        f"timeout_seconds = {_HYDE_TIMEOUT_SECONDS}\n"
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
        col = "claude-cli-timeout"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "graceful fallback", "hyde": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["hyde_applied"] is False

    [invocation] = fake.invocations()
    assert "graceful fallback" in invocation.stdin, "the call must have reached the hanging binary"


_RAG_FUSION_CLAUDE_CLI_TOML = (
    "[rag_fusion]\nenabled = true\nprovider = \"claude_cli\"\nmodel = \"haiku\"\n"
)
_RAG_FUSION_REPLY = "documentation variant one\ndocumentation variant two"
_RAG_FUSION_TIMEOUT_SECONDS = _HYDE_TIMEOUT_SECONDS  # same fake-binary start-up allowance as the HyDE twin


def test_search_rag_fusion_claude_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: HTTP /search with rag_fusion=True and provider=claude_cli fuses the variants the ``claude`` binary printed.

    HTTP → pipeline → RAGFusionGenerator → ClaudeCliClient → fake binary (decomposition prompt on standard input).
    """
    fake = _install_fake_claude(tmp_path, monkeypatch, reply=_RAG_FUSION_REPLY)
    doc = tmp_path / "claude_cli_rag_fusion.md"
    doc.write_text("# Claude CLI RAG Fusion\n\nQuery decomposition via the Claude CLI.\n" * 4)

    with make_real_app(tmp_path, monkeypatch, toml_content=_RAG_FUSION_CLAUDE_CLI_TOML) as (client, cfg, api_key):
        assert cfg.rag_fusion.provider == "claude_cli"
        col = "claude-cli-rag-fusion"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "query decomposition", "rag_fusion": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["rag_fusion_applied"] is True
        assert resp.json()["rag_fusion_queries_used"] == 2

    [invocation] = fake.invocations()
    assert "query decomposition" in invocation.stdin
    assert "generate 2 alternative" in invocation.stdin
    assert "query decomposition" not in " ".join(invocation.argv)


def test_search_rag_fusion_claude_cli_retries_one_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG Fusion's ``claude_cli`` client keeps the one transport retry (Q38): a first non-zero exit is absorbed."""
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)
    fake = _install_fake_claude(tmp_path, monkeypatch, reply=_RAG_FUSION_REPLY, exit_codes=(1, 0))
    doc = tmp_path / "claude_cli_rag_fusion_retry.md"
    doc.write_text("# Retry\n\nThe second attempt answers.\n" * 4)

    with make_real_app(tmp_path, monkeypatch, toml_content=_RAG_FUSION_CLAUDE_CLI_TOML) as (client, _cfg, api_key):
        col = "claude-cli-rag-fusion-retry"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "second attempt", "rag_fusion": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["rag_fusion_applied"] is True

    assert len(fake.invocations()) == 2


def test_search_rag_fusion_claude_cli_timeout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude subprocess outlives ``[rag_fusion].timeout_seconds`` → search still 200 with rag_fusion_applied=False."""
    fake = _install_fake_claude(tmp_path, monkeypatch, hang_seconds=_HANG_SECONDS)
    doc = tmp_path / "claude_cli_rag_fusion_timeout.md"
    doc.write_text("# Timeout\n\nGraceful fallback content.\n" * 4)
    toml = _RAG_FUSION_CLAUDE_CLI_TOML + f"timeout_seconds = {_RAG_FUSION_TIMEOUT_SECONDS}\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
        col = "claude-cli-rag-fusion-timeout"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "graceful fallback", "rag_fusion": True},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["rag_fusion_applied"] is False
        assert resp.json()["rag_fusion_attempted"] is True

    [invocation] = fake.invocations()
    assert "graceful fallback" in invocation.stdin, "the decomposition prompt must have reached the hanging binary"
