"""Interactive prompts and pickers for the install wizard."""
from __future__ import annotations

import json
import logging
import shutil
import urllib.request
from collections.abc import Callable

import click

from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT, OLLAMA_BASE_URL_DEFAULT
from archon_search.platform.types import GpuType
from archon_search.profiles import VALID_PROFILE_NAMES, InstallProfile

from .config_writer import WizardFeatures
from .render import _render_profile_table

logger = logging.getLogger(__name__)

_CHOICE_MAP = {"1": "minimal", "2": "balanced", "3": "max", "": "minimal"}


def _select_profile(
    profile_flag: str | None,
    multilingual_flag: bool,
    non_interactive: bool,
) -> tuple[str, bool]:
    """Select and validate an install profile.

    Returns a (profile_name, multilingual) tuple.
    """
    # Explicit profile flag: validate and return immediately.
    if profile_flag is not None:
        if profile_flag not in VALID_PROFILE_NAMES:
            raise click.BadParameter(
                f"{profile_flag!r} is not a valid profile. "
                f"Valid options: {sorted(VALID_PROFILE_NAMES)}",
                param_hint="--profile",
            )
        return (profile_flag, multilingual_flag)

    # Non-interactive: use defaults.
    if non_interactive:
        if not multilingual_flag:
            logger.info("No profile specified — defaulting to 'minimal'.")
            logger.info("No --multilingual flag — defaulting to English models.")
        else:
            logger.info("No profile specified — defaulting to 'minimal'.")
        return ("minimal", multilingual_flag)

    # Interactive: show table and prompt.
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    table = _render_profile_table(multilingual=multilingual_flag, width=terminal_width)
    if table:
        print(table)

    for attempt in range(3):
        try:
            raw = input("Choice [1-3, default 1]: ").strip()
        except EOFError:
            print("No input received (EOF). Aborting.")
            raise SystemExit(1)

        if raw in _CHOICE_MAP:
            return (_CHOICE_MAP[raw], multilingual_flag)

        remaining = 2 - attempt
        if remaining > 0:
            print(f"Invalid choice {raw!r}. Please enter 1, 2, or 3.")
        else:
            print("Too many invalid attempts. Aborting.")
            raise SystemExit(1)

    raise SystemExit(1)  # unreachable, satisfies type checker


_OLLAMA_FETCH_TIMEOUT_SECONDS = 5


def _fetch_ollama_models(base_url: str) -> list[str]:
    """Fetch installed model names from an Ollama server's ``/api/tags`` endpoint.

    Returns a sorted list of model names, or ``[]`` on any failure — connection
    refused, timeout, HTTP error, malformed JSON, or zero models installed.
    Never raises: the wizard falls back to free-text entry on an empty list.
    """
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=_OLLAMA_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 (operator-supplied Ollama URL)
            payload = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — best-effort probe; any failure (incl. http.client.IncompleteRead on a truncated body) → free-text fallback
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    # Guard on str: a malformed entry like {"name": 123} would make sorted() raise
    # TypeError (unorderable mixed types) — which must not escape this function.
    names = [n for m in models if isinstance(m, dict) and isinstance(n := m.get("name"), str) and n]
    return sorted(names)


def _pick_ollama_model(models: list[str]) -> str:
    """Show a numbered menu of ``models`` and return the chosen name.

    One retry on an out-of-range or non-numeric entry; returns ``""`` on EOF or
    after a second invalid entry (server startup then rejects the empty model).
    """
    print("\nInstalled Ollama models:")
    for i, name in enumerate(models, start=1):
        print(f"  {i}. {name}")
    for attempt in range(2):
        try:
            raw = input(f"Select a model by number [1-{len(models)}]: ").strip()
        except EOFError:
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(models):
            return models[int(raw) - 1]
        if attempt == 0:
            print(f"  Enter a number between 1 and {len(models)}.")
    return ""


def _prompt_model_freetext(feature_label: str) -> str:
    """Free-text model-name prompt with one retry; ``""`` on EOF or exhaustion."""
    for attempt in range(2):
        try:
            m = input(f"Model name for {feature_label} (required for non-Anthropic providers): ").strip()
        except EOFError:
            return ""
        if m:
            return m
        if attempt == 0:
            print("  Model name is required for non-Anthropic providers.")
    return ""


