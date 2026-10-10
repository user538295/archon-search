"""Extra-package installers plus the query-expansion flag revert."""
from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import click
import tomlkit

from archon_search._durable_io import atomic_write_bytes
from archon_search.uv_tool import (
    TOOL_NAME,
    UnsupportedReceiptError,
    extra_install_hint,
    find_receipt,
    install_extra_command,
    install_option_flags,
    installed_torch_backend,
    load_receipt,
    tool_env,
    unrecorded_install_hint,
)

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


def _install_extra(extra: str, label: str, dry_run: bool = False) -> None:
    """Install ``archon-search[<extra>]`` into the running interpreter's environment.

    Prints ``[dry-run] Would install archon-search[<extra>]`` and returns early
    when *dry_run* is True.

    In a ``uv tool install archon-search`` environment (``sys.prefix`` holds a
    ``uv-receipt.toml``) the extra is recorded in uv's receipt — see
    :func:`_uv_tool_install_extra` — so ``uv tool upgrade`` keeps it. Anywhere
    else (pip venv, pipx, checkout): ``uv pip install --python <sys.executable>``
    with a ``sys.executable -m pip install`` fallback.

    Raises ``InstallError`` when the install fails.
    """
    package = f"{TOOL_NAME}[{extra}]"
    if dry_run:
        click.echo(f"[dry-run] Would install {package}")
        return

    click.echo(f"Installing {label}...")
    receipt_path = find_receipt(Path(sys.prefix))
    if receipt_path is None:
        _uv_or_pip_install([package], [], f"Failed to install {package}")
    else:
        _uv_tool_install_extra(receipt_path, extra, package)
    click.echo(f"{label.capitalize()} installed.")


