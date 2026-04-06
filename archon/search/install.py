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

from archon.cli.console import Console
from archon.platform import get_search_service, get_runtime
from archon.platform.types import GpuType
from archon.search.pipeline import create_pipeline

if TYPE_CHECKING:
    from archon.config.loader import Config, SearchConfig

logger = logging.getLogger("archon")

_SEARCH_PACKAGES = ["lancedb", "fastembed", "docling", "markitdown", "trafilatura", "chonkie", "fastmcp"]
_WAIT_FOR_SERVICE_TIMEOUT = 60


class SearchInstaller:
    """Installs and manages the search service end-to-end."""

    def __init__(self, config_file: str | None = None, dry_run: bool = False, console: Console | None = None) -> None:
        self._console = console or Console()
        self.config_file = config_file or str(Path.home() / ".archon" / "config.toml")
        self.dry_run = dry_run

        # Load config — token not required; RAG commands are independent of the Telegram bot
        from archon.config.loader import load_config
        cfg = load_config(config_file=self.config_file, require_token=False)
        self.cfg: SearchConfig = cfg.search
        self._full_cfg: Config = cfg

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

        if gpu == "cuda":
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
        """Write providers list to [search] section via tomlkit based on gpu type.

        - "cuda": write ["CUDAExecutionProvider"]
        - "apple_silicon": write ["CoreMLExecutionProvider"]
        - "none": no-op
        No-op when dry_run=True.
        """
        _provider_map = {
            "cuda": "CUDAExecutionProvider",
            "apple_silicon": "CoreMLExecutionProvider",
        }
        target_provider = _provider_map.get(gpu)
        if target_provider is None or self.dry_run:
            return

        config_path = Path(self.config_file)
        if not config_path.exists():
            logger.warning("Config file %s not found — skipping provider config", config_path)
            return

        doc = tomlkit.parse(config_path.read_text())
        if "search" not in doc:
            doc["search"] = tomlkit.table()

        search_section = doc["search"]
        if isinstance(search_section, dict):
            existing_providers = search_section.get("providers", [])
            if target_provider in existing_providers:
                return  # already set — skip to preserve user-extended chains
            search_section["providers"] = [target_provider]

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
        """Delegate to get_search_service().register()."""
        get_search_service().register(dry_run=self.dry_run)

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
        from archon.search.progress import IndexingStateStore  # noqa: PLC0415
        from archon.search.sync import SearchCollectionSync  # noqa: PLC0415

        pipeline = create_pipeline(self.cfg)
        try:
            await pipeline.store.connect()
            state_store = IndexingStateStore(Path(self.cfg.db_path).expanduser())
            sync = SearchCollectionSync(
                pipeline,
                state_store=state_store,
                pinned_collections=self._full_cfg.search.pinned_collections,
                embedding_model=self._full_cfg.search.embedding_model,
                chunk_size=self._full_cfg.search.chunk_size,
                auto_reindex_on_chunk_size_change=self._full_cfg.search.auto_reindex_on_chunk_size_change,
            )
            await sync.sync(self._full_cfg.search.collections)
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

        self._console.info(f"Search installer — GPU detected: {gpu}")
        self._console.info("Note: first run will download ~150MB of model data.")

        if not non_interactive:
            answer = input("Proceed with installation? [y/N] ").strip().lower()
            if answer != "y":
                self._console.info("Installation aborted.")
                return 1

        # Warn if service already running
        if self._is_service_running():
            self._console.warn("Warning: Search service is already running. Proceeding anyway.")

        # Dependencies
        missing = self.check_deps()
        if missing:
            self._console.info(f"[1/5] Installing packages: {', '.join(missing)} ...")
            self.install_deps(gpu=gpu)
            self._console.success("[1/5] Packages installed.")
        else:
            self._console.success("[1/5] All packages already installed.")

        # Configure execution providers based on GPU type
        if not self.dry_run and gpu == "apple_silicon":
            self._console.info("[2/5] Validating GPU acceleration (first run downloads ~150 MB model data) ...")
            if self.validate_providers(["CoreMLExecutionProvider"]):
                self.configure_providers(gpu=gpu)
                self._console.success("[2/5] CoreML acceleration validated — GPU/Neural Engine active.")
            else:
                self._console.warn("[2/5] Warning: CoreML validation failed — falling back to CPU. macOS 12+ required.")
        else:
            self._console.info(f"[2/5] Configuring providers for {gpu} ...")
            self.configure_providers(gpu=gpu)
            self._console.success(f"[2/5] Providers configured for {gpu}.")

        # Create data directory
        self._console.info("[3/5] Creating data directory ...")
        self.create_data_dir()

        # Register and start service (bootstrap happens in the background via the server's startup sync)
        self._console.info("[4/5] Starting search service ...")
        self.write_service_file()
        rc = self.load_service()
        if rc != 0:
            self._console.error(f"Service start returned exit code {rc}.")
            return rc

        # Wait for service readiness — indexing runs in background via server's asyncio.create_task
        self._console.info("[5/5] Waiting for service readiness ...")
        if not self.dry_run:
            ready = self._wait_for_service()
            if not ready:
                self._console.warn(f"Search service did not become ready within {_WAIT_FOR_SERVICE_TIMEOUT} seconds.")
                return 1

        self._console.success("Search service installed and running.")
        return 0

    # ------------------------------------------------------------------
    # Uninstall flow
    # ------------------------------------------------------------------

    def run_uninstall(self, delete_db: bool = False) -> int:
        """Stop and unregister the search service. Optionally delete the database."""
        rag_svc = get_search_service()
        rag_svc.stop(dry_run=self.dry_run)
        rag_svc.unregister(dry_run=self.dry_run)

        if delete_db:
            db_path = Path(self.cfg.db_path).expanduser()
            if db_path.exists():
                if not self.dry_run:
                    rmtree(db_path)
                    self._console.info(f"Deleted search database at {db_path}.")

        self._console.info("Search service uninstalled. Remove [search] section from config.toml to disable.")
        return 0