def _prompt_ollama_model(feature_label: str, default_base_url: str) -> tuple[str, str]:
    """Prompt for the Ollama base URL, then pick a model from the installed list.

    Base URL is asked first (needed to fetch the model list); an empty answer
    keeps ``default_base_url``. When models are found the user picks by number;
    when the server is unreachable or has none, the wizard explains why and
    falls back to free-text entry.

    Returns ``(ollama_base_url, model)``. ``ollama_base_url`` is ``""`` when it
    resolves to the built-in default (config supplies it), otherwise the custom
    URL so it survives config regeneration.
    """
    try:
        raw_url = input(f"Ollama base URL for {feature_label} [{default_base_url}]: ").strip()
    except EOFError:
        raw_url = ""
    base_url = raw_url or default_base_url
    stored = "" if base_url == OLLAMA_BASE_URL_DEFAULT else base_url

    models = _fetch_ollama_models(base_url)
    if models:
        return stored, _pick_ollama_model(models)

    print(
        f"  No models available from {base_url}.\n"
        f"  Ollama may not be running there, the address may be wrong, or no models are "
        f'installed yet. If it is running, install one with "ollama pull <model-name>" and\n'
        f"  re-run the wizard to pick from the list.\n"
        f"  Falling back to manual model-name entry."
    )
    return stored, _prompt_model_freetext(feature_label)


_LLAMA_CPP_FETCH_TIMEOUT_SECONDS = 5


def _fetch_llama_cpp_models(base_url: str) -> list[str]:
    """Fetch model ids from a llama-server's OpenAI-compatible ``/v1/models`` endpoint.

    Parses ``data[].id`` (NOT Ollama's ``models[].name`` shape). Returns ``[]`` on
    any failure — connection refused, timeout, HTTP error, malformed JSON, or zero
    models loaded. Never raises: the wizard falls back to free-text entry on an
    empty list.
    """
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=_LLAMA_CPP_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 (operator-supplied llama-server URL)
            payload = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — best-effort probe; any failure → free-text fallback
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    # Guard on str: a malformed entry like {"id": 123} must not crash the wizard.
    return [i for m in data if isinstance(m, dict) and isinstance(i := m.get("id"), str) and i]


def _pick_llama_cpp_model(models: list[str]) -> str:
    """Show a numbered menu of ``models`` and return the chosen name.

    One retry on an out-of-range or non-numeric entry; returns ``""`` on EOF or
    after a second invalid entry (server startup then rejects the empty model).
    """
    print("\nAvailable llama-server models:")
    for i, name in enumerate(models, start=1):
        print(f"  {i}. {name}")
    for attempt in range(2):
        try:
            raw = input(f"Select a model by number [1-{len(models)}]: ").strip()
        except EOFError:
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(models):
            return models[int(raw) - 1]
        if attempt == 0:
            print(f"  Enter a number between 1 and {len(models)}.")
    return ""


def _prompt_llama_cpp_model(feature_label: str) -> tuple[str, str]:
    """Prompt for the llama-server base URL, then pick a model from ``/v1/models``.

    Base URL is asked first (needed to fetch the model list); an empty answer
    keeps ``LLAMA_CPP_BASE_URL_DEFAULT``. When models are found the user picks by
    number; when the server is unreachable or has none, the wizard explains why
    and falls back to free-text entry.

    Returns ``(llama_cpp_base_url, model)``. ``llama_cpp_base_url`` is ``""``
    when it resolves to the built-in default (config supplies it), otherwise the
    custom URL so it survives config regeneration.
    """
    try:
        raw_url = input(f"llama-server base URL for {feature_label} [{LLAMA_CPP_BASE_URL_DEFAULT}]: ").strip()
    except EOFError:
        raw_url = ""
    base_url = raw_url or LLAMA_CPP_BASE_URL_DEFAULT
    stored = "" if base_url == LLAMA_CPP_BASE_URL_DEFAULT else base_url

    models = _fetch_llama_cpp_models(base_url)
    if models:
        return stored, _pick_llama_cpp_model(models)

    print(
        f"  No models available from {base_url}.\n"
        f"  llama-server may not be running there, or the address may be wrong.\n"
        f"  If it is running, make sure it is serving a model, then re-run the\n"
        f"  wizard to pick from the list.\n"
        f"  Falling back to manual model-name entry."
    )
    return stored, _prompt_model_freetext(feature_label)


