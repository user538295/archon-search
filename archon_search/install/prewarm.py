"""Disk-space guard, model pre-warm (download, then load), and reinstall guards."""
from __future__ import annotations

import logging
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from archon_search.config import SearchConfig
from archon_search.embedder import ModelEmbedder
from archon_search.model_specs import get_embedder_spec, get_reranker_spec
from archon_search.model_store import ensure_model
from archon_search.paths import (
    GRAPH_NER_MODEL_NAME,
    GRAPH_NER_MODEL_REVISION,
    get_data_dir,
)
from archon_search.platform.runtime import get_search_service
from archon_search.profiles import InstallProfile
from archon_search.prose_extraction_backend import load_gliner_model
from archon_search.reranker import ModelReranker
from archon_search.torch_runtime import CPU_DEVICE, resolve_device

from .config_writer import WizardFeatures, _write_profile_config
from .errors import InstallError, NeedsForceDeleteError
from .extras import CODE_EXTRA_SIZE_ESTIMATE, GRAPH_MODEL_SIZE_ESTIMATE
from .licenses import FASTTEXT_ARTIFACT
from .provisioning import BYTES_PER_MB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Disk space guard
# ---------------------------------------------------------------------------

# The disk guard requires this multiple of the planned download to be free.
_DISK_HEADROOM_FACTOR = 2


def _planned_total_bytes(profile: InstallProfile, features: WizardFeatures | None = None) -> int:
    """Return the planned-download byte total for *profile* plus every selected extra.

    Provisioned artifacts contribute their pinned ``size_bytes``; pip/uv-resolved or lazily
    fetched ones contribute their declared MB estimate. The disk guard, the service-ready
    timeouts and the wizard's displayed figure are all derived from this one total.
    """
    total = profile.download_mb * BYTES_PER_MB
    if features is None:
        return total
    if features.install_code_extra:
        total += CODE_EXTRA_SIZE_ESTIMATE.declared_mb * BYTES_PER_MB
    if features.install_graph_extra:
        total += GRAPH_MODEL_SIZE_ESTIMATE.declared_mb * BYTES_PER_MB
    if features.install_multilingual_extra:
        total += FASTTEXT_ARTIFACT.size_bytes
    return total


def _check_disk_space(
    profile: InstallProfile,
    base_path: Path | None = None,
    features: WizardFeatures | None = None,
) -> None:
    """Raise InstallError if the filesystem has insufficient free space for the selection."""
    if base_path is None:
        base_path = get_data_dir()

    check_path = base_path
    while not check_path.exists():
        check_path = check_path.parent

    usage = shutil.disk_usage(check_path)
    # No ceil(): the summed byte total is already an integer.
    required_free_bytes = _planned_total_bytes(profile, features) * _DISK_HEADROOM_FACTOR
    if usage.free < required_free_bytes:
        raise InstallError(
            f"Insufficient disk space. This install requires"
            f" ~{required_free_bytes // BYTES_PER_MB} MB free;"
            f" only {usage.free // BYTES_PER_MB} MB available."
        )


# ---------------------------------------------------------------------------
# Model pre-warm: download, then load
# ---------------------------------------------------------------------------

# Pre-warm deadline: the download size at an assumed throughput, clamped to this range.
_PREWARM_TIMEOUT_MAX_SECONDS = 1800
_PREWARM_TIMEOUT_MIN_SECONDS = 300
_PREWARM_ASSUMED_BYTES_PER_SECOND = 100_000

# Fixed, trivial probe input: one inference proves the model runs, not just loads.
_PROBE_TEXT = "archon search pre-warm probe"


