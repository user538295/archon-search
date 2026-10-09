## Bug: `wizard --no-eager-load` writes `eager_load_embedders = false` but lists no eager-load feature in the summary

**ID**: S553-no_eager_load_writes_key_false
**Scenario**: S553
**Severity**: medium
**Version**: archon-search, version 26.10.2150

### What happened
A completing 'archon-search wizard --profile minimal --non-interactive --skip-preload --no-eager-load' (isolated --config/--db-path) exits 0 but the written [database] section has no eager_load_embedders key.

### What should happen
- Step 1: exit `0`; no entry in the summary's "Optional features:" list mentions eager loading (`:614`, `:471`); no `archon-search.toml` exists at the `--config` path (`:571`).
- Step 2 (discriminator): exit `0`; the optional-features list has an entry mentioning eager loading (`:471`). The label wording is not documented, so only presence is asserted.
- Step 3: exit `0`; the written config has `[database].eager_load_embedders = false` (TOML boolean) — the explicit flag always writes its key (`:732`, `:775`).

### Steps to reproduce
1. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --no-eager-load`
2. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --eager-load`
3. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/data/search" --profile minimal --non-interactive --skip-preload --no-eager-load`

### Evidence
```
exit 0; read_toml()['database'].get('eager_load_embedders') -> None; [database] keys include auto_reindex_on_chunk_size_change, chunk_size, db_path, embedding_model, ... but not eager_load_embedders (20_wizard.md:775 says an explicit flag always writes its key)
```