def _prompt_graph_provider(
    ask_yn: "Callable[[str], bool]",
    ask_choice: "Callable[[str, set[str], str], str]",
) -> tuple[str, str, str]:
    """Ask whether to enable LLM-backed graph enrichment and gather its settings.

    Graph enrichment (community summaries, relationship labels) is optional and
    disabled by default — unlike HyDE/RAG Fusion, ``[graph].provider`` is itself
    the enable gate (there is no separate ``[graph].enabled`` for enrichment;
    that key gates the graph subsystem — entity extraction, PPR, communities —
    which works fine without enrichment). Offers the four v1 enrichment
    providers (anthropic/openai/ollama/llama_cpp) — ``claude_cli`` has no v1
    enrichment client (deferred; see ``EnrichmentClientFactory``) and is
    intentionally not offered here.

    Returns ``(provider, extraction_model, llama_cpp_base_url)``, all ``""``
    when the operator declines enrichment. ``extraction_model`` is always
    prompted (even for anthropic) because, unlike ``HyDEConfig``/
    ``RAGFusionConfig``, ``GraphConfig.extraction_model`` has no built-in
    default model.
    """
    print(
        "\nLLM-backed graph enrichment:\n"
        "  Uses an LLM to write community summaries and label relationship types\n"
        "  during graph community builds. Optional — the graph subsystem (entity\n"
        "  extraction, PPR, communities) works without it.\n"
        "  Default: disabled."
    )
    if not ask_yn("Enable LLM-backed graph enrichment? [y/N]: "):
        return "", "", ""

    provider = ask_choice(
        "Which provider for graph enrichment? (anthropic/openai/ollama/llama_cpp) [anthropic]: ",
        {"anthropic", "openai", "ollama", "llama_cpp"},
        "anthropic",
    )
    if provider == "llama_cpp":
        base_url, model = _prompt_llama_cpp_model("graph enrichment")
        return provider, model, base_url
    return provider, _prompt_model_freetext("graph enrichment"), ""


# Curated Claude model aliases shown by the wizard. The Claude CLI has no
# runtime `models` subcommand, so this list is hardcoded and MUST be kept
# current with Anthropic releases.
_CLAUDE_MODEL_ALIASES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


def _check_claude_cli_present() -> bool:
    """Warn (but don't block) when the ``claude`` command is not on PATH.

    Returns True when found. Mirrors the "wizard guides, doesn't block"
    decision — a missing CLI surfaces its error on first search, exactly as a
    missing ANTHROPIC_API_KEY does today.
    """
    if shutil.which("claude") is not None:
        return True
    print(
        "  WARNING: 'claude' command not found in PATH.\n"
        "  The claude_cli provider needs Claude Code installed and logged in.\n"
        "  Install it from https://claude.ai/code, then start the server.\n"
        "  Writing the config anyway — query expansion will fall back until 'claude' is available."
    )
    return False


def _pick_claude_model(feature_label: str) -> str:
    """Pick a Claude model: numbered alias, free-text full ID, or blank default.

    Returns ``""`` when the operator leaves it blank (Claude Code uses its own
    configured default) or on EOF.
    """
    print("\nClaude model aliases:")
    for i, name in enumerate(_CLAUDE_MODEL_ALIASES, start=1):
        print(f"  {i}. {name}")
    print("  Or type a full model ID (e.g. claude-haiku-4-5-20251001).")
    print("  Leave blank to use Claude Code's configured default.")
    try:
        raw = input(f"Model for {feature_label} (number, name, or blank): ").strip()
    except EOFError:
        return ""
    if not raw:
        return ""
    if raw.isdigit() and 1 <= int(raw) <= len(_CLAUDE_MODEL_ALIASES):
        return _CLAUDE_MODEL_ALIASES[int(raw) - 1]
    return raw


