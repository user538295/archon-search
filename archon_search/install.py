"""SearchInstaller — install, configure, and manage the search service (Task 7.1)."""
from __future__ import annotations

import contextlib
import importlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING

import click
import tomlkit

from archon_search._durable_io import atomic_write_bytes
from archon_search.config import SearchConfig, get_default_config_path, load_config
from archon_search.key_manager import get_key_file, load_or_generate_key
from archon_search.pipeline import create_pipeline
from archon_search.platform.runtime import get_runtime, get_search_service
from archon_search.platform.types import GpuType
from archon_search.profiles import JINA_RERANKER_MODEL, InstallProfile, VALID_PROFILE_NAMES, get_profile

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Advisory install lock (Task C0-2.1)
# ---------------------------------------------------------------------------

class InstallLockError(Exception):
    """Raised when the install lock is already held by another process."""


def _install_lock_path() -> Path:
    return Path.home() / ".archon-search" / ".install.lock"


def _pid_is_alive(pid: int) -> bool:
    """Return True if *pid* refers to a running process, False if dead."""
    if sys.platform == "win32":
        try:
            import psutil  # type: ignore[import-untyped]
            return psutil.pid_exists(pid)
        except ImportError:
            # psutil unavailable on Windows — treat as stale (conservative proceed)
            return False
    else:
        try:
            os.kill(pid, 0)
            return True  # no exception → process alive
        except ProcessLookupError:
            return False  # ESRCH → dead
        except PermissionError:
            return True  # EPERM → alive, different user


@contextlib.contextmanager
def _acquire_install_lock() -> Iterator[None]:
    """Context manager that holds an advisory file-based install lock."""
    lock_path = _install_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _try_create() -> int:
        """Atomically create the lock file; return the fd or raise FileExistsError."""
        return os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # noqa: durable-write — PID lock file; O_EXCL is the atomic guard, not a data write

    def _claim_lock() -> None:
        """Write our PID:timestamp into the fd obtained from _try_create."""
        fd = _try_create()
        try:
            os.write(fd, f"{os.getpid()}:{int(time.time())}".encode())
        except OSError:
            os.close(fd)
            lock_path.unlink(missing_ok=True)
            raise
        os.close(fd)

    retry_done = False
    while True:
        try:
            _claim_lock()
            break  # lock acquired
        except FileExistsError:
            # Lock file already exists — inspect it
            try:
                contents = lock_path.read_text()
                parts = contents.split(":")
                pid = int(parts[0])
            except (ValueError, IndexError, OSError):
                # Corrupted / unreadable → treat as stale
                lock_path.unlink(missing_ok=True)
                if retry_done:
                    raise InstallLockError("Could not acquire install lock (corrupted lock)") from None
                retry_done = True
                continue

            if _pid_is_alive(pid):
                raise InstallLockError(
                    f"Install is already running (PID {pid}). "
                    "Wait for it to finish or remove ~/.archon-search/.install.lock if stale."
                )

            # PID is dead → stale lock
            lock_path.unlink(missing_ok=True)
            if retry_done:
                raise InstallLockError("Could not acquire install lock after removing stale lock") from None
            retry_done = True
            # loop again to retry O_EXCL

    try:
        yield
    finally:
        try:
            contents = lock_path.read_text()
            if contents.split(":")[0] == str(os.getpid()):
                lock_path.unlink(missing_ok=True)
        except OSError:
            lock_path.unlink(missing_ok=True)


_SEARCH_PACKAGES = ["lancedb", "fastembed", "docling", "markitdown", "trafilatura", "chonkie", "fastmcp"]
_WAIT_FOR_SERVICE_TIMEOUT = 60


# ---------------------------------------------------------------------------
# WizardFeatures dataclass (Task C8-1.1)
# ---------------------------------------------------------------------------

@dataclass
class WizardFeatures:
    """Carries optional-feature choices from wizard prompt functions to config writer."""

    install_code_extra: bool = False
    disable_reranker: bool = False
    enable_watch: bool = False
    enable_telemetry: bool = False
    eager_load_embedders: bool = False
    routing_strategy: str = "centroid"
    log_format: str = "text"
    # C15 Tier 1 deployment flags
    host: str | None = None
    port: int | None = None
    db_path: str | None = None
    log_level: str | None = None
    log_to_stderr: bool = False
    top_k: int | None = None
    telemetry_retention_days: int | None = None
    # C15 Tier 2 AI query expansion flags
    enable_hyde: bool = False
    enable_rag_fusion: bool = False


def _write_profile_config(
    config_path: Path,
    profile: InstallProfile,
    profile_name: str,
    multilingual: bool,
    features: WizardFeatures | None = None,
) -> None:
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()

    if "database" not in doc:
        doc.add("database", tomlkit.table())

    db = doc["database"]
    db["embedding_model"] = profile.embedder
    db["reranker_model"] = profile.reranker if profile.reranker is not None else ""
    db["chunk_size"] = profile.chunk_size
    db["profile"] = profile_name
    db["multilingual"] = multilingual

    if features is not None:
        _apply_wizard_features_to_toml(doc, features)

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())


def _profile_toml(profile_name: str, multilingual: bool, features: WizardFeatures | None = None) -> str:
    # Delegates to _default_toml() for a complete all-sections base, then overlays
    # profile-specific [database] values via _write_profile_config logic in-memory.
    # This ensures every section generated by _default_toml() is present without
    # duplicating section-building logic here.
    from archon_search.cli.config_cmd import _default_toml  # noqa: PLC0415

    doc = tomlkit.parse(_default_toml())
    profile = get_profile(profile_name, multilingual)

    db = doc["database"]
    db["embedding_model"] = profile.embedder
    db["reranker_model"] = profile.reranker if profile.reranker is not None else ""
    db["chunk_size"] = profile.chunk_size
    db["profile"] = profile_name
    db["multilingual"] = multilingual

    if features is not None:
        _apply_wizard_features_to_toml(doc, features)

    return tomlkit.dumps(doc)


def _apply_wizard_features_to_toml(doc: tomlkit.TOMLDocument, features: WizardFeatures) -> None:
    """Write non-default WizardFeatures fields to *doc* in-place.

    Only fields that differ from WizardFeatures defaults are written, to avoid
    TOML clutter for basic installs. Missing sections are created via tomlkit.table().
    ``install_code_extra`` is intentionally NOT written — it controls a subprocess
    install, not a config key.
    """

    def _ensure_section(name: str) -> None:
        if name not in doc:
            doc.add(name, tomlkit.table())

    if features.disable_reranker:
        _ensure_section("database")
        doc["database"]["reranker_model"] = ""

    if features.eager_load_embedders:
        _ensure_section("database")
        doc["database"]["eager_load_embedders"] = True

    if features.enable_watch:
        _ensure_section("collections")
        doc["collections"]["watch"] = True

    if features.enable_telemetry:
        _ensure_section("telemetry")
        doc["telemetry"]["enabled"] = True

    if features.routing_strategy != "centroid":
        _ensure_section("routing")
        doc["routing"]["routing_strategy"] = features.routing_strategy

    if features.log_format != "text":
        _ensure_section("logging")
        doc["logging"]["format"] = features.log_format

    # C15 Tier 1 deployment flags
    if features.host is not None:
        _ensure_section("server")
        doc["server"]["host"] = features.host

    if features.port is not None:
        _ensure_section("server")
        doc["server"]["port"] = features.port

    if features.db_path is not None:
        _ensure_section("database")
        doc["database"]["db_path"] = features.db_path

    if features.log_level is not None:
        _ensure_section("logging")
        doc["logging"]["level"] = features.log_level

    if features.log_to_stderr:
        _ensure_section("logging")
        doc["logging"]["log_file"] = ""

    if features.top_k is not None:
        _ensure_section("database")
        doc["database"]["top_k_return"] = features.top_k
        doc["database"]["top_k_retrieve"] = max(15, 3 * features.top_k)

    if features.telemetry_retention_days is not None and features.enable_telemetry:
        _ensure_section("telemetry")
        doc["telemetry"]["retention_days"] = features.telemetry_retention_days

    # C15 Tier 2 AI query expansion flags
    if features.enable_hyde:
        _ensure_section("hyde")
        doc["hyde"]["enabled"] = True

    if features.enable_rag_fusion:
        _ensure_section("rag_fusion")
        doc["rag_fusion"]["enabled"] = True


