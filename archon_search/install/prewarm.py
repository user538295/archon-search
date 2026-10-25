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
# Disk space guard (Task C0-2.2)
# ---------------------------------------------------------------------------

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
    required_free_bytes = _planned_total_bytes(profile, features) * 2
    if usage.free < required_free_bytes:
        raise InstallError(
            f"Insufficient disk space. This install requires"
            f" ~{required_free_bytes // BYTES_PER_MB} MB free;"
            f" only {usage.free // BYTES_PER_MB} MB available."
        )


# ---------------------------------------------------------------------------
# Model pre-warm: download, then load (Task C0-2.3, S671)
# ---------------------------------------------------------------------------

# Fixed, trivial probe input: one inference proves the model runs, not just loads.
_PROBE_TEXT = "archon search pre-warm probe"


def _prewarm_timeout(profile: InstallProfile) -> int:
    estimated_bytes = profile.download_mb * 1_000_000
    return min(1800, max(300, estimated_bytes // 100_000))


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
    without it). A reranker failure only warns (S202). The graph NER model is
    non-fatal too (``_prewarm_graph_model``).

    *timeout* (seconds; the caller passes ``_prewarm_timeout(profile)``) sets a
    deadline that is checked between models: it cannot interrupt a running download
    or load, but once it has passed, the models not yet started are skipped.
    Hub download progress is printed to stderr — not suppressed. When
    ``install_graph_extra`` is set, the graph NER model (Task BE-8) is pre-warmed
    inside the same deadline as the embedder/reranker, placed on the device
    ``graph_device`` resolves to (Task FE-5).

    Returns the graph NER pre-warm's resolved torch device (Task BE-14's stage-2
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
            # Non-fatal (S202): a model load error or transient download hiccup
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
# Graph NER model pre-warm (Task BE-8 — optional, non-fatal)
# ---------------------------------------------------------------------------

def _prewarm_graph_model(device: str = CPU_DEVICE) -> str | None:
    """Pre-warm the graph NER/RelEx model (gliner.GLiNER).

    Mirrors the reranker's non-fatal branch in ``_prewarm_models``: a failure
    here must never abort the wizard — the model downloads on first use
    instead. Loads through ``load_gliner_model``, the same loader as the
    runtime lazy-load in ProseExtractionBackend.load(), so pre-warm and first
    use share one on-disk cache (get_graph_models_dir()) rather than two.

    Returns the resolved torch device string ("cpu"/"cuda"/"mps") read off the
    loaded model on success (Task BE-14's stage-2 device probe rides this
    pre-warm), or ``None`` on any failure.
    """
    try:
        model = load_gliner_model(GRAPH_NER_MODEL_NAME, GRAPH_NER_MODEL_REVISION)
        # Place the model exactly where the runtime will (mirrors _load_sync in
        # prose_extraction_backend.py) rather than reporting gliner's auto
        # placement, so the device stage 2 probes is the device ingest gets.
        model = model.to(resolve_device(device))
        return model.device.type
    except Exception as exc:
        # Non-fatal: the [graph] extra may not be installed, or this may be a
        # transient download hiccup — either must not abort the wizard.
        logger.warning(
            "Failed to pre-warm graph NER model %r: %s — "
            "the model will be downloaded on first use.",
            GRAPH_NER_MODEL_NAME, exc,
        )
        return None


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
            _write_profile_config(config_path, profile, profile_name, multilingual, features=features, clear_collections=True)
        except Exception:
            print(
                f"Install failed after database deletion. Your previous config has been preserved at "
                f"{bak_path} for reference. "
                "Run archon-search install to create a fresh install from scratch.",
                file=sys.stderr,
            )
            raise SystemExit(1)
