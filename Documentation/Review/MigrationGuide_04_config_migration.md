# Review: MigrationGuide/04_config_migration.md

## Summary

The doc is largely accurate against `archon_search/config.py`, `constants.py`, and `archon-search.toml.example`. The schema table, defaults, validation ranges, and the `export_enabled` silent-coerce behavior all match the source. The only material inaccuracy is the description of what the example-file comment says about `export_enabled` — the doc misquotes the example file, claiming it states `export_enabled = true` "currently raises ConfigError" when in fact the example file already correctly describes the silent-coerce behavior.

Several minor framing issues (e.g. "Loader behavior … lines 209–217" pinning a line range, and the "CLAUDE.md / ADR-05" attribution) are unverifiable here without reading those secondary sources, and the namespaces description omits the regex validation that lives in `constants.py` (but `load_config` itself does not invoke `_validate_namespace`, so the doc is technically correct that the loader only checks string-types).

## Inaccuracies (numbered)

1. **Line 57 — misquotes the example file.** The doc says: *"the comment in `archon-search.toml.example` says 'setting it to true currently raises ConfigError'"*. The actual comment in `archon-search.toml.example` (lines 67–71) says the opposite: *"the config loader logs a warning and silently coerces this to false. No external transmission occurs. Tracked as TEL-1…"*. The example file already describes the real behavior; there is no mismatch between the example file and the loader. The mismatch the doc claims to expose does not exist in the example file.

2. **Line 56 — fragile line-number citation.** The doc cites "See `archon_search/config.py` lines 209–217" for the `export_enabled` handling. The current source has that handling at lines 209–217 in `config.py` *today*, which happens to be correct, but pinning a line range in prose is brittle and the doc has no mechanism to keep it accurate across edits. Not factually wrong as of the current SHA, but flagged as a maintenance hazard.

3. **Line 45 — `[namespaces]` row understates the constraints.** The table says values must be strings "otherwise `ConfigError`". The loader (`config.py` lines 225–233) enforces *only* string-type for both key and value — it does not call `_validate_namespace` from `constants.py` (the `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` regex, plus the `deny-all` reserved name). The doc's claim is accurate as far as it goes, but readers may assume namespace identifier rules are enforced at config load — they are not. Worth a sentence clarifying that the regex check happens elsewhere (callers of `_validate_namespace`), not in `load_config`.

## Verified claims

- Loader entrypoint name and return type: `load_config()` returns `SearchConfig`. (`config.py` line 114.)
- Missing file returns an all-defaults `SearchConfig`. (`config.py` lines 121–124.)
- All keys optional; unknown keys silently ignored via `doc.get(section, {})` reads. (`config.py` lines 131, 140, 169, 186, 194, 200, 225.)
- Type/range errors raise `ConfigError`. (`config.py` `_coerce_int`/`_coerce_float`/`_coerce_bool` + explicit range checks.)
- `port` range `1..65535` enforced. (`config.py` lines 136–137.)
- `routing_confidence_threshold` range `[0.0, 1.0]`. (`config.py` lines 177–178.)
- `chunk_size`, `top_k_retrieve`, `top_k_return`, `routing_shortlist_size`, `max_parallel_collections` must be `> 0`. (`config.py` lines 149, 160–161, 165–166, 172–173, 182–183.)
- `[telemetry].retention_days` must be `>= 1`. (`config.py` lines 206–207.)
- `[telemetry].log_dir` must be non-empty. (`config.py` lines 220–221.)
- Every default in the schema table matches the dataclasses exactly:
  - `host="127.0.0.1"`, `port=8765` — `SearchConfig` lines 30–31.
  - `db_path="~/.archon-search/search"` — line 33.
  - `embedding_model="BAAI/bge-small-en-v1.5"` — line 34.
  - `reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2"` — line 35.
  - `chunk_size=512` — line 36.
  - `auto_reindex_on_chunk_size_change=True` — line 37.
  - `providers=[]` — line 38.
  - `top_k_retrieve=15`, `top_k_return=5` — lines 39–40.
  - `routing_shortlist_size=8`, `routing_confidence_threshold=0.30`, `max_parallel_collections=3` — lines 42–44.
  - `pinned_collections=[]`, `collections=[]`, `watch=False` — lines 46–48.
  - `level="INFO"`, `log_file="~/.archon-search/logs/archon-search.log"` — lines 50–51.
  - `TelemetryConfig.enabled=False`, `retention_days=30`, `export_enabled=False`, `log_dir="~/.archon-search/search-logs"` — lines 21–24.
- `export_enabled=True` triggers the warning `"telemetry: export_enabled is reserved for a future release and will be ignored"` and stores `False`. (`config.py` lines 213–215.) Warning text matches the doc's quote verbatim.
- `archon-search config show / get / set` CLI subcommands exist. (`archon_search/cli/config_cmd.py` lines 55, 66, 91.)
- `archon-search.toml.example` is the bundled reference file, lives at repo root, and its defaults match the dataclass.
- `[collections].collections` is documented as "Static collection list; empty means manage over HTTP" — the example file (lines 47–49) phrases this as "Leave empty to manage collections via the HTTP API", semantically equivalent.

## Unverifiable / ambiguous

- **Line 58 — "CLAUDE.md / ADR-05 describe a stricter contract."** Not checked in this review (out of scope: review limited to `config.py`, `constants.py`, `archon-search.toml.example`). If the doc is to assert what those sources say, it should quote them or be re-verified when those files change.
- **Line 60 — "Tracked as TEL-1 in `Architecture/530_technical_debt_refactoring_roadmap.md`."** Not verified against that file in this review. The example file's own comment (line 70–71) does corroborate the TEL-1 tracking reference, so this is at least internally consistent across the two sources I did read.
- **Line 47 cross-links** to `510_release_and_environment_strategy.md` and `130_data_architecture_and_persistence.md` — link targets not validated.
- **Line 90 — `[next release]` section in `BREAKING.md`** — not verified that this section heading exists in the current `BREAKING.md`.
- **Line 9 — "An entirely missing file is valid."** Verified at the loader level (returns defaults on `FileNotFoundError`), but the doc earlier states the file is "the single config file consumed by the server"; the server may have side conditions (e.g. `key_manager.py` writes `.search.env`) that are out of scope here. The loader claim itself is correct.