# ---------------------------------------------------------------------------
# Hand-edit detection (Task C14-5.4)
# ---------------------------------------------------------------------------

def _detect_config_hand_edits(
    config_path: Path,
    prev_profile_name: str,
    prev_multilingual: bool,
) -> bool:
    """Return True if the on-disk config has values that differ from wizard defaults.

    Compares only wizard-written keys (union of _write_profile_config and
    _apply_wizard_features_to_toml output). Returns True (always warn) if
    prev_profile_name is not a recognized profile name.
    """
    # Unknown profile — always warn
    try:
        profile = get_profile(prev_profile_name, prev_multilingual)
    except ValueError:
        return True

    # Read the on-disk config
    doc = tomlkit.parse(config_path.read_text())
    db = doc.get("database", {})

    # Compare [database] wizard-written keys against profile defaults
    expected_reranker = profile.reranker if profile.reranker is not None else ""
    if db.get("embedding_model") != profile.embedder:
        return True
    if db.get("reranker_model") != expected_reranker:
        return True
    if db.get("chunk_size") != profile.chunk_size:
        return True
    if db.get("profile") != prev_profile_name:
        return True
    if db.get("multilingual") != prev_multilingual:
        return True

    # Compare optional-feature keys against WizardFeatures() static defaults
    # Absent key = static default in effect — NOT a hand-edit.
    # Only a key that is PRESENT and DIFFERENT from the static default counts.
    defaults = WizardFeatures()

    # eager_load_embedders (in [database])
    if "eager_load_embedders" in db and db["eager_load_embedders"] != defaults.eager_load_embedders:
        return True

    # collections.watch
    collections = doc.get("collections", {})
    if "watch" in collections and collections["watch"] != defaults.enable_watch:
        return True

    # telemetry.enabled
    telemetry = doc.get("telemetry", {})
    if "enabled" in telemetry and telemetry["enabled"] != defaults.enable_telemetry:
        return True

    # routing.routing_strategy
    routing = doc.get("routing", {})
    if "routing_strategy" in routing and routing["routing_strategy"] != defaults.routing_strategy:
        return True

    # logging.format
    logging_section = doc.get("logging", {})
    if "format" in logging_section and logging_section["format"] != defaults.log_format:
        return True

    return False


# ---------------------------------------------------------------------------
# Disk space guard (Task C0-2.2)
# ---------------------------------------------------------------------------

class InstallError(Exception):
    """Raised to abort an install due to a pre-flight check failure."""


def _check_disk_space(profile: InstallProfile, base_path: Path | None = None) -> None:
    """Raise InstallError if the filesystem has insufficient free space for *profile*."""
    if base_path is None:
        base_path = Path.home() / ".archon-search"

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
                raise InstallError(
                    f"Failed to pre-warm reranker model {profile.reranker!r}: {exc}"
                ) from exc

        timer.cancel()
    except InstallError:
        timer.cancel()
        raise


# ---------------------------------------------------------------------------
# Reinstall guard (Task C0-2.4)
# ---------------------------------------------------------------------------

class NeedsForceDeleteError(InstallError):
    """Raised when a model or chunk_size conflict requires --force --delete-db to resolve."""


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
        response = input("WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: ")
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


_PROFILE_ORDER = ("minimal", "balanced", "max")
_PROFILE_CAPS = {"minimal": "Minimal", "balanced": "Balanced", "max": "Max"}


def _render_profile_table(multilingual: bool, width: int = 80) -> str:
    """Return a formatted profile comparison table string. Does NOT print."""
    profiles = [get_profile(name, multilingual) for name in _PROFILE_ORDER]
    lines: list[str] = []

    if width >= 80:
        # Header
        lines.append("  Profile      Download    Quality       Speed (CPU / Apple Silicon)")
        lines.append("  ─────────    ────────    ───────       ───────────────────────────")
        # Data rows
        for n, (name, p) in enumerate(zip(_PROFILE_ORDER, profiles), start=1):
            if p.download_mb >= 1000:
                size_str = f"~{p.download_mb / 1000:.1f} GB"
            else:
                size_str = f"~{p.download_mb} MB"
            cap = _PROFILE_CAPS[name]
            recommended = "  ← Recommended" if name == "balanced" else ""
            lines.append(
                f"  {n}) {cap:<9}  {size_str:<10}  {p.quality_stars:<12}  "
                f"~{p.cpu_ms} ms/query  / ~{p.metal_ms} ms{recommended}"
            )
        lines.append("")
        # Models section
        lines.append("  Models (all sizes from fastembed registry, verified):")
        for n, (name, p) in enumerate(zip(_PROFILE_ORDER, profiles), start=1):
            if p.reranker is None:
                lines.append(f"  {n}) {p.embedder}  (no reranker)")
            else:
                lines.append(f"  {n}) {p.embedder} + {p.reranker}")
        lines.append("")
        # Best-for section
        lines.append("  Best for:")
        lines.append("  1) Personal use, <10k docs, fast responses, low RAM")
        lines.append("  2) Team use, 10k–200k docs, good recall, ~1 GB RAM")
        lines.append("  3) Large corpora, 200k+ docs, highest precision, ~2.5 GB RAM")
    else:
        # Narrow: one line per profile
        for n, (name, p) in enumerate(zip(_PROFILE_ORDER, profiles), start=1):
            if p.download_mb >= 1000:
                size_str = f"~{p.download_mb / 1000:.1f} GB"
            else:
                size_str = f"~{p.download_mb} MB"
            cap = _PROFILE_CAPS[name]
            star = "*" if name == "balanced" else ""
            if p.reranker is not None:
                lines.append(f"  {n}) {cap}{star}: {p.embedder} + {p.reranker} ({size_str})")
            else:
                lines.append(f"  {n}) {cap}{star}: {p.embedder} (no reranker) ({size_str})")
        lines.append("  * Recommended for most users")

    lines.append("")
    if not multilingual:
        lines.append("  Add --multilingual to use multilingual models instead.")
    else:
        lines.append("  (Showing multilingual models)")

    return "\n".join(lines)


