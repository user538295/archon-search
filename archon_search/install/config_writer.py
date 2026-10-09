"""Config/TOML writing plus the shared ``WizardFeatures`` dataclass."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path

import tomlkit

from archon_search._durable_io import atomic_write_bytes
from archon_search.config import SearchConfig
from archon_search.profiles import InstallProfile, get_profile

from .errors import InstallError


# ---------------------------------------------------------------------------
# WizardFeatures dataclass (Task C8-1.1)
# ---------------------------------------------------------------------------

@dataclass
class WizardFeatures:
    """Carries optional-feature choices from wizard prompt functions to config writer."""

    install_code_extra: bool = False
    install_graph_extra: bool = False
    install_multilingual_extra: bool = False
    disable_reranker: bool = False
    # None = never answered (key omitted from config); a value = explicitly chosen (key written),
    # even when it equals the config default (e.g. `--no-watch`, `--routing-strategy centroid`).
    # Plain defaults cannot tell "explicitly asked for the default" from "never answered".
    enable_watch: bool | None = None
    enable_telemetry: bool | None = None
    eager_load_embedders: bool | None = None
    routing_strategy: str | None = None
    log_format: str | None = None
    # C15 Tier 1 deployment flags
    host: str | None = None
    port: int | None = None
    db_path: str | None = None
    log_level: str | None = None
    top_k: int | None = None
    telemetry_retention_days: int | None = None
    # C15 Tier 2 AI query expansion flags — same tri-state as above: None = never
    # answered (no `enabled` key), False = explicitly declined (`enabled = false`).
    enable_hyde: bool | None = None
    enable_rag_fusion: bool | None = None
    # G10 BE-8: provider-selection for HyDE / RAG Fusion
    hyde_provider: str = "anthropic"          # "anthropic" | "openai" | "ollama" | "claude_cli" | "llama_cpp"
    hyde_model: str = ""                      # empty = use provider default
    hyde_ollama_base_url: str = ""            # empty = use config default
    hyde_llama_cpp_base_url: str = ""         # empty = use config default
    rag_fusion_provider: str = "anthropic"
    rag_fusion_model: str = ""
    rag_fusion_ollama_base_url: str = ""
    rag_fusion_llama_cpp_base_url: str = ""
    # FE-2: [graph] LLM-backed enrichment provider (independent of install_graph_extra/graph.enabled)
    graph_provider: str = ""                  # "" = enrichment disabled (GraphConfig.provider stays None)
    graph_extraction_model: str = ""
    graph_llama_cpp_base_url: str = ""        # empty = use config default
    graph_ollama_base_url: str = ""           # empty = use config default


# Tri-state optional keys written by `_apply_wizard_features_to_toml`:
# (TOML section, TOML key, WizardFeatures field, dotted SearchConfig attribute holding the default).
# `[hyde]`/`[rag_fusion]` `enabled` share the tri-state but are deliberately not listed: True also
# writes a provider block, so they have their own writer (`_apply_query_expansion`).
_OPTIONAL_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("database", "eager_load_embedders", "eager_load_embedders", "eager_load_embedders"),
    ("collections", "watch", "enable_watch", "watch"),
    ("telemetry", "enabled", "enable_telemetry", "telemetry.enabled"),
    ("routing", "routing_strategy", "routing_strategy", "routing_strategy"),
    ("logging", "format", "log_format", "log_format"),
)

# `[database]` keys every run rewrites from the chosen profile (`_apply_profile_keys`).
_PROFILE_DATABASE_KEYS: tuple[str, ...] = (
    "embedding_model", "reranker_model", "chunk_size", "profile", "multilingual",
)

# Non-profile keys a re-run rewrites or removes from its own answers even when no flag names them:
# (TOML section, TOML key, dotted SearchConfig attribute holding the default).
_DEFAULTED_RERUN_KEYS: tuple[tuple[str, str, str], ...] = (
    *((section, key, default_attr) for section, key, _, default_attr in _OPTIONAL_KEYS),
    ("hyde", "enabled", "hyde.enabled"),
    ("rag_fusion", "enabled", "rag_fusion.enabled"),
    ("telemetry", "retention_days", "telemetry.retention_days"),
)

# The keys hand-edit detection guards: the keys a re-run rewrites or removes from its own answers
# even when no flag names them, except `[database].providers`. Deployment keys (`[server]`,
# `db_path`, `[logging].level`, top-k), `[graph]` and the HyDE/RAG Fusion provider/model keys are
# excluded: a re-run writes them only when their flag or answer is given. `[database].providers` is
# excluded although Step 9 rewrites it from the GPU answer: a declined GPU prompt, `--disable-gpu`
# or a failed CoreML check writes `[]` on every such run, and `configure_providers` keeps a chain
# that already holds the detected provider, so guarding it would warn on re-runs that leave it unchanged.
_RERUN_MANAGED_KEYS: tuple[tuple[str, str], ...] = (
    *(("database", key) for key in _PROFILE_DATABASE_KEYS),
    *((section, key) for section, key, _ in _DEFAULTED_RERUN_KEYS),
)


def _profile_database_values(profile: InstallProfile, profile_name: str, multilingual: bool) -> dict[str, object]:
    """Return the ``[database]`` values a run writes for the chosen profile, keyed by ``_PROFILE_DATABASE_KEYS``."""
    return dict(
        zip(
            _PROFILE_DATABASE_KEYS,
            (
                profile.embedder,
                profile.reranker if profile.reranker is not None else "",
                profile.chunk_size,
                profile_name,
                multilingual,
            ),
            strict=True,
        )
    )


def _apply_profile_keys(
    doc: tomlkit.TOMLDocument, profile: InstallProfile, profile_name: str, multilingual: bool
) -> None:
    """Write the chosen profile's ``[database]`` answers into *doc* in-place."""
    if "database" not in doc:
        doc.add("database", tomlkit.table())

    db = doc["database"]
    for key, value in _profile_database_values(profile, profile_name, multilingual).items():
        db[key] = value


