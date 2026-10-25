"""The GLiNER checkpoint is fetched through ``model_store`` at a pinned revision with a file allow-list (SecReview #9).

A fake downloader stands in for ``huggingface_hub.snapshot_download``; the real gliner library is never imported.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archon_search import model_store
from archon_search.model_store import ModelDownloadError, ensure_pinned_snapshot
from archon_search.paths import (
    GRAPH_NER_MODEL_FILES,
    GRAPH_NER_MODEL_NAME,
    GRAPH_NER_MODEL_REVISION,
    get_graph_models_dir,
)
from archon_search.prose_extraction_backend import load_gliner_model

_REPO = "org/some-model"
_REVISION = "0" * 40
_FILES = ("config.json", "model.safetensors")
_SENSITIVE = "https://hf.example/secret-token-path"


def _fake_downloader(cache_dir: Path, *, write: tuple[str, ...] = _FILES) -> MagicMock:
    """A ``snapshot_download`` stand-in that writes *write* into the hub-style snapshot dir and returns it."""

    def download(*, repo_id: str, revision: str, allow_patterns: list[str], cache_dir: Path) -> str:
        snapshot = cache_dir / f"models--{repo_id.replace('/', '--')}" / "snapshots" / revision
        snapshot.mkdir(parents=True)
        for name in write:
            (snapshot / name).write_text("x")
        return str(snapshot)

    return MagicMock(side_effect=download)


def test_downloads_only_the_allow_listed_files_at_the_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = _fake_downloader(tmp_path)
    monkeypatch.setattr(model_store, "snapshot_download", downloader)

    snapshot = ensure_pinned_snapshot(_REPO, _REVISION, _FILES, tmp_path)

    downloader.assert_called_once_with(
        repo_id=_REPO, revision=_REVISION, allow_patterns=list(_FILES), cache_dir=tmp_path
    )
    assert snapshot == tmp_path / "models--org--some-model" / "snapshots" / _REVISION


def test_a_complete_cached_snapshot_never_touches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloader = _fake_downloader(tmp_path)
    monkeypatch.setattr(model_store, "snapshot_download", downloader)
    ensure_pinned_snapshot(_REPO, _REVISION, _FILES, tmp_path)
    downloader.reset_mock()

    ensure_pinned_snapshot(_REPO, _REVISION, _FILES, tmp_path)

    downloader.assert_not_called()


def test_offline_miss_is_an_error_not_a_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloader = _fake_downloader(tmp_path)
    monkeypatch.setattr(model_store, "snapshot_download", downloader)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    with pytest.raises(ModelDownloadError, match="offline"):
        ensure_pinned_snapshot(_REPO, _REVISION, _FILES, tmp_path)

    downloader.assert_not_called()


def test_download_failure_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_store, "snapshot_download", MagicMock(side_effect=OSError(_SENSITIVE)))

    with pytest.raises(ModelDownloadError) as excinfo:
        ensure_pinned_snapshot(_REPO, _REVISION, _FILES, tmp_path)

    assert _SENSITIVE not in str(excinfo.value)


def test_a_snapshot_missing_an_allow_listed_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_store, "snapshot_download", _fake_downloader(tmp_path, write=_FILES[:1]))

    with pytest.raises(ModelDownloadError, match="missing required files"):
        ensure_pinned_snapshot(_REPO, _REVISION, _FILES, tmp_path)


def test_the_gliner_allow_list_is_weights_config_and_tokenizer_only() -> None:
    assert "model.safetensors" in GRAPH_NER_MODEL_FILES
    assert "gliner_config.json" in GRAPH_NER_MODEL_FILES
    assert not any(name.endswith((".bin", ".pt", ".pth", ".pkl", ".py", ".onnx")) for name in GRAPH_NER_MODEL_FILES)
    assert set(GRAPH_NER_MODEL_FILES).isdisjoint({"README.md", "trainer_state.json", ".gitattributes"})


def _install_fake_gliner(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    from_pretrained = MagicMock(return_value=object())
    gliner_mod = types.ModuleType("gliner")
    gliner_mod.GLiNER = types.SimpleNamespace(from_pretrained=from_pretrained)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gliner", gliner_mod)
    return from_pretrained


def test_load_downloads_with_the_pinned_revision_and_allow_list_then_loads_the_snapshot_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    downloader = _fake_downloader(get_graph_models_dir(), write=GRAPH_NER_MODEL_FILES)
    monkeypatch.setattr(model_store, "snapshot_download", downloader)
    from_pretrained = _install_fake_gliner(monkeypatch)

    load_gliner_model(GRAPH_NER_MODEL_NAME, GRAPH_NER_MODEL_REVISION)

    downloader.assert_called_once_with(
        repo_id=GRAPH_NER_MODEL_NAME,
        revision=GRAPH_NER_MODEL_REVISION,
        allow_patterns=list(GRAPH_NER_MODEL_FILES),
        cache_dir=get_graph_models_dir(),
    )
    (model_id,), kwargs = from_pretrained.call_args
    assert model_id == str(
        get_graph_models_dir()
        / f"models--{GRAPH_NER_MODEL_NAME.replace('/', '--')}"
        / "snapshots"
        / GRAPH_NER_MODEL_REVISION
    )
    assert kwargs["local_files_only"] is True


def test_a_download_failure_never_reaches_gliner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty data dir: a snapshot an earlier test left in the worker's shared one would make this a cache hit.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(model_store, "snapshot_download", MagicMock(side_effect=OSError("no network")))
    from_pretrained = _install_fake_gliner(monkeypatch)

    with pytest.raises(ModelDownloadError):
        load_gliner_model(GRAPH_NER_MODEL_NAME, GRAPH_NER_MODEL_REVISION)

    from_pretrained.assert_not_called()