def _render_summary(
    profile_name: str,
    profile: InstallProfile,
    multilingual: bool,
    providers: list[str],
    features: WizardFeatures | None = None,
    *,
    db_path: str = "",
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key_file: str = "",
    download_mb: int = 0,
) -> str:
    """Return an install summary block string. Does NOT print."""
    cap = _PROFILE_CAPS.get(profile_name, profile_name.capitalize())
    lang = "Multilingual" if multilingual else "English"
    reranker_str = profile.reranker if profile.reranker is not None else "(none)"
    providers_str = ", ".join(providers) if providers else "(CPU default)"
    lines = [
        f"  Installing: {cap} · {lang}",
        f"  Embedder:   {profile.embedder}",
        f"  Reranker:   {reranker_str}",
        f"  Chunk size: {profile.chunk_size} tokens",
        f"  Providers:  {providers_str}",
    ]
    if db_path:
        lines.append(f"  Database:   {db_path}")
    lines.append(f"  Server:     http://{host}:{port}")
    # API key display: mask first-8...last-4; show path for reference
    api_key_display = _mask_api_key(api_key_file)
    lines.append(f"  API key:    {api_key_display}  (full key: {api_key_file or _KEY_FILE_PLACEHOLDER})")
    if download_mb:
        lines.append(f"  Download:   ~{download_mb} MB")
    lines += [
        "",
        "  Note: Model files are downloaded now. ONNX session initialization happens in the",
        "  server process on first query — expect ~5–15s latency on first search.",
    ]
    if features is not None:
        feature_bullets: list[str] = []
        if features.install_code_extra:
            feature_bullets.append("• Code enrichment (tree-sitter)")
        if features.disable_reranker:
            feature_bullets.append("• Reranker disabled")
        if features.enable_watch:
            feature_bullets.append("• Watch directories (auto-reindex)")
        if features.enable_telemetry:
            feature_bullets.append("• Telemetry enabled")
        if features.eager_load_embedders:
            feature_bullets.append("• Eager load embedders at startup")
        if features.routing_strategy != "centroid":
            feature_bullets.append(f"• Routing: {features.routing_strategy}")
        if features.log_format != "text":
            feature_bullets.append(f"• Log format: {features.log_format}")
        if feature_bullets:
            lines.append("")
            lines.append("  Optional features:")
            lines.extend(f"    {b}" for b in feature_bullets)
    return "\n".join(lines)


# SYNC: must match the default returned by `key_manager.get_key_file()`
# (i.e. `get_data_dir() / ".search.env"` when neither ARCHON_SEARCH_KEY_FILE
# nor ARCHON_SEARCH_DATA_DIR is set). Display-only fallback for the
# pre-install summary when `api_key_file` is not yet known; never used as
# a real path. If the data-dir default ever shifts (XDG compliance etc.),
# update this string alongside `archon_search.paths.get_data_dir()`.
_KEY_FILE_PLACEHOLDER = "~/.archon-search/.search.env"


