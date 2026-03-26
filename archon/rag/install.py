"""RagInstaller — install, configure, and manage the RAG service (Task 7.1)."""
from __future__ import annotations

import asyncio
import importlib
import logging
import subprocess
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING

import tomlkit

from archon.platform import get_rag_service, get_runtime
from archon.platform.types import GpuType
from archon.rag.pipeline import create_pipeline

if TYPE_CHECKING:
    from archon.config.loader import Config, RagConfig

logger = logging.getLogger("archon")

_RAG_PACKAGES = ["lancedb", "fastembed", "docling", "markitdown", "trafilatura", "chonkie", "fastmcp"]


class RagInstaller:
    """Installs and manages the RAG service end-to-end."""

    def __init__(self, config_file: str = "config.toml", dry_run: bool = False) -> None:
        self.config_file = config_file
        self.dry_run = dry_run

        # Load config
        from archon.config.loader import load_config
        cfg = load_config(config_file)
        self.cfg: RagConfig = cfg.rag
        self._full_cfg: Config = cfg

    # ------------------------------------------------------------------
    # Dependency checks
    # ------------------------------------------------------------------

    def check_deps(self) -> list[str]:
        """Return list of package names that cannot be imported."""
        missing: list[str] = []
        for pkg in _RAG_PACKAGES:
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
        """Install RAG dependencies. No-op when dry_run=True."""
        if self.dry_run:
            return

        if gpu == "cuda":
            subprocess.run(
                ["uv", "pip", "uninstall", "fastembed", "-y"],
                check=False,
            )
            subprocess.run(
                ["uv", "pip", "install", "fastembed-gpu>=0.7.4", "onnxruntime-gpu"],
                check=True,
            )
        else:
            subprocess.run(
                ["uv", "pip", "install", "fastembed>=0.7.4"],
                check=True,
            )

        subprocess.run(
            ["uv", "pip", "install", "lancedb", "docling", "markitdown",
             "trafilatura", "chonkie", "fastmcp"],
            check=True,
        )

    # ------------------------------------------------------------------
    # Provider configuration
    # ------------------------------------------------------------------

    def configure_providers(self, gpu: bool) -> None:
        """Write providers = ["CUDAExecutionProvider"] to [rag] section via tomlkit.

        No-op when gpu=False or dry_run=True.
        """
        if not gpu or self.dry_run:
            return

        config_path = Path(self.config_file)
        if not config_path.exists():
            logger.warning("Config file %s not found — skipping provider config", config_path)
            return

        doc = tomlkit.parse(config_path.read_text())
        if "rag" not in doc:
            doc["rag"] = tomlkit.table()

        rag_section = doc["rag"]
        if isinstance(rag_section, dict):
            providers = rag_section.get("providers")
            if providers and "CUDAExecutionProvider" in providers:
                return  # already set
            rag_section["providers"] = ["CUDAExecutionProvider"]

        config_path.write_text(tomlkit.dumps(doc))

    # ------------------------------------------------------------------
    # Data directory
    # ------------------------------------------------------------------

    def create_data_dir(self) -> None:
        """Create the RAG database directory. No-op when dry_run=True."""
        if self.dry_run:
            return
        db_path = Path(self.cfg.db_path).expanduser()
        db_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Service management delegation
    # ------------------------------------------------------------------

    def write_service_file(self) -> None:
        """Delegate to get_rag_service().register()."""
        get_rag_service().register(dry_run=self.dry_run)

    def load_service(self) -> int:
        """Delegate to get_rag_service().start()."""
        return get_rag_service().start(dry_run=self.dry_run)

    def unload_service(self) -> int:
        """Delegate to get_rag_service().stop()."""
        return get_rag_service().stop(dry_run=self.dry_run)

    # ------------------------------------------------------------------
    # History collection bootstrap
    # ------------------------------------------------------------------

    async def create_history_collection(self) -> None:
        """Ingest history directory into RAG store using direct pipeline access."""
        history_dir = Path(self._full_cfg.history.directory).expanduser() / "sessions"
        pipeline = create_pipeline(self.cfg)
        try:
            await pipeline.store.connect()
            await pipeline.ingest_directory(history_dir, self.cfg.history_collection)
        finally:
            await pipeline.store.disconnect()

    # ------------------------------------------------------------------
    # HTTP probe (service readiness)
    # ------------------------------------------------------------------

    def _is_service_running(self) -> bool:
        """Check if the RAG HTTP service is already running."""
        try:
            import urllib.request
            url = f"http://{self.cfg.host}:{self.cfg.port}/health"
            with urllib.request.urlopen(url, timeout=1):
                return True
        except Exception:
            return False

    def _wait_for_service(self, timeout: int = 30) -> bool:
        """Poll HTTP health endpoint until ready or timeout. Returns True if up."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_service_running():
                return True
            time.sleep(1)
        return False

    # ------------------------------------------------------------------
    # Full install flow
    # ------------------------------------------------------------------

    def run(self, non_interactive: bool = False) -> int:
        """Execute the full install flow. Returns 0 on success."""
        gpu = self.detect_gpu()

        print(f"RAG installer — GPU detected: {gpu}")
        print("Note: first run will download ~150MB of model data.")

        if not non_interactive:
            answer = input("Proceed with installation? [y/N] ").strip().lower()
            if answer != "y":
                print("Installation aborted.")
                return 1

        # Warn if service already running
        if self._is_service_running():
            print("Warning: RAG service is already running. Proceeding anyway.")

        # Dependencies
        missing = self.check_deps()
        if missing:
            print(f"Installing missing packages: {', '.join(missing)}")
            self.install_deps(gpu=gpu)

        # Configure CUDA providers if GPU available
        self.configure_providers(gpu=gpu)

        # Create data directory
        self.create_data_dir()

        # Bootstrap history collection
        if not self.dry_run:
            asyncio.run(self.create_history_collection())

        # Register and start service
        self.write_service_file()
        rc = self.load_service()
        if rc != 0:
            print(f"Service start returned exit code {rc}.")
            return rc

        # Wait for readiness
        if not self.dry_run:
            ready = self._wait_for_service(timeout=30)
            if not ready:
                print("RAG service did not become ready within 30 seconds.")
                return 1

        print("RAG service installed and running successfully.")
        return 0

    # ------------------------------------------------------------------
    # Uninstall flow
    # ------------------------------------------------------------------

    def run_uninstall(self, delete_db: bool = False) -> int:
        """Stop and unregister the RAG service. Optionally delete the database."""
        rag_svc = get_rag_service()
        rag_svc.stop(dry_run=self.dry_run)
        rag_svc.unregister(dry_run=self.dry_run)

        if delete_db:
            db_path = Path(self.cfg.db_path).expanduser()
            if db_path.exists():
                if not self.dry_run:
                    rmtree(db_path)
                    print(f"Deleted RAG database at {db_path}.")

        print("RAG service uninstalled. Remove [rag] section from config.toml to disable.")
        return 0