def _fresh_config_doc() -> tomlkit.TOMLDocument:
    """Return the base document of a fresh install: only the loopback ``[server].host``.

    Every unanswered key is left out so it resolves to its ``SearchConfig`` default
    (20_wizard.md: "Only the choices you actually made are written"), with one
    deliberate security exception: ``[server].host`` is always written as the
    loopback default. Without it, ``archon-search serve`` (``load_config(serve=True)``)
    would default the bind address to ``0.0.0.0`` and expose the server on every
    interface. An answered ``--host`` still overrides it via the feature overlay.
    """
    doc = tomlkit.document()
    doc.add("server", tomlkit.table())
    doc["server"]["host"] = SearchConfig().host
    return doc


def _write_profile_config(
    config_path: Path,
    profile: InstallProfile,
    profile_name: str,
    multilingual: bool,
    features: WizardFeatures | None = None,
    clear_collections: bool = False,
) -> None:
    """Overlay the profile and wizard answers onto *config_path*, keeping every other key.

    A missing *config_path* (``--force --delete-db`` with no existing config) starts
    from the same base as a fresh install (:func:`_fresh_config_doc`).

    ``clear_collections`` (``--force --delete-db``) empties the
    ``collections``/``pinned_collections`` registrations the file already holds —
    the store they point at was just wiped — without adding either key to a file
    that never had it.
    """
    doc = tomlkit.parse(config_path.read_text()) if config_path.exists() else _fresh_config_doc()

    _apply_profile_keys(doc, profile, profile_name, multilingual)

    if features is not None:
        _apply_wizard_features_to_toml(doc, features)

    if clear_collections and "collections" in doc:
        for key in ("collections", "pinned_collections"):
            if key in doc["collections"]:
                doc["collections"][key] = []

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())


def _profile_toml(profile_name: str, multilingual: bool, features: WizardFeatures | None = None) -> str:
    """Render a fresh-install config holding only the operator's answers (see :func:`_fresh_config_doc`)."""
    doc = _fresh_config_doc()
    _apply_profile_keys(doc, get_profile(profile_name, multilingual), profile_name, multilingual)

    if features is not None:
        _apply_wizard_features_to_toml(doc, features)

    return tomlkit.dumps(doc)


