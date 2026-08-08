"""Disk-space guard, model pre-warm downloader, and reinstall guards."""
from __future__ import annotations

import logging
import shutil
import sys
import threading
import warnings
from pathlib import Path

from archon_search.config import SearchConfig
from archon_search.paths import get_data_dir
from archon_search.platform.runtime import get_search_service
from archon_search.profiles import InstallProfile

from .config_writer import WizardFeatures, _write_profile_config
from .errors import InstallError, NeedsForceDeleteError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Disk space guard (Task C0-2.2)
# ---------------------------------------------------------------------------

def _check_disk_space(profile: InstallProfile, base_path: Path | None = None) -> None:
    """Raise InstallError if the filesystem has insufficient free space for *profile*."""
    if base_path is None:
        base_path = get_data_dir()

    check_path = base_path
    while not check_path.exists():
        check_path = check_path.parent

    usage = shutil.disk_usage(check_path)
    required_bytes = profile.download_mb * 1024 * 1024 * 2
    if usage.free < required_bytes:
        raise InstallError(
            f"Insufficient disk space. This profile requires ~{profile.download_mb * 2} MB free;"
            f" only {usage.free // 1_000_000} MB available."
        )


# ---------------------------------------------------------------------------
# Model pre-warm downloader (Task C0-2.3)
# ---------------------------------------------------------------------------

def _prewarm_timeout(profile: InstallProfile) -> int:
    estimated_bytes = profile.download_mb * 1_000_000
    return min(1800, max(300, estimated_bytes // 100_000))


def _prewarm_models(profile: InstallProfile, timeout: int | None = None) -> None:
    """Download embedder (and optionally reranker) model files to the fastembed cache.

    Uses a threading.Timer for cross-platform timeout (signal.alarm is POSIX-only).
    fastembed/HF progress is printed to stderr — not suppressed.
    """
    import fastembed  # noqa: PLC0415 — lazy; not installed at import time
    TextEmbedding = fastembed.TextEmbedding  # noqa: N806
    try:
        TextCrossEncoder = fastembed.TextCrossEncoder  # noqa: N806
    except AttributeError:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

    if timeout is None:
        timeout = _prewarm_timeout(profile)

    cancelled = threading.Event()
    timer = threading.Timer(timeout, cancelled.set)
    timer.start()

    try:
        try:
            # Some multilingual embedders (e.g. intfloat/multilingual-e5-large)
            # emit a UserWarning about switching to mean pooling on __init__.
            # Mean pooling is the correct behavior for these models; the warning
            # is not actionable, so suppress it narrowly at this prewarm site.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", category=UserWarning, message=".*mean pooling.*"
                )
                TextEmbedding(profile.embedder, lazy_load=True)
        except Exception as exc:
            raise InstallError(f"Failed to pre-warm embedder model {profile.embedder!r}: {exc}") from exc

        if cancelled.is_set():
            timer.cancel()
            logger.warning(
                "Model pre-warm timed out after %ss. Service will start without cached model files,"
                " same as default behavior.",
                timeout,
            )
            return

        if profile.reranker is not None:
            try:
                TextCrossEncoder(profile.reranker, lazy_load=True)
            except Exception as exc:
                # Non-fatal: a CoreML ONNX error or transient download hiccup must
                # not abort the wizard.  The reranker will download on first search.
                logger.warning(
                    "Failed to pre-warm reranker model %r: %s — "
                    "the model will be downloaded on first search.",
                    profile.reranker, exc,
                )

        timer.cancel()
    except InstallError:
        timer.cancel()
        raise


# ---------------------------------------------------------------------------
# Reinstall guard (Task C0-2.4)
# ---------------------------------------------------------------------------

def _check_reinstall_guard(
    existing_cfg: SearchConfig,
    new_profile: InstallProfile,
    new_profile_name: str,
    new_multilingual: bool,
) -> None:
    """Raise NeedsForceDeleteError when switching model or chunk_size would invalidate the index.

    A reranker-only change is safe and does NOT trigger the guard.
    The new_profile_name and new_multilingual parameters are accepted for future extensibility.
    """
    if existing_cfg.embedding_model == new_profile.embedder and existing_cfg.chunk_size == new_profile.chunk_size:
        return
    raise NeedsForceDeleteError(
        f"Existing index uses {existing_cfg.embedding_model} (chunk_size={existing_cfg.chunk_size}). "
        f"Switching to {new_profile.embedder} (chunk_size={new_profile.chunk_size}) requires re-indexing all documents. "
        "Run with --force --delete-db to proceed."
    )


def _execute_force_reinstall(
    config_path: Path,
    db_path: Path,
    profile: InstallProfile,
    profile_name: str,
    multilingual: bool,
    non_interactive: bool,
    dry_run: bool = False,
    features: WizardFeatures | None = None,
) -> None:
    """Execute the force-delete-db rollback sequence."""
    # Step 1: Backup config
    bak_path = config_path.with_suffix(".toml.bak")
    has_backup = False
    if config_path.exists():
        if dry_run:
            print(f"[dry-run] Would create backup at {bak_path}.")
        else:
            shutil.copy2(config_path, bak_path)
            has_backup = True

    # Step 2: Confirmation gate
    if not non_interactive:
        try:
            response = input("WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: ")
        except EOFError:
            response = ""
        if response != "yes":
            if has_backup:
                shutil.copy2(bak_path, config_path)
            print("Aborted.")
            raise SystemExit(1)

    # Step 3: Stop service
    if dry_run:
        print("[dry-run] Would stop service.")
    else:
        try:
            get_search_service().stop()
        except RuntimeError:
            pass  # treat service-not-running as no-op
        except Exception:
            if has_backup:
                shutil.copy2(bak_path, config_path)
            raise

    # Step 4: Delete DB directory
    if dry_run:
        print(f"[dry-run] Would delete database at {db_path}.")
    else:
        if db_path.exists():
            try:
                shutil.rmtree(db_path)
            except Exception:
                print(
                    f"Install failed during database deletion. Your previous config has been preserved at "
                    f"{bak_path} for reference. "
                    "Run archon-search install to create a fresh install from scratch.",
                    file=sys.stderr,
                )
                raise SystemExit(1)

    # Step 5: Write new profile config
    if dry_run:
        print(f"[dry-run] Would write profile config for {profile_name}.")
    else:
        tmp = config_path.with_suffix(".toml.tmp")
        tmp.unlink(missing_ok=True)
        try:
            _write_profile_config(config_path, profile, profile_name, multilingual, features=features)
        except Exception:
            print(
                f"Install failed after database deletion. Your previous config has been preserved at "
                f"{bak_path} for reference. "
                "Run archon-search install to create a fresh install from scratch.",
                file=sys.stderr,
            )
            raise SystemExit(1)
