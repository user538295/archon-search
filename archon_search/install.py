"""SearchInstaller — install, configure, and manage the search service (Task 7.1)."""
from __future__ import annotations

import contextlib
import importlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
import shutil
from shutil import rmtree
from typing import TYPE_CHECKING

import tomlkit

from archon_search._durable_io import atomic_write_bytes
from archon_search.config import SearchConfig, load_config
from archon_search.pipeline import create_pipeline
from archon_search.platform.runtime import get_runtime, get_search_service
from archon_search.platform.types import GpuType
from archon_search.profiles import InstallProfile, get_profile

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


def _write_profile_config(
    config_path: Path,
    profile: InstallProfile,
    profile_name: str,
    multilingual: bool,
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

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())


def _profile_toml(profile_name: str, multilingual: bool) -> str:
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

    return tomlkit.dumps(doc)


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
    TextCrossEncoder = fastembed.TextCrossEncoder  # noqa: N806

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
) -> None:
    """Execute the force-delete-db rollback sequence."""
    # Step 1: Backup config
    bak_path = config_path.with_suffix(".toml.bak")
    has_backup = False
    if config_path.exists():
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
            _write_profile_config(config_path, profile, profile_name, multilingual)
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
            lines.append(
                f"  {n}) {cap:<9}  {size_str:<10}  {p.quality_stars:<12}  "
                f"~{p.cpu_ms} ms/query  / ~{p.metal_ms} ms"
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
            if p.reranker is not None:
                lines.append(f"  {n}) {cap}: {p.embedder} + {p.reranker} ({size_str})")
            else:
                lines.append(f"  {n}) {cap}: {p.embedder} (no reranker) ({size_str})")

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
        "",
        "  Note: Model files are downloaded now. ONNX session initialization happens in the",
        "  server process on first query — expect ~5–15s latency on first search.",
    ]
    return "\n".join(lines)


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
                embedding_model=self.cfg.embedding_model,
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

    def run(self, non_interactive: bool = False) -> int:
        """Execute the full install flow. Returns 0 on success."""
        gpu = self.detect_gpu()

        print(f"Search installer — GPU detected: {gpu}")
        print("Note: first run will download ~150MB of model data.")

        if not non_interactive:
            answer = input("Proceed with installation? [y/N] ").strip().lower()
            if answer != "y":
                print("Installation aborted.")
                return 1

        # Warn if service already running
        if self._is_service_running():
            print("Warning: Search service is already running. Proceeding anyway.")

        # Dependencies
        missing = self.check_deps()
        if missing:
            print(f"[1/5] Installing packages: {', '.join(missing)} ...")
            self.install_deps(gpu=gpu)
            print("[1/5] Packages installed.")
        else:
            print("[1/5] All packages already installed.")

        # Configure execution providers based on GPU type
        if not self.dry_run and gpu == GpuType.METAL:
            print("[2/5] Validating GPU acceleration (first run downloads ~150 MB model data) ...")
            if self.validate_providers(["CoreMLExecutionProvider"]):
                self.configure_providers(gpu=gpu)
                print("[2/5] CoreML acceleration validated — GPU/Neural Engine active.")
            else:
                print("[2/5] Warning: CoreML validation failed — falling back to CPU. macOS 12+ required.")
        else:
            print(f"[2/5] Configuring providers for {gpu} ...")
            self.configure_providers(gpu=gpu)
            print(f"[2/5] Providers configured for {gpu}.")

        # Create data directory
        print("[3/5] Creating data directory ...")
        self.create_data_dir()

        # Register and start service (bootstrap happens in the background via the server's startup sync)
        print("[4/5] Starting search service ...")
        self.write_service_file()
        rc = self.load_service()
        if rc != 0:
            print(f"Service start returned exit code {rc}.", file=sys.stderr)
            return rc

        # Wait for service readiness — indexing runs in background via server's asyncio.create_task
        print("[5/5] Waiting for service readiness ...")
        if not self.dry_run:
            ready = self._wait_for_service()
            if not ready:
                print(f"Warning: Search service did not become ready within {_WAIT_FOR_SERVICE_TIMEOUT} seconds.")
                return 1

        print("Search service installed and running.")
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