def _reconcile_ollama_base_url(section: tomlkit.items.Table, base_url: str) -> None:
    """Set or clear ``ollama_base_url`` on a config *section* table.

    The wizard mutates an existing config on re-run, so a plain conditional write
    would leak a previously-saved custom URL when the operator keeps Ollama but
    reverts the URL to the default (``base_url == ""``). Writing authoritatively —
    set a custom value, else delete any stale key so config falls back to
    ``OLLAMA_BASE_URL_DEFAULT`` — keeps the stored URL in step with that choice.

    Scope: this only runs while the section's provider is still Ollama. Switching
    provider *away* is not handled: the ``anthropic`` branch of
    ``_apply_query_expansion`` returns before touching the provider keys, so a stale
    non-anthropic provider block from an earlier run is not cleared (known gap, not
    tracked in a backlog file).
    """
    if base_url:
        section["ollama_base_url"] = base_url
    elif "ollama_base_url" in section:
        del section["ollama_base_url"]


def _reconcile_llama_cpp_base_url(section: tomlkit.items.Table, base_url: str) -> None:
    """Set or clear ``llama_cpp_base_url`` on a config *section* table.

    Mirrors :func:`_reconcile_ollama_base_url` for the llama.cpp base URL — see
    that function's docstring for the re-run leak this prevents.
    """
    if base_url:
        section["llama_cpp_base_url"] = base_url
    elif "llama_cpp_base_url" in section:
        del section["llama_cpp_base_url"]


