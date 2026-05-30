"""Tests for reinstall guard (Task C0-2.4)."""
from __future__ import annotations

import dataclasses

import pytest

from archon_search.config import SearchConfig
from archon_search.install import InstallError, NeedsForceDeleteError, _check_reinstall_guard
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES


def _make_cfg(embedding_model: str, chunk_size: int, reranker_model: str = "", profile: str = "minimal") -> SearchConfig:
    cfg = SearchConfig()
    cfg.embedding_model = embedding_model
    cfg.chunk_size = chunk_size
    cfg.reranker_model = reranker_model
    cfg.profile = profile
    return cfg


# ---------------------------------------------------------------------------
# 1. Idempotent — same embedder + chunk_size → no exception
# ---------------------------------------------------------------------------

def test_guard_idempotent_same_model_and_chunk() -> None:
    minimal = ENGLISH_PROFILES["minimal"]
    cfg = _make_cfg(minimal.embedder, minimal.chunk_size)
    _check_reinstall_guard(cfg, minimal, "minimal", False)


# ---------------------------------------------------------------------------
# 2. Different embedder → NeedsForceDeleteError with correct message
# ---------------------------------------------------------------------------

def test_guard_different_embedder_raises() -> None:
    minimal = ENGLISH_PROFILES["minimal"]
    max_profile = ENGLISH_PROFILES["max"]
    cfg = _make_cfg(minimal.embedder, minimal.chunk_size)
    with pytest.raises(NeedsForceDeleteError, match="requires re-indexing all documents"):
        _check_reinstall_guard(cfg, max_profile, "max", False)


# ---------------------------------------------------------------------------
# 3. Same embedder, different chunk_size → raises
# ---------------------------------------------------------------------------

def test_guard_different_chunk_size_raises() -> None:
    minimal = ENGLISH_PROFILES["minimal"]
    profile_different_chunk = dataclasses.replace(minimal, chunk_size=1024)
    cfg = _make_cfg(minimal.embedder, minimal.chunk_size)
    with pytest.raises(NeedsForceDeleteError, match="--force --delete-db"):
        _check_reinstall_guard(cfg, profile_different_chunk, "minimal", False)


# ---------------------------------------------------------------------------
# 4. Reranker-only change — same embedder + chunk_size → no exception
# ---------------------------------------------------------------------------

def test_guard_reranker_only_change_does_not_raise() -> None:
    minimal = ENGLISH_PROFILES["minimal"]
    profile_new_reranker = dataclasses.replace(minimal, reranker="new-reranker-model")
    cfg = _make_cfg(minimal.embedder, minimal.chunk_size, reranker_model="old-reranker")
    _check_reinstall_guard(cfg, profile_new_reranker, "minimal", False)


# ---------------------------------------------------------------------------
# 5. Legacy install (profile == "") — idempotent when models match
# ---------------------------------------------------------------------------

def test_guard_legacy_install_idempotent_when_models_match() -> None:
    minimal = ENGLISH_PROFILES["minimal"]
    cfg = _make_cfg(minimal.embedder, minimal.chunk_size, profile="")
    # Must not raise — same model, legacy install just compares raw fields
    _check_reinstall_guard(cfg, minimal, "minimal", False)


# ---------------------------------------------------------------------------
# 6. Legacy install (profile == "") — raises when model differs
# ---------------------------------------------------------------------------

def test_guard_legacy_install_raises_when_embedder_differs() -> None:
    minimal = ENGLISH_PROFILES["minimal"]
    max_profile = ENGLISH_PROFILES["max"]
    cfg = _make_cfg(minimal.embedder, minimal.chunk_size, profile="")
    with pytest.raises(NeedsForceDeleteError):
        _check_reinstall_guard(cfg, max_profile, "max", False)


# ---------------------------------------------------------------------------
# 7. Multilingual profile — different embedder names still trigger guard
# ---------------------------------------------------------------------------

def test_guard_multilingual_different_embedder_raises() -> None:
    ml_minimal = MULTILINGUAL_PROFILES["minimal"]
    ml_max = MULTILINGUAL_PROFILES["max"]
    cfg = _make_cfg(ml_minimal.embedder, ml_minimal.chunk_size)
    with pytest.raises(NeedsForceDeleteError):
        _check_reinstall_guard(cfg, ml_max, "max", True)


# ---------------------------------------------------------------------------
# 8. NeedsForceDeleteError is a subclass of InstallError
# ---------------------------------------------------------------------------

def test_needs_force_delete_error_is_install_error() -> None:
    assert issubclass(NeedsForceDeleteError, InstallError)