def _prompt_provider(
    feature_label: str,
    ask_choice: "Callable[[str, set[str], str], str]",
    ollama_base_url_default: str,
) -> tuple[str, str, str, str]:
    """Ask which provider to use for *feature_label* and gather its model settings.

    Returns ``(provider, model, ollama_base_url, llama_cpp_base_url)``. ``model``,
    ``ollama_base_url``, and ``llama_cpp_base_url`` are ``""`` when they resolve
    to the config default.
    """
    provider = ask_choice(
        f"Which provider for {feature_label}? "
        f"(anthropic/openai/ollama/claude_cli/llama_cpp) [anthropic]: ",
        {"anthropic", "openai", "ollama", "claude_cli", "llama_cpp"},
        "anthropic",
    )
    if provider == "ollama":
        base_url, model = _prompt_ollama_model(feature_label, ollama_base_url_default)
        return provider, model, base_url, ""
    if provider == "llama_cpp":
        base_url, model = _prompt_llama_cpp_model(feature_label)
        return provider, model, "", base_url
    if provider == "openai":
        return provider, _prompt_model_freetext(feature_label), "", ""
    if provider == "claude_cli":
        _check_claude_cli_present()
        return provider, _pick_claude_model(feature_label), "", ""
    return provider, "", "", ""


