"""Tests for BE-8: HyDE/RAG Fusion key availability in GET /status (E0b C2).

Tests:
  - HyDEGenerator.is_key_available() returns True/False based on ANTHROPIC_API_KEY
  - RAGFusionGenerator.is_key_available() returns True/False based on ANTHROPIC_API_KEY
  - GET /status includes hyde.key_available when hyde.enabled=True
  - GET /status returns hyde=null when hyde.enabled=False
  - Same for rag_fusion
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


def _make_client_with_expansion_config(
    tmp_db: Path,
    *,
    hyde_enabled: bool = False,
    rag_fusion_enabled: bool = False,
) -> TestClient:
    """Build a TestClient with specific HyDE and RAG Fusion configuration.

    Follows the pattern established by _make_client_with_mcp_config and
    _make_client_with_telemetry_config in test_routes_status.py.
    """
    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.hyde = HyDEConfig(enabled=hyde_enabled)
    config.rag_fusion = RAGFusionConfig(enabled=rag_fusion_enabled)
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
# Unit tests: HyDEGenerator.is_key_available()
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_hyde_key_available_true_when_key_set(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HyDEGenerator.is_key_available() returns True when ANTHROPIC_API_KEY is set (BE-8 S6).

    Also asserts that GET /status reflects key_available=True in the hyde sub-object
    when hyde.enabled=True.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    from archon_search.hyde import HyDEGenerator
    from unittest.mock import MagicMock

    stub_embedder = MagicMock()
    generator = HyDEGenerator(embedder=stub_embedder, config=HyDEConfig())
    assert generator.is_key_available() is True

    client = _make_client_with_expansion_config(tmp_db, hyde_enabled=True)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "hyde" in data
    hyde = data["hyde"]
    assert hyde is not None
    assert hyde["key_available"] is True


@pytest.mark.integration
def test_status_hyde_key_available_false_when_key_absent(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HyDEGenerator.is_key_available() returns False when ANTHROPIC_API_KEY is absent (BE-8 S7).

    conftest.py clears ANTHROPIC_API_KEY for every test, so this requires no
    extra monkeypatch action.  The monkeypatch param is named to document intent.
    """
    # ANTHROPIC_API_KEY is already cleared by conftest.py — no setenv needed.
    from archon_search.hyde import HyDEGenerator
    from unittest.mock import MagicMock

    stub_embedder = MagicMock()
    generator = HyDEGenerator(embedder=stub_embedder, config=HyDEConfig())
    assert generator.is_key_available() is False

    client = _make_client_with_expansion_config(tmp_db, hyde_enabled=True)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "hyde" in data
    hyde = data["hyde"]
    assert hyde is not None
    assert hyde["key_available"] is False


# ---------------------------------------------------------------------------
# Unit tests: RAGFusionGenerator.is_key_available()
# ---------------------------------------------------------------------------


def test_rag_fusion_generator_is_key_available_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAGFusionGenerator.is_key_available() returns True when ANTHROPIC_API_KEY is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    from archon_search.rag_fusion import RAGFusionGenerator

    generator = RAGFusionGenerator(config=RAGFusionConfig())
    assert generator.is_key_available() is True


def test_rag_fusion_generator_is_key_available_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAGFusionGenerator.is_key_available() returns False when ANTHROPIC_API_KEY is absent."""
    # ANTHROPIC_API_KEY already cleared by conftest.py.
    from archon_search.rag_fusion import RAGFusionGenerator

    generator = RAGFusionGenerator(config=RAGFusionConfig())
    assert generator.is_key_available() is False


# ---------------------------------------------------------------------------
# Integration tests: GET /status via TestClient
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_response_key_available_via_test_client(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status with monkeypatched ANTHROPIC_API_KEY includes hyde sub-object
    with key_available=True (BE-8 C2, S6).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    client = _make_client_with_expansion_config(tmp_db, hyde_enabled=True)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "hyde" in data
    hyde = data["hyde"]
    assert hyde is not None
    assert hyde["key_available"] is True


@pytest.mark.integration
def test_status_hyde_null_when_hyde_disabled(tmp_db: Path) -> None:
    """GET /status returns hyde=null when config.hyde.enabled=False (BE-8 C2).

    Key availability is irrelevant when the feature is not configured.
    """
    client = _make_client_with_expansion_config(tmp_db, hyde_enabled=False)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "hyde" in data
    assert data["hyde"] is None


@pytest.mark.integration
def test_status_rag_fusion_null_when_rag_fusion_disabled(tmp_db: Path) -> None:
    """GET /status returns rag_fusion=null when config.rag_fusion.enabled=False (BE-8 C2)."""
    client = _make_client_with_expansion_config(tmp_db, rag_fusion_enabled=False)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "rag_fusion" in data
    assert data["rag_fusion"] is None


@pytest.mark.integration
def test_status_rag_fusion_key_available_true_when_key_set(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status includes rag_fusion.key_available=True when ANTHROPIC_API_KEY is set (BE-8 C2, S6)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    client = _make_client_with_expansion_config(tmp_db, rag_fusion_enabled=True)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "rag_fusion" in data
    rf = data["rag_fusion"]
    assert rf is not None
    assert rf["key_available"] is True


@pytest.mark.integration
def test_status_rag_fusion_key_available_false_when_key_absent(tmp_db: Path) -> None:
    """GET /status includes rag_fusion.key_available=False when ANTHROPIC_API_KEY is absent (BE-8 C2, S7)."""
    client = _make_client_with_expansion_config(tmp_db, rag_fusion_enabled=True)
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "rag_fusion" in data
    rf = data["rag_fusion"]
    assert rf is not None
    assert rf["key_available"] is False


# ---------------------------------------------------------------------------
# Call-time evaluation guarantee (C1-B-2)
# ---------------------------------------------------------------------------


def test_hyde_generator_is_key_available_reflects_call_time_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_key_available() reads ANTHROPIC_API_KEY at call time, not construction time.

    Constructs the generator with no key, verifies False, then sets the key and
    verifies True without reconstruction — proving the docstring's "at call time"
    guarantee has a regression guard.
    """
    from archon_search.hyde import HyDEGenerator
    from unittest.mock import MagicMock

    stub_embedder = MagicMock()
    generator = HyDEGenerator(embedder=stub_embedder, config=HyDEConfig())
    assert generator.is_key_available() is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-late-set")
    assert generator.is_key_available() is True


def test_rag_fusion_generator_is_key_available_reflects_call_time_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAGFusionGenerator.is_key_available() reads ANTHROPIC_API_KEY at call time, not construction time."""
    from archon_search.rag_fusion import RAGFusionGenerator

    generator = RAGFusionGenerator(config=RAGFusionConfig())
    assert generator.is_key_available() is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-late-set")
    assert generator.is_key_available() is True


# ---------------------------------------------------------------------------
# Combined-enabled test (C1-T-04)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_both_hyde_and_rag_fusion_enabled(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status returns both hyde and rag_fusion sub-objects when both features are enabled."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    client = _make_client_with_expansion_config(
        tmp_db, hyde_enabled=True, rag_fusion_enabled=True
    )
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "hyde" in data
    assert "rag_fusion" in data
    hyde = data["hyde"]
    rf = data["rag_fusion"]
    assert hyde is not None
    assert rf is not None
    assert hyde["key_available"] is True
    assert rf["key_available"] is True
