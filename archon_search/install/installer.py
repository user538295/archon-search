"""Installer strategy hub: BaseInstaller plus the dry-run and real strategies.

Split into a read-only ``BaseInstaller`` (ABC) with two concrete strategies:
``RealInstaller`` executes destructive actions, ``DryRunInstaller`` stubs them.
Use ``create_installer(config_file, dry_run)`` to obtain the right one.
"""
from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from shutil import rmtree
from typing import Protocol, runtime_checkable

import click
import tomlkit

from archon_search._durable_io import atomic_write_bytes, ensure_private_dir
from archon_search.config import (
    SearchConfig,
    get_default_config_path,
    load_config,
    removed_keys_in,
    warn_if_legacy_config_ignored,
)
from archon_search.key_manager import get_key_file, load_or_generate_key
from archon_search.paths import get_data_dir, get_fasttext_models_dir
from archon_search.pipeline import create_pipeline
from archon_search.platform.runtime import get_runtime, get_search_service
from archon_search.platform.service import get_secrets_env_file
from archon_search.platform.types import GpuType
from archon_search.profiles import InstallProfile, get_profile
from archon_search.torch_runtime import (
    AUTO_DEVICE,
    CPU_DEVICE,
    DEVICE_SETTINGS,
    resolve_device,
)

from .config_writer import (
    WizardFeatures,
    _assert_features_persisted,
    _detect_config_hand_edits,
    _profile_toml,
    _record_wizard_values,
    _revert_graph_enabled_flag,
    _revert_multilingual_flag,
    _write_profile_config,
)
from .device_probe import probe_device_availability, probe_device_validation
from .errors import InstallError, NeedsForceDeleteError
from .extras import (
    _cuda_visible_in_subprocess,
    _install_code_extra,
    _install_cuda_torch,
    _install_graph_extra,
    _install_multilingual_extra,
    _install_query_expansion_extras,
    _revert_graph_enrichment_flags,
    _revert_query_expansion_flags,
    _torch_cuda_available,
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
    _planned_total_bytes,
    _prewarm_models,
    _prewarm_timeout,
)
from .prompts import ask_yes_no
from .provisioning import BYTES_PER_MB, TORCH_DEVICE_CPU
from .render import _print_next_steps, _render_provision_failure, _render_summary

# Deliberately unused by run(): these names are the patch seam for the S194/S201/S202
# regression guards asserting run() never calls the race-prone legacy cleanup.
from .service_ops import (  # noqa: F401 — patch seam, see comment above
    _create_secrets_env,
    _legacy_service_path,
    _remove_legacy_service,
)
from .wizard import (
    _graph_candidate_device,
    _prompt_gpu_confirm,
    _prompt_graph_accelerator,
    _prompt_multilingual,
    _prompt_optional_features,
    _select_profile,
)

logger = logging.getLogger(__name__)


_WAIT_FOR_SERVICE_TIMEOUT = 60
# Import-time crash markers that identify a first-launch environment race (e.g.
# a service started before its freshly-installed venv is fully materialised).
# Their presence in the service log lets the readiness gate tolerate one
# supervisor crash-and-restart cycle instead of reporting a bare timeout.
_STARTUP_CRASH_MARKERS = ("ModuleNotFoundError", "ImportError")
# TOML sections configure_device can target.
_DATABASE_SECTION = "database"
_GRAPH_SECTION = "graph"
# The key configure_device writes in either section.
_DEVICE_KEY = "device"
# Printed after the config write when an explicit --telemetry-retention-days was written
# while the run's telemetry answer is not "on": the value is kept but inert until telemetry
# is enabled. {prefix}/{outcome} narrate a real write or a dry-run rehearsal.
_RETENTION_INERT_WARNING = (
    "{prefix}Warning: [telemetry].retention_days = {days} {outcome}, but telemetry is not enabled, "
    "so it has no effect until you enable telemetry (--telemetry, or answer y at the telemetry prompt)."
)
# The operator guide the failed-CUDA warning points at: installing a CUDA build of PyTorch
# for each install method. A URL, because pip/uv-tool installs ship no docs.
_REPO_BLOB_BASE = "https://github.com/user538295/archon-search/blob/main"
_CUDA_GUIDE_DOC = "Documentation/UserManual/10_installation.md"
_CUDA_GUIDE_HEADING = "NVIDIA GPU: run embedding and reranking on CUDA"