def _prompt_optional_features(
    non_interactive: bool,
    profile: InstallProfile,
    *,
    install_code: bool | None = None,
    disable_reranker: bool | None = None,
    enable_watch: bool | None = None,
    enable_telemetry: bool | None = None,
    eager_load: bool | None = None,
    routing_strategy: str | None = None,
    log_format: str | None = None,
    enable_hyde: bool | None = None,
    enable_rag_fusion: bool | None = None,
    hyde_ollama_base_url_default: str = OLLAMA_BASE_URL_DEFAULT,
    rag_fusion_ollama_base_url_default: str = OLLAMA_BASE_URL_DEFAULT,
) -> WizardFeatures:
    """Ask seven optional-feature questions after profile selection.

    Each keyword argument pre-answers its question when not None; None triggers
    an interactive prompt (or the default in non-interactive mode). The
    ``*_ollama_base_url_default`` values pre-fill the Ollama base-URL prompt so a
    re-run keeps the address already saved in config with a single Enter.
    """

    def _ask_yn(prompt_text: str, default: bool = False) -> bool:
        """Ask a yes/no question; return ``default`` on EOFError or empty input."""
        try:
            raw = input(prompt_text).strip().lower()
        except EOFError:
            return default
        return raw in {"y", "yes"}

    def _ask_choice(prompt_text: str, valid: set[str], default: str) -> str:
        """Ask a choice question with one retry; fall back to default on second invalid."""
        for attempt in range(2):
            try:
                raw = input(prompt_text).strip().lower()
            except EOFError:
                return default
            if not raw:
                return default
            if raw in valid:
                return raw
            if attempt == 0:
                print(f"Invalid value {raw!r}. Valid options: {sorted(valid)}")
        return default

    # --- install_code_extra + install_graph_extra (BE-11: bundled auto-install) ---
    print(
        "\nCode enrichment (tree-sitter) + code graphing:\n"
        "  Parses and indexes code files structurally — functions, classes, docstrings.\n"
        "  Installs tree-sitter language parsers (~50 MB) and graph enrichment (spaCy),\n"
        "  and enables graph.enabled in the generated config. Both are set up together\n"
        "  automatically so code graphing works out of the box. Recommended if your\n"
        "  corpus includes source code. Default: disabled."
    )
    if install_code is not None:
        _install_code_extra_val = install_code
    elif non_interactive:
        _install_code_extra_val = False
    else:
        _install_code_extra_val = _ask_yn(
            "Index code files (installs tree-sitter + graph enrichment, enables graph)? [y/N]: "
        )
    # [code] and [graph] are installed as a bundle — opting into code indexing
    # automatically includes graph enrichment, so guided (wizard) users never hit
    # the degraded-startup path (S9).
    _install_graph_extra_val = _install_code_extra_val

    # --- disable_reranker (skipped when profile has no reranker) ---
    if profile.reranker is not None:
        print(
            "\nReranker:\n"
            "  A second-stage cross-encoder model that re-scores results for better precision.\n"
            "  Disabling it reduces latency and RAM but lowers recall quality.\n"
            "  Default: enabled (for profiles that include a reranker)."
        )
    if profile.reranker is None:
        _disable_reranker_val = False
    elif disable_reranker is not None:
        _disable_reranker_val = disable_reranker
    elif non_interactive:
        _disable_reranker_val = False
    else:
        _disable_reranker_val = not _ask_yn("Keep reranker enabled? [Y/n]: ", default=True)

    # --- enable_watch ---
    print(
        "\nFilesystem watcher:\n"
        "  Monitors watched directories and automatically re-indexes files when they change.\n"
        "  Uses watchdog. Increases background CPU usage slightly.\n"
        "  Default: disabled."
    )
    if enable_watch is not None:
        _enable_watch_val = enable_watch
    elif non_interactive:
        _enable_watch_val = False
    else:
        _enable_watch_val = _ask_yn("Auto-watch directories and re-index on file changes? [y/N]: ")

    # --- enable_telemetry ---
    print(
        "\nLocal telemetry:\n"
        "  Logs per-query metadata (collection, result count, latency) to\n"
        "  ~/.archon-search/search-logs/. No query text is stored. Opt-in.\n"
        "  Default: disabled."
    )
    if enable_telemetry is not None:
        _enable_telemetry_val = enable_telemetry
    elif non_interactive:
        _enable_telemetry_val = False
    else:
        _enable_telemetry_val = _ask_yn("Enable local query telemetry? [y/N]: ")

    # --- eager_load_embedders ---
    print(
        "\nEager embedder loading:\n"
        "  Pre-loads the embedding model at server startup instead of on the first query.\n"
        "  Eliminates first-query latency (~5-15s on first search without this).\n"
        "  Default: disabled."
    )
    if eager_load is not None:
        _eager_load_val = eager_load
    elif non_interactive:
        _eager_load_val = False
    else:
        _eager_load_val = _ask_yn(
            "Pre-load embedding models at startup (eliminates first-query latency)? [y/N]: "
        )

    # --- routing_strategy ---
    print(
        "\nRouting strategy:\n"
        "  centroid: routes queries to collections using centroid similarity (fast, default).\n"
        "  hybrid: combines centroid with keyword scoring (slightly slower, more accurate\n"
        "  for mixed corpora with distinct topic clusters).\n"
        "  Default: centroid."
    )
    if routing_strategy is not None:
        _routing_val = routing_strategy
    elif non_interactive:
        _routing_val = "centroid"
    else:
        _routing_val = _ask_choice(
            "Routing strategy (centroid/hybrid) [centroid]: ",
            valid={"centroid", "hybrid"},
            default="centroid",
        )

    # --- log_format ---
    print(
        "\nLog format:\n"
        "  text: human-readable log lines (default).\n"
        "  json: structured JSON logs, suitable for log aggregation pipelines.\n"
        "  Default: text."
    )
    if log_format is not None:
        _log_format_val = log_format
    elif non_interactive:
        _log_format_val = "text"
    else:
        _log_format_val = _ask_choice(
            "Log format (text/json) [text]: ",
            valid={"text", "json"},
            default="text",
        )

    # --- HyDE / RAG Fusion (C15 Tier 2 / G10 BE-8) ---
    # No API key gate — prompt whenever interactive and not pre-answered.
    _hyde_provider_val = "anthropic"
    _hyde_model_val = ""
    _hyde_ollama_base_url_val = ""
    _hyde_llama_cpp_base_url_val = ""
    _rag_fusion_provider_val = "anthropic"
    _rag_fusion_model_val = ""
    _rag_fusion_ollama_base_url_val = ""
    _rag_fusion_llama_cpp_base_url_val = ""
    _graph_provider_val = ""
    _graph_extraction_model_val = ""
    _graph_llama_cpp_base_url_val = ""

    if enable_hyde is not None or enable_rag_fusion is not None:
        # One or both flags pre-answered — use them directly; no provider prompts.
        _enable_hyde_val = enable_hyde if enable_hyde is not None else False
        _enable_rag_fusion_val = enable_rag_fusion if enable_rag_fusion is not None else False
    elif non_interactive:
        _enable_hyde_val = False
        _enable_rag_fusion_val = False
    else:
        print(
            "\nAI query expansion (HyDE + RAG Fusion):\n"
            "  HyDE generates hypothetical answers to improve embedding recall.\n"
            "  RAG Fusion runs multiple query reformulations and merges results.\n"
            "  Providers:\n"
            "    anthropic  - Anthropic API (needs ANTHROPIC_API_KEY)\n"
            "    openai     - OpenAI API (needs OPENAI_API_KEY)\n"
            "    ollama     - runs locally, no API key\n"
            "    claude_cli - uses Claude Code's login, no API key\n"
            "  Default: disabled."
        )
        if _ask_yn("Enable AI query expansion (HyDE + RAG Fusion)? [y/N]: "):
            _enable_hyde_val = True
            _enable_rag_fusion_val = True

            # Provider selection for HyDE. Ollama asks the base URL first (needed
            # to fetch the installed-model list) then a numbered picker; OpenAI
            # keeps free-text model entry; claude_cli checks PATH then offers a
            # curated alias picker with a free-text fallback.
            (
                _hyde_provider_val,
                _hyde_model_val,
                _hyde_ollama_base_url_val,
                _hyde_llama_cpp_base_url_val,
            ) = _prompt_provider("HyDE", _ask_choice, hyde_ollama_base_url_default)

            # Provider selection for RAG Fusion (same flow, independent picker).
            (
                _rag_fusion_provider_val,
                _rag_fusion_model_val,
                _rag_fusion_ollama_base_url_val,
                _rag_fusion_llama_cpp_base_url_val,
            ) = _prompt_provider("RAG Fusion", _ask_choice, rag_fusion_ollama_base_url_default)
        else:
            _enable_hyde_val = False
            _enable_rag_fusion_val = False

        # Graph enrichment provider step (FE-2): independent of HyDE/RAG Fusion,
        # but shares their "truly interactive" gate — skipped whenever either
        # flag was pre-answered or the wizard is running non-interactively.
        _graph_provider_val, _graph_extraction_model_val, _graph_llama_cpp_base_url_val = (
            _prompt_graph_provider(_ask_yn, _ask_choice)
        )

    return WizardFeatures(
        install_code_extra=_install_code_extra_val,
        install_graph_extra=_install_graph_extra_val,
        disable_reranker=_disable_reranker_val,
        enable_watch=_enable_watch_val,
        enable_telemetry=_enable_telemetry_val,
        eager_load_embedders=_eager_load_val,
        routing_strategy=_routing_val,
        log_format=_log_format_val,
        enable_hyde=_enable_hyde_val,
        enable_rag_fusion=_enable_rag_fusion_val,
        hyde_provider=_hyde_provider_val,
        hyde_model=_hyde_model_val,
        hyde_ollama_base_url=_hyde_ollama_base_url_val,
        hyde_llama_cpp_base_url=_hyde_llama_cpp_base_url_val,
        rag_fusion_provider=_rag_fusion_provider_val,
        rag_fusion_model=_rag_fusion_model_val,
        rag_fusion_ollama_base_url=_rag_fusion_ollama_base_url_val,
        rag_fusion_llama_cpp_base_url=_rag_fusion_llama_cpp_base_url_val,
        graph_provider=_graph_provider_val,
        graph_extraction_model=_graph_extraction_model_val,
        graph_llama_cpp_base_url=_graph_llama_cpp_base_url_val,
    )


