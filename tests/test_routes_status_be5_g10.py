"""Tests for BE-5 (G10): provider field in HyDE/RAG Fusion status detail (C3, S13).

Tests:
  - GET /status includes hyde.provider when hyde.enabled=True
  - GET /status includes rag_fusion.provider when rag_fusion.enabled=True
  - key_available is always True for ollama provider (regardless of ANTHROPIC_API_KEY)
  - key_available for openai provider reads OPENAI_API_KEY
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import HyDEConfig, RAGFusionConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "search"
    db.mkdir()
    return db


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_client_with_providers(
    tmp_db: Path,
    *,
    hyde_enabled: bool = False,
    hyde_provider: str = "anthropic",
    rag_fusion_enabled: bool = False,
    rag_fusion_provider: str = "anthropic",
) -> TestClient:
    """Build a TestClient with specific HyDE and RAG Fusion provider configuration."""
    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.hyde = HyDEConfig(enabled=hyde_enabled, provider=hyde_provider)
    config.rag_fusion = RAGFusionConfig(enabled=rag_fusion_enabled, provider=rag_fusion_provider)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


# ---------------------------------------------------------------------------
# S13: provider field in status response
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_shows_hyde_provider_ollama(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status includes hyde.provider == 'ollama' when config.hyde.provider = 'ollama' (BE-5 S13)."""
    client = _make_client_with_providers(tmp_db, hyde_enabled=True, hyde_provider="ollama")
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "hyde" in data
    hyde = data["hyde"]
    assert hyde is not None
    assert hyde["provider"] == "ollama"


@pytest.mark.integration
def test_status_shows_rag_fusion_provider_anthropic(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status includes rag_fusion.provider == 'anthropic' when config.rag_fusion.provider = 'anthropic' (BE-5 S13)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    client = _make_client_with_providers(
        tmp_db, rag_fusion_enabled=True, rag_fusion_provider="anthropic"
    )
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "rag_fusion" in data
    rf = data["rag_fusion"]
    assert rf is not None
    assert rf["provider"] == "anthropic"
    assert rf["key_available"] is True


# ---------------------------------------------------------------------------
# Root-2: provider-aware key_available
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_ollama_key_available_is_true(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """key_available is always True for ollama provider, regardless of ANTHROPIC_API_KEY (BE-5 Root-2)."""
    # Explicitly set ANTHROPIC_API_KEY to prove ollama ignores it
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set-but-ollama-ignores")
    client = _make_client_with_providers(tmp_db, hyde_enabled=True, hyde_provider="ollama")
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    hyde = data["hyde"]
    assert hyde is not None
    assert hyde["key_available"] is True


def test_status_openai_key_available_checks_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_key_available() for openai provider reads OPENAI_API_KEY (BE-5 Root-2).

    Tests HyDEGenerator directly (provider='openai' is not yet wired in
    create_app — that is BE-6).  OPENAI_API_KEY unset → False; set → True.
    """
    from archon_search.hyde import HyDEGenerator
    from unittest.mock import MagicMock

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    stub_embedder = MagicMock()
    generator = HyDEGenerator(
        embedder=stub_embedder,
        config=HyDEConfig(provider="openai"),
        provider=MagicMock(),  # stub provider so no ImportError
    )
    assert generator.is_key_available() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    assert generator.is_key_available() is True


# ---------------------------------------------------------------------------
# C1-I-21: RAGFusionGenerator.is_key_available() branches
# ---------------------------------------------------------------------------


def test_rag_fusion_is_key_available_ollama_always_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAGFusionGenerator.is_key_available() returns True for ollama regardless of env keys (C1-I-21)."""
    from archon_search.rag_fusion import RAGFusionGenerator
    from unittest.mock import MagicMock

    # Even with no API keys present, ollama must return True
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    generator = RAGFusionGenerator(
        config=RAGFusionConfig(provider="ollama"),
        provider=MagicMock(),  # stub provider so no ImportError
    )
    assert generator.is_key_available() is True

    # Also True when keys happen to be set
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set-but-ollama-ignores")
    assert generator.is_key_available() is True


def test_rag_fusion_is_key_available_openai_checks_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAGFusionGenerator.is_key_available() reads OPENAI_API_KEY for openai provider (C1-I-21)."""
    from archon_search.rag_fusion import RAGFusionGenerator
    from unittest.mock import MagicMock

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    generator = RAGFusionGenerator(
        config=RAGFusionConfig(provider="openai"),
        provider=MagicMock(),  # stub provider so no ImportError
    )
    assert generator.is_key_available() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    assert generator.is_key_available() is True