def _github_heading_anchor(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation except hyphens/underscores/spaces, spaces → hyphens."""
    return "".join(c for c in heading.lower() if c.isalnum() or c in "-_ ").replace(" ", "-")


_CUDA_GUIDE_URL = f"{_REPO_BLOB_BASE}/{_CUDA_GUIDE_DOC}#{_github_heading_anchor(_CUDA_GUIDE_HEADING)}"

# The summary's `Device:` labels, and the accelerator name the fallback warning uses.
_CPU_LABEL = "CPU"
_ACCELERATOR_LABELS: dict[GpuType, str] = {GpuType.METAL: "Apple GPU (mps)", GpuType.CUDA: "CUDA"}
_ACCELERATOR_NAMES: dict[GpuType, str] = {GpuType.METAL: "Apple GPU", GpuType.CUDA: "CUDA"}
# The torch devices `auto` may resolve to when an accelerator is usable.
_ACCELERATOR_DEVICES = frozenset({"mps", "cuda"})

# The summary is shown before Step 9's accelerator check, so its `Device:` line can only
# name what is about to be checked; Step 9 prints the outcome afterwards.
_GPU_CHECK_PENDING_NOTE = "— checked after you confirm; CPU if it fails"


def _planned_device_labels(gpu: GpuType, enable_gpu: bool, dry_run: bool) -> list[str]:
    """The summary's `Device:` labels before Step 9 has probed anything.

    A real Metal/CUDA install names the accelerator it will check, flagged as pending.
    A dry run probes nothing, so it names the accelerator it would write, unflagged.
    Anything else runs on CPU.
    """
    label = _ACCELERATOR_LABELS.get(gpu) if enable_gpu else None
    if label is None:
        return [_CPU_LABEL]
    return [label if dry_run else f"{label} {_GPU_CHECK_PENDING_NOTE}"]


def _require_device_setting(device: str) -> None:
    """Raise ValueError unless *device* is one of ``DEVICE_SETTINGS``."""
    if device not in DEVICE_SETTINGS:
        raise ValueError(f"unknown device {device!r}; expected one of: {', '.join(DEVICE_SETTINGS)}")


def _compute_svc_timeout(eager_load: bool, total_bytes: int) -> int:
    """Return the service-ready wait timeout in seconds.

    The warm-up task now runs on every startup; eager_load_embedders only
    widens its scope to every distinct per-collection model, which can take
    several minutes for large models.  Scale to ~100ms per MB of the
    planned-download total, capped at 10 minutes. With eager load off, only
    the default embedder and reranker are warmed, so the default 60 s wait
    is sufficient.
    # ponytail: linear scale is fine; a smarter heuristic adds no value here
    """
    if not eager_load:
        return _WAIT_FOR_SERVICE_TIMEOUT
    return min(600, max(_WAIT_FOR_SERVICE_TIMEOUT, total_bytes // (10 * BYTES_PER_MB)))


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

    def detect_gpu(self) -> GpuType: ...
    def configure_device(self, device: str, *, section: str = ...) -> None: ...
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
    #: True only for the wizard: config reads ignore the removed ``providers`` keys it is about to rewrite.
    _legacy_config: bool = False

    def __init__(self, config_file: str | None = None, *, legacy_config: bool = False) -> None:
        """*legacy_config* lets every config read ignore the removed ``providers`` keys (the wizard rewrites the file)."""
        self.config_file = config_file
        self._legacy_config = legacy_config
        cfg = self._load_config(self.config_path)
        self.cfg: SearchConfig = cfg

    def _load_config(self, path: Path) -> SearchConfig:
        return load_config(path, drop_removed_keys=self._legacy_config)

    def _announce_removed_keys(self, config_path: Path) -> None:
        """Print one line naming the removed keys *config_path* holds and that the wizard drops."""
        if not self._legacy_config or not config_path.exists():
            return
        removed = removed_keys_in(tomlkit.parse(config_path.read_text(encoding="utf-8")))
        if removed:
            verb = "Would drop" if self.dry_run else "Dropping"
            print(f"{verb} removed config keys {', '.join(removed)} (replaced by device; see BREAKING.md)")

    @property
    def config_path(self) -> Path:
        """The config file this installer reads and writes: ``--config``, else the default path.

        Always absolute: it is baked into the service unit / plist, whose working
        directory is not the one the wizard ran in. Symlinks are kept, not resolved,
        so repointing a link later still redirects the service.
        """
        return Path(self.config_file).expanduser().absolute() if self.config_file else get_default_config_path()

    # ------------------------------------------------------------------
    # Read-only methods (no filesystem / service / subprocess mutation)
    # ------------------------------------------------------------------

    def detect_gpu(self) -> GpuType:
        """Return the GPU type detected by the platform runtime."""
        return get_runtime().detect_gpu_type()

    def _accelerator_validated(self) -> bool:
        """True when PyTorch's ``auto`` device is an accelerator and both models pass on it.

        ``auto`` resolving to ``cpu`` means this PyTorch build lacks the accelerator, so
        nothing is probed. Otherwise delegates to :func:`validate_device` so the wizard and
        the background startup check probe identically: the embedder and the reranker must
        both pass (a disabled reranker, ``reranker_model == ""``, passes with no probe).

        Never raises — the caller falls back to CPU. Failures and probe warnings are logged.
        """
        try:
            # Lazy: model_validation pulls in the model backends; keep that off CLI startup.
            from archon_search.model_validation import validate_device

            if resolve_device(AUTO_DEVICE) not in _ACCELERATOR_DEVICES:
                logger.warning("device validation: PyTorch found no usable accelerator")
                return False
            embedder_ok, reranker_ok, warnings = validate_device(
                AUTO_DEVICE, self.cfg.embedding_model, self.cfg.reranker_model
            )
        except Exception as exc:  # noqa: BLE001 — device validation runs third-party backends; any failure means "not validated"
            logger.warning("device validation failed: %s", exc)
            return False
        for warning in warnings:
            logger.warning("device validation: %s", warning)
        return embedder_ok and reranker_ok

    def _probe_and_configure_device(self, gpu: GpuType) -> list[str]:
        """Step 9: write ``[database].device`` for the GPU the user accepted; return the summary labels.

        *gpu* is the accepted accelerator — ``GpuType.NONE`` when none was detected, the
        prompt was declined or ``--disable-gpu`` was given — and then ``cpu`` is written.
        A dry run probes nothing: it narrates the ``auto`` write and, for CUDA on
        linux/x86_64, the torch swap. Otherwise ``auto`` is written only when both models
        pass on an accelerator; any other outcome writes ``cpu`` with the fallback warning,
        because a re-run keeps the previous ``[database].device``.

        On linux/x86_64 with CUDA accepted and ``torch.cuda.is_available()`` false, the
        ``+cu126`` torch swap is the enabling step. This process keeps the torch it
        already imported, so the swap is verified in a fresh interpreter instead.
        """
        label = _ACCELERATOR_LABELS.get(gpu)
        if label is None:
            self.configure_device(CPU_DEVICE)
            return [_CPU_LABEL]
        swap_platform = (
            gpu == GpuType.CUDA and sys.platform.startswith("linux") and platform.machine() == "x86_64"
        )
        if self.dry_run:
            self.configure_device(AUTO_DEVICE)
            if swap_platform:
                _install_cuda_torch(dry_run=True)
            return [label]
        if swap_platform and not _torch_cuda_available():
            _install_cuda_torch(dry_run=False)
            validated = _cuda_visible_in_subprocess()
        else:
            validated = self._accelerator_validated()
        if validated:
            self.configure_device(AUTO_DEVICE)
            return [label]
        self._print_accelerator_fallback(gpu)
        self.configure_device(CPU_DEVICE)
        return [_CPU_LABEL]

    @staticmethod
    def _print_accelerator_fallback(gpu: GpuType) -> None:
        """Warn that the accepted accelerator failed validation; CUDA adds why and where to fix it."""
        name = _ACCELERATOR_NAMES[gpu]
        logger.warning("%s validation failed — falling back to CPU", name)
        print(f"Warning: {name} validation failed — falling back to CPU.")
        if gpu == GpuType.CUDA:
            print(
                "  Text search and result ranking will run on CPU: PyTorch found no usable CUDA "
                "device (a CPU-only PyTorch build, or no compatible NVIDIA driver), or a model failed its check."
            )
            print(
                "  To run them on the GPU, install a CUDA build of PyTorch and an NVIDIA driver "
                "it supports, then re-run the wizard."
            )
            print(f"  Commands for each install method: {_CUDA_GUIDE_URL}")

    def _warn_if_retention_inert(self, features: WizardFeatures) -> None:
        """After the config write, warn on stderr that an explicit retention_days is inert
        because the run's telemetry answer is not "on" (config_writer writes it regardless)."""
        if features.telemetry_retention_days is not None and not features.enable_telemetry:
            prefix, outcome = ("[dry-run] ", "would be written") if self.dry_run else ("", "was written")
            print(
                _RETENTION_INERT_WARNING.format(
                    prefix=prefix, days=features.telemetry_retention_days, outcome=outcome
                ),
                file=sys.stderr,
            )

    async def _bootstrap_collections(self) -> None:
        """Sync configured collections into the search store."""
        from archon_search.progress import IndexingStateStore
        from archon_search.sync import SearchCollectionSync

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
                device=self.cfg.device,
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
        except Exception:  # noqa: BLE001 — any failure to reach the health endpoint means the service is not running
            return False

    def _service_log_offset(self) -> int:
        """Return the current byte size of the service log (0 if absent/unreadable).

        Captured *before* the service is (re)started so :meth:`_service_log_crash`
        only ever sees crashes from this launch — never a stale crash line from a
        previous same-day run appended to a rotated-daily log.
        """
        log_file = getattr(self.cfg, "log_file", None)
        if not log_file:
            return 0
        try:
            return Path(log_file).expanduser().stat().st_size
        except OSError:
            return 0

    def _service_log_crash(self, since_offset: int = 0) -> str | None:
        """Return the last import-time crash line written after ``since_offset``.

        A fresh install can launch the service before its venv is fully
        materialised; the first launch then dies on ``ModuleNotFoundError`` /
        ``ImportError`` and the supervisor relaunches it. Only bytes after
        ``since_offset`` are scanned, so a stale crash from an earlier run cannot
        trigger the readiness gate. Returns the last matching line so the gate can
        tolerate the restart and surface the cause; returns ``None`` when the log
        is absent, unreadable, or shows no fresh crash.
        """
        log_file = getattr(self.cfg, "log_file", None)
        if not log_file:
            return None
        try:
            with Path(log_file).expanduser().open("r", errors="replace") as fh:
                fh.seek(since_offset)
                text = fh.read()
        except OSError:
            return None
        crash_line: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(marker) for marker in _STARTUP_CRASH_MARKERS):
                crash_line = stripped
        return crash_line

    def _wait_for_service(
        self, timeout: int = _WAIT_FOR_SERVICE_TIMEOUT, log_offset: int = 0
    ) -> bool:
        """Poll HTTP health endpoint until ready or timeout. Returns True if up.

        Tolerates a single supervisor crash-and-restart cycle: a first launch
        against a not-yet-materialised environment crashes on import and is
        relaunched by launchd/systemd, but the crash plus the restart throttle
        can consume the whole budget even when the second launch becomes healthy
        well inside it. When such a startup crash is visible in the service log,
        the deadline is extended once so the poll survives the restart; if the
        service still never comes up, the crash is surfaced instead of a bare
        timeout.
        """
        deadline = time.monotonic() + timeout
        extended = False
        print("Waiting for search service", end="", flush=True)
        try:
            while time.monotonic() < deadline:
                if self._is_service_running():
                    print(" ready.")
                    return True
                if not extended and self._service_log_crash(log_offset):
                    # First launch crashed; grant one more budget for the restart.
                    deadline += timeout
                    extended = True
                print(".", end="", flush=True)
                time.sleep(1)
            print(" timed out.")
            crash = self._service_log_crash(log_offset)
            if crash:
                print(f"  Last service-log error: {crash}")
            return False
        except KeyboardInterrupt:
            print()
            raise

    # ------------------------------------------------------------------
    # Destructive operations — implemented by the concrete subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    def configure_device(self, device: str, *, section: str = _DATABASE_SECTION) -> None: ...

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
    def install_lock(self) -> contextlib.AbstractContextManager[object]: ...

    @abstractmethod
    def create_logs_dir(self) -> None: ...

    @abstractmethod
    def download_fasttext_model(self) -> None: ...

    @abstractmethod
    def prepare_db_path(self, expanded: Path) -> bool: ...

    @abstractmethod
    def persist_fresh_config(
        self, config_path: Path, profile_name: str, is_multilingual: bool, features: WizardFeatures
    ) -> None: ...

    @abstractmethod
    def persist_reinstall_config(
        self, config_path: Path, prof: object, profile_name: str,
        is_multilingual: bool, features: WizardFeatures, has_edits: bool,
    ) -> None: ...

    @abstractmethod
    def record_wizard_values(self, config_path: Path) -> None: ...

    @abstractmethod
    def write_server_key(self, server_key: str) -> None: ...

    @abstractmethod
    def preload_models(
        self, prof: InstallProfile, install_graph_extra: bool = False, graph_device: str = CPU_DEVICE,
    ) -> str | None: ...

    @abstractmethod
    def register_and_start(self) -> int: ...

    @abstractmethod
    def delete_database(self, db_path: Path) -> bool: ...

    # ------------------------------------------------------------------
    # Orchestration (shared; mutation is delegated to the abstract methods)
    # ------------------------------------------------------------------

    def _configure_graph_device(
        self,
        candidate: str,
        availability: str,
        prewarm_device: str | None,
        non_interactive: bool,
    ) -> None:
        """Settle ``[graph].device`` from the two-stage device probe (S31/S32).

        The accelerator is offered — with itself as the default — only when
        stage 1 said it was available AND stage 2 (riding the pre-warm) resolved
        the model onto it. Pre-warm not having run, or either stage failing,
        shows no prompt at all and settles on CPU silently: the reason is logged at
        WARNING and never raised, and never printed as wizard UI. "Silently" means no
        prompt and no exception — not invisible: the CLI configures no logging, so the
        WARNING reaches the operator's stderr via ``logging.lastResort``, which is what
        makes S33's category reach them at all. A proven failure — which
        includes an accelerator that was asked for and that the pre-warm never landed
        on — writes ``cpu`` outright. Only the genuinely empty answer (nothing was asked
        for and nothing came back) writes nothing at all, since unset already reads as
        CPU and there is no evidence to justify overwriting an operator's own value.
        """
        result = probe_device_validation(candidate, prewarm_device)
        if result.not_yet_validated:
            # No evidence either way. Unset already means CPU (GraphConfig.device defaults
            # to cpu), so writing nothing gives a fresh install the same CPU outcome
            # without clobbering a value an operator set by hand.
            logger.warning("Graph accelerator not validated (pre-warm did not run) — leaving CPU.")
            return
        chosen = CPU_DEVICE
        if result.failure is not None:
            logger.warning(
                "Graph accelerator not validated (%s) — writing [graph].device = 'cpu'.",
                result.failure,
            )
        elif (
            availability != TORCH_DEVICE_CPU
            and result.device != TORCH_DEVICE_CPU
            and _prompt_graph_accelerator(non_interactive, result.device)
        ):
            chosen = candidate
        self.configure_device(chosen, section=_GRAPH_SECTION)

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
        # FE-5: [graph].device for non-interactive installs
        graph_device: str | None = None,
    ) -> int:
        """Execute the full install flow. Returns 0 on success."""
        print(self._MODE_BANNER)
        warn_if_legacy_config_ignored(self.config_file)
        # Validate --force requires --delete-db
        if force and not delete_db:
            print("--force requires --delete-db. To force a reinstall, use both flags together.")
            return 1
        # Fail before any download or config write if the service could never be
        # registered (Step 15 always registers): a path the platform refuses, or an
        # unsupported platform (NotImplementedError is a RuntimeError).
        # write_service_file() re-checks right before stopping the service.
        try:
            get_search_service().validate_registration(config_path=str(self.config_path))
        except RuntimeError as exc:
            print(f"Error during install: {exc}", file=sys.stderr)
            return 1

        # A dry-run serializes against nothing (it writes no config/db), so it
        # skips the install lock — whose parent.mkdir would otherwise leave the
        # data dir (~/.archon-search) behind.
        with self.install_lock(), contextlib.ExitStack() as on_exit:
            # Step 0: log directory. The legacy-service cleanup is deliberately
            # NOT done here: removing it before the reinstall guard (Step 5) or
            # any other abort point would dismantle a running service that the
            # wizard then refuses to replace, leaving it unrecoverable by
            # `start`. It runs just before Step 15, once the install is
            # committed to registering the new service.
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
                except (InstallError, OSError) as exc:
                    # Degrade to English-only rather than aborting, so the
                    # server still boots (mirrors _revert_multilingual_flag's
                    # philosophy). The config is not written until Step 6/7/8,
                    # so setting is_multilingual=False here is the rollback:
                    # the config writer emits multilingual=false and the
                    # [multilingual] extra install is skipped. (Dry never raises.)
                    # OSError is caught alongside InstallError because the mkdir,
                    # chmod, disk_usage and unlink calls that precede the download
                    # raise it bare — uncaught, they crash the wizard on a
                    # read-only models dir instead of degrading (S11).
                    kind = getattr(exc, "kind", None)
                    # The sanitized copy deliberately drops the exception text, and
                    # only two of the raise sites log it themselves — so record it
                    # here or the byte counts and paths are lost with no diagnostic.
                    logger.warning(
                        "fasttext model provisioning failed (%s)", kind, exc_info=exc
                    )
                    print(_render_provision_failure(kind), file=sys.stderr)
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
                # --enable-hyde/--enable-rag-fusion are presence-only (no --no-enable-*): absent → None = never answered.
                enable_hyde=enable_hyde if enable_hyde else None,
                enable_rag_fusion=enable_rag_fusion if enable_rag_fusion else None,
                hyde_ollama_base_url_default=self.cfg.hyde.ollama_base_url,
                rag_fusion_ollama_base_url_default=self.cfg.rag_fusion.ollama_base_url,
                graph_ollama_base_url_default=self.cfg.graph.ollama_base_url,
                hyde_llama_cpp_base_url_default=self.cfg.hyde.llama_cpp_base_url,
                rag_fusion_llama_cpp_base_url_default=self.cfg.rag_fusion.llama_cpp_base_url,
                graph_llama_cpp_base_url_default=self.cfg.graph.llama_cpp_base_url,
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
            config_path = self.config_path
            self._announce_removed_keys(config_path)

            # Step 5: reinstall check
            db_path = Path(self.cfg.db_path).expanduser()
            reindex_confirmed = False
            if config_path.exists():
                existing_cfg = self._load_config(config_path)
                db_path = Path(existing_cfg.db_path).expanduser()
                try:
                    _check_reinstall_guard(existing_cfg, prof, profile_name, is_multilingual)
                except NeedsForceDeleteError as exc:
                    if self.dry_run:
                        print(f"[dry-run] Warning: {exc} (proceeding in dry-run mode; pass --force --delete-db to apply)")
                    elif force and delete_db:
                        pass  # Branch A below performs the rebuild
                    elif non_interactive:
                        # Automation needs the failure signal plus a copy-pasteable command.
                        print(str(exc))
                        return 1
                    else:
                        # Interactive: offer to rebuild the index now instead of dead-ending.
                        print(
                            f"Switching to {prof.embedder} (chunk_size={prof.chunk_size}) changes the "
                            f"embedding model and requires rebuilding the index. The existing index "
                            f"({existing_cfg.embedding_model}, chunk_size={existing_cfg.chunk_size}) and "
                            "its data will be permanently deleted."
                        )
                        try:
                            response = input("Rebuild the index now? Type 'yes' to proceed: ")
                        except EOFError:
                            response = ""
                        if response != "yes":
                            print("Aborted. Existing configuration and index kept unchanged.")
                            return 0
                        force = True
                        delete_db = True
                        reindex_confirmed = True

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
                        non_interactive or reindex_confirmed, dry_run=self.dry_run, features=features
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
                    elif not ask_yes_no(
                        "Existing config has custom values. Overwrite with profile defaults? [y/n]: ",
                        eof_default=False,
                    ):
                        print("Installation aborted.")
                        return 1

                self.persist_reinstall_config(
                    config_path, prof, profile_name, is_multilingual, features, has_edits
                )

            self._warn_if_retention_inert(features)

            # Every exit from here on — success, abort, failure or exception — records what
            # the wizard left in the config, after any flag revert, so the next re-run tells
            # the wizard's own values from hand edits (_detect_config_hand_edits).
            on_exit.callback(self.record_wizard_values, config_path)

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
                self.cfg = cfg = self._load_config(config_path)

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

            # Steps 11-13 (disk check, summary, confirmation) come BEFORE Step 9: its
            # accelerator check downloads and loads both models, so the download size must
            # be shown and a "no" must cost nothing — no probe, no download, no device write.

            # Step 11: disk space check
            try:
                _check_disk_space(prof, features=features)
            except InstallError as exc:
                print(str(exc))
                if features.install_graph_extra:
                    _revert_graph_enabled_flag(config_path, self.dry_run)
                if features.install_multilingual_extra:
                    _revert_multilingual_flag(config_path, self.dry_run)
                if features.enable_hyde or features.enable_rag_fusion:
                    _revert_query_expansion_flags(config_path, self.dry_run)
                if features.graph_provider:
                    _revert_graph_enrichment_flags(config_path, self.dry_run)
                return 1

            # Step 12: summary display
            from archon_search import key_manager as _key_manager
            print(_render_summary(
                profile_name, prof, is_multilingual,
                _planned_device_labels(gpu, enable_gpu, self.dry_run), features,
                db_path=str(Path(cfg.db_path).expanduser()),
                host=features.host if features.host is not None else cfg.host,
                port=features.port if features.port is not None else cfg.port,
                api_key_file=str(_key_manager.get_key_file()),
                total_bytes=_planned_total_bytes(prof, features),
                config_path=str(config_path),
            ))

            # Step 13: confirmation
            if not non_interactive and not ask_yes_no("Proceed? [y/n]: ", eof_default=False):
                print("Installation aborted.")
                if features.install_graph_extra:
                    _revert_graph_enabled_flag(config_path, self.dry_run)
                if features.install_multilingual_extra:
                    _revert_multilingual_flag(config_path, self.dry_run)
                if features.enable_hyde or features.enable_rag_fusion:
                    _revert_query_expansion_flags(config_path, self.dry_run)
                if features.graph_provider:
                    _revert_graph_enrichment_flags(config_path, self.dry_run)
                return 1

            # Step 9: device configuration (detection + user confirm already done in Step 2b)
            device_labels = self._probe_and_configure_device(gpu if enable_gpu else GpuType.NONE)
            if not self.dry_run:
                if enable_gpu and gpu in _ACCELERATOR_LABELS:
                    # The summary above could only name what was about to be checked.
                    print(f"  Device:     {', '.join(device_labels)}")
                # Step 9 rewrote [database].device after Step 8b loaded the config.
                # Step 14's pre-warm reads self.cfg, and no step before it rewrites the
                # [database] model or device keys again (the flag reverts below touch
                # [graph], [hyde], [rag_fusion] and [database].multilingual only).
                self.cfg = cfg = self._load_config(config_path)

            # Step 9b: graph device candidate + the free stage-1 availability
            # pre-check (FE-5 / S31). Runs before the model pre-warm and never loads
            # a model; stage 2 rides the Step 14 pre-warm.
            graph_candidate = _graph_candidate_device(gpu, enable_gpu, graph_device, non_interactive)
            graph_availability = probe_device_availability(graph_candidate)
            if graph_device and not features.install_graph_extra:
                print(
                    "  Note: --graph-device was ignored — graph extraction is not "
                    "being installed, so [graph].device is not written."
                )

            # Step 10: create data directory
            self.create_data_dir()

            # Before Step 14: install code enrichment packages if requested
            if features.install_code_extra:
                try:
                    _install_code_extra(dry_run=self.dry_run)
                except InstallError as exc:
                    print(f"Warning: code enrichment install failed: {exc}", file=sys.stderr)
                    # Non-fatal — continue

            # Before Step 14: install graph enrichment packages if requested (BE-11)
            graph_extra_ready = features.install_graph_extra
            if features.install_graph_extra:
                try:
                    _install_graph_extra(dry_run=self.dry_run)
                except InstallError as exc:
                    graph_extra_ready = False
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
                    _secrets_env_path = get_secrets_env_file()
                    _created = _create_secrets_env(_secrets_env_path, dry_run=self.dry_run)
                    if _created:
                        print(
                            f"  Created:    {_secrets_env_path}\n"
                            "  Add your provider API key (ANTHROPIC_API_KEY=<key> or\n"
                            "  OPENAI_API_KEY=<key>) to this file so the managed service can\n"
                            "  load it at start time for AI query expansion."
                        )
                except OSError as exc:
                    print(f"Warning: could not create .secrets.env: {exc}", file=sys.stderr)

            # Step 14: pre-warm
            graph_prewarm_device: str | None = None
            if not skip_preload:
                try:
                    graph_prewarm_device = self.preload_models(
                        prof,
                        install_graph_extra=features.install_graph_extra,
                        graph_device=graph_candidate,
                    )
                except InstallError as exc:
                    # Real-only: dry never raises, so this rollback runs only when
                    # a real embedder download or load fails.
                    print(f"Model pre-warm failed: {exc}", file=sys.stderr)
                    if branch == "fresh":
                        config_path.unlink(missing_ok=True)
                        config_path.with_suffix(".toml.bak").unlink(missing_ok=True)
                    elif branch == "idempotent":
                        bak = config_path.with_suffix(".toml.bak")
                        if bak.exists():
                            shutil.copy2(bak, config_path)
                            # The restored file is the previous run's config, which the
                            # previous record describes; recording it now would pass off any
                            # hand edit it holds as wizard-written. Keep that record instead.
                            on_exit.pop_all()
                    # branch == "force": leave backup, new config stays
                    return 1

            # After Step 14: write the custom server key only once pre-warm has passed,
            # so a failed install leaves the key file as it was.
            if server_key is not None:
                self.write_server_key(server_key)

            # Step 14b: settle [graph].device from the two-stage probe (FE-5).
            # After pre-warm because stage 2 rides it. Auxiliary — a failure here
            # must never fail the install, so CPU is written and the reason logged.
            # skip_preload is absence of evidence, not evidence of failure: without a
            # pre-warm there is no stage 2 at all, and the probe cannot tell that apart
            # from a real failure — so settle nothing rather than clobber a hand-set value.
            if graph_extra_ready and not skip_preload:
                try:
                    self._configure_graph_device(
                        graph_candidate, graph_availability,
                        graph_prewarm_device, non_interactive,
                    )
                except Exception:  # noqa: BLE001 — auxiliary step: a failure here must never fail the install
                    # Sanitized on purpose: exc_info here would reach the operator's
                    # stderr through logging.lastResort in an unconfigured CLI process.
                    logger.warning("Could not settle [graph].device — leaving it unchanged (CPU).")

            # Step 15: register and start service. Snapshot the log size first so
            # the readiness gate only reads crashes from this launch.
            _log_offset = self._service_log_offset()
            rc = self.register_and_start()
            if rc != 0:
                print(f"Service start returned exit code {rc}.", file=sys.stderr)
                return rc

            # Step 16: wait for readiness
            if not self.dry_run:
                _svc_timeout = _compute_svc_timeout(
                    cfg.eager_load_embedders, _planned_total_bytes(prof, features)
                )
                ready = self._wait_for_service(timeout=_svc_timeout, log_offset=_log_offset)
                if not ready:
                    print(f"Warning: Search service did not become ready within {_svc_timeout} seconds.")
                    return 1

            # Step 16b: next steps guidance (non-dry-run only)
            if not self.dry_run:
                _print_next_steps(cfg.host, cfg.port, str(_key_manager.get_key_file()), str(config_path))

            # Step 17: completion message
            if not self.dry_run:
                if server_key is not None:
                    # Key was already written after Step 14 — report it directly so
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
            if (
                not self.dry_run
                and not features.enable_hyde
                and not features.enable_rag_fusion
                and not os.environ.get("ANTHROPIC_API_KEY")
            ):
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
        warn_if_legacy_config_ignored(self.config_file)
        config_path = self.config_path
        if not config_path.exists():
            click.echo(
                f"No configuration found at {config_path}. Run 'archon-search wizard' first"
                " to choose a profile and download models.",
                err=True,
            )
            return 1

        self.write_service_file()
        _log_offset = self._service_log_offset()
        rc = self.load_service()
        if rc != 0:
            click.echo(f"Service start returned exit code {rc}.", err=True)
            return rc

        if not self.dry_run:
            _start_cfg = load_config(config_path)
            _start_prof = get_profile(_start_cfg.profile or "minimal", _start_cfg.multilingual)
            _svc_timeout = _compute_svc_timeout(
                _start_cfg.eager_load_embedders, _planned_total_bytes(_start_prof)
            )
            ready = self._wait_for_service(timeout=_svc_timeout, log_offset=_log_offset)
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

    def configure_device(self, device: str, *, section: str = _DATABASE_SECTION) -> None:
        _require_device_setting(device)
        print(f"[DRY RUN] Would set [{section}] device = {device!r}.")

    def create_data_dir(self) -> None:
        print("[DRY RUN] Would create the search data directory.")

    def write_service_file(self) -> None:
        get_search_service().replace_registration(dry_run=True, config_path=str(self.config_path))

    def load_service(self) -> int:
        return get_search_service().start(dry_run=True)

    def unload_service(self) -> int:
        return get_search_service().stop(dry_run=True)

    # -- extracted mutations: narrate, never touch the system -------------

    def install_lock(self) -> contextlib.AbstractContextManager[object]:
        # A dry-run writes no config/db, so it serializes against nothing and
        # skips the real lock — whose parent.mkdir would leave the data dir behind.
        return contextlib.nullcontext()

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
        self, config_path: Path, profile_name: str, is_multilingual: bool, features: WizardFeatures
    ) -> None:
        print(f"[DRY RUN] Would write config: {config_path}")
        print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")

    def persist_reinstall_config(
        self, config_path: Path, prof: object, profile_name: str,
        is_multilingual: bool, features: WizardFeatures, has_edits: bool,
    ) -> None:
        if has_edits:
            print(
                "[DRY RUN] Would prompt: Existing config has custom values."
                " Overwrite with profile defaults?"
            )
        print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")
        print(f"[DRY RUN] Would overwrite config: {config_path}")

    def record_wizard_values(self, config_path: Path) -> None:
        pass

    def write_server_key(self, server_key: str) -> None:
        print(f"[dry-run] Would write server key to {get_key_file()}.")

    def preload_models(
        self, prof: InstallProfile, install_graph_extra: bool = False, graph_device: str = CPU_DEVICE,
    ) -> str | None:
        print(f"[DRY RUN] Would download and load models (~{prof.download_mb} MB).")
        if install_graph_extra:
            print("[DRY RUN] Would pre-warm graph NER model.")
        return None

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

    def configure_device(self, device: str, *, section: str = _DATABASE_SECTION) -> None:
        """Write ``[section].device`` via tomlkit; an unchanged value skips the rewrite.

        The skip avoids comment reflow. A missing config file or a non-table *section*
        is logged and skipped.

        Raises:
            ValueError: if *device* is not one of ``DEVICE_SETTINGS``.
        """
        _require_device_setting(device)
        config_path = self.config_path
        if not config_path.exists():
            logger.warning("Config file %s not found — skipping device config", config_path)
            return

        doc = tomlkit.parse(config_path.read_text())
        existing = doc.get(section, {})
        if not isinstance(existing, dict):
            logger.warning("[%s] in %s is not a table — skipping device config", section, config_path)
            return
        if existing.get(_DEVICE_KEY) == device:
            return
        if section not in doc:
            doc[section] = tomlkit.table()
        doc[section][_DEVICE_KEY] = device
        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())

    def create_data_dir(self) -> None:
        """Create the search database directory."""
        db_path = Path(self.cfg.db_path).expanduser()
        ensure_private_dir(db_path)

    def write_service_file(self) -> None:
        """Replace the registered service (validate, stop the running instance, register).

        Threads the installer's config_path into register() so the service the
        installer starts reads the config just written, not a default the
        service resolves on its own (S206). A registration the platform rejects
        fails before the running service is stopped, so it keeps running.
        """
        get_search_service().replace_registration(dry_run=False, config_path=str(self.config_path))

    def load_service(self) -> int:
        """Delegate to get_search_service().start()."""
        return get_search_service().start(dry_run=False)

    def unload_service(self) -> int:
        """Delegate to get_search_service().stop()."""
        return get_search_service().stop(dry_run=False)

    # -- extracted mutations: perform the real work ----------------------

    def install_lock(self) -> contextlib.AbstractContextManager[object]:
        return _acquire_install_lock()

    def create_logs_dir(self) -> None:
        ensure_private_dir(get_data_dir() / "logs")

    def download_fasttext_model(self) -> None:
        _download_fasttext_model(get_fasttext_models_dir())

    def prepare_db_path(self, expanded: Path) -> bool:
        try:
            ensure_private_dir(expanded)
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
        self, config_path: Path, profile_name: str, is_multilingual: bool, features: WizardFeatures
    ) -> None:
        # config_path.parent IS the data dir on the default invocation, so this
        # mkdir must stay gated behind RealInstaller or a dry-run recreates it.
        config_path.with_suffix(config_path.suffix + ".tmp").unlink(missing_ok=True)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(config_path, _profile_toml(profile_name, is_multilingual, features).encode())
        shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))

    def persist_reinstall_config(
        self, config_path: Path, prof: object, profile_name: str,
        is_multilingual: bool, features: WizardFeatures, has_edits: bool,
    ) -> None:
        shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))
        _write_profile_config(config_path, prof, profile_name, is_multilingual, features=features)
        print(f"  Backup:     {config_path.with_suffix('.toml.bak')}")

    def record_wizard_values(self, config_path: Path) -> None:
        _record_wizard_values(config_path)

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

    def preload_models(
        self, prof: InstallProfile, install_graph_extra: bool = False, graph_device: str = CPU_DEVICE,
    ) -> str | None:
        print("[4/5] Downloading and loading models...")
        # self.cfg carries the [database] model and device keys the service starts
        # with: run() reloads it after Step 9 rewrites the device.
        return _prewarm_models(
            self.cfg, _prewarm_timeout(prof),
            install_graph_extra=install_graph_extra, graph_device=graph_device,
        )

    def register_and_start(self) -> int:
        print("[5/5] Starting search service...")
        self.write_service_file()
        return self.load_service()

    def delete_database(self, db_path: Path) -> bool:
        rmtree(db_path)
        print(f"Deleted search database at {db_path}.")
        return True


def create_installer(
    config_file: str | None = None, dry_run: bool = False, *, legacy_config: bool = False
) -> InstallerProtocol:
    """Return the installer for *dry_run*: DryRunInstaller if true, else RealInstaller.

    ``legacy_config`` is for the wizard only: it drops the removed ``providers`` keys a config still holds
    instead of raising ``ConfigError``, because it rewrites the file.
    """
    if dry_run:
        return DryRunInstaller(config_file, legacy_config=legacy_config)
    return RealInstaller(config_file, legacy_config=legacy_config)