def _apply_wizard_features_to_toml(doc: tomlkit.TOMLDocument, features: WizardFeatures) -> None:
    """Write WizardFeatures fields to *doc* in-place.

    Only the operator's actual choices are written (20_wizard.md §"Only the
    choices you actually made are written"). ``enable_watch``,
    ``enable_telemetry``, ``eager_load_embedders``, ``routing_strategy``,
    ``log_format``, ``enable_hyde`` and ``enable_rag_fusion`` are optional
    because a bare default cannot distinguish "explicitly asked for the default"
    from "never answered": ``None`` means unanswered — the key is removed if a
    prior run wrote it (re-run safe) — and any value, including ``False`` or one
    equal to the config default, is written. ``disable_reranker`` stays
    conditional — re-run safety there is provided by the profile writer, which
    writes the profile's ``reranker_model`` default *before* calling this
    function. A missing section is created via tomlkit.table() only when a key
    is written into it, and a table left empty — not even a comment — by a
    removed key is dropped. ``install_code_extra`` itself is
    intentionally NOT written — it controls a subprocess install, not a config
    key. ``install_graph_extra`` also controls a subprocess install, but
    additionally writes ``graph.enabled = true`` (BE-11): without it, the
    auto-installed ``[graph]`` extras are inert (C1-I-1).
    ``install_multilingual_extra`` is likewise NOT written here — unlike graph,
    the ``[database].multilingual`` key is written separately by the profile
    config writer (``_write_profile_config``/``_profile_toml``), so no overlay
    flag is needed.
    """

    def _ensure_section(name: str) -> None:
        if name not in doc:
            doc.add(name, tomlkit.table())

    def _remove_key(section: str, key: str) -> None:
        """Drop a stale key; drop its table too once nothing — not even a comment — is left."""
        if section in doc and key in doc[section]:
            del doc[section][key]
            # Rendered text, not a key count: a table holding only a comment must survive.
            if not doc[section].as_string().strip():
                del doc[section]

    def _set_optional(section: str, key: str, value: bool | str | None) -> None:
        """None = unanswered (key removed); any value is written, even if it equals the default.

        Removing a key also drops its table when nothing — not even a comment — is left
        (see ``_remove_key``).
        """
        if value is None:
            _remove_key(section, key)
        else:
            _ensure_section(section)
            doc[section][key] = value

    def _apply_query_expansion(
        section: str,
        enabled: bool | None,
        provider: str,
        model: str,
        ollama_base_url: str,
        llama_cpp_base_url: str,
    ) -> None:
        """Write one C15 Tier 2 query-expansion table (``[hyde]`` or ``[rag_fusion]``).

        True writes ``enabled = true`` plus the provider block (G10 BE-8); False/None go
        through ``_set_optional`` like the tri-state keys (False → ``enabled = false``,
        None → stale key removed).
        """
        if not enabled:
            _set_optional(section, "enabled", enabled)
            return
        _ensure_section(section)
        table = doc[section]
        table["enabled"] = True
        if provider == "anthropic":
            return
        table["provider"] = provider
        if provider == "claude_cli":
            # Model is optional for claude_cli — a blank means "use Claude
            # Code's default", so only write it when the operator chose one.
            # Writing "" would trip config.py's non-empty guard at startup.
            if model:
                table["model"] = model
            return
        # Always write model for ollama/openai (even empty string) so
        # config.py's "if not model: raise ConfigError" guard fires at
        # startup when the operator left the model prompt blank.
        table["model"] = model
        if provider == "ollama":
            _reconcile_ollama_base_url(table, ollama_base_url)
        elif provider == "llama_cpp":
            _reconcile_llama_cpp_base_url(table, llama_cpp_base_url)

    if features.disable_reranker:
        _ensure_section("database")
        doc["database"]["reranker_model"] = ""  # stays conditional — profile writer pre-fills the default

    for section, key, field, _ in _OPTIONAL_KEYS:
        _set_optional(section, key, getattr(features, field))

    # C15 Tier 1 deployment flags
    if features.host is not None:
        _ensure_section("server")
        doc["server"]["host"] = features.host

    if features.port is not None:
        _ensure_section("server")
        doc["server"]["port"] = features.port

    if features.db_path is not None:
        _ensure_section("database")
        doc["database"]["db_path"] = features.db_path

    if features.log_level is not None:
        _ensure_section("logging")
        doc["logging"]["level"] = features.log_level

    if features.top_k is not None:
        _ensure_section("database")
        doc["database"]["top_k_return"] = features.top_k
        doc["database"]["top_k_retrieve"] = max(15, 3 * features.top_k)

    # Unanswered (None) and explicit-off (False) both drop retention_days: it only
    # applies while telemetry is explicitly on.
    telemetry_on = bool(features.enable_telemetry)
    if features.telemetry_retention_days is not None and telemetry_on:
        _ensure_section("telemetry")
        doc["telemetry"]["retention_days"] = features.telemetry_retention_days
    elif not telemetry_on:
        _remove_key("telemetry", "retention_days")

    # C15 Tier 2 AI query expansion flags (G10 BE-8: provider/model/base_url).
    _apply_query_expansion(
        "hyde",
        features.enable_hyde,
        features.hyde_provider,
        features.hyde_model,
        features.hyde_ollama_base_url,
        features.hyde_llama_cpp_base_url,
    )
    _apply_query_expansion(
        "rag_fusion",
        features.enable_rag_fusion,
        features.rag_fusion_provider,
        features.rag_fusion_model,
        features.rag_fusion_ollama_base_url,
        features.rag_fusion_llama_cpp_base_url,
    )

    # FE-2: LLM-backed graph enrichment provider — independent of graph.enabled
    # (BE-11, below): `[graph].provider` is itself the enrichment gate (no
    # separate `enabled` key), so nothing is written when the operator declined.
    # A provider chosen with no model (EOF, or two invalid free-text entries)
    # would otherwise write `extraction_model = ""`, which config.py rejects at
    # load time with an unhandled ConfigError — so the whole block is skipped
    # and enrichment stays disabled instead.
    if features.graph_provider and not features.graph_extraction_model:
        print(
            "Warning: graph enrichment was left disabled because no model was chosen.",
            file=sys.stderr,
        )
    elif features.graph_provider:
        _ensure_section("graph")
        doc["graph"]["provider"] = features.graph_provider
        doc["graph"]["extraction_model"] = features.graph_extraction_model
        if features.graph_provider == "llama_cpp":
            _reconcile_llama_cpp_base_url(doc["graph"], features.graph_llama_cpp_base_url)
        elif features.graph_provider == "ollama":
            _reconcile_ollama_base_url(doc["graph"], features.graph_ollama_base_url)

    # BE-11: [graph] enabled must be written whenever the [graph] extras were
    # auto-installed, or the install is inert — see C1-I-1 / C1-A-4.
    if features.install_graph_extra:
        _ensure_section("graph")
        doc["graph"]["enabled"] = True


