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

from archon_search._durable_io import ensure_private_dir, restrict_tree_to_owner
from archon_search.log_redaction import redact_secrets
from archon_search.model_specs import (
    CONFIG_FILE,
    TOKENIZER_FILE,
    WEIGHTS_FILE,
    EmbedderSpec,
    RerankerSpec,
)
from archon_search.paths import get_models_dir

logger = logging.getLogger(__name__)

#: Files the runtime needs; also the download allow-list.
MODEL_FILES: tuple[str, ...] = (WEIGHTS_FILE, CONFIG_FILE, TOKENIZER_FILE)
_OFFLINE_ENV_VAR = "HF_HUB_OFFLINE"
#: Values huggingface_hub treats as "enabled" for its boolean env vars (case-insensitive).
_ENV_TRUE_VALUES = frozenset({"1", "ON", "YES", "TRUE"})
#: ``huggingface_hub`` cache layout: ``<cache>/models--<org>--<name>/snapshots/<revision>``.
_HUB_REPO_DIR_PREFIX = "models--"
_HUB_REPO_SEPARATOR = "--"
_HUB_SNAPSHOTS_DIR = "snapshots"

# Fixed, wire-safe phase texts. Never interpolate a caught exception into them:
# hub errors can embed cache paths, URLs and tokens.
_OFFLINE_MISS_DETAIL = "offline mode is on and the model is not in the local cache"
_DOWNLOAD_FAILED_DETAIL = "download failed; check the network connection and free disk space"
_INCOMPLETE_DETAIL = "the downloaded snapshot is missing required files"
#: Longest exception text a download-failure log line carries.
_LOG_MESSAGE_MAX_CHARS = 500
#: Mode bits that give the group or others any access.
_GROUP_OTHER_BITS = 0o077


class ModelDownloadError(RuntimeError):
    """A model could not be made available locally.

    The message is fixed text naming the repo and the failed phase; the
    underlying cause is only ever logged.
    """

    def __init__(self, repo: str, detail: str) -> None:
        super().__init__(f"could not fetch model {repo!r}: {detail}")
        self.repo = repo


def _has_files(snapshot: Path, files: tuple[str, ...]) -> bool:
    return all((snapshot / name).is_file() for name in files)


def _is_complete(snapshot: Path) -> bool:
    return _has_files(snapshot, MODEL_FILES)


def _is_offline() -> bool:
    return os.environ.get(_OFFLINE_ENV_VAR, "").upper() in _ENV_TRUE_VALUES


def _scrub(text: str) -> str:
    """Drop credentials from a hub exception message and cap its length for a one-line log message."""
    text = redact_secrets(text)
    return text if len(text) <= _LOG_MESSAGE_MAX_CHARS else text[:_LOG_MESSAGE_MAX_CHARS] + "..."


def _log_download_failure(repo: str, exc: Exception) -> None:
    """Log why a download failed: WARNING with the exception class and a scrubbed, length-capped message.

    The wire text (:class:`ModelDownloadError`) stays fixed. The raw cause and traceback go to DEBUG;
    credentials in them are redacted by the ``archon_search`` log handlers (``logging_setup``), not here.
    """
    logger.warning("model download failed for %r: %s: %s", repo, type(exc).__name__, _scrub(str(exc)))
    logger.debug("model download failed for %r: %s", repo, exc, exc_info=exc)


def _snapshot_dir(cache_dir: Path, repo: str, revision: str) -> Path:
    """Where the hub cache layout puts *repo* at *revision* under *cache_dir*."""
    repo_dir = f"{_HUB_REPO_DIR_PREFIX}{repo.replace('/', _HUB_REPO_SEPARATOR)}"
    return cache_dir / repo_dir / _HUB_SNAPSHOTS_DIR / revision


def _is_loose(cache_dir: Path) -> bool:
    """True when *cache_dir* gives group or others any access (never on Windows, which has no such mode bits)."""
    if not hasattr(os, "geteuid"):
        return False
    try:
        return bool(cache_dir.stat().st_mode & _GROUP_OTHER_BITS)
    except OSError:
        return False


def _restrict_cache(cache_dir: Path) -> None:
    """Make *cache_dir*, its ancestors inside the data dir and everything under it owner-only.

    Best effort: a failure (read-only cache, chmod refused) is logged, never raised, so it can neither fail a
    cache hit nor replace the error of a failed download that this runs after. Entries owned by another uid are
    skipped without a message; each step runs even when the other failed.
    """
    for tighten in (ensure_private_dir, restrict_tree_to_owner):
        try:
            tighten(cache_dir)
        except OSError as exc:
            logger.warning("could not restrict the model cache %s to its owner: %s", cache_dir, exc)


def _ensure_snapshot(repo: str, revision: str, files: tuple[str, ...], cache_dir: Path) -> Path:
    """Return the snapshot of *repo* at *revision* holding every one of *files*, downloading just those when absent.

    A cache hit never touches the network; ``HF_HUB_OFFLINE=1`` turns a miss into an error.

    Raises:
        ModelDownloadError: on offline miss, download failure or an incomplete snapshot.
    """
    snapshot = _snapshot_dir(cache_dir, repo, revision)
    if _has_files(snapshot, files):
        if _is_loose(cache_dir):  # a cache an older version filled under the umask
            _restrict_cache(cache_dir)
        return snapshot
    if _is_offline():
        raise ModelDownloadError(repo, _OFFLINE_MISS_DETAIL)
    try:
        ensure_private_dir(cache_dir)
        snapshot = Path(
            snapshot_download(repo_id=repo, revision=revision, allow_patterns=list(files), cache_dir=cache_dir)
        )
    except Exception as exc:  # hub raises many unrelated types; all map to one error
        _log_download_failure(repo, exc)
        raise ModelDownloadError(repo, _DOWNLOAD_FAILED_DETAIL) from exc
    finally:
        _restrict_cache(cache_dir)  # the hub writes under the umask, also on a partial download
    if not _has_files(snapshot, files):
        raise ModelDownloadError(repo, _INCOMPLETE_DETAIL)
    return snapshot


def model_path(spec: EmbedderSpec | RerankerSpec) -> Path | None:
    """Return the cached snapshot of *spec* at its pinned revision, or ``None``.

    Never touches the network. A snapshot counts only when every file in
    ``MODEL_FILES`` exists, so a partial download is a miss.
    """
    snapshot = _snapshot_dir(get_models_dir(), spec.repo, spec.revision)
    return snapshot if _is_complete(snapshot) else None


def ensure_model(spec: EmbedderSpec | RerankerSpec) -> Path:
    """Return the snapshot directory of *spec*, downloading it when absent.

    A cache hit never touches the network. ``HF_HUB_OFFLINE=1`` turns a miss
    into an error instead of a download.

    Raises:
        ModelDownloadError: on offline miss, download failure or an incomplete snapshot.
    """
    return _ensure_snapshot(spec.repo, spec.revision, MODEL_FILES, get_models_dir())


def ensure_pinned_snapshot(repo: str, revision: str, files: tuple[str, ...], cache_dir: Path) -> Path:
    """Return the snapshot directory of *repo* at the pinned *revision*, downloading just *files* when absent.

    For a model whose own library would otherwise fetch the whole repo (and anything else it chooses): the caller
    loads from the returned local directory only. A cache hit — every file of *files* present under
    ``cache_dir`` — never touches the network; ``HF_HUB_OFFLINE=1`` turns a miss into an error.

    Raises:
        ModelDownloadError: on offline miss, download failure or an incomplete snapshot.
    """
    return _ensure_snapshot(repo, revision, files, cache_dir)