def _mask_api_key(api_key_file: str) -> str:
    """Read the API key from file and return a masked representation.

    Returns 'first-8...last-4' if the key is long enough, or
    '(not yet generated)' if the file doesn't exist or key can't be read.
    """
    if not api_key_file:
        return "(not yet generated)"
    key_path = Path(api_key_file)
    try:
        content = key_path.read_text()
    except OSError:
        return "(not yet generated)"
    for line in content.splitlines():
        if line.startswith("ARCHON_SEARCH_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if len(key) >= 12:
                return f"{key[:8]}…{key[-4:]}"
            return "(not yet generated)"
    return "(not yet generated)"


def _print_next_steps(host: str, port: int, api_key_file: str) -> None:
    """Print post-install guidance to stdout."""
    from archon_search import key_manager
    key_path = Path(api_key_file) if api_key_file else key_manager.get_key_file()
    print(f"\narchon-search is running on http://{host}:{port}\n")
    print("Next steps:")
    print("  archon-search ingest <path>           # add documents to search")
    print("  archon-search status                  # check service health")
    print("  archon-search sync                    # sync watched directories")
    print("  archon-search stop                    # stop the service")
    print("  archon-search wizard --top-k 20       # increase results per query (default: 5)")
    print(f"\nAPI key: (full key: {key_path})")
    print(f"Config:  {get_default_config_path()}")


# ---------------------------------------------------------------------------
# Jina license gate (Task C0-3.2)
# ---------------------------------------------------------------------------

def _requires_jina_license(profile: InstallProfile) -> bool:
    """Return True if *profile* uses the Jina reranker model (CC-BY-NC-4.0)."""
    return profile.reranker == JINA_RERANKER_MODEL


def _prompt_jina_license(non_interactive: bool, accept_jina_license: bool = False) -> None:
    """Print the Jina CC-BY-NC-4.0 warning and gate on user / flag acceptance.

    Raises SystemExit(1) if the license is not accepted.
    """
    print(
        "WARNING: jinaai/jina-reranker-v2-base-multilingual is licensed CC-BY-NC-4.0\n"
        "(non-commercial use only). Commercial use of multilingual profiles 2 and 3\n"
        "requires an alternative reranker. You will be required to confirm license\n"
        "acceptance before this model is downloaded."
    )

    if accept_jina_license:
        return

    if non_interactive:
        print(
            "Non-interactive mode: Jina license automatically declined. "
            "Use an English profile for commercial installs."
        )
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fasttext license gate (Task 4.1)
# ---------------------------------------------------------------------------


def _prompt_fasttext_license(non_interactive: bool, accept_fasttext_license: bool = False) -> None:
    """Print the fasttext CC-BY-SA 3.0 warning and gate on user / flag acceptance.

    Raises SystemExit(1) if the license is not accepted.
    Pattern mirrors _prompt_jina_license exactly.
    """
    print(
        "WARNING: lid.176.ftz (fasttext language identification model) is licensed CC-BY-SA 3.0.\n"
        "This model was created by Facebook Research and redistributed under CC-BY-SA 3.0.\n"
        "You must comply with its terms for any use."
    )

    if accept_fasttext_license:
        return

    if non_interactive:
        print("Non-interactive mode: fasttext license automatically declined.")
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fasttext model download (Task 4.2)
# ---------------------------------------------------------------------------

FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"


def _download_fasttext_model(models_dir: Path) -> None:
    """Download the fasttext language identification model to *models_dir*.

    - Creates *models_dir* (mode 0o700) if absent.
    - No-op if ``lid.176.ftz`` already exists in *models_dir*.
    - Uses ``urllib.request.urlopen`` with an explicit 120-second socket timeout
      instead of ``urlretrieve`` (which has no timeout).
    - Raises ``InstallError`` on network failure or if the downloaded file is empty/corrupt.
    """
    target = models_dir / "lid.176.ftz"

    if target.exists():
        logger.debug("fasttext model already present at %s — skipping download", target)
        return

    models_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    print("[4b/5] Downloading fasttext language model...")

    try:
        with urllib.request.urlopen(FASTTEXT_MODEL_URL, timeout=120) as response:
            with target.open("wb") as out_file:
                shutil.copyfileobj(response, out_file)
    except urllib.error.URLError as exc:
        target.unlink(missing_ok=True)
        raise InstallError(
            f"Failed to download fasttext lid.176.ftz model: {exc}. "
            "Check your network connection and re-run install."
        ) from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise InstallError(
            f"Failed to write fasttext lid.176.ftz model to disk: {exc}. "
            "Check available disk space and permissions."
        ) from exc

    # Validate download
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise InstallError(
            "fasttext model download appears corrupt (empty file); re-run install."
        )


_CHOICE_MAP = {"1": "minimal", "2": "balanced", "3": "max", "": "minimal"}


def _select_profile(
    profile_flag: str | None,
    multilingual_flag: bool,
    non_interactive: bool,
) -> tuple[str, bool]:
    """Select and validate an install profile.

    Returns a (profile_name, multilingual) tuple.
    """
    # Explicit profile flag: validate and return immediately.
    if profile_flag is not None:
        if profile_flag not in VALID_PROFILE_NAMES:
            raise click.BadParameter(
                f"{profile_flag!r} is not a valid profile. "
                f"Valid options: {sorted(VALID_PROFILE_NAMES)}",
                param_hint="--profile",
            )
        return (profile_flag, multilingual_flag)

    # Non-interactive: use defaults.
    if non_interactive:
        if not multilingual_flag:
            logger.info("No profile specified — defaulting to 'minimal'.")
            logger.info("No --multilingual flag — defaulting to English models.")
        else:
            logger.info("No profile specified — defaulting to 'minimal'.")
        return ("minimal", multilingual_flag)

    # Interactive: show table and prompt.
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    table = _render_profile_table(multilingual=multilingual_flag, width=terminal_width)
    if table:
        print(table)

    for attempt in range(3):
        try:
            raw = input("Choice [1-3, default 1]: ").strip()
        except EOFError:
            print("No input received (EOF). Aborting.")
            raise SystemExit(1)

        if raw in _CHOICE_MAP:
            return (_CHOICE_MAP[raw], multilingual_flag)

        remaining = 2 - attempt
        if remaining > 0:
            print(f"Invalid choice {raw!r}. Please enter 1, 2, or 3.")
        else:
            print("Too many invalid attempts. Aborting.")
            raise SystemExit(1)

    raise SystemExit(1)  # unreachable, satisfies type checker


def _prompt_optional_features(
    non_interactive: bool,
    profile: InstallProfile,
    *,
    install_code: bool | None = None,
    disable_reranker: bool | None = None,
    enable_watch: bool | None = None,
    enable_telemetry: bool | None = None,
    eager_load: bool | None = None,
    routing_strategy: str | None = None,
    log_format: str | None = None,
    log_to_stderr: bool | None = None,
    enable_hyde: bool | None = None,
    enable_rag_fusion: bool | None = None,
) -> WizardFeatures:
    """Ask seven optional-feature questions after profile selection.

    Each keyword argument pre-answers its question when not None; None triggers
    an interactive prompt (or the default in non-interactive mode).
    """

    def _ask_yn(prompt_text: str, default: bool = False) -> bool:
        """Ask a yes/no question; return ``default`` on EOFError or empty input."""
        try:
            raw = input(prompt_text).strip().lower()
        except EOFError:
            return default
        return raw in {"y", "yes"}

    def _ask_choice(prompt_text: str, valid: set[str], default: str) -> str:
        """Ask a choice question with one retry; fall back to default on second invalid."""
        for attempt in range(2):
            try:
                raw = input(prompt_text).strip().lower()
            except EOFError:
                return default
            if not raw:
                return default
            if raw in valid:
                return raw
            if attempt == 0:
                print(f"Invalid value {raw!r}. Valid options: {sorted(valid)}")
        return default

    # --- install_code_extra ---
    print(
        "\nCode enrichment (tree-sitter):\n"
        "  Parses and indexes code files structurally — functions, classes, docstrings.\n"
        "  Installs tree-sitter and language parsers (~50 MB). Recommended if your corpus\n"
        "  includes source code. Default: disabled."
    )
    if install_code is not None:
        _install_code_extra_val = install_code
    elif non_interactive:
        _install_code_extra_val = False
    else:
        _install_code_extra_val = _ask_yn("Index code files (installs tree-sitter enrichment)? [y/N]: ")

    # --- disable_reranker (skipped when profile has no reranker) ---
    if profile.reranker is not None:
        print(
            "\nReranker:\n"
            "  A second-stage cross-encoder model that re-scores results for better precision.\n"
            "  Disabling it reduces latency and RAM but lowers recall quality.\n"
            "  Default: enabled (for profiles that include a reranker)."
        )
    if profile.reranker is None:
        _disable_reranker_val = False
    elif disable_reranker is not None:
        _disable_reranker_val = disable_reranker
    elif non_interactive:
        _disable_reranker_val = False
    else:
        _disable_reranker_val = _ask_yn("Disable reranker for lower latency? [y/N]: ")

    # --- enable_watch ---
    print(
        "\nFilesystem watcher:\n"
        "  Monitors watched directories and automatically re-indexes files when they change.\n"
        "  Uses watchdog. Increases background CPU usage slightly.\n"
        "  Default: disabled."
    )
    if enable_watch is not None:
        _enable_watch_val = enable_watch
    elif non_interactive:
        _enable_watch_val = False
    else:
        _enable_watch_val = _ask_yn("Auto-watch directories and re-index on file changes? [y/N]: ")

    # --- enable_telemetry ---
    print(
        "\nLocal telemetry:\n"
        "  Logs per-query metadata (collection, result count, latency) to\n"
        "  ~/.archon-search/search-logs/. No query text is stored. Opt-in.\n"
        "  Default: disabled."
    )
    if enable_telemetry is not None:
        _enable_telemetry_val = enable_telemetry
    elif non_interactive:
        _enable_telemetry_val = False
    else:
        _enable_telemetry_val = _ask_yn("Enable local query telemetry? [y/N]: ")

    # --- eager_load_embedders ---
    print(
        "\nEager embedder loading:\n"
        "  Pre-loads the embedding model at server startup instead of on the first query.\n"
        "  Eliminates first-query latency (~5-15s on first search without this).\n"
        "  Default: disabled."
    )
    if eager_load is not None:
        _eager_load_val = eager_load
    elif non_interactive:
        _eager_load_val = False
    else:
        _eager_load_val = _ask_yn(
            "Pre-load embedding models at startup (eliminates first-query latency)? [y/N]: "
        )

    # --- routing_strategy ---
    print(
        "\nRouting strategy:\n"
        "  centroid: routes queries to collections using centroid similarity (fast, default).\n"
        "  hybrid: combines centroid with keyword scoring (slightly slower, more accurate\n"
        "  for mixed corpora with distinct topic clusters).\n"
        "  Default: centroid."
    )
    if routing_strategy is not None:
        _routing_val = routing_strategy
    elif non_interactive:
        _routing_val = "centroid"
    else:
        _routing_val = _ask_choice(
            "Routing strategy (centroid/hybrid) [centroid]: ",
            valid={"centroid", "hybrid"},
            default="centroid",
        )

    # --- log_format ---
    print(
        "\nLog format:\n"
        "  text: human-readable log lines (default).\n"
        "  json: structured JSON logs, suitable for log aggregation pipelines.\n"
        "  Default: text."
    )
    if log_format is not None:
        _log_format_val = log_format
    elif non_interactive:
        _log_format_val = "text"
    else:
        _log_format_val = _ask_choice(
            "Log format (text/json) [text]: ",
            valid={"text", "json"},
            default="text",
        )

    # --- log_to_stderr conditional follow-up ---
    # Only prompt when: json format chosen interactively AND not already answered by flag.
    if log_to_stderr is not None:
        # Flag pre-answered — use it directly
        _log_to_stderr_val = log_to_stderr
    elif _log_format_val == "json" and not non_interactive:
        print(
            "\nLog to stderr only?\n"
            "  Routes all log output to stderr instead of a file.\n"
            "  Canonical container combo: --log-format json --log-to-stderr."
        )
        _log_to_stderr_val = _ask_yn("Log to stderr only? [y/N]: ")
    else:
        _log_to_stderr_val = False

    # --- HyDE / RAG Fusion (C15 Tier 2) ---
    # Prompt only when ANTHROPIC_API_KEY is set and interactive and not pre-answered.
    if enable_hyde is not None or enable_rag_fusion is not None:
        # One or both flags pre-answered — use them directly; no prompt.
        _enable_hyde_val = enable_hyde if enable_hyde is not None else False
        _enable_rag_fusion_val = enable_rag_fusion if enable_rag_fusion is not None else False
    elif non_interactive:
        _enable_hyde_val = False
        _enable_rag_fusion_val = False
    elif os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "\nAI query expansion (HyDE + RAG Fusion):\n"
            "  HyDE generates hypothetical answers to improve embedding recall.\n"
            "  RAG Fusion runs multiple query reformulations and merges results.\n"
            "  Both require $ANTHROPIC_API_KEY and add per-query latency.\n"
            "  Default: disabled."
        )
        if _ask_yn("Enable AI query expansion (HyDE + RAG Fusion)? [y/N]: "):
            _enable_hyde_val = True
            _enable_rag_fusion_val = True
        else:
            _enable_hyde_val = False
            _enable_rag_fusion_val = False
    else:
        _enable_hyde_val = False
        _enable_rag_fusion_val = False

    return WizardFeatures(
        install_code_extra=_install_code_extra_val,
        disable_reranker=_disable_reranker_val,
        enable_watch=_enable_watch_val,
        enable_telemetry=_enable_telemetry_val,
        eager_load_embedders=_eager_load_val,
        routing_strategy=_routing_val,
        log_format=_log_format_val,
        log_to_stderr=_log_to_stderr_val,
        enable_hyde=_enable_hyde_val,
        enable_rag_fusion=_enable_rag_fusion_val,
    )


