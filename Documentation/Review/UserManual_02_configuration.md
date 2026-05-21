# Review: UserManual/02_configuration.md

Sources of truth: `archon_search/config.py`, `archon_search/constants.py`, `archon_search/cli/config_cmd.py`, `archon_search/key_manager.py`, `archon-search.toml.example`.

## Summary

The document is largely accurate. Every documented section, key, default value, type, and validation rule matches `SearchConfig` and `load_config` in `archon_search/config.py`. Authentication behaviour matches `key_manager.py`. A few minor inaccuracies and one ambiguity were found; none are load-bearing for end users.

## Inaccuracies (numbered)

1. **Line 32 — `config set` coercion order and case-sensitivity understated.**
   Doc: "coerces the value to `bool`/`int`/`float` when it can, otherwise stores it as a string."
   Code (`cli/config_cmd.py:114-127`): coercion order is bool → int → float → string, and the bool check is `value.lower() == "true" / "false"` so `True`, `TRUE`, etc. are all coerced. Doc is directionally correct but does not state the order or the case-insensitivity. Minor.

2. **Line 32 — `config set` does NOT validate against `SearchConfig`.**
   Doc implies `set` writes a typed value into the section. Code (`cli/config_cmd.py:91-133`) writes to *any* `section.field` pair, even ones that don't exist on `SearchConfig`; nothing is validated until the next `load_config` call. Worth noting because `config set foo.bar baz` silently succeeds.

3. **Line 100 — "Must be a lowercase hex string" is correct but incomplete.**
   Code (`key_manager.py:22`): `_HEX_RE = re.compile(r"^[0-9a-f]+$")`. There is no length requirement. The doc's wording is fine; the auto-generated key is 64 hex chars (`secrets.token_hex(32)`, `key_manager.py:85`) but user-supplied keys via the env var can be any non-empty lowercase hex string. Worth clarifying that length is unconstrained for env-var-supplied keys.

4. **Line 101 — "The loader will `chmod 600` the file if its permissions are wider" is not quite right.**
   Code (`key_manager.py:54-59`): the loader compares `mode != 0o600` and attempts `chmod(0o600)`. This tightens *or loosens* the mode to exactly `600`; the comparison is not "wider than 600". In practice this only matters if a user deliberately set mode `400` — the loader would then *widen* it to `600`. Minor but technically incorrect.

5. **Line 101 — "format is a single line `ARCHON_SEARCH_API_KEY=<hex>`" is too strict.**
   Code (`key_manager.py:66-73`) iterates `content.splitlines()` and accepts the first line starting with `ARCHON_SEARCH_API_KEY=`. Trailing whitespace is stripped. Additional lines are ignored. The file does not have to be a single line.

6. **Line 90 — citation `archon_search/config.py:209-217` is essentially correct** but the precise range for the silent-coercion branch is `config.py:209-217` (verified). The doc also says the README and example describe this as "rejected" — `archon-search.toml.example:67-71` actually says "silently coerces this to false", matching the code. So the doc's claim that the example contradicts the code is itself out of date. The example file no longer says "rejected".

## Verified claims

- Default config path resolution (env var, tilde expansion, cwd-relative for non-absolute paths, fallback to `~/.archon-search/archon-search.toml`) — matches `config.py:82-91`.
- Missing-file returns all-defaults `SearchConfig` — `config.py:121-124`.
- `[server]` table: `host` default `127.0.0.1`, `port` default `8765`, port range `[1, 65535]` validated, `ConfigError` on out-of-range — `config.py:30-31, 131-138`.
- `[database]` table:
  - `db_path` default `~/.archon-search/search` — `config.py:33`.
  - `embedding_model` default `BAAI/bge-small-en-v1.5` — `config.py:34`.
  - `reranker_model` default `cross-encoder/ms-marco-MiniLM-L-6-v2` — `config.py:35`.
  - `chunk_size` default `512`, must be > 0 — `config.py:36, 147-151`.
  - `auto_reindex_on_chunk_size_change` default `True` — `config.py:37, 152-155`.
  - `providers` default `[]` — `config.py:38`.
  - `top_k_retrieve` default `15`, must be > 0 — `config.py:39, 158-162`.
  - `top_k_return` default `5`, must be > 0 — `config.py:40, 163-167`.
