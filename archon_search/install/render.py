"""Output rendering: profile table, install summary, next-steps guidance."""
from __future__ import annotations

from pathlib import Path

from archon_search.config import get_default_config_path
from archon_search.profiles import InstallProfile, get_profile

from .config_writer import WizardFeatures

_PROFILE_ORDER = ("minimal", "balanced", "max")
_PROFILE_CAPS = {"minimal": "Minimal", "balanced": "Balanced", "max": "Max"}


def _render_profile_table(multilingual: bool, width: int = 80) -> str:
    """Return a formatted profile comparison table string. Does NOT print."""
    profiles = [get_profile(name, multilingual) for name in _PROFILE_ORDER]
    lines: list[str] = []

    if width >= 80:
        # Header
        lines.append("  Profile      Download    Quality       Speed (CPU / Apple Silicon)")
        lines.append("  ─────────    ────────    ───────       ───────────────────────────")
        # Data rows
        for n, (name, p) in enumerate(zip(_PROFILE_ORDER, profiles), start=1):
            if p.download_mb >= 1000:
                size_str = f"~{p.download_mb / 1000:.1f} GB"
            else:
                size_str = f"~{p.download_mb} MB"
            cap = _PROFILE_CAPS[name]
            recommended = "  ← Recommended" if name == "balanced" else ""
            lines.append(
                f"  {n}) {cap:<9}  {size_str:<10}  {p.quality_stars:<12}  "
                f"~{p.cpu_ms} ms/query  / ~{p.metal_ms} ms{recommended}"
            )
        lines.append("")
        # Models section
        lines.append("  Models (all sizes from fastembed registry, verified):")
        for n, (name, p) in enumerate(zip(_PROFILE_ORDER, profiles), start=1):
            if p.reranker is None:
                lines.append(f"  {n}) {p.embedder}  (no reranker)")
            else:
                lines.append(f"  {n}) {p.embedder} + {p.reranker}")
        lines.append("")
        # Best-for section
        lines.append("  Best for:")
        lines.append("  1) Personal use, <10k docs, fast responses, low RAM")
        lines.append("  2) Team use, 10k–200k docs, good recall, ~1 GB RAM")
        lines.append("  3) Large corpora, 200k+ docs, highest precision, ~2.5 GB RAM")
    else:
        # Narrow: one line per profile
        for n, (name, p) in enumerate(zip(_PROFILE_ORDER, profiles), start=1):
            if p.download_mb >= 1000:
                size_str = f"~{p.download_mb / 1000:.1f} GB"
            else:
                size_str = f"~{p.download_mb} MB"
            cap = _PROFILE_CAPS[name]
            star = "*" if name == "balanced" else ""
            if p.reranker is not None:
                lines.append(f"  {n}) {cap}{star}: {p.embedder} + {p.reranker} ({size_str})")
            else:
                lines.append(f"  {n}) {cap}{star}: {p.embedder} (no reranker) ({size_str})")
        lines.append("  * Recommended for most users")

    lines.append("")
    if not multilingual:
        lines.append("  Add --multilingual to use multilingual models instead.")
    else:
        lines.append("  (Showing multilingual models)")

    return "\n".join(lines)


