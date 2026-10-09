## Bug: `wizard --no-telemetry` writes `[telemetry].enabled = false` but lists no telemetry feature in the summary

**ID**: S556-no_telemetry_writes_enabled_false
**Scenario**: S556
**Severity**: medium
**Version**: archon-search, version 26.10.2150

### What happened
A completing 'archon-search wizard --profile minimal --non-interactive --skip-preload --no-telemetry' (isolated --config/--db-path) exits 0 but the written config has no [telemetry] section at all.

### What should happen
- Step 1: exit `0`; no entry in the summary's "Optional features:" list mentions telemetry (`:613`, `:471`); no `archon-search.toml` exists at the `--config` path (`:571`).
- Step 2 (discriminator): exit `0`; the optional-features list has an entry mentioning telemetry (`:471`). The label wording is not documented, so only presence is asserted.
- Step 3: exit `0`; the written config has `[telemetry].enabled = false` (TOML boolean) — the explicit flag always writes its key (`:747`, `:775`).

### Steps to reproduce
1. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --no-telemetry`
2. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --telemetry`
3. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/data/search" --profile minimal --non-interactive --skip-preload --no-telemetry`

### Evidence
```
exit 0; read_toml().get('telemetry', {}) -> {} so [telemetry].enabled is absent (20_wizard.md:747, :775 say the explicit flag always writes the key)
```