def _prewarm_timeout(profile: InstallProfile) -> int:
    estimated_bytes = profile.download_mb * BYTES_PER_MB
    return min(
        _PREWARM_TIMEOUT_MAX_SECONDS,
        max(_PREWARM_TIMEOUT_MIN_SECONDS, estimated_bytes // _PREWARM_ASSUMED_BYTES_PER_SECOND),
    )


class _PrewarmFailure(Exception):
    """A model failed to download or load; the caller decides whether that is fatal."""


def _prewarm_model(
    role: str, model_name: str, download: Callable[[], object], load: Callable[[], object]
) -> None:
    """Run *download*, then *load*; raise _PrewarmFailure naming the phase that failed.

    *download* fetches the pinned snapshot (``ensure_model``): the repo lookup and the
    file download, but no model in memory. *load* runs the service's own backend
    (``ModelEmbedder.encode`` / ``ModelReranker.predict``), so the weights load exactly
    as the server loads them, and one inference runs. Both models are locals of the
    callables, so they are released when this returns.
    """
    try:
        download()
    except Exception as exc:
        cause = str(exc) or type(exc).__name__
        raise _PrewarmFailure(
            f"Could not resolve or download the {role} model {model_name!r}: {cause}"
        ) from exc
    try:
        load()
    except Exception as exc:
        cause = str(exc) or type(exc).__name__
        raise _PrewarmFailure(
            f"The {role} model {model_name!r} was downloaded but cannot be loaded: {cause}"
        ) from exc


def _prewarm_models(
    cfg: SearchConfig,
    timeout: float,
    install_graph_extra: bool = False,
    graph_device: str = CPU_DEVICE,
) -> str | None:
    """Download and load the configured embedder (and reranker, and optionally the
    graph NER model) so a model that cannot load fails the install, not the first ingest.

    The embedder and reranker come from *cfg* — the config the service starts with —
    and load through the service's own backends on its device (``[database].device``).
    Each is loaded, probed with one inference and released before the next, so peak
    memory is one model.

    Failure policy: an embedder failure raises InstallError (the service cannot embed
    without it). A reranker failure only warns. The graph NER model is
    non-fatal too (``_prewarm_graph_model``).

    *timeout* (seconds; the caller passes ``_prewarm_timeout(profile)``) sets a
    deadline that is checked between models: it cannot interrupt a running download
    or load, but once it has passed, the models not yet started are skipped.
    Hub download progress is printed to stderr — not suppressed. When
    ``install_graph_extra`` is set, the graph NER model is pre-warmed
    inside the same deadline as the embedder/reranker, placed on the device
    ``graph_device`` resolves to.

    Returns the graph NER pre-warm's resolved torch device (the install's stage-2
    device probe rides this), or ``None`` if the graph pre-warm did not run (not
    requested, or skipped after the deadline) or ran and failed.
    """
    deadline = time.monotonic() + timeout
    try:
        _prewarm_model(
            "embedder",
            cfg.embedding_model,
            download=lambda: ensure_model(get_embedder_spec(cfg.embedding_model)),
            load=lambda: ModelEmbedder(cfg.embedding_model, device=cfg.device).encode(
                [_PROBE_TEXT]
            ),
        )
    except _PrewarmFailure as exc:
        raise InstallError(str(exc)) from exc

    def _prewarm_reranker() -> None:
        try:
            _prewarm_model(
                "reranker",
                cfg.reranker_model,
                download=lambda: ensure_model(get_reranker_spec(cfg.reranker_model)),
                load=lambda: ModelReranker(cfg.reranker_model, device=cfg.device).predict(
                    [(_PROBE_TEXT, _PROBE_TEXT)]
                ),
            )
        except _PrewarmFailure as exc:
            # Non-fatal: a model load error or transient download hiccup
            # must not abort the wizard; the service still searches. str(exc), not
            # exc: a LogRecord holding the exception would keep the failed model's
            # frames — and the model — alive.
            logger.warning(
                "%s — the install continues, but /search skips reranking and /explain with"
                " rerank fails until the model loads. See 'Model pre-warm fails' in the"
                " wizard troubleshooting.",
                str(exc),
            )

    # The models after the embedder, in order; only the graph step returns a value.
    steps: list[tuple[str, Callable[[], str | None]]] = []
    if cfg.reranker_model:
        steps.append(("reranker", _prewarm_reranker))
    if install_graph_extra:
        steps.append(("graph NER", lambda: _prewarm_graph_model(graph_device)))

    device: str | None = None
    for index, (_name, step) in enumerate(steps):
        if time.monotonic() >= deadline:
            skipped = [name for name, _step in steps[index:]]
            models = " and ".join(skipped) + (" model" if len(skipped) == 1 else " models")
            pronoun = "it" if len(skipped) == 1 else "them"
            logger.warning(
                "Model pre-warm timed out after %ss with the embedder ready — skipping the %s;"
                " the service downloads %s when it starts or first uses %s.",
                timeout,
                models,
                pronoun,
                pronoun,
            )
            return None
        device = step()
    return device


# ---------------------------------------------------------------------------
# Graph NER model pre-warm (optional, non-fatal)
# ---------------------------------------------------------------------------

def _prewarm_graph_model(device: str = CPU_DEVICE) -> str | None:
    """Pre-warm the graph NER/RelEx model (gliner.GLiNER).

    Mirrors the reranker's non-fatal branch in ``_prewarm_models``: a failure
    here must never abort the wizard — the model downloads on first use
    instead. Loads through ``load_gliner_model``, the same loader as the
    runtime lazy-load in ProseExtractionBackend.load(), so pre-warm and first
    use share one on-disk cache (get_graph_models_dir()) rather than two.

    Returns the resolved torch device string ("cpu"/"cuda"/"mps") read off the
    loaded model on success (the stage-2 device probe rides this
    pre-warm), or ``None`` on any failure.
    """
    try:
        model = load_gliner_model(GRAPH_NER_MODEL_NAME, GRAPH_NER_MODEL_REVISION)
        # Place the model exactly where the runtime will (mirrors _load_sync in
        # prose_extraction_backend.py) rather than reporting gliner's auto
        # placement, so the device stage 2 probes is the device ingest gets.
        model = model.to(resolve_device(device))
        return model.device.type
    except Exception as exc:  # noqa: BLE001 — gliner/huggingface may fail in arbitrary ways; pre-warm is non-fatal
        # Non-fatal: the [graph] extra may not be installed, or this may be a
        # transient download hiccup — either must not abort the wizard.
        logger.warning(
            "Failed to pre-warm graph NER model %r: %s — "
            "the model will be downloaded on first use.",
            GRAPH_NER_MODEL_NAME, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Reinstall guard
# ---------------------------------------------------------------------------

def _check_reinstall_guard(
    existing_cfg: SearchConfig,
    new_profile: InstallProfile,
    new_profile_name: str,
    new_multilingual: bool,
) -> None:
    """Raise NeedsForceDeleteError when switching model or chunk_size would invalidate the index.

    A reranker-only change is safe and does NOT trigger the guard.
    new_profile_name and new_multilingual are woven into the suggested re-run command so
    the message hands the operator an exact, copy-pasteable line rather than bare flags.
    """
    if existing_cfg.embedding_model == new_profile.embedder and existing_cfg.chunk_size == new_profile.chunk_size:
        return
    ml_flag = "--multilingual" if new_multilingual else "--no-multilingual"
    command = f"archon-search wizard --profile {new_profile_name} {ml_flag} --force --delete-db"
    raise NeedsForceDeleteError(
        f"Switching to {new_profile.embedder} (chunk_size={new_profile.chunk_size}) requires "
        f"rebuilding the index; the existing index ({existing_cfg.embedding_model}, "
        f"chunk_size={existing_cfg.chunk_size}) and its data will be deleted. To proceed, run:\n"
        f"  {command}"
    )


def _back_up_config(config_path: Path, bak_path: Path, dry_run: bool) -> bool:
    """Copy *config_path* to *bak_path*; return whether a backup now exists to restore from."""
    if dry_run:
        if config_path.exists():
            print(f"[dry-run] Would create backup at {bak_path}.")
        return False
    try:
        shutil.copy2(config_path, bak_path)
    except FileNotFoundError:
        return False  # no config to preserve (a fresh install, or removed since the caller looked)
    return True


def _restore_config(config_path: Path, bak_path: Path, has_backup: bool) -> None:
    if has_backup:
        shutil.copy2(bak_path, config_path)


def _confirm_deletion(config_path: Path, bak_path: Path, has_backup: bool) -> None:
    """Ask the operator to type ``yes``; otherwise restore the config and exit with status 1."""
    try:
        response = input("WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: ")
    except EOFError:
        response = ""
    if response != "yes":
        _restore_config(config_path, bak_path, has_backup)
        print("Aborted.")
        raise SystemExit(1)


def _stop_service(config_path: Path, bak_path: Path, has_backup: bool, dry_run: bool) -> None:
    """Stop the running service so it releases the database; restore the config if that fails.

    A service that is not running needs no stop. A running one that cannot be stopped aborts
    the reinstall before anything is deleted.
    """
    if dry_run:
        print("[dry-run] Would stop service.")
        return
    try:
        service = get_search_service()
        if service.status().running:
            service.stop()
    except Exception:
        _restore_config(config_path, bak_path, has_backup)
        raise


def _report_preserved_config(phase: str, bak_path: Path, cause: Exception) -> None:
    print(
        f"Install failed {phase}. Your previous config has been preserved at "
        f"{bak_path} for reference. "
        "Run archon-search install to create a fresh install from scratch. "
        f"Cause: {cause}",
        file=sys.stderr,
    )


def _delete_database(db_path: Path, bak_path: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] Would delete database at {db_path}.")
        return
    try:
        shutil.rmtree(db_path)
    except FileNotFoundError:
        pass  # nothing to delete: no index yet, or removed since the caller looked
    except OSError as exc:
        _report_preserved_config("during database deletion", bak_path, exc)
        raise SystemExit(1) from exc


def _write_new_profile_config(
    config_path: Path,
    bak_path: Path,
    profile: InstallProfile,
    profile_name: str,
    multilingual: bool,
    features: WizardFeatures | None,
    dry_run: bool,
) -> None:
    if dry_run:
        print(f"[dry-run] Would write profile config for {profile_name}.")
        return
    stale_tmp_path = config_path.with_suffix(".toml.tmp")
    stale_tmp_path.unlink(missing_ok=True)
    try:
        _write_profile_config(config_path, profile, profile_name, multilingual, features=features, clear_collections=True)
    except Exception as exc:  # any config-write failure is reported and mapped to SystemExit(1)
        _report_preserved_config("after database deletion", bak_path, exc)
        raise SystemExit(1) from exc


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
    """Execute the force-delete-db rollback sequence: back up the config, confirm, stop the
    service, delete the database, write the new profile config."""
    bak_path = config_path.with_suffix(".toml.bak")
    has_backup = _back_up_config(config_path, bak_path, dry_run)
    if not non_interactive:
        _confirm_deletion(config_path, bak_path, has_backup)
    _stop_service(config_path, bak_path, has_backup, dry_run)
    _delete_database(db_path, bak_path, dry_run)
    _write_new_profile_config(config_path, bak_path, profile, profile_name, multilingual, features, dry_run)
