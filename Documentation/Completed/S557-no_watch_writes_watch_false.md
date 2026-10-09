## Bug: `wizard --no-watch` writes `[collections].watch = false` but lists no watcher feature in the summary

**ID**: S557-no_watch_writes_watch_false
**Scenario**: S557
**Severity**: medium
**Version**: archon-search, version 26.10.2150

### What happened
A completing 'wizard --profile minimal --non-interactive --skip-preload --no-watch' exits 0 but writes no [collections].watch key; [collections] = {'collections': [], 'pinned_collections': []}.

### What should happen
- Step 1: exit `0`; the summary's "Optional features:" list contains no `Watch directories (auto-reindex)` entry (`:612`, `:471`); no `archon-search.toml` exists at the `--config` path (`:571`).
- Step 2 (discriminator): exit `0`; the optional-features list contains `Watch directories (auto-reindex)` **verbatim** (`:466`).
- Step 3: exit `0`; the written config has `[collections].watch = false` (TOML boolean) — the explicit flag always writes its key (`:741`, `:775`).

### Steps to reproduce
1. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --no-watch`
2. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --watch`
3. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/data/search" --profile minimal --non-interactive --skip-preload --no-watch`

### Evidence
```
assert None is False — [collections]={'collections': [], 'pinned_collections': []} (archon-search archon-search, version 26.10.2150)
```