def _prompt_multilingual(non_interactive: bool, flag_value: bool | None) -> bool:
    """Ask whether the corpus includes non-English documents.

    Returns ``flag_value`` unchanged when it is not None (flag takes precedence).
    Returns False in non-interactive mode (default: English).
    Otherwise prints a yes/no prompt and returns the user's answer.
    """
    if flag_value is not None:
        return flag_value
    if non_interactive:
        return False
    try:
        raw = input("Will your corpus include non-English documents? [y/N]: ").strip().lower()
    except EOFError:
        print("No input received (EOF). Using English.")
        return False
    return raw in {"y", "yes"}


def _prompt_gpu_confirm(non_interactive: bool, gpu: GpuType) -> bool:
    """Ask the user to confirm GPU acceleration after auto-detection.

    Returns True immediately when ``gpu`` is ``GpuType.NONE`` (nothing to confirm).
    Returns True immediately when ``non_interactive`` is True (auto-enable).
    For Metal or CUDA GPUs, prints a [Y/n] prompt; accepts "n"/"no" as decline.
    On EOFError returns True (auto-enable).
    """
    if gpu == GpuType.NONE:
        return True
    if non_interactive:
        return True
    if gpu == GpuType.METAL:
        prompt = "Apple Silicon detected — enable Metal acceleration? [Y/n]: "
    else:
        prompt = "NVIDIA GPU detected — enable CUDA acceleration? [Y/n]: "
    try:
        raw = input(prompt).strip().lower()
    except EOFError:
        return True
    return raw not in {"n", "no"}


# ---------------------------------------------------------------------------
# Code enrichment package install (Task C8-2.3)
# ---------------------------------------------------------------------------