# ---------------------------------------------------------------------------
# Wizard record: the managed values the wizard last left in the config
# ---------------------------------------------------------------------------

def _wizard_record_path(config_path: Path) -> Path:
    """Return the record file kept next to *config_path* (named like the ``.toml.bak`` backup)."""
    return config_path.with_suffix(".toml.wizard.json")


def _managed_values(doc: tomlkit.TOMLDocument) -> dict[str, object]:
    """Return ``{"section.key": value}`` for the ``_RERUN_MANAGED_KEYS`` present in *doc*.

    A section that is not a table (e.g. a hand-edited ``hyde = "oops"``) holds no managed key.
    """
    plain = doc.unwrap()
    values: dict[str, object] = {}
    for section, key in _RERUN_MANAGED_KEYS:
        table = plain.get(section)
        if isinstance(table, dict) and key in table:
            values[f"{section}.{key}"] = table[key]
    return values


def _record_wizard_values(config_path: Path) -> None:
    """Record the managed values the wizard left in *config_path*; drop the record if it has no config.

    The next re-run compares the config against this record (``_detect_config_hand_edits``),
    so a value the wizard wrote itself is never mistaken for a hand edit. An ``OSError`` or a
    config that no longer parses (``ValueError``) only warns: it runs on every install exit
    and must not mask the run's own result.
    """
    record_path = _wizard_record_path(config_path)
    try:
        if not config_path.exists():
            record_path.unlink(missing_ok=True)
            return
        values = _managed_values(tomlkit.parse(config_path.read_text()))
        record_path.with_suffix(record_path.suffix + ".tmp").unlink(missing_ok=True)
        # A non-JSON value (e.g. a hand-edited TOML datetime) is recorded as text, so it never
        # equals the on-disk value and the next re-run reports it as a hand edit.
        atomic_write_bytes(record_path, json.dumps(values, sort_keys=True, default=str).encode())
    except (OSError, ValueError) as exc:
        print(
            f"Warning: could not update the wizard record {record_path}: {exc}. A later wizard "
            "re-run may misreport hand edits.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Hand-edit detection (Task C14-5.4)
# ---------------------------------------------------------------------------

def _read_wizard_record(config_path: Path) -> dict[str, object] | None:
    """Return the wizard record for *config_path*, or None when it is missing or unreadable."""
    try:
        record = json.loads(_wizard_record_path(config_path).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _differs_from_baseline(current: dict[str, object], prev_profile_name: str, prev_multilingual: bool) -> bool:
    """Return True if the managed values *current* differ from what a wizard run could have written.

    The profile ``[database]`` keys must match the previous profile; every other managed key
    must be absent or equal its ``SearchConfig()`` default, so a wizard-written explicit
    default (e.g. ``--no-watch``) passes but a wizard-written non-default value does not.
    An unrecognized *prev_profile_name* always differs.
    """
    try:
        profile = get_profile(prev_profile_name, prev_multilingual)
    except ValueError:
        return True

    expected_database_values = _profile_database_values(profile, prev_profile_name, prev_multilingual)
    if any(current.get(f"database.{key}") != value for key, value in expected_database_values.items()):
        return True

    config_defaults = SearchConfig()
    for section, key, default_attr in _DEFAULTED_RERUN_KEYS:
        default = attrgetter(default_attr)(config_defaults)
        if current.get(f"{section}.{key}", default) != default:
            return True
    return False


def _detect_config_hand_edits(
    config_path: Path,
    prev_profile_name: str,
    prev_multilingual: bool,
) -> bool:
    """Return True if a ``_RERUN_MANAGED_KEYS`` key was edited since the wizard last wrote the config.

    With a wizard record (``_record_wizard_values``), any managed key changed, added or
    removed since the last run is a hand edit — including an edit back to a default — while
    a value the wizard wrote itself is not. Without a readable record (configs written
    before records existed) it falls back to ``_differs_from_baseline``, which cannot tell
    a wizard-written non-default value from a hand edit. Keys outside
    ``_RERUN_MANAGED_KEYS`` are never compared: ``[server]`` and the other deployment keys,
    ``[graph]`` and the HyDE/RAG Fusion provider/model keys, which a re-run writes only when
    asked to, and ``[database].providers``, which Step 9 rewrites from the GPU answer (``[]``
    after a declined GPU prompt, ``--disable-gpu`` or a failed CoreML check) and otherwise
    leaves unchanged when it already holds the detected provider.
    """
    current = _managed_values(tomlkit.parse(config_path.read_text()))
    record = _read_wizard_record(config_path)
    if record is not None:
        return current != record
    return _differs_from_baseline(current, prev_profile_name, prev_multilingual)


def _assert_features_persisted(cfg: SearchConfig, features: WizardFeatures) -> None:
    """Verify the wizard's HyDE / RAG Fusion choices survived the config round-trip.

    Called in ``run()`` immediately after the freshly-written config is reloaded
    via ``load_config``. Surfaces a silent write/parse failure at install time —
    with an actionable message — instead of letting a feature show as enabled in
    the wizard but disabled on the server. Raises ``InstallError`` on mismatch.
    """
    if features.enable_hyde and not cfg.hyde.enabled:
        raise InstallError(
            "[hyde].enabled was requested but is not present in the written "
            "archon-search.toml after reload — the config write did not persist. "
            "This is a bug; please re-run the wizard and report it if it recurs."
        )
    if features.enable_rag_fusion and not cfg.rag_fusion.enabled:
        raise InstallError(
            "[rag_fusion].enabled was requested but is not present in the written "
            "archon-search.toml after reload — the config write did not persist. "
            "This is a bug; please re-run the wizard and report it if it recurs."
        )


def _revert_graph_enabled_flag(config_path: Path, dry_run: bool) -> None:
    """Revert ``graph.enabled`` to false in the written config.

    ``run()`` writes ``graph.enabled=true`` early (config-write branches)
    whenever ``features.install_graph_extra`` is set, before the ``[graph]``
    extras are actually installed. If a later step aborts or fails before
    that install succeeds, leaving ``graph.enabled=true`` on disk would
    hard-fail the next server start via app.py's ``_check_graph_deps`` (graph
    extra absent) — strictly worse than BE-11's soft-degrade goal. Call this at
    every such early-return point (C3-A-1 fix).
    """
    if dry_run or not config_path.exists():
        return
    doc = tomlkit.parse(config_path.read_text())
    if "graph" in doc and "enabled" in doc["graph"]:
        doc["graph"]["enabled"] = False
        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())
        print(
            "Warning: graph.enabled has been reverted to false because "
            "the install did not complete; re-run the wizard or install "
            "archon-search[graph] manually to enable code/prose graphing.",
            file=sys.stderr,
        )


def _revert_multilingual_flag(config_path: Path, dry_run: bool) -> None:
    """Revert ``[database].multilingual`` to false in the written config.

    ``run()`` writes ``multilingual=true`` early (config-write branches, Step
    6/7/8) whenever a multilingual profile is selected, before the
    ``[multilingual]`` extra is installed. If a later step aborts or fails
    before that install succeeds, leaving ``multilingual=true`` on disk would
    hard-fail the next server start via app.py's ``_check_multilingual_deps``
    (fasttext-wheel absent). Reverting lets the server start English-only
    instead of crashing. Mirrors :func:`_revert_graph_enabled_flag`. Note the
    ``multilingual=true`` flag reverted here is written by the profile config
    writer (``_write_profile_config``/``_profile_toml``), NOT by the
    ``_apply_wizard_features_to_toml`` overlay that the graph flag comes from.
    """
    if dry_run or not config_path.exists():
        return
    doc = tomlkit.parse(config_path.read_text())
    if "database" in doc and "multilingual" in doc["database"]:
        doc["database"]["multilingual"] = False
        atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())
        print(
            "Warning: multilingual has been reverted to false because the "
            "install did not complete; the server will start in English-only "
            "mode. Re-run the wizard or install archon-search[multilingual] "
            "manually to enable multilingual language detection.",
            file=sys.stderr,
        )