- `[routing]` table: `routing_shortlist_size` default `8` (>0), `routing_confidence_threshold` default `0.30` (in `[0.0, 1.0]`), `max_parallel_collections` default `3` (>0) — `config.py:42-44, 169-184`.
- `[collections]` table: `pinned_collections=[]`, `collections=[]`, `watch=False` — `config.py:46-48, 186-192`.
- `[logging]` table: `level="INFO"`, `log_file="~/.archon-search/logs/archon-search.log"` — `config.py:50-51, 194-198`.
- `[telemetry]` table:
  - `enabled` default `False` — `config.py:21`.
  - `retention_days` default `30`, must be `>= 1` (error message: `"[telemetry].retention_days must be >= 1"`) — `config.py:22, 204-208`.
  - `log_dir` default `~/.archon-search/search-logs`, must be non-empty — `config.py:24, 218-222`.
  - `export_enabled` default `False`; setting to `True` is silently coerced to `False` with a warning log — `config.py:209-217`.
- `[namespaces]` is a `dict[str, str]`; non-string keys or values raise `ConfigError` — `config.py:55, 225-233`.
- API key resolution order (env var → key file → auto-generated 64-char hex written atomically with mode 600) — `key_manager.py:25-36, 82-132`.
- `ARCHON_SEARCH_API_KEY` env-var precedence over file — `key_manager.py:27-29`.
- `ARCHON_SEARCH_KEY_FILE` overrides default `~/.archon-search/.search.env` — `key_manager.py:14-19`.
- Invalid env var value is logged and ignored, falling through to file/auto-generate — `key_manager.py:42-46`.
- `archon-search config show / get / set` exist and use `section.field` form, error on malformed keys — `cli/config_cmd.py:55-133`.
- `[server]`, `[database]`, `[routing]`, `[collections]`, `[logging]`, `[telemetry]`, `[namespaces]` are the complete set of sections — matches `SearchConfig` fields in `config.py:27-55`.

## Unverifiable / ambiguous

- **Line 13 — "structural mistakes (bad TOML, wrong types) fail loudly at load time."** Bad TOML does raise `ConfigError` (`config.py:127-129`). "Wrong types" is partially true: `str()` is applied permissively to string fields (`host`, `db_path`, `embedding_model`, `reranker_model`, `level`, `log_file`), so e.g. `host = 123` would be coerced to `"123"` rather than raising. Bool/int/float fields do raise. Doc's claim is too broad.
- **Line 11 — "Every key has a default in `archon_search/config.py:SearchConfig`."** True for all keys actually listed, but `namespaces` is a `dict[str, str]` with no per-entry defaults (the table itself defaults to `{}`). Not inaccurate, just worth flagging.
- **Line 19 — "Tilde and relative paths are expanded; relative paths resolve against the current working directory, not `$HOME`."** Verified for `ARCHON_SEARCH_CONFIG` (`config.py:85-90`). The same wording could mislead a reader into thinking *all* path values in the TOML get tilde expansion at load time. They don't — `config.py` stores `db_path`, `log_file`, `telemetry.log_dir` as raw strings; expansion happens at the use site. Not a defect of this section's text, but adjacent table notes ("Tilde is expanded" for `db_path`) describe runtime behaviour rather than load behaviour. Unverified from this file alone.
- **Line 53 — "If `chunk_size` changes between starts, affected collections are reindexed automatically."** The flag is loaded faithfully; the actual reindex behaviour lives elsewhere (likely `pipeline.py` / `sync.py`) and was not verified in this review.