def _prompt_multilingual(non_interactive: bool, flag_value: bool | None) -> bool:
    """Ask whether the corpus includes non-English documents.

    Returns ``flag_value`` unchanged when it is not None (flag takes precedence).
    Returns False in non-interactive mode (default: English).
    Otherwise prints a yes/no prompt and returns the user's answer.
    """
    if flag_value is not None:
        return flag_value
    if non_interactive:
        return False
    try:
        raw = input("Will your corpus include non-English documents? [y/N]: ").strip().lower()
    except EOFError:
        print("No input received (EOF). Using English.")
        return False
    return raw in {"y", "yes"}


def _prompt_gpu_confirm(non_interactive: bool, gpu: GpuType) -> bool:
    """Ask the user to confirm GPU acceleration after auto-detection.

    Returns True immediately when ``gpu`` is ``GpuType.NONE`` (nothing to confirm).
    Returns True immediately when ``non_interactive`` is True (auto-enable).
    For Metal or CUDA GPUs, prints a [Y/n] prompt; accepts "n"/"no" as decline.
    On EOFError returns True (auto-enable).
    """
    if gpu == GpuType.NONE:
        return True
    if non_interactive:
        return True
    if gpu == GpuType.METAL:
        prompt = "Apple Silicon detected — enable Metal acceleration? [Y/n]: "
    else:
        prompt = "NVIDIA GPU detected — enable CUDA acceleration? [Y/n]: "
    try:
        raw = input(prompt).strip().lower()
    except EOFError:
        return True
    return raw not in {"n", "no"}
