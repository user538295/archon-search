"""Installer strategy hub: BaseInstaller plus the dry-run and real strategies.

Split into a read-only ``BaseInstaller`` (ABC) with two concrete strategies:
``RealInstaller`` executes destructive actions, ``DryRunInstaller`` stubs them.
Use ``create_installer(config_file, dry_run)`` to obtain the right one.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from shutil import rmtree
from typing import Protocol, runtime_checkable

import click
import tomlkit

from archon_search._durable_io import atomic_write_bytes
from archon_search.config import SearchConfig, get_default_config_path, load_config
from archon_search.key_manager import get_key_file, load_or_generate_key
from archon_search.paths import get_data_dir
from archon_search.pipeline import create_pipeline
from archon_search.platform.runtime import get_runtime, get_search_service
from archon_search.platform.types import GpuType
from archon_search.profiles import InstallProfile, get_profile

from .config_writer import (
    WizardFeatures,
    _assert_features_persisted,
    _detect_config_hand_edits,
    _profile_toml,
    _revert_graph_enabled_flag,
    _revert_multilingual_flag,
    _write_profile_config,
)
from .errors import InstallError, NeedsForceDeleteError
from .extras import (
    _install_code_extra,
    _install_graph_extra,
    _install_multilingual_extra,
    _install_query_expansion_extras,
    _revert_query_expansion_flags,
)
from .licenses import (
    _download_fasttext_model,
    _prompt_fasttext_license,
    _prompt_jina_license,
    _requires_jina_license,
)
from .lock import _acquire_install_lock
from .prewarm import (
    _check_disk_space,
    _check_reinstall_guard,
    _execute_force_reinstall,
    _prewarm_models,
)
from .render import _print_next_steps, _render_summary
from .service_ops import _create_secrets_env, _legacy_service_path, _remove_legacy_service
from .wizard import (
    _prompt_gpu_confirm,
    _prompt_multilingual,
    _prompt_optional_features,
    _select_profile,
)

logger = logging.getLogger(__name__)


_SEARCH_PACKAGES = ["lancedb", "fastembed", "docling", "markitdown", "trafilatura", "chonkie", "fastmcp"]
_WAIT_FOR_SERVICE_TIMEOUT = 60


def _compute_svc_timeout(eager_load: bool, download_mb: int) -> int:
    """Return the service-ready wait timeout in seconds.

    With eager_load_embedders=True the server runs ONNX reconstruction before
    accepting requests; that can take several minutes for large models.  Scale
    to ~100ms per MB, capped at 10 minutes.  Without eager load the default
    60 s is sufficient.
    # ponytail: linear scale is fine; a smarter heuristic adds no value here
    """
    if not eager_load:
        return _WAIT_FOR_SERVICE_TIMEOUT
    return min(600, max(_WAIT_FOR_SERVICE_TIMEOUT, download_mb * 100 // 1000))


# ---------------------------------------------------------------------------
# Installer strategy pattern (dry-run enforcement)
# ---------------------------------------------------------------------------


@runtime_checkable
class InstallerProtocol(Protocol):
    """Public surface both concrete installers implement.

    A runtime-checkable Protocol so call sites depend on the capability, not a
    concrete class. ``create_installer`` returns either ``DryRunInstaller`` or
    ``RealInstaller``, both of which satisfy this protocol.
    """

    config_file: str | None
    cfg: SearchConfig
    dry_run: bool

    def check_deps(self) -> list[str]: ...
    def detect_gpu(self) -> GpuType: ...
    def install_deps(self, gpu: GpuType) -> None: ...
    def validate_providers(self, providers: list[str]) -> bool: ...
    def validate_embedder_only(self, providers: list[str]) -> bool: ...
    def configure_providers(self, gpu: GpuType) -> None: ...
    def configure_reranker_providers(self, providers: list[str]) -> None: ...
    def clear_reranker_providers(self) -> None: ...
    def create_data_dir(self) -> None: ...
    def write_service_file(self) -> None: ...
    def load_service(self) -> int: ...
    def unload_service(self) -> int: ...
    def run(self, *args: object, **kwargs: object) -> int: ...
    def run_register_and_start(self) -> int: ...
    def run_uninstall(self, delete_db: bool = ...) -> int: ...


class BaseInstaller(ABC):
    """Read-only install logic shared by the dry-run and real installers.

    Holds config parsing, GPU detection, provider validation, and the install /
    register / uninstall orchestration. Every operation that mutates the system
    (filesystem writes, package installs, service lifecycle) is an abstract method
    here, implemented by the concrete subclasses: ``DryRunInstaller`` narrates them
    so nothing is mutated, ``RealInstaller`` executes them. run()/run_uninstall()
    call these unconditionally and never touch the system directly.

    This routes the mutation surface through the abstract boundary, but it is not
    a complete safety proof: a raw mutation typed inline into run() would bypass
    it. The backstop is ``test_dry_run_backstop_touches_nothing_real`` — a full
    dry-run that fails if any real seam is invoked anywhere.
    """

    #: Overridden per concrete class; a few non-mutating run() branches still read it.
    dry_run: bool = False
    #: Banner printed at the very start of run() announcing the mode.
    _MODE_BANNER: str = ""

    def __init__(self, config_file: str | None = None) -> None:
        self.config_file = config_file
        cfg = load_config(path=Path(config_file) if config_file else None)
        self.cfg: SearchConfig = cfg

    # ------------------------------------------------------------------
    # Read-only methods (no filesystem / service / subprocess mutation)
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

    def detect_gpu(self) -> GpuType:
        """Return the GPU type detected by the platform runtime."""
        return get_runtime().detect_gpu_type()

    def validate_providers(self, providers: list[str]) -> bool:
        """Check that all non-CPU providers are available and both models load.

        Delegates to :func:`validate_providers_shared` (C3) so the wizard and the
        background startup check probe identically. Returns True only if BOTH the
        embedder and the reranker pass under *providers*; a disabled reranker
        (``reranker_model == ""``) reports ``reranker_ok = True`` with no probe.

        Never raises — caller handles fallback. The whole body is guarded so that
        a broken lazy import (``model_validation`` imports ``fastembed`` at module
        scope) or a future contract breach in ``validate_providers_shared`` yields
        ``False`` rather than crashing the install wizard.
        """
        try:
            # Lazy import — model_validation imports fastembed/onnxruntime at
            # module scope; importing it here keeps that cost off CLI startup.
            from archon_search.model_validation import validate_providers_shared

            embedder_ok, reranker_ok, warnings = validate_providers_shared(
                providers,
                self.cfg.embedding_model,
                self.cfg.reranker_model,
            )
        except Exception as exc:
            logger.warning("validate_providers: provider validation failed: %s", exc)
            return False
        for warning in warnings:
            logger.warning("validate_providers: %s", warning)
        return embedder_ok and reranker_ok

    def validate_embedder_only(self, providers: list[str]) -> bool:
        """Like validate_providers but only probes the embedder (reranker_model="")."""
        try:
            from archon_search.model_validation import validate_providers_shared

            embedder_ok, _, warnings = validate_providers_shared(
                providers,
                self.cfg.embedding_model,
                "",  # disabled reranker → always ok
            )
        except Exception as exc:
            logger.warning("validate_embedder_only: %s", exc)
            return False
        for warning in warnings:
            logger.warning("validate_embedder_only: %s", warning)
        return embedder_ok

    def _probe_and_configure_coreml(
        self, gpu: "GpuType"
    ) -> "tuple[list[str], str | None, bool]":
        """Probe CoreML, write config, return (provider_labels, gpu_provider, split_coreml)."""
        if self.validate_providers(["CoreMLExecutionProvider"]):
            self.configure_providers(gpu=gpu)
            self.clear_reranker_providers()  # self-heal: clear stale split config
            return ["CoreML (Apple Silicon)"], "CoreMLExecutionProvider", False
        elif self.validate_embedder_only(["CoreMLExecutionProvider"]):
            # Split: embedder on CoreML, reranker on CPU
            self.configure_providers(gpu=gpu)
            self.configure_reranker_providers([])
            return ["CoreML — text search; CPU — result ranking"], "CoreMLExecutionProvider", True
        else:
            logger.warning("CoreML validation failed — falling back to CPU")
            print("Warning: CoreML validation failed — falling back to CPU.")
            return [], None, False

    def _fe1_reprobe(self, gpu_provider: "str | None", prof: "InstallProfile", split_coreml: bool) -> None:
        """Re-validate GPU provider after model download (FE-1). No-op in split mode."""
        if gpu_provider is not None and prof.reranker is not None and not split_coreml:
            if not self.validate_providers([gpu_provider]):
                logger.warning(
                    "Model validation under provider %s failed after "
                    "download — the reranker (%r) will fall back to "
                    "CPU at runtime.",
                    gpu_provider,
                    prof.reranker,
                )
                print(
                    f"Warning: model validation failed under {gpu_provider} — "
                    "the reranker will fall back to CPU."
                )

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
    # Destructive operations — implemented by the concrete subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    def install_deps(self, gpu: GpuType) -> None: ...

    @abstractmethod
    def configure_providers(self, gpu: GpuType) -> None: ...

    @abstractmethod
    def configure_reranker_providers(self, providers: list[str]) -> None: ...

    @abstractmethod
    def clear_reranker_providers(self) -> None: ...

    @abstractmethod
    def create_data_dir(self) -> None: ...

    @abstractmethod
    def write_service_file(self) -> None: ...

    @abstractmethod
    def load_service(self) -> int: ...

    @abstractmethod
    def unload_service(self) -> int: ...

    # -- mutations extracted out of run() so dry-run cannot perform them.
    #    Real executes; Dry narrates. run() calls these unconditionally.

    @abstractmethod
    def install_lock(self) -> "contextlib.AbstractContextManager[object]": ...

    @abstractmethod
    def remove_legacy_service(self, legacy: Path) -> None: ...

    @abstractmethod
    def create_logs_dir(self) -> None: ...

    @abstractmethod
    def download_fasttext_model(self) -> None: ...

    @abstractmethod
    def prepare_db_path(self, expanded: Path) -> bool: ...

    @abstractmethod
    def persist_fresh_config(
        self, config_path: Path, profile_name: str, is_multilingual: bool, features: "WizardFeatures"
    ) -> None: ...

    @abstractmethod
    def persist_reinstall_config(
        self, config_path: Path, prof: object, profile_name: str,
        is_multilingual: bool, features: "WizardFeatures", has_edits: bool,
    ) -> None: ...

    @abstractmethod
    def write_gpu_providers_disabled(self, config_path: Path) -> None: ...

    @abstractmethod
    def write_server_key(self, server_key: str) -> None: ...

    @abstractmethod
    def preload_models(self, prof: object, gpu_provider: str | None, split_coreml: bool) -> None: ...

    @abstractmethod
    def register_and_start(self) -> int: ...

    @abstractmethod
    def delete_database(self, db_path: Path) -> bool: ...

    # ------------------------------------------------------------------
    # Orchestration (shared; mutation is delegated to the abstract methods)
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
        top_k: int | None = None,
        telemetry_retention_days: int | None = None,
        # C15 Tier 2 AI query expansion flags
        enable_hyde: bool = False,
        enable_rag_fusion: bool = False,
        # C15 Tier 2 custom server key
        server_key: str | None = None,
    ) -> int:
        """Execute the full install flow. Returns 0 on success."""
        print(self._MODE_BANNER)
        # Validate --force requires --delete-db
        if force and not delete_db:
            print("--force requires --delete-db. To force a reinstall, use both flags together.")
            return 1

        # A dry-run serializes against nothing (it writes no config/db), so it
        # skips the install lock — whose parent.mkdir would otherwise leave the
        # data dir (~/.archon-search) behind.
        with self.install_lock():
            # Step 0: legacy cleanup + log directory
            legacy = _legacy_service_path()
            if legacy.exists():
                self.remove_legacy_service(legacy)
            self.create_logs_dir()

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

            # Step 3b: fasttext license gate + model download.
            # NOT gated by skip_preload: lid.176.ftz is a ~1 MB *required* runtime
            # asset (app.py's _check_multilingual_deps hard-fails without it), not
            # one of the heavy embedder/reranker weights that --skip-preload defers.
            # The license gate is unchanged — non-interactive still requires
            # --accept-fasttext-license (SystemExit stops the install).
            if is_multilingual:
                try:
                    _prompt_fasttext_license(non_interactive, accept_fasttext_license=accept_fasttext_license)
                except SystemExit as e:
                    return int(e.code) if e.code is not None else 1
                try:
                    self.download_fasttext_model()
                except InstallError as exc:
                    # Degrade to English-only rather than aborting, so the
                    # server still boots (mirrors _revert_multilingual_flag's
                    # philosophy). The config is not written until Step 6/7/8,
                    # so setting is_multilingual=False here is the rollback:
                    # the config writer emits multilingual=false and the
                    # [multilingual] extra install is skipped. (Dry never raises.)
                    print(
                        f"Warning: fasttext model download failed: {exc}\n"
                        "Continuing in English-only mode. Re-run the wizard "
                        "to enable multilingual language detection.",
                        file=sys.stderr,
                    )
                    is_multilingual = False

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
                enable_hyde=enable_hyde if enable_hyde else None,
                enable_rag_fusion=enable_rag_fusion if enable_rag_fusion else None,
                hyde_ollama_base_url_default=self.cfg.hyde.ollama_base_url,
                rag_fusion_ollama_base_url_default=self.cfg.rag_fusion.ollama_base_url,
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
            if top_k is not None:
                features.top_k = top_k
            if telemetry_retention_days is not None:
                features.telemetry_retention_days = telemetry_retention_days
            if enable_hyde:
                features.enable_hyde = enable_hyde
            if enable_rag_fusion:
                features.enable_rag_fusion = enable_rag_fusion

            # A multilingual profile needs the [multilingual] extra (fasttext-wheel)
            # or the server hard-fails at startup. Derived from the resolved profile
            # choice, so it fires identically in interactive and non-interactive paths.
            features.install_multilingual_extra = is_multilingual

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
                if not self.prepare_db_path(_expanded_db_path):
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
                self.persist_fresh_config(config_path, profile_name, is_multilingual, features)
            else:
                # Branch C: idempotent reinstall (same profile)
                branch = "idempotent"

                # Overwrite warning: detect hand-edits before writing
                prev_profile_name = existing_cfg.profile
                prev_multilingual = existing_cfg.multilingual
                has_edits = _detect_config_hand_edits(config_path, prev_profile_name, prev_multilingual)

                # Interactive overwrite confirm is a real-mode-only prompt (not a
                # mutation); dry-run narrates it inside persist_reinstall_config.
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

                self.persist_reinstall_config(
                    config_path, prof, profile_name, is_multilingual, features, has_edits
                )

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

            # Step 8c: assert the wizard's HyDE / RAG Fusion choices persisted through
            # the write→parse→load round-trip. Surfaces a silent config-write failure
            # at install time rather than as a feature that shows enabled in the wizard
            # but disabled on the server. Skipped in dry-run (nothing was written).
            if not self.dry_run:
                try:
                    _assert_features_persisted(cfg, features)
                except InstallError as exc:
                    print(f"Install error: {exc}", file=sys.stderr)
                    _revert_query_expansion_flags(config_path, self.dry_run)
                    return 1

            # Step 9: GPU provider configuration (detection + user confirm already done in Step 2b)
            providers: list[str] = []
            # ONNX provider string configured for the Metal/CoreML GPU path, or
            # None otherwise. Drives the post-prewarm reranker re-validation in
            # Step 14 (FE-1). CUDA install-time validation is out of scope (D6
            # plan, "Out of Scope"), so the CUDA branch deliberately leaves this
            # None — no post-prewarm probe runs for CUDA.
            gpu_provider: str | None = None
            split_coreml: bool = False
            if not enable_gpu:
                # User declined GPU — write providers = [] explicitly to override any previous setting
                gpu_config_path = Path(self.config_file) if self.config_file else get_default_config_path()
                self.write_gpu_providers_disabled(gpu_config_path)
            elif not self.dry_run and gpu == GpuType.METAL:
                providers, gpu_provider, split_coreml = self._probe_and_configure_coreml(gpu)
            elif gpu == GpuType.CUDA:
                self.configure_providers(gpu=gpu)
                providers = ["CUDA"]
                # CUDA post-prewarm validation is out of scope (D6); leave
                # gpu_provider None so the FE-1 block does not probe CUDA.
            else:
                self.configure_providers(gpu=gpu)

            # Step 10: create data directory
            self.create_data_dir()

            # Step 11: disk space check
            try:
                _check_disk_space(prof)
            except InstallError as exc:
                print(str(exc))
                if features.install_graph_extra:
                    _revert_graph_enabled_flag(config_path, self.dry_run)
                if features.install_multilingual_extra:
                    _revert_multilingual_flag(config_path, self.dry_run)
                if features.enable_hyde or features.enable_rag_fusion:
                    _revert_query_expansion_flags(config_path, self.dry_run)
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
                    if features.install_graph_extra:
                        _revert_graph_enabled_flag(config_path, self.dry_run)
                    if features.install_multilingual_extra:
                        _revert_multilingual_flag(config_path, self.dry_run)
                    if features.enable_hyde or features.enable_rag_fusion:
                        _revert_query_expansion_flags(config_path, self.dry_run)
                    return 1

            # Before Step 14: install code enrichment packages if requested
            if features.install_code_extra:
                try:
                    _install_code_extra(dry_run=self.dry_run)
                except InstallError as exc:
                    print(f"Warning: code enrichment install failed: {exc}", file=sys.stderr)
                    # Non-fatal — continue

            # Before Step 14: install graph enrichment packages if requested (BE-11)
            if features.install_graph_extra:
                try:
                    _install_graph_extra(dry_run=self.dry_run)
                except InstallError as exc:
                    print(f"Warning: graph enrichment install failed: {exc}", file=sys.stderr)
                    # Non-fatal — continue, but roll back the already-written
                    # graph.enabled=true config flag (C2-A / C3-A-1 fix).
                    _revert_graph_enabled_flag(config_path, self.dry_run)

            # Before Step 14: install multilingual package if a multilingual profile
            # was selected (2026-07-15-040). Same shape as the graph install above:
            # non-fatal, and roll back the already-written multilingual=true flag on
            # failure so the server starts English-only rather than crashing.
            if features.install_multilingual_extra:
                try:
                    _install_multilingual_extra(dry_run=self.dry_run)
                except InstallError as exc:
                    print(f"Warning: multilingual install failed: {exc}", file=sys.stderr)
                    _revert_multilingual_flag(config_path, self.dry_run)

            # Before Step 14: install provider packages for enabled HyDE / RAG Fusion.
            # Same shape as the graph/multilingual installs above: non-fatal, and
            # roll back the already-written enabled=true flags on failure so the next
            # server start does not hard-fail on the missing provider package
            # (the anthropic guard in app.py's _check_provider_deps).
            # ponytail: revert wiring is triplicated across the disk-space/decline/install exit paths (mirrors the pre-existing graph & multilingual reverts); consolidate into one _revert_optional_feature_flags helper if a 4th revertable feature is added.
            if features.enable_hyde or features.enable_rag_fusion:
                _failed_sections = _install_query_expansion_extras(features, dry_run=self.dry_run)
                if _failed_sections:
                    _revert_query_expansion_flags(config_path, self.dry_run, sections=tuple(_failed_sections))

            # Before Step 14b: create .secrets.env when AI query expansion is enabled
            if features.enable_hyde or features.enable_rag_fusion:
                try:
                    _secrets_env_path = get_data_dir() / ".secrets.env"
                    _created = _create_secrets_env(_secrets_env_path, dry_run=self.dry_run)
                    if _created:
                        print(
                            f"  Created:    {_secrets_env_path}\n"
                            "  Add ANTHROPIC_API_KEY=<key> to this file so the managed service\n"
                            "  can source it at start time for AI query expansion."
                        )
                except OSError as exc:
                    print(f"Warning: could not create .secrets.env: {exc}", file=sys.stderr)

            # Before Step 14b: write custom server key if provided
            if server_key is not None:
                self.write_server_key(server_key)

            # Step 14: pre-warm
            if not skip_preload:
                try:
                    self.preload_models(prof, gpu_provider, split_coreml)
                except InstallError as exc:
                    # Real-only: dry never raises, so this rollback runs only when
                    # a real download fails.
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
            rc = self.register_and_start()
            if rc != 0:
                print(f"Service start returned exit code {rc}.", file=sys.stderr)
                return rc

            # Step 16: wait for readiness
            if not self.dry_run:
                _svc_timeout = _compute_svc_timeout(features.eager_load_embedders, prof.download_mb)
                ready = self._wait_for_service(timeout=_svc_timeout)
                if not ready:
                    print(f"Warning: Search service did not become ready within {_svc_timeout} seconds.")
                    return 1

            # Step 16b: next steps guidance (non-dry-run only)
            if not self.dry_run:
                _print_next_steps(cfg.host, cfg.port, str(_key_manager.get_key_file()))

            # Step 17: completion message
            if not self.dry_run:
                if server_key is not None:
                    # Key was already written at step 14b — report it directly so
                    # load_or_generate_key() doesn't overwrite the key file via persist_key.
                    _api_key = server_key
                    _key_source = f"file: {get_key_file()}"
                else:
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
            _start_cfg = load_config(config_path)
            _start_prof = get_profile(_start_cfg.profile or "minimal", _start_cfg.multilingual)
            _svc_timeout = _compute_svc_timeout(_start_cfg.eager_load_embedders, _start_prof.download_mb)
            ready = self._wait_for_service(timeout=_svc_timeout)
            if not ready:
                click.echo(
                    f"Warning: Search service did not become ready within {_svc_timeout} seconds.",
                    err=True,
                )
                return 1

        if self.dry_run:
            click.echo("[DRY RUN] Would register and start the archon-search service.")
        else:
            click.echo("archon-search service registered and running.")
        return 0

    def run_uninstall(self, delete_db: bool = False) -> int:
        """Stop and unregister the search service. Optionally delete the database."""
        rag_svc = get_search_service()
        rag_svc.stop(dry_run=self.dry_run)
        rag_svc.unregister(dry_run=self.dry_run)

        db_deleted = False
        if delete_db:
            db_path = Path(self.cfg.db_path).expanduser()
            if db_path.exists():
                db_deleted = self.delete_database(db_path)

        if db_deleted:
            print(
                "Search service uninstalled. Search database deleted. Your archon-search.toml settings are preserved."
            )
        else:
            print(
                "Search service uninstalled. Your search settings are preserved in archon-search.toml."
            )
        return 0


class DryRunInstaller(BaseInstaller):
    """Rehearsal installer: describes destructive actions without performing them."""

    dry_run = True
    _MODE_BANNER = "=== DRY-RUN MODE: No changes will be made ==="

    def install_deps(self, gpu: GpuType) -> None:
        print("[DRY RUN] Would install search dependencies.")

    def configure_providers(self, gpu: GpuType) -> None:
        print("[DRY RUN] Would configure GPU execution providers.")

    def configure_reranker_providers(self, providers: list[str]) -> None:
        print("[DRY RUN] Would configure reranker execution providers.")

    def clear_reranker_providers(self) -> None:
        print("[DRY RUN] Would clear reranker execution providers.")

    def create_data_dir(self) -> None:
        print("[DRY RUN] Would create the search data directory.")

    def write_service_file(self) -> None:
        svc = get_search_service()
        svc.pre_activate_cleanup(dry_run=True)
        svc.register(dry_run=True)

    def load_service(self) -> int:
        return get_search_service().start(dry_run=True)

    def unload_service(self) -> int:
        return get_search_service().stop(dry_run=True)

    # -- extracted mutations: narrate, never touch the system -------------

    def install_lock(self) -> "contextlib.AbstractContextManager[object]":
        # A dry-run writes no config/db, so it serializes against nothing and
        # skips the real lock — whose parent.mkdir would leave the data dir behind.
        return contextlib.nullcontext()

    def remove_legacy_service(self, legacy: Path) -> None:
        print(f"[DRY RUN] Would remove legacy service file: {legacy}")

    def create_logs_dir(self) -> None:
        pass

    def download_fasttext_model(self) -> None:
        print("[DRY RUN] Would download fasttext model.")

    def prepare_db_path(self, expanded: Path) -> bool:
        # Preview without mutating: probe the nearest existing ancestor for
        # writability so dry-run surfaces the same failure a real run would hit.
        probe = expanded
        while not probe.exists():
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            print(
                f"Error: db_path {expanded} is not writable. Choose a writable directory.",
                file=sys.stderr,
            )
            return False
        print(f"[DRY RUN] Would create db_path directory: {expanded}")
        return True

    def persist_fresh_config(
        self, config_path: Path, profile_name: str, is_multilingual: bool, features: "WizardFeatures"
    ) -> None:
        print(f"[DRY RUN] Would write config: {config_path}")
        print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")

    def persist_reinstall_config(
        self, config_path: Path, prof: object, profile_name: str,
        is_multilingual: bool, features: "WizardFeatures", has_edits: bool,
    ) -> None:
        if has_edits:
            print(
                "[DRY RUN] Would prompt: Existing config has custom values."
                " Overwrite with profile defaults?"
            )
        print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")
        print(f"[DRY RUN] Would overwrite config: {config_path}")

    def write_gpu_providers_disabled(self, config_path: Path) -> None:
        pass

    def write_server_key(self, server_key: str) -> None:
        print(f"[dry-run] Would write server key to {get_key_file()}.")

    def preload_models(self, prof: object, gpu_provider: str | None, split_coreml: bool) -> None:
        print(f"[DRY RUN] Would download models (~{prof.download_mb} MB).")

    def register_and_start(self) -> int:
        print("[DRY RUN] Would register and start the search service.")
        return 0

    def delete_database(self, db_path: Path) -> bool:
        print(f"[dry-run] Would delete database at {db_path}.")
        return False


class RealInstaller(BaseInstaller):
    """Executing installer: performs the real filesystem, package, and service work."""

    dry_run = False
    _MODE_BANNER = "=== REAL MODE: System will be modified ==="

    def install_deps(self, gpu: GpuType) -> None:
        """Install search dependencies into the same Python that runs this process."""
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

    def configure_providers(self, gpu: GpuType) -> None:
        """Write providers list to [database] section via tomlkit based on gpu type.

        - GpuType.CUDA: write ["CUDAExecutionProvider"]
        - GpuType.METAL: write ["CoreMLExecutionProvider"]
        - GpuType.NONE: no-op
        """
        _provider_map = {
            GpuType.CUDA: "CUDAExecutionProvider",
            GpuType.METAL: "CoreMLExecutionProvider",
        }
        target_provider = _provider_map.get(gpu)
        if target_provider is None:
            return

        config_path = Path(self.config_file) if self.config_file else get_default_config_path()
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

    def configure_reranker_providers(self, providers: list[str]) -> None:
        """Write reranker_providers to [database] section (empty list = CPU)."""
        config_path = Path(self.config_file) if self.config_file else get_default_config_path()
        if not config_path.exists():
            logger.warning(
                "Config file %s not found — skipping reranker_providers config",
                config_path,
            )
            return
        doc = tomlkit.parse(config_path.read_text())
        # Check existing value first to avoid unnecessary TOML rewrites (comment reflow risk)
        db = doc.get("database", {})
        if isinstance(db, dict) and db.get("reranker_providers") == providers:
            return
        if "database" not in doc:
            doc["database"] = tomlkit.table()
        arr = tomlkit.array()
        arr.extend(providers)
        doc["database"]["reranker_providers"] = arr
        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())

    def clear_reranker_providers(self) -> None:
        """Remove reranker_providers from [database] if present (self-heal on upgrade)."""
        config_path = Path(self.config_file) if self.config_file else get_default_config_path()
        if not config_path.exists():
            return
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        # Only clear the stale auto-written split marker ([]); preserve user-set values.
        if "database" in doc and doc["database"].get("reranker_providers") == []:
            del doc["database"]["reranker_providers"]
            atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())

    def create_data_dir(self) -> None:
        """Create the search database directory."""
        db_path = Path(self.cfg.db_path).expanduser()
        db_path.mkdir(parents=True, exist_ok=True)

    def write_service_file(self) -> None:
        """Stop legacy service then register the new search service."""
        svc = get_search_service()
        svc.pre_activate_cleanup(dry_run=False)
        svc.register(dry_run=False)

    def load_service(self) -> int:
        """Delegate to get_search_service().start()."""
        return get_search_service().start(dry_run=False)

    def unload_service(self) -> int:
        """Delegate to get_search_service().stop()."""
        return get_search_service().stop(dry_run=False)

    # -- extracted mutations: perform the real work ----------------------

    def install_lock(self) -> "contextlib.AbstractContextManager[object]":
        return _acquire_install_lock()

    def remove_legacy_service(self, legacy: Path) -> None:
        _remove_legacy_service(legacy)

    def create_logs_dir(self) -> None:
        (get_data_dir() / "logs").mkdir(parents=True, exist_ok=True)

    def download_fasttext_model(self) -> None:
        _download_fasttext_model(get_data_dir() / "models")

    def prepare_db_path(self, expanded: Path) -> bool:
        try:
            expanded.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Error: could not create db_path directory {expanded}: {exc}", file=sys.stderr)
            return False
        if not os.access(expanded, os.W_OK):
            print(
                f"Error: db_path {expanded} is not writable. Choose a writable directory.",
                file=sys.stderr,
            )
            return False
        return True

    def persist_fresh_config(
        self, config_path: Path, profile_name: str, is_multilingual: bool, features: "WizardFeatures"
    ) -> None:
        # config_path.parent IS the data dir on the default invocation, so this
        # mkdir must stay gated behind RealInstaller or a dry-run recreates it.
        config_path.with_suffix(config_path.suffix + ".tmp").unlink(missing_ok=True)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(config_path, _profile_toml(profile_name, is_multilingual, features).encode())
        shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))

    def persist_reinstall_config(
        self, config_path: Path, prof: object, profile_name: str,
        is_multilingual: bool, features: "WizardFeatures", has_edits: bool,
    ) -> None:
        shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))
        _write_profile_config(config_path, prof, profile_name, is_multilingual, features=features)
        print(f"  Backup:     {config_path.with_suffix('.toml.bak')}")

    def write_gpu_providers_disabled(self, config_path: Path) -> None:
        if not config_path.exists():
            return
        gpu_doc = tomlkit.parse(config_path.read_text())
        if "database" not in gpu_doc:
            gpu_doc.add("database", tomlkit.table())
        gpu_doc["database"]["providers"] = tomlkit.array()
        atomic_write_bytes(config_path, tomlkit.dumps(gpu_doc).encode())

    def write_server_key(self, server_key: str) -> None:
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

    def preload_models(self, prof: object, gpu_provider: str | None, split_coreml: bool) -> None:
        print("[4/5] Downloading models...")
        _prewarm_models(prof)
        self._fe1_reprobe(gpu_provider, prof, split_coreml)

    def register_and_start(self) -> int:
        print("[5/5] Starting search service...")
        self.write_service_file()
        return self.load_service()

    def delete_database(self, db_path: Path) -> bool:
        rmtree(db_path)
        print(f"Deleted search database at {db_path}.")
        return True


def create_installer(config_file: str | None = None, dry_run: bool = False) -> InstallerProtocol:
    """Return the installer for *dry_run*: DryRunInstaller if true, else RealInstaller."""
    if dry_run:
        return DryRunInstaller(config_file)
    return RealInstaller(config_file)