def _render_summary(
    profile_name: str,
    profile: InstallProfile,
    multilingual: bool,
    providers: list[str],
    features: WizardFeatures | None = None,
    *,
    db_path: str = "",
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key_file: str = "",
    download_mb: int = 0,
) -> str:
    """Return an install summary block string. Does NOT print."""
    cap = _PROFILE_CAPS.get(profile_name, profile_name.capitalize())
    lang = "Multilingual" if multilingual else "English"
    reranker_str = profile.reranker if profile.reranker is not None else "(none)"
    providers_str = ", ".join(providers) if providers else "(CPU default)"
    lines = [
        f"  Installing: {cap} · {lang}",
        f"  Embedder:   {profile.embedder}",
        f"  Reranker:   {reranker_str}",
        f"  Chunk size: {profile.chunk_size} tokens",
        f"  Providers:  {providers_str}",
    ]
    if db_path:
        lines.append(f"  Database:   {db_path}")
    lines.append(f"  Server:     http://{host}:{port}")
    # API key display: mask first-8...last-4; show path for reference
    api_key_display = _mask_api_key(api_key_file)
    lines.append(f"  API key:    {api_key_display}  (full key: {api_key_file or _KEY_FILE_PLACEHOLDER})")
    if download_mb:
        lines.append(f"  Download:   ~{download_mb} MB")
    lines += [
        "",
        "  Note: Model files are downloaded now. ONNX session initialization happens in the",
        "  server process on first query — expect ~5–15s latency on first search.",
    ]
    if features is not None:
        feature_bullets: list[str] = []
        if features.install_code_extra:
            feature_bullets.append("• Code enrichment (tree-sitter)")
        if features.install_graph_extra:
            feature_bullets.append("• Graph enrichment (code graphing)")
        if features.install_multilingual_extra:
            feature_bullets.append("• Language detection (fasttext)")
        if features.enable_hyde:
            feature_bullets.append(f"• HyDE: enabled (provider: {features.hyde_provider})")
        if features.enable_rag_fusion:
            feature_bullets.append(f"• RAG Fusion: enabled (provider: {features.rag_fusion_provider})")
        if features.disable_reranker:
            feature_bullets.append("• Reranker disabled")
        if features.enable_watch:
            feature_bullets.append("• Watch directories (auto-reindex)")
        if features.enable_telemetry:
            feature_bullets.append("• Telemetry enabled")
        if features.eager_load_embedders:
            feature_bullets.append("• Eager load models at startup")
        if features.routing_strategy not in (None, "centroid"):
            feature_bullets.append(f"• Routing: {features.routing_strategy}")
        if features.log_format not in (None, "text"):
            feature_bullets.append(f"• Log format: {features.log_format}")
        if feature_bullets:
            lines.append("")
            lines.append("  Optional features:")
            lines.extend(f"    {b}" for b in feature_bullets)
    return "\n".join(lines)


# SYNC: must match the default returned by `key_manager.get_key_file()`
# (i.e. `get_data_dir() / ".search.env"` when neither ARCHON_SEARCH_KEY_FILE
# nor ARCHON_SEARCH_DATA_DIR is set). Display-only fallback for the
# pre-install summary when `api_key_file` is not yet known; never used as
# a real path. If the data-dir default ever shifts (XDG compliance etc.),
# update this string alongside `archon_search.paths.get_data_dir()`.
_KEY_FILE_PLACEHOLDER = "~/.archon-search/.search.env"


def _mask_api_key(api_key_file: str) -> str:
    """Read the API key from file and return a masked representation.

    Returns 'first-8...last-4' if the key is long enough, or
    '(not yet generated)' if the file doesn't exist or key can't be read.
    """
    if not api_key_file:
        return "(not yet generated)"
    key_path = Path(api_key_file)
    try:
        content = key_path.read_text()
    except OSError:
        return "(not yet generated)"
    for line in content.splitlines():
        if line.startswith("ARCHON_SEARCH_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if len(key) >= 12:
                return f"{key[:8]}…{key[-4:]}"
            return "(not yet generated)"
    return "(not yet generated)"


def _print_next_steps(host: str, port: int, api_key_file: str) -> None:
    """Print post-install guidance to stdout."""
    from archon_search import key_manager
    key_path = Path(api_key_file) if api_key_file else key_manager.get_key_file()
    print(f"\narchon-search is running on http://{host}:{port}\n")
    print("Next steps:")
    print("  archon-search ingest --path <path>    # add documents to search")
    print("  archon-search status                  # check service health")
    print("  archon-search sync                    # sync watched directories")
    print("  archon-search stop                    # stop the service")
    print("  archon-search wizard --top-k 20       # increase results per query (default: 5)")
    print(f"\nAPI key: (full key: {key_path})")
    print(f"Config:  {get_default_config_path()}")
