"""SearchInstaller — install, configure, and manage the search service (Task 7.1)."""
from __future__ import annotations

import importlib
import logging
import subprocess
import sys
import time
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING

import tomlkit

from archon_search.config import SearchConfig, load_config
from archon_search.pipeline import create_pipeline
from archon_search.platform.runtime import get_runtime, get_search_service
from archon_search.platform.types import GpuType

if TYPE_CHECKING:
    pass

logger = logging.getLogger("archon")

_SEARCH_PACKAGES = ["lancedb", "fastembed", "docling", "markitdown", "trafilatura", "chonkie", "fastmcp"]
_WAIT_FOR_SERVICE_TIMEOUT = 60


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

        config_path = Path(self.config_file) if self.config_file else Path.home() / ".archon" / "archon-search.toml"
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

        config_path.write_text(tomlkit.dumps(doc))

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