def _uv_tool_install_extra(receipt_path: Path, extra: str, package: str) -> None:
    """Add *extra* to the uv tool environment owning *receipt_path*.

    Re-runs ``uv tool install`` with the receipt's own requirements, options
    and excludes (fed on stdin) plus the extra (never ``--reinstall``/``--force``:
    those recreate the env under the running wizard), so uv syncs the env in
    place and records the extra. ``--torch-backend`` follows the installed
    torch build, which keeps a ``+cpu`` or wizard-installed ``+cu126`` torch
    through the re-resolve. There is no pip fallback: uv tool environments ship
    no pip.

    A receipt (or torch build) the command cannot reproduce faithfully falls
    back to ``uv pip install`` with the receipt's index options and a warning:
    the extra works now but the next ``uv tool upgrade`` removes it.
    """
    receipt = None
    env = None
    stdin = None
    try:
        receipt = load_receipt(receipt_path)
        command, stdin = install_extra_command(receipt, extra, backend=installed_torch_backend())
        env = tool_env(receipt_path, receipt, os.environ)
    except UnsupportedReceiptError as exc:
        print(
            f"Warning: cannot record {package} in the uv tool receipt ({exc}); installing it "
            f"with `uv pip install` instead, so the next `uv tool upgrade {TOOL_NAME}` will "
            f"remove it. To keep it, run: {unrecorded_install_hint(extra)}",
            file=sys.stderr,
        )
        command = ["uv", "pip", "install", "--python", sys.executable, package, *_fallback_option_flags(receipt)]
        stdin = None
    try:
        subprocess.run(
            command, check=True, capture_output=True, env=env, input=stdin.encode() if stdin is not None else None
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace")
        raise InstallError(f"Failed to install {package}: {stderr}") from exc
    except OSError as exc:
        raise InstallError(f"Failed to install {package}: {exc}") from exc


def _fallback_option_flags(receipt: dict[str, object] | None) -> list[str]:
    """The receipt's index options for the ``uv pip install`` fallback; none when unreadable or unrenderable."""
    if receipt is None:
        return []
    try:
        return install_option_flags(receipt)
    except UnsupportedReceiptError:
        return []


# ---------------------------------------------------------------------------
# CUDA torch swap (wizard-cuda-torch-upgrade)
# ---------------------------------------------------------------------------

# PyTorch CUDA wheel index for the GPU torch swap. The `+<tag>` local build the
# swap pins (below) is derived from this URL's last path segment, so the index
# and the pinned build can never drift apart. cu126 is the lowest CUDA toolkit
# still shipping the S269-pinned torch/torchvision cp312 wheels, so it has the
# widest NVIDIA-driver compatibility (cu129/cu130 demand much newer drivers).
# Verified against download.pytorch.org/whl/cu126/{torch,torchvision}/ on
# 2026-08-02. Only used on linux/x86_64 (see the gating in installer.py).
_CUDA_TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu126"
# The local-version tag ("cu126") for the pinned CUDA build — parsed from the
# index URL so the two stay coupled by construction.
_CUDA_LOCAL_TAG = _CUDA_TORCH_INDEX_URL.rsplit("/", 1)[-1]


def _install_cuda_torch(dry_run: bool = False) -> None:
    """Best-effort reinstall of torch/torchvision as the CUDA build.

    Reinstalls torch/torchvision as the ``+cu126`` build of the SAME public
    version that is installed, so docling's PDF/OCR parsing runs on an NVIDIA
    GPU. The installed build is ``+cpu`` for a ``uv sync`` checkout (S269 pin,
    linux/x86_64 only) or a CPU-only install, and PyPI's CUDA build for a plain
    pip / ``uv tool`` install on Linux. The version is derived from the
    installed build (``importlib.metadata``, stripping the local tag) and
    re-pinned to the exact CUDA local build, e.g. ``torch==2.13.0+cu126``.

    The local ``+cu126`` tag is REQUIRED: a bare public specifier ``==2.13.0``
    is satisfied by the installed ``2.13.0+cpu`` (PEP 440 ignores the local label
    when the requirement has none), so pip/uv would report "already satisfied"
    and the swap would silently do nothing. Pinning the local build forces the
    reinstall. ``--extra-index-url`` (not ``--index-url``) keeps PyPI available
    for any transitive dependency the CUDA build needs.

    Best-effort: on any failure (torch not installed, both uv and pip fail) it
    logs a warning to stderr and returns, leaving any installed build in place.
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
            f"skipping the CUDA swap: {exc}",
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
            f"Warning: CUDA torch install failed; keeping the installed build: {exc}",
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
GRAPH_MODEL_SIZE_ESTIMATE = SizeEstimateSpec(
    name="gliner-relex-multi-v1.0", declared_mb=1218, license="Apache-2.0"
)


def _install_code_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[code]`` (tree-sitter enrichment packages).

    Thin wrapper around :func:`_install_extra`.  Public interface unchanged.
    """
    _install_extra("code", "code enrichment", dry_run)


def _install_graph_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[graph]`` (gliner prose-extraction packages).

    Thin wrapper around :func:`_install_extra`.  The gliner NER checkpoint is
    fetched lazily by ``GLiNER.from_pretrained`` at first ingest, never through
    the wizard, so no model provisioning happens here.
    """
    _install_extra("graph", "graph enrichment", dry_run)


def _install_multilingual_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[multilingual]`` (fasttext-wheel language detection).

    Thin wrapper around :func:`_install_extra`.  Required by the server whenever
    ``[database].multilingual = true``; without it ``_check_multilingual_deps``
    hard-fails at startup.
    """
    _install_extra("multilingual", "multilingual language detection", dry_run)


# Maps a HyDE / RAG Fusion provider to the archon-search extra that supplies its package.
# 'claude_cli' is absent by design: it has no pip package (availability is the
# `claude` binary on PATH). [hyde] and [rag_fusion] are byte-identical extras
# (both pull anthropic>=0.40), so anthropic maps to a single name — this is what
# lets the dedup below collapse anthropic-backed HyDE + RAG Fusion to one install.
_PROVIDER_EXTRA: dict[str, str] = {
    "anthropic": "hyde",
    "openai": "openai-provider",
    "ollama": "ollama",
}


def _install_query_expansion_extras(features: WizardFeatures, dry_run: bool = False) -> list[str]:
    """Install provider packages for enabled HyDE / RAG Fusion features.

    Resolves each enabled feature's provider to its archon-search extra and
    installs each unique extra once (so the common case — both features on the
    default anthropic provider — installs a single extra, while mixed providers
    install each). ``claude_cli`` needs no package and is skipped. Returns the
    list of feature SECTIONS (``"hyde"`` / ``"rag_fusion"``) whose provider extra
    failed to install (empty when all succeeded); never raises, so the caller can
    revert exactly the flags whose extra failed. When both features share an
    extra (both on anthropic), a single failure marks BOTH sections failed.
    """
    # Map each enabled section to its provider extra (None = no package, e.g. claude_cli).
    section_extras: list[tuple[str, str | None]] = []
    if features.enable_hyde:
        section_extras.append(("hyde", _PROVIDER_EXTRA.get(features.hyde_provider)))
    if features.enable_rag_fusion:
        section_extras.append(("rag_fusion", _PROVIDER_EXTRA.get(features.rag_fusion_provider)))

    # Install each distinct extra once.
    extras: list[str] = []
    for _section, extra in section_extras:
        if extra and extra not in extras:
            extras.append(extra)
    failed_extras: list[str] = []
    for extra in extras:
        try:
            _install_extra(extra, "AI query expansion", dry_run)
        except InstallError as exc:
            print(
                f"Warning: AI query expansion install failed for {TOOL_NAME}[{extra}]: {exc}",
                file=sys.stderr,
            )
            failed_extras.append(extra)

    # Report the sections whose extra failed (a shared failed extra fails both).
    return [section for section, extra in section_extras if extra is not None and extra in failed_extras]


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
            f"manually — e.g. `{extra_install_hint('hyde')}` (Anthropic, the "
            f"default), `{extra_install_hint('openai-provider')}` (OpenAI), or "
            f"`{extra_install_hint('ollama')}` (Ollama) — then set enabled = true under "
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
