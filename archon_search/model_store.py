"""Local model cache: locate and download pinned model snapshots.

The cache is the Hugging Face hub layout under ``get_models_dir()``:
``models--<org>--<name>/snapshots/<revision>/``. Only the three files the
runtime reads are ever downloaded (never ``pytorch_model.bin``, ONNX or README).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from huggingface_hub import snapshot_download

from archon_search.model_specs import EmbedderSpec, RerankerSpec
from archon_search.paths import get_models_dir

logger = logging.getLogger(__name__)

#: Files the runtime needs; also the download allow-list.
MODEL_FILES: tuple[str, ...] = ("model.safetensors", "config.json", "tokenizer.json")
_OFFLINE_ENV_VAR = "HF_HUB_OFFLINE"
#: Values huggingface_hub treats as "enabled" for its boolean env vars (case-insensitive).
_ENV_TRUE_VALUES = frozenset({"1", "ON", "YES", "TRUE"})

# Fixed, wire-safe phase texts. Never interpolate a caught exception into them:
# hub errors can embed cache paths, URLs and tokens.
_OFFLINE_MISS_DETAIL = "offline mode is on and the model is not in the local cache"
_DOWNLOAD_FAILED_DETAIL = "download failed; check the network connection and free disk space"
_INCOMPLETE_DETAIL = "the downloaded snapshot is missing required files"


class ModelDownloadError(RuntimeError):
    """A model could not be made available locally.

    The message is fixed text naming the repo and the failed phase; the
    underlying cause is only ever logged.
    """

    def __init__(self, repo: str, detail: str) -> None:
        super().__init__(f"could not fetch model {repo!r}: {detail}")
        self.repo = repo


def _is_complete(snapshot: Path) -> bool:
    return all((snapshot / name).is_file() for name in MODEL_FILES)


def _offline() -> bool:
    return os.environ.get(_OFFLINE_ENV_VAR, "").upper() in _ENV_TRUE_VALUES


def model_path(spec: EmbedderSpec | RerankerSpec) -> Path | None:
    """Return the cached snapshot of *spec* at its pinned revision, or ``None``.

    Never touches the network. A snapshot counts only when every file in
    ``MODEL_FILES`` exists, so a partial download is a miss.
    """
    repo_dir = f"models--{spec.repo.replace('/', '--')}"
    snapshot = get_models_dir() / repo_dir / "snapshots" / spec.revision
    return snapshot if _is_complete(snapshot) else None


def ensure_model(spec: EmbedderSpec | RerankerSpec) -> Path:
    """Return the snapshot directory of *spec*, downloading it when absent.

    A cache hit never touches the network. ``HF_HUB_OFFLINE=1`` turns a miss
    into an error instead of a download.

    Raises:
        ModelDownloadError: on offline miss, download failure or an incomplete snapshot.
    """
    cached = model_path(spec)
    if cached is not None:
        return cached
    if _offline():
        raise ModelDownloadError(spec.repo, _OFFLINE_MISS_DETAIL)
    try:
        snapshot = Path(
            snapshot_download(
                repo_id=spec.repo,
                revision=spec.revision,
                allow_patterns=list(MODEL_FILES),
                cache_dir=get_models_dir(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — hub raises many unrelated types; all map to one error
        logger.debug("model download failed for %r: %s", spec.repo, exc, exc_info=True)
        raise ModelDownloadError(spec.repo, _DOWNLOAD_FAILED_DETAIL) from exc
    if not _is_complete(snapshot):
        raise ModelDownloadError(spec.repo, _INCOMPLETE_DETAIL)
    return snapshot
