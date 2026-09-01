"""Extra-package installers plus the query-expansion flag revert."""
from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import click
import tomlkit

from archon_search._durable_io import atomic_write_bytes, fsync_dir, fsync_tree
from archon_search.paths import get_models_dir

from .config_writer import WizardFeatures
from .errors import InstallError
from .provisioning import SizeEstimateSpec


# ---------------------------------------------------------------------------
# Code enrichment package install (Task C8-2.3)
# ---------------------------------------------------------------------------

def _uv_or_pip_install(packages: list[str], extra_args: list[str], fail_label: str) -> None:
    """Install *packages* via ``uv pip install`` with a plain-``pip`` fallback.

    *extra_args* are appended to both the uv and pip command lines (e.g. an
    ``--extra-index-url``). Raises ``InstallError`` (message prefixed with
    *fail_label*) only when BOTH uv and pip fail.
    """
    python = sys.executable
    try:
        subprocess.run(
            ["uv", "pip", "install", "--python", python, *packages, *extra_args],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        # uv missing / not executable / wrong-arch (OSError) or failed
        # (CalledProcessError) — fall back to pip

        try:
            subprocess.run(
                [python, "-m", "pip", "install", *packages, *extra_args],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as pip_exc:
            stderr = (pip_exc.stderr or b"").decode(errors="replace")
            raise InstallError(f"{fail_label}: {stderr}") from pip_exc


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
    _uv_or_pip_install([package], [], f"Failed to install {package}")
    click.echo(f"{label.capitalize()} installed.")


# ---------------------------------------------------------------------------
# CUDA torch swap (wizard-cuda-torch-upgrade)
# ---------------------------------------------------------------------------

# PyTorch CUDA wheel index for the GPU torch swap. The `+<tag>` local build the
# swap pins (below) is derived from this URL's last path segment, so the index
# and the pinned build can never drift apart. cu126 is the lowest CUDA toolkit
# still shipping the S269-pinned torch/torchvision cp312 wheels, so it has the
# widest NVIDIA-driver compatibility (cu129/cu130 demand much newer drivers).
# Verified against download.pytorch.org/whl/cu126/{torch,torchvision}/ on
# 2026-08-02. Only used on linux/x86_64 (the sole platform S269 pins to +cpu).
_CUDA_TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu126"
# The local-version tag ("cu126") for the pinned CUDA build — parsed from the
# index URL so the two stay coupled by construction.
_CUDA_LOCAL_TAG = _CUDA_TORCH_INDEX_URL.rsplit("/", 1)[-1]


def _install_cuda_torch(dry_run: bool = False) -> None:
    """Best-effort swap of the CPU-only torch build for the CUDA build.

    Replaces the S269 CPU-only torch/torchvision with the CUDA build of the SAME
    public version so docling's PDF/OCR parsing runs on an NVIDIA GPU. The
    version is derived from the already-installed build (``importlib.metadata``,
    stripping the local tag) and re-pinned to the exact CUDA local build, e.g.
    ``torch==2.13.0+cu126``.

    The local ``+cu126`` tag is REQUIRED: a bare public specifier ``==2.13.0``
    is satisfied by the installed ``2.13.0+cpu`` (PEP 440 ignores the local label
    when the requirement has none), so pip/uv would report "already satisfied"
    and the swap would silently do nothing. Pinning the local build forces the
    reinstall. ``--extra-index-url`` (not ``--index-url``) keeps PyPI available
    for any transitive dependency the CUDA build needs.

    Best-effort: on any failure (torch not installed, both uv and pip fail) it
    logs a warning to stderr and returns, leaving the working CPU build in place.
    Prints ``[DRY RUN] Would install CUDA torch from {index}`` and returns early
    when *dry_run* is True.
    """
    if dry_run:
        click.echo(f"[DRY RUN] Would install CUDA torch from {_CUDA_TORCH_INDEX_URL}")
        return

    try:
        torch_version = importlib.metadata.version("torch").split("+")[0]
        torchvision_version = importlib.metadata.version("torchvision").split("+")[0]
    except importlib.metadata.PackageNotFoundError as exc:
        print(
            f"Warning: cannot determine installed torch/torchvision version; "
            f"keeping the CPU build: {exc}",
            file=sys.stderr,
        )
        return

    click.echo("Installing CUDA torch/torchvision...")
    packages = [
        f"torch=={torch_version}+{_CUDA_LOCAL_TAG}",
        f"torchvision=={torchvision_version}+{_CUDA_LOCAL_TAG}",
    ]
    try:
        _uv_or_pip_install(
            packages,
            ["--extra-index-url", _CUDA_TORCH_INDEX_URL],
            "Failed to install CUDA torch",
        )
    except InstallError as exc:
        print(
            f"Warning: CUDA torch install failed; keeping the CPU build: {exc}",
            file=sys.stderr,
        )
        return

    click.echo("CUDA torch/torchvision installed.")


# Ten pip/uv-resolved wheels with no pinned URL and no stable byte count across platforms, so
# this is a declared estimate rather than a measured total: 16.8 MB installed on macOS/arm64
# (2026-08-31), rounded up for platform variance.
CODE_EXTRA_SIZE_ESTIMATE = SizeEstimateSpec(name="archon-search[code]", declared_mb=18)

# The graph NER checkpoint is fetched lazily by ``GLiNER.from_pretrained``, never through the
# digest-pinned provisioning seam, so it declares an estimate too. 1218 MB is the 1,276,776,490
# byte ONNX export measured during the graph-NER spike — a ballpark for the display and the disk
# guard, not a verified download size.
GRAPH_MODEL_SIZE_ESTIMATE = SizeEstimateSpec(name="gliner-relex-multi-v1.0", declared_mb=1218)


def _install_code_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[code]`` (tree-sitter enrichment packages).

    Thin wrapper around :func:`_install_extra`.  Public interface unchanged.
    """
    _install_extra("archon-search[code]", "code enrichment", dry_run)


SPACY_MODEL_NAME = "en_core_web_sm"
SPACY_COMPATIBILITY_URL = (
    "https://raw.githubusercontent.com/explosion/spacy-models/master/compatibility.json"
)
SPACY_MODEL_WHEEL_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "{name}-{version}/{name}-{version}-py3-none-any.whl"
)
_SPACY_COMPATIBILITY_TIMEOUT_SECONDS = 60
_SPACY_WHEEL_TIMEOUT_SECONDS = 300


def _resolve_spacy_model_version(spacy_version: str) -> str:
    """Return the ``en_core_web_sm`` release pinned to *spacy_version*.

    spaCy models are version-locked to the library (spaCy 3.8.x loads only
    3.8.x models), and the authoritative mapping is the compatibility table
    published alongside the model releases — the same one ``spacy download``
    consults. That table is keyed by MAJOR.MINOR (e.g. ``"3.8"``), not the
    full patch version, except for prereleases which are keyed by their full
    version — this mirrors ``spacy.cli.download.get_compatibility()`` exactly.
    The first listed release wins — the same element
    ``spacy.cli.download.get_version`` picks (``comp[model][0]``).
    """
    import spacy.util  # noqa: PLC0415

    key = (
        spacy_version
        if spacy.util.is_prerelease_version(spacy_version)
        else spacy.util.get_minor_version(spacy_version)
    )
    try:
        with urllib.request.urlopen(
            SPACY_COMPATIBILITY_URL, timeout=_SPACY_COMPATIBILITY_TIMEOUT_SECONDS
        ) as response:
            table = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InstallError(
            f"could not read the spaCy compatibility table: {exc}"
        ) from exc

    versions = table.get("spacy", {}).get(key, {}).get(SPACY_MODEL_NAME, [])
    if not versions:
        raise InstallError(
            f"no {SPACY_MODEL_NAME} release is listed for spaCy {key!r} "
            f"(resolved from installed spaCy {spacy_version})"
        )
    version = str(versions[0])
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        # The version drives both a download URL and a filesystem path below;
        # a malformed value from the (remote, TLS'd) compatibility table must
        # never reach either.
        raise InstallError(
            f"{SPACY_MODEL_NAME} version {version!r} from the compatibility "
            "table is not a well-formed dotted version"
        )
    return version


def _download_spacy_model(models_dir: Path) -> Path:
    """Provision ``en_core_web_sm`` into *models_dir* and return its directory.

    The runtime never downloads or installs the model (2026-08-19-030): a
    ``uv tool install`` venv has no package installer, and PyPI rejects the URL
    dependency that would otherwise ship it, so the wizard is the only channel.
    The model wheel is a zip whose inner ``en_core_web_sm-<ver>/`` directory is
    the model itself — ``spacy.load()`` accepts that path directly.

    - No-op if the pinned version is already present.
    - Smoke-loads the placed model before reporting success; a model that is on
      disk but unloadable is worse than none, because the runtime would resolve
      it and then degrade on every ingest.
    - Raises ``InstallError`` for a failed fetch, a bad wheel or a model that
      will not load. Filesystem operations outside that path (``mkdir``,
      ``chmod``, the staging rename) can still raise ``OSError`` — the caller
      catches both, so a read-only or full data dir warns and continues rather
      than aborting the wizard (2026-08-19-030 C2-B-3).
    """
    importlib.invalidate_caches()  # [graph] may have been installed moments ago
    try:
        import spacy  # noqa: PLC0415
    except ImportError as exc:
        raise InstallError(f"spaCy is not importable: {exc}") from exc

    version = _resolve_spacy_model_version(spacy.__version__)
    target = models_dir / f"{SPACY_MODEL_NAME}-{version}"
    if (target / "config.cfg").is_file():
        # Presence is not usability. A truncated or corrupted tree still has a
        # `config.cfg`, which the runtime resolver accepts and then fails to
        # load — latching into permanent degradation whose notice tells the
        # operator to re-run this wizard, which a bare presence check would
        # turn into a no-op. That is an unrecoverable loop, so verify before
        # claiming success and re-provision otherwise (2026-08-19-030 C2-I-5).
        try:
            spacy.load(str(target))
        except Exception:  # noqa: BLE001 — any load failure means re-provision
            shutil.rmtree(target, ignore_errors=True)
        else:
            click.echo(f"spaCy model already provisioned at {target}")
            return target

    click.echo(f"Downloading {SPACY_MODEL_NAME} {version}...")
    models_dir.mkdir(parents=True, exist_ok=True)
    # `mkdir(mode=...)` is a no-op on an existing directory and is not applied
    # to parents, so set it explicitly — and unconditionally, so a directory
    # created by an earlier version is corrected too (2026-08-19-030 C2-B-23).
    models_dir.chmod(0o700)
    url = SPACY_MODEL_WHEEL_URL.format(name=SPACY_MODEL_NAME, version=version)

    with tempfile.TemporaryDirectory(dir=models_dir) as staging_name:
        staging = Path(staging_name)
        wheel = staging / "model.whl"
        try:
            with urllib.request.urlopen(url, timeout=_SPACY_WHEEL_TIMEOUT_SECONDS) as response:
                with wheel.open("wb") as out_file:
                    shutil.copyfileobj(response, out_file)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(staging)
        except (urllib.error.URLError, OSError, zipfile.BadZipFile) as exc:
            raise InstallError(
                f"failed to fetch {SPACY_MODEL_NAME} {version}: {exc}. "
                "Check your network connection and re-run the wizard."
            ) from exc

        inner = staging / SPACY_MODEL_NAME / f"{SPACY_MODEL_NAME}-{version}"
        if not (inner / "config.cfg").is_file():
            raise InstallError(
                f"{SPACY_MODEL_NAME} {version} wheel has an unexpected layout "
                f"(no model directory at {inner.name})"
            )
        # fsync the extracted files before they become reachable at `target`:
        # otherwise a crash between extraction and page-cache flush can leave
        # `target` with a valid config.cfg but truncated weight files, which
        # `find_spacy_model()` resolves happily and which then latches into
        # permanent silent degradation (2026-08-19-030) — the failure mode
        # this whole change set exists to prevent.
        fsync_tree(inner)
        if target.exists():
            # Rubble from a prior interrupted provisioning run (the
            # `config.cfg` guard above only catches a *complete* target).
            shutil.rmtree(target)
        # Staging sits inside models_dir, so this is a same-filesystem rename:
        # a half-extracted model is never visible at `target`, its contents are
        # already fsynced above, and the fsync_dir below makes the rename
        # itself survive a crash.
        shutil.move(str(inner), str(target))  # noqa: durable-write — fsynced before and after; same-filesystem rename
        fsync_dir(models_dir)

    try:
        spacy.load(str(target))
    except Exception as exc:
        # `ignore_errors`: a cleanup failure must not replace the real
        # diagnostic with an OSError from the handler (2026-08-19-030 C2-I-14).
        shutil.rmtree(target, ignore_errors=True)
        raise InstallError(
            f"provisioned {SPACY_MODEL_NAME} {version} failed to load: {exc}"
        ) from exc

    click.echo(f"spaCy model {SPACY_MODEL_NAME} {version} provisioned at {target}")
    return target


def _install_graph_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[graph]`` and provision the spaCy NER model.

    PyPI prohibits direct URL dependencies, so ``en_core_web_sm`` cannot be
    declared in the package extras and is not on PyPI under any name. The
    wizard therefore fetches the pinned model wheel from the spacy-models
    GitHub release and places it under the data dir, where the runtime resolves
    it by path (2026-08-19-030). A failed fetch is non-fatal: graph ingest keeps
    working for code symbols and degrades for prose.
    """
    _install_extra("archon-search[graph]", "graph enrichment", dry_run)
    models_dir = get_models_dir() / "spacy"
    if dry_run:
        click.echo(f"[dry-run] Would provision spaCy model {SPACY_MODEL_NAME} into {models_dir}")
        return
    try:
        _download_spacy_model(models_dir)
    except (InstallError, OSError) as exc:
        print(
            f"Warning: spaCy model provisioning failed: {exc}. "
            "Graph ingest will extract code symbols only until the wizard is re-run.",
            file=sys.stderr,
        )


def _install_multilingual_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[multilingual]`` (fasttext-wheel language detection).

    Thin wrapper around :func:`_install_extra`.  Required by the server whenever
    ``[database].multilingual = true``; without it ``_check_multilingual_deps``
    hard-fails at startup.
    """
    _install_extra("archon-search[multilingual]", "multilingual language detection", dry_run)


# Maps a HyDE / RAG Fusion provider to the pip extra that supplies its package.
# 'claude_cli' is absent by design: it has no pip package (availability is the
# `claude` binary on PATH). [hyde] and [rag_fusion] are byte-identical extras
# (both pull anthropic>=0.40), so anthropic maps to a single name — this is what
# lets the dedup below collapse anthropic-backed HyDE + RAG Fusion to one install.
_PROVIDER_EXTRA: dict[str, str] = {
    "anthropic": "archon-search[hyde]",
    "openai": "archon-search[openai-provider]",
    "ollama": "archon-search[ollama]",
}


def _install_query_expansion_extras(features: WizardFeatures, dry_run: bool = False) -> list[str]:
    """Install provider packages for enabled HyDE / RAG Fusion features.

    Resolves each enabled feature's provider to its pip extra and installs each
    unique package once (so the common case — both features on the default
    anthropic provider — installs a single package, while mixed providers install
    each). ``claude_cli`` needs no package and is skipped. Returns the list of
    feature SECTIONS (``"hyde"`` / ``"rag_fusion"``) whose provider package failed
    to install (empty when all succeeded); never raises, so the caller can revert
    exactly the flags whose package failed. When both features share a package
    (both on anthropic), a single failure marks BOTH sections failed.
    """
    # Map each enabled section to its provider package (None = no package, e.g. claude_cli).
    section_packages: list[tuple[str, str | None]] = []
    if features.enable_hyde:
        section_packages.append(("hyde", _PROVIDER_EXTRA.get(features.hyde_provider)))
    if features.enable_rag_fusion:
        section_packages.append(("rag_fusion", _PROVIDER_EXTRA.get(features.rag_fusion_provider)))

    # Install each distinct package once.
    packages: list[str] = []
    for _section, pkg in section_packages:
        if pkg and pkg not in packages:
            packages.append(pkg)
    failed_packages: list[str] = []
    for package in packages:
        try:
            _install_extra(package, "AI query expansion", dry_run)
        except InstallError as exc:
            print(f"Warning: AI query expansion install failed for {package}: {exc}", file=sys.stderr)
            failed_packages.append(package)

    # Report the sections whose package failed (a shared failed package fails both).
    return [section for section, pkg in section_packages if pkg is not None and pkg in failed_packages]


def _revert_query_expansion_flags(
    config_path: Path, dry_run: bool, *, sections: tuple[str, ...] = ("hyde", "rag_fusion")
) -> None:
    """Disable the given query-expansion *sections* and strip the provider keys
    the wizard wrote, so no startup guard fires post-rollback.

    Mirrors :func:`_revert_graph_enabled_flag`. ``run()`` writes ``enabled=true``
    (and, for non-anthropic providers, ``provider``/``model``/``ollama_base_url``)
    early — before the provider package is installed. If a later step aborts or an
    install fails, leaving those keys on disk would hard-fail the next server start
    via app.py's ``_check_provider_deps``: the anthropic guard is enabled-gated, but
    the ollama/openai guards fire unconditionally on the provider value. Stripping
    the provider keys reverts the section to the enabled-gated anthropic default,
    which does not fire when disabled.
    """
    if dry_run or not config_path.exists():
        return
    doc = tomlkit.parse(config_path.read_text())
    changed = False
    for section in sections:
        if section in doc and doc[section].get("enabled"):
            doc[section]["enabled"] = False
            for key in ("provider", "model", "ollama_base_url", "llama_cpp_base_url"):
                if key in doc[section]:
                    del doc[section][key]
            changed = True
    if changed:
        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())
        print(
            "Warning: HyDE / RAG Fusion have been disabled because the install did "
            "not complete. Re-run the wizard, or install the provider package "
            "manually — e.g. `pip install archon-search[hyde]` (Anthropic, the "
            "default), `archon-search[openai-provider]` (OpenAI), or "
            "`archon-search[ollama]` (Ollama) — then set enabled = true under "
            "[hyde] / [rag_fusion] in archon-search.toml.",
            file=sys.stderr,
        )


def _revert_graph_enrichment_flags(config_path: Path, dry_run: bool) -> None:
    """Strip the LLM-backed graph-enrichment keys the wizard wrote, on abort.

    Mirrors :func:`_revert_query_expansion_flags`, but scoped to ``[graph]``'s
    enrichment fields only. Unlike HyDE/RAG Fusion (which have their own
    ``enabled`` gate), ``[graph].provider`` IS the enrichment gate — this only
    strips ``provider``/``extraction_model``/``llama_cpp_base_url``/
    ``ollama_base_url``, and deliberately does NOT touch ``graph.enabled``,
    which independently gates the graph subsystem itself (entity extraction,
    PPR, communities) via
    :func:`archon_search.install.config_writer._revert_graph_enabled_flag`.
    """
    if dry_run or not config_path.exists():
        return
    doc = tomlkit.parse(config_path.read_text())
    if "graph" not in doc:
        return
    changed = False
    for key in ("provider", "extraction_model", "llama_cpp_base_url", "ollama_base_url"):
        if key in doc["graph"]:
            del doc["graph"][key]
            changed = True
    if changed:
        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())
        print(
            "Warning: LLM-backed graph enrichment has been disabled because the "
            "install did not complete. The graph subsystem (entity extraction, "
            "PPR, communities) is unaffected. Re-run the wizard, or set "
            "[graph].provider / extraction_model manually in archon-search.toml, "
            "to re-enable enrichment.",
            file=sys.stderr,
        )
