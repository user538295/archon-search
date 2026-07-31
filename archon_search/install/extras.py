"""Extra-package installers plus the query-expansion flag revert."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
import tomlkit

from archon_search._durable_io import atomic_write_bytes

from .config_writer import WizardFeatures
from .errors import InstallError


# ---------------------------------------------------------------------------
# Code enrichment package install (Task C8-2.3)
# ---------------------------------------------------------------------------

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
    python = sys.executable
    try:
        subprocess.run(
            ["uv", "pip", "install", "--python", python, package],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # uv not found or failed — fall back to pip
        try:
            subprocess.run(
                [python, "-m", "pip", "install", package],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as pip_exc:
            stderr = (pip_exc.stderr or b"").decode(errors="replace")
            raise InstallError(f"Failed to install {package}: {stderr}") from pip_exc

    click.echo(f"{label.capitalize()} installed.")


def _install_code_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[code]`` (tree-sitter enrichment packages).

    Thin wrapper around :func:`_install_extra`.  Public interface unchanged.
    """
    _install_extra("archon-search[code]", "code enrichment", dry_run)


def _install_graph_extra(dry_run: bool = False) -> None:
    """Install ``archon-search[graph]`` and the spaCy model.

    PyPI prohibits direct URL dependencies, so ``en_core_web_sm`` cannot be
    declared in the package extras. It is installed separately as the
    ``en-core-web-sm`` PyPI wheel via ``uv pip install`` — the same route
    :func:`_install_extra` uses for every other package, avoiding the
    virtual-environment-detection assumption that ``python -m spacy download``
    makes (which fails in a uv-tool install context).
    """
    _install_extra("archon-search[graph]", "graph enrichment", dry_run)
    if dry_run:
        click.echo("[dry-run] Would run: uv pip install en-core-web-sm")
        return
    click.echo("Downloading spaCy model en_core_web_sm...")
    try:
        subprocess.run(
            ["uv", "pip", "install", "--python", sys.executable, "en-core-web-sm"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = ": " + (exc.stderr or b"").decode(errors="replace").strip()
        print(
            f"Warning: spaCy model download failed{detail}. "
            "The model will be downloaded automatically on first graph ingest.",
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
            for key in ("provider", "model", "ollama_base_url"):
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