def _install_extra(package: str, label: str, dry_run: bool = False) -> None:
    """Install an arbitrary pip *package* via uv (with pip fallback).

    Prints ``[dry-run] Would install {package}`` and returns early when
    *dry_run* is True.

    Primary path: ``uv pip install --python <sys.executable> {package}``.
    Falls back to ``sys.executable -m pip install {package}`` when uv is
    absent or fails.

    Raises ``InstallError`` if both paths fail.
    """
    if dry_run:
        click.echo(f"[dry-run] Would install {package}")
        return

    click.echo(f"Installing {label}...")
    python = sys.executable
    try:
        subprocess.run(
            ["uv", "pip", "install", "--python", python, package],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # uv not found or failed — fall back to pip
        try:
            subprocess.run(
                [python, "-m", "pip", "install", package],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as pip_exc:
            stderr = (pip_exc.stderr or b"").decode(errors="replace")
            raise InstallError(f"Failed to install {package}: {stderr}") from pip_exc

    click.echo(f"{label.capitalize()} installed.")


def _install_code_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[code]`` (tree-sitter enrichment packages).

    Thin wrapper around :func:`_install_extra`.  Public interface unchanged.
    """
    _install_extra("archon-search[code]", "code enrichment", dry_run)


# ---------------------------------------------------------------------------
# Legacy service cleanup (Task 3.4)
# ---------------------------------------------------------------------------

def _legacy_service_path() -> Path:
    """Return the path to a legacy externally-managed search service file."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.archon.search.plist"
    return Path.home() / ".config" / "systemd" / "user" / "archon-search.service"


def _remove_legacy_service(legacy_path: Path) -> None:
    """Unload and remove a legacy externally-managed service definition."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "unload", str(legacy_path)], check=False, capture_output=True)
        elif sys.platform.startswith("linux"):
            service_name = legacy_path.stem
            subprocess.run(["systemctl", "--user", "stop", service_name], check=False, capture_output=True)
            subprocess.run(["systemctl", "--user", "disable", service_name], check=False, capture_output=True)
    except Exception:
        pass  # best-effort
    try:
        legacy_path.unlink(missing_ok=True)
        click.echo(f"Removed legacy service file: {legacy_path}")
    except Exception as exc:
        click.echo(f"Warning: could not remove legacy service file: {exc}", err=True)


class SearchInstaller:
    """Installs and manages the search service end-to-end."""

    def __init__(self, config_file: str | None = None, dry_run: bool = False) -> None:
        self.config_file = config_file
        self.dry_run = dry_run

        cfg = load_config(path=Path(config_file) if config_file else None)
        self.cfg: SearchConfig = cfg

    # ------------------------------------------------------------------
    # Dependency checks
    # ------------------------------------------------------------------

    def check_deps(self) -> list[str]:
        """Return list of package names that cannot be imported."""
        missing: list[str] = []
        for pkg in _SEARCH_PACKAGES:
            try:
                importlib.import_module(pkg)
            except ImportError:
                missing.append(pkg)
        return missing

    # ------------------------------------------------------------------
    # GPU detection
    # ------------------------------------------------------------------

    def detect_gpu(self) -> GpuType:
        """Return the GPU type detected by the platform runtime."""
        return get_runtime().detect_gpu_type()

    # ------------------------------------------------------------------
    # Dependency installation
    # ------------------------------------------------------------------

    def install_deps(self, gpu: GpuType) -> None:
        """Install search dependencies into the same Python that runs this process. No-op when dry_run=True."""
        if self.dry_run:
            return

        python = sys.executable

        if gpu == GpuType.CUDA:
            subprocess.run(
                ["uv", "pip", "uninstall", "--python", python, "fastembed", "-y"],
                check=False,
            )
            subprocess.run(
                ["uv", "pip", "install", "--python", python, "fastembed-gpu>=0.8.0", "onnxruntime-gpu"],
                check=True,
            )
        else:
            subprocess.run(
                ["uv", "pip", "install", "--python", python, "fastembed>=0.8.0"],
                check=True,
            )

        subprocess.run(
            ["uv", "pip", "install", "--python", python, "lancedb", "docling", "markitdown",
             "trafilatura", "chonkie", "fastmcp"],
            check=True,
        )

    # ------------------------------------------------------------------
    # Provider validation
    # ------------------------------------------------------------------

    def validate_providers(self, providers: list[str]) -> bool:
        """Check that all non-CPU providers are available and embedding works.

        Returns True only if:
        1. Every non-CPU provider in `providers` is listed by onnxruntime.get_available_providers().
        2. TextEmbedding can be instantiated and produces an embedding without error.

        Never raises — caller handles fallback.
        """
        non_cpu = [p for p in providers if "CPU" not in p]
        if non_cpu:
            try:
                import onnxruntime  # lazy — not installed on all systems
                available = onnxruntime.get_available_providers()
            except Exception as exc:
                logger.warning("validate_providers: could not query onnxruntime providers: %s", exc)
                return False
            missing = [p for p in non_cpu if p not in available]
            if missing:
                logger.warning(
                    "validate_providers: providers not available in onnxruntime: %s", missing
                )
                return False

        try:
            from fastembed import TextEmbedding  # lazy — not installed on all systems
            model = TextEmbedding(self.cfg.embedding_model, providers=providers)
            list(model.embed(["archon search test"]))
        except Exception as exc:
            logger.warning("validate_providers: embedding test failed: %s", exc)
            return False

        return True

    # ------------------------------------------------------------------
    # Provider configuration
    # ------------------------------------------------------------------

    def configure_providers(self, gpu: GpuType) -> None:
        """Write providers list to [database] section via tomlkit based on gpu type.

        - GpuType.CUDA: write ["CUDAExecutionProvider"]
        - GpuType.METAL: write ["CoreMLExecutionProvider"]
        - GpuType.NONE: no-op
        No-op when dry_run=True.
        """
        _provider_map = {
            GpuType.CUDA: "CUDAExecutionProvider",
            GpuType.METAL: "CoreMLExecutionProvider",
        }
        target_provider = _provider_map.get(gpu)
        if target_provider is None or self.dry_run:
            return

        config_path = Path(self.config_file) if self.config_file else Path.home() / ".archon-search" / "archon-search.toml"
        if not config_path.exists():
            logger.warning("Config file %s not found — skipping provider config", config_path)
            return

        doc = tomlkit.parse(config_path.read_text())
        if "database" not in doc:
            doc["database"] = tomlkit.table()

        database_section = doc["database"]
        if isinstance(database_section, dict):
            existing_providers = database_section.get("providers", [])
            if target_provider in existing_providers:
                return  # already set — skip to preserve user-extended chains
            database_section["providers"] = [target_provider]

        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())

    # ------------------------------------------------------------------
    # Data directory
    # ------------------------------------------------------------------

    def create_data_dir(self) -> None:
        """Create the search database directory. No-op when dry_run=True."""
        if self.dry_run:
            return
        db_path = Path(self.cfg.db_path).expanduser()
        db_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Service management delegation
    # ------------------------------------------------------------------

    def write_service_file(self) -> None:
        """Stop legacy service then register the new search service."""
        svc = get_search_service()
        svc.pre_activate_cleanup(dry_run=self.dry_run)
        svc.register(dry_run=self.dry_run)

    def load_service(self) -> int:
        """Delegate to get_search_service().start()."""
        return get_search_service().start(dry_run=self.dry_run)

    def unload_service(self) -> int:
        """Delegate to get_search_service().stop()."""
        return get_search_service().stop(dry_run=self.dry_run)

    # ------------------------------------------------------------------
    # History collection bootstrap
    # ------------------------------------------------------------------

    async def _bootstrap_collections(self) -> None:
        """Sync configured collections into the search store."""
        from archon_search.progress import IndexingStateStore  # noqa: PLC0415
        from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

        pipeline = create_pipeline(self.cfg)
        try:
            await pipeline.store.connect()
            state_store = IndexingStateStore(Path(self.cfg.db_path).expanduser())
            sync = SearchCollectionSync(
                pipeline,
                state_store=state_store,
                pinned_collections=self.cfg.pinned_collections,
                chunk_size=self.cfg.chunk_size,
                auto_reindex_on_chunk_size_change=self.cfg.auto_reindex_on_chunk_size_change,
            )
            await sync.sync(self.cfg.pinned_collections)
        finally:
            await pipeline.store.disconnect()

    # ------------------------------------------------------------------
    # HTTP probe (service readiness)
    # ------------------------------------------------------------------

    def _is_service_running(self) -> bool:
        """Check if the search HTTP service is already running."""
        try:
            import urllib.request
            url = f"http://{self.cfg.host}:{self.cfg.port}/health"
            with urllib.request.urlopen(url, timeout=1):
                return True
        except Exception:
            return False

    def _wait_for_service(self, timeout: int = _WAIT_FOR_SERVICE_TIMEOUT) -> bool:
        """Poll HTTP health endpoint until ready or timeout. Returns True if up."""
        deadline = time.monotonic() + timeout
        print("Waiting for search service", end="", flush=True)
        try:
            while time.monotonic() < deadline:
                if self._is_service_running():
                    print(" ready.")
                    return True
                print(".", end="", flush=True)
                time.sleep(1)
            print(" timed out.")
            return False
        except KeyboardInterrupt:
            print()
            raise

    # ------------------------------------------------------------------
    # Full install flow
    # ------------------------------------------------------------------

    def run(
        self,
        non_interactive: bool = False,
        profile: str | None = None,
        multilingual: bool | None = None,
        skip_preload: bool = False,
        force: bool = False,
        delete_db: bool = False,
        accept_jina_license: bool = False,
        accept_fasttext_license: bool = False,
        *,
        install_code: bool | None = None,
        disable_reranker: bool | None = None,
        enable_watch: bool | None = None,
        enable_telemetry: bool | None = None,
        eager_load: bool | None = None,
        routing_strategy: str | None = None,
        log_format: str | None = None,
        disable_gpu: bool = False,
        # C15 Tier 1 deployment flags
        host: str | None = None,
        port: int | None = None,
        db_path: str | None = None,
        log_level: str | None = None,
        log_to_stderr: bool = False,
        top_k: int | None = None,
        telemetry_retention_days: int | None = None,
        # C15 Tier 2 AI query expansion flags
        enable_hyde: bool = False,
        enable_rag_fusion: bool = False,
        # C15 Tier 2 custom server key
        server_key: str | None = None,
    ) -> int:
        """Execute the full install flow. Returns 0 on success."""
        # Validate --force requires --delete-db
        if force and not delete_db:
            print("--force requires --delete-db. To force a reinstall, use both flags together.")
            return 1

        with _acquire_install_lock():
            # Step 0: legacy cleanup + log directory
            legacy = _legacy_service_path()
            if legacy.exists():
                _remove_legacy_service(legacy)
            log_dir = Path.home() / ".archon-search" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            # Before Step 1: resolve multilingual via interactive prompt
            is_multilingual = _prompt_multilingual(non_interactive, multilingual)

            # Step 1: profile selection
            try:
                profile_name, is_multilingual = _select_profile(profile, is_multilingual, non_interactive)
            except SystemExit as e:
                return int(e.code) if e.code is not None else 1
            except click.BadParameter as e:
                print(str(e))
                return 1

            # Step 2: get profile data
            prof = get_profile(profile_name, is_multilingual)

            # Step 2b: GPU detection + user confirmation (prompt only — writes stay in Step 9)
            gpu = self.detect_gpu()
            enable_gpu = not disable_gpu and _prompt_gpu_confirm(non_interactive, gpu)

            # Step 3: Jina license gate
            if _requires_jina_license(prof):
                try:
                    _prompt_jina_license(non_interactive, accept_jina_license=accept_jina_license)
                except SystemExit as e:
                    return int(e.code) if e.code is not None else 1

            # Step 3b: fasttext license gate + model download
            if is_multilingual and not skip_preload:
                try:
                    _prompt_fasttext_license(non_interactive, accept_fasttext_license=accept_fasttext_license)
                except SystemExit as e:
                    return int(e.code) if e.code is not None else 1
                if self.dry_run:
                    print("[DRY RUN] Would download fasttext model.")
                else:
                    try:
                        _download_fasttext_model(Path.home() / ".archon-search" / "models")
                    except InstallError as exc:
                        print(f"fasttext model download failed: {exc}", file=sys.stderr)
                        return 1

            # Step 3c: collect optional-feature choices (after all license gates)
            features = _prompt_optional_features(
                non_interactive,
                prof,
                install_code=install_code,
                disable_reranker=disable_reranker,
                enable_watch=enable_watch,
                enable_telemetry=enable_telemetry,
                eager_load=eager_load,
                routing_strategy=routing_strategy,
                log_format=log_format,
                log_to_stderr=log_to_stderr if log_to_stderr else None,
                enable_hyde=enable_hyde if enable_hyde else None,
                enable_rag_fusion=enable_rag_fusion if enable_rag_fusion else None,
            )

            # Step 3d: overlay C15 Tier 1 flag values onto features
            # These are flags-only (no interactive prompts), so they override
            # whatever _prompt_optional_features() may have set.
            if host is not None:
                features.host = host
            if port is not None:
                features.port = port
            if log_level is not None:
                features.log_level = log_level
            if log_to_stderr:
                features.log_to_stderr = log_to_stderr
            if top_k is not None:
                features.top_k = top_k
            if telemetry_retention_days is not None:
                features.telemetry_retention_days = telemetry_retention_days
            if enable_hyde:
                features.enable_hyde = enable_hyde
            if enable_rag_fusion:
                features.enable_rag_fusion = enable_rag_fusion

            # Step 3d-ii: non-loopback host security note
            _effective_host = features.host
            if _effective_host is not None and _effective_host != "127.0.0.1":
                print(
                    f"Note: binding to {_effective_host} exposes the service on all interfaces. "
                    "Ensure a firewall or reverse proxy is in place if this host is reachable externally."
                )

            # Step 3e: db_path special handling — validate and record for use below
            # We handle db_path separately because it requires filesystem operations.
            _db_path_override: str | None = db_path
            if _db_path_override is not None:
                features.db_path = _db_path_override
                _expanded_db_path = Path(_db_path_override).expanduser()
                try:
                    _expanded_db_path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    print(f"Error: could not create db_path directory {_expanded_db_path}: {exc}", file=sys.stderr)
                    return 1
                if not os.access(_expanded_db_path, os.W_OK):
                    print(
                        f"Error: db_path {_expanded_db_path} is not writable. "
                        "Choose a writable directory.",
                        file=sys.stderr,
                    )
                    return 1

            # Step 4: config path
            config_path = Path(self.config_file) if self.config_file else get_default_config_path()

            # Step 5: reinstall check
            db_path = Path(self.cfg.db_path).expanduser()
            if config_path.exists():
                existing_cfg = load_config(config_path)
                db_path = Path(existing_cfg.db_path).expanduser()
                try:
                    _check_reinstall_guard(existing_cfg, prof, profile_name, is_multilingual)
                except NeedsForceDeleteError as exc:
                    if self.dry_run:
                        print(f"[dry-run] Warning: {exc} (proceeding in dry-run mode; pass --force --delete-db to apply)")
                    elif not (force and delete_db):
                        print(str(exc))
                        return 1

                # db_path migration note: warn when --db-path differs from existing config
                if _db_path_override is not None:
                    existing_db_path_str = str(Path(existing_cfg.db_path).expanduser())
                    new_db_path_str = str(Path(_db_path_override).expanduser())
                    if existing_db_path_str != new_db_path_str:
                        print(
                            f"Note: changing db_path from {existing_db_path_str} to {new_db_path_str}. "
                            "Existing indexed data remains at the old location and will not be migrated automatically."
                        )

            # Step 6/7/8: config write branches
            branch: str
            if force and delete_db:
                # Branch A: force-delete reinstall
                branch = "force"
                try:
                    _execute_force_reinstall(
                        config_path, db_path, prof, profile_name, is_multilingual,
                        non_interactive, dry_run=self.dry_run, features=features
                    )
                except SystemExit as e:
                    return int(e.code) if e.code is not None else 1
            elif not config_path.exists():
                # Branch B: fresh install
                branch = "fresh"
                tmp = config_path.with_suffix(config_path.suffix + ".tmp")
                tmp.unlink(missing_ok=True)
                config_path.parent.mkdir(parents=True, exist_ok=True)
                if not self.dry_run:
                    atomic_write_bytes(config_path, _profile_toml(profile_name, is_multilingual, features).encode())
                    shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))
                else:
                    print(f"[DRY RUN] Would write config: {config_path}")
                    print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")
            else:
                # Branch C: idempotent reinstall (same profile)
                branch = "idempotent"

                # Overwrite warning: detect hand-edits before writing
                prev_profile_name = existing_cfg.profile
                prev_multilingual = existing_cfg.multilingual
                has_edits = _detect_config_hand_edits(config_path, prev_profile_name, prev_multilingual)

                if has_edits and not self.dry_run:
                    if non_interactive:
                        print(
                            "[warn] Existing config has custom values; overwriting with profile defaults."
                        )
                    else:
                        try:
                            answer = input(
                                "Existing config has custom values. Overwrite with profile defaults? [y/N]: "
                            ).strip().lower()
                        except EOFError:
                            answer = ""
                        if answer not in ("y", "yes"):
                            print("Installation aborted.")
                            return 1

                if not self.dry_run:
                    shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))
                    _write_profile_config(config_path, prof, profile_name, is_multilingual, features=features)
                    print(f"  Backup:     {config_path.with_suffix('.toml.bak')}")
                else:
                    if has_edits:
                        print(
                            "[DRY RUN] Would prompt: Existing config has custom values."
                            " Overwrite with profile defaults?"
                        )
                    print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")
                    print(f"[DRY RUN] Would overwrite config: {config_path}")

            # Step 8b: reload config with freshly-written values
            if self.dry_run and branch == "fresh":
                fd, tmp_path_str = tempfile.mkstemp(suffix=".toml")
                try:
                    os.write(fd, _profile_toml(profile_name, is_multilingual, features).encode())
                    os.close(fd)
                    fd = -1
                    self.cfg = cfg = load_config(Path(tmp_path_str))
                finally:
                    if fd != -1:
                        os.close(fd)
                    os.unlink(tmp_path_str)
            else:
                self.cfg = cfg = load_config(config_path)

            # Step 9: GPU provider configuration (detection + user confirm already done in Step 2b)
            providers: list[str] = []
            if not enable_gpu:
                # User declined GPU — write providers = [] explicitly to override any previous setting
                gpu_config_path = Path(self.config_file) if self.config_file else get_default_config_path()
                if gpu_config_path.exists() and not self.dry_run:
                    gpu_doc = tomlkit.parse(gpu_config_path.read_text())
                    if "database" not in gpu_doc:
                        gpu_doc.add("database", tomlkit.table())
                    gpu_doc["database"]["providers"] = tomlkit.array()
                    atomic_write_bytes(gpu_config_path, tomlkit.dumps(gpu_doc).encode())
            elif not self.dry_run and gpu == GpuType.METAL:
                if self.validate_providers(["CoreMLExecutionProvider"]):
                    self.configure_providers(gpu=gpu)
                    providers = ["CoreML (Apple Silicon)"]
                else:
                    print("Warning: CoreML validation failed — falling back to CPU.")
            elif gpu == GpuType.CUDA:
                self.configure_providers(gpu=gpu)
                providers = ["CUDA"]
            else:
                self.configure_providers(gpu=gpu)

            # Step 10: create data directory
            self.create_data_dir()

            # Step 11: disk space check
            try:
                _check_disk_space(prof)
            except InstallError as exc:
                print(str(exc))
                return 1

            # Step 12: summary display
            from archon_search import key_manager as _key_manager
            print(_render_summary(
                profile_name, prof, is_multilingual, providers, features,
                db_path=str(Path(cfg.db_path).expanduser()),
                host=features.host if features.host is not None else cfg.host,
                port=features.port if features.port is not None else cfg.port,
                api_key_file=str(_key_manager.get_key_file()),
                download_mb=prof.download_mb,
            ))

            # Step 13: confirmation
            if not non_interactive:
                answer = input("Proceed? [Y/n]: ").strip().lower()
                if answer not in ("y", ""):
                    print("Installation aborted.")
                    return 1

            # Before Step 14: install code enrichment packages if requested
            if features.install_code_extra:
                try:
                    _install_code_extra(dry_run=self.dry_run)
                except InstallError as exc:
                    print(f"Warning: code enrichment install failed: {exc}", file=sys.stderr)
                    # Non-fatal — continue

            # Before Step 14b: write custom server key if provided
            if server_key is not None and not self.dry_run:
                _key_file = get_key_file()
                atomic_write_bytes(_key_file, f"ARCHON_SEARCH_API_KEY={server_key}\n".encode())
                os.chmod(_key_file, 0o600)
                print(
                    "Note: your server key may appear in shell history. "
                    "Consider using ARCHON_SEARCH_API_KEY env var instead."
                )
                if os.environ.get("ARCHON_SEARCH_API_KEY"):
                    print(
                        "Warning: ARCHON_SEARCH_API_KEY env var is set and takes priority over the key file. "
                        "Your --server-key value was written to disk but will not be used while "
                        "ARCHON_SEARCH_API_KEY is set."
                    )
                print("Server key updated. Restart the service to apply: archon-search restart.")
            elif server_key is not None and self.dry_run:
                print(f"[dry-run] Would write server key to {get_key_file()}.")

            # Step 14: pre-warm
            if not skip_preload:
                if self.dry_run:
                    print(f"[DRY RUN] Would download models (~{prof.download_mb} MB).")
                else:
                    print("[4/5] Downloading models...")
                    try:
                        _prewarm_models(prof)
                    except InstallError as exc:
                        print(f"Model download failed: {exc}", file=sys.stderr)
                        if branch == "fresh":
                            config_path.unlink(missing_ok=True)
                            config_path.with_suffix(".toml.bak").unlink(missing_ok=True)
                        elif branch == "idempotent":
                            bak = config_path.with_suffix(".toml.bak")
                            if bak.exists():
                                shutil.copy2(bak, config_path)
                        # branch == "force": leave backup, new config stays
                        return 1

            # Step 15: register and start service
            print("[5/5] Starting search service...")
            self.write_service_file()
            rc = self.load_service()
            if rc != 0:
                print(f"Service start returned exit code {rc}.", file=sys.stderr)
                return rc

            # Step 16: wait for readiness
            if not self.dry_run:
                ready = self._wait_for_service()
                if not ready:
                    print(f"Warning: Search service did not become ready within {_WAIT_FOR_SERVICE_TIMEOUT} seconds.")
                    return 1

            # Step 16b: next steps guidance (non-dry-run only)
            if not self.dry_run:
                _print_next_steps(cfg.host, cfg.port, str(_key_manager.get_key_file()))

            # Step 17: completion message
            if not self.dry_run:
                _api_key, _key_source = load_or_generate_key()
                if _key_source == "env var":
                    print(
                        f"  API key: {_api_key}"
                        "  (source: $ARCHON_SEARCH_API_KEY env var — keep this key private)"
                    )
                elif _key_source == "auto-generated":
                    print(
                        f"  API key: {_api_key}"
                        f"  (generated fresh — keep this key private; also stored at: {get_key_file()})"
                    )
                else:
                    print(
                        f"  API key: {_api_key}"
                        f"  (keep this key private; also stored at: {get_key_file()})"
                    )
            # Post-install hint: if ANTHROPIC_API_KEY not set and HyDE/RAG Fusion not requested
            if not self.dry_run and not features.enable_hyde and not features.enable_rag_fusion:
                if not os.environ.get("ANTHROPIC_API_KEY"):
                    print(
                        "Tip: Set $ANTHROPIC_API_KEY to enable AI query expansion "
                        "(HyDE + RAG Fusion) next run."
                    )

            lang = "Multilingual" if is_multilingual else "English"
            print(f"archon-search installed and running. Profile: {profile_name.capitalize()} · {lang}.")
            return 0

    # ------------------------------------------------------------------
    # Register-and-start flow (used by `archon-search install`)
    # ------------------------------------------------------------------

    def run_register_and_start(self) -> int:
        """Register and start the service. Requires wizard to have been run first.

        Returns 0 on success, non-zero on failure.
        """
        config_path = Path(self.config_file) if self.config_file else get_default_config_path()
        if not config_path.exists():
            click.echo(
                "No configuration found. Run 'archon-search wizard' first to choose a profile"
                " and download models.",
                err=True,
            )
            return 1

        self.write_service_file()
        rc = self.load_service()
        if rc != 0:
            click.echo(f"Service start returned exit code {rc}.", err=True)
            return rc

        if not self.dry_run:
            ready = self._wait_for_service()
            if not ready:
                click.echo(
                    f"Warning: Search service did not become ready within"
                    f" {_WAIT_FOR_SERVICE_TIMEOUT} seconds.",
                    err=True,
                )
                return 1

        click.echo("archon-search service registered and running.")
        return 0

    # ------------------------------------------------------------------
    # Uninstall flow
    # ------------------------------------------------------------------

    def run_uninstall(self, delete_db: bool = False) -> int:
        """Stop and unregister the search service. Optionally delete the database."""
        rag_svc = get_search_service()
        rag_svc.stop(dry_run=self.dry_run)
        rag_svc.unregister(dry_run=self.dry_run)

        db_deleted = False
        if delete_db:
            db_path = Path(self.cfg.db_path).expanduser()
            if db_path.exists():
                if not self.dry_run:
                    rmtree(db_path)
                    print(f"Deleted search database at {db_path}.")
                    db_deleted = True

        if db_deleted:
            print(
                "Search service uninstalled. Search database deleted. Your archon-search.toml settings are preserved."
            )
        else:
            print(
                "Search service uninstalled. Your search settings are preserved in archon-search.toml."
            )
        return 0
