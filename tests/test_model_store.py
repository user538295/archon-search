"""Tests for archon_search.model_store."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.model_specs import EMBEDDER_SPECS, EmbedderSpec
from archon_search.model_store import ModelDownloadError, ensure_model, model_path

_SPEC = EMBEDDER_SPECS["BAAI/bge-small-en-v1.5"]
_FILES = ("model.safetensors", "config.json", "tokenizer.json")
_SNAPSHOT_DOWNLOAD = "archon_search.model_store.snapshot_download"
_SECRET = "/Users/secret/path token=hf_abc123"


@pytest.fixture(autouse=True)
def models_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return tmp_path / "models"


def _snapshot_dir(root: Path, spec: EmbedderSpec = _SPEC, revision: str | None = None) -> Path:
    org_name = spec.repo.replace("/", "--")
    return root / f"models--{org_name}" / "snapshots" / (revision or spec.revision)


def _populate(snapshot: Path, files: tuple[str, ...] = _FILES) -> Path:
    snapshot.mkdir(parents=True)
    for name in files:
        (snapshot / name).write_bytes(b"x")
    return snapshot


def test_returns_none_when_snapshot_missing() -> None:
    assert model_path(_SPEC) is None


def test_returns_path_when_all_files_present(models_root: Path) -> None:
    snapshot = _populate(_snapshot_dir(models_root))

    assert model_path(_SPEC) == snapshot


def test_returns_none_when_weights_missing(models_root: Path) -> None:
    _populate(_snapshot_dir(models_root), files=("config.json", "tokenizer.json"))

    assert model_path(_SPEC) is None


def test_wrong_revision_is_not_a_hit(models_root: Path) -> None:
    _populate(_snapshot_dir(models_root, revision="0" * 40))

    assert model_path(_SPEC) is None


def test_ensure_returns_the_cached_snapshot_without_downloading(models_root: Path) -> None:
    snapshot = _populate(_snapshot_dir(models_root))

    with patch(_SNAPSHOT_DOWNLOAD) as download:
        assert ensure_model(_SPEC) == snapshot

    download.assert_not_called()


def test_download_requests_only_the_three_files_at_the_pinned_revision(models_root: Path) -> None:
    def fake_download(**kwargs: object) -> str:
        return str(_populate(_snapshot_dir(models_root)))

    with patch(_SNAPSHOT_DOWNLOAD, side_effect=fake_download) as download:
        result = ensure_model(_SPEC)

    download.assert_called_once_with(
        repo_id=_SPEC.repo,
        revision=_SPEC.revision,
        allow_patterns=["model.safetensors", "config.json", "tokenizer.json"],
        cache_dir=models_root,
    )
    assert result == _snapshot_dir(models_root)


def test_offline_miss_raises_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    with patch(_SNAPSHOT_DOWNLOAD) as download:
        with pytest.raises(ModelDownloadError) as excinfo:
            ensure_model(_SPEC)

    download.assert_not_called()
    assert _SPEC.repo in str(excinfo.value)
    assert "offline" in str(excinfo.value)


def test_offline_hit_works(models_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    snapshot = _populate(_snapshot_dir(models_root))

    assert ensure_model(_SPEC) == snapshot


def test_error_text_names_repo_not_exception_text(caplog: pytest.LogCaptureFixture) -> None:
    with patch(_SNAPSHOT_DOWNLOAD, side_effect=OSError(_SECRET)):
        with caplog.at_level(logging.DEBUG, logger="archon_search.model_store"):
            with pytest.raises(ModelDownloadError) as excinfo:
                ensure_model(_SPEC)

    assert _SPEC.repo in str(excinfo.value)
    assert _SECRET not in str(excinfo.value)
    logged = [r for r in caplog.records if _SECRET in r.getMessage() and r.exc_info]
    assert logged, "the cause must be logged with its traceback"
    assert all(r.levelno == logging.DEBUG for r in logged)


def test_incomplete_download_raises_download_error(tmp_path: Path) -> None:
    incomplete = _populate(tmp_path / "snap", files=("config.json",))

    with patch(_SNAPSHOT_DOWNLOAD, return_value=str(incomplete)):
        with pytest.raises(ModelDownloadError) as excinfo:
            ensure_model(_SPEC)

    assert _SPEC.repo in str(excinfo.value)


@pytest.mark.live_eval
def test_ensure_model_downloads_bge_small(models_root: Path) -> None:
    path = ensure_model(_SPEC)

    assert path == model_path(_SPEC)
    assert all((path / name).is_file() for name in _FILES)
