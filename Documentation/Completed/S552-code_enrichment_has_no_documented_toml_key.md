## Bug: `wizard --no-code` skips the tree-sitter code-enrichment extras (no TOML observable is documented)

**ID**: S552-code_enrichment_has_no_documented_toml_key
**Scenario**: S552
**Severity**: medium
**Version**: archon-search, version 26.8.1931

### What happened
AssertionError: UserManual/20_wizard.md no longer states that the wizard does not configure the `[graph]` section — the worklist bullet's 'code graph disabled' claim may now be documented, so S552 must be re-implemented against it
assert 'The wizard does not configure the `[graph]` section' in '**Purpose**: Comprehensive guide to the `archon-search wizard` command — what it does, every prompt it asks, all CLI ... [`160_troubleshooting.md`](./160_troubleshooting.md) — detailed troubleshooting for service and ingestion failures.

### What should happen
- Step 1: exit `0`, and the summary's "Optional features:" list contains no `Code enrichment (tree-sitter)` entry — code enrichment is the non-interactive default of "Disabled" (`:500`), and "Only non-default optional features are listed" (`:382`).
- Step 1: the output contains no "would install" line naming the `archon-search[code]` extra — `--no-code` "skip[s] tree-sitter code enrichment packages" (`:465`), and `--dry-run` prints every action the run would take (`:462`), so an install that would happen would have to appear.
- Step 2 (discriminator): exit `0`, and the summary's optional-features list contains `Code enrichment (tree-sitter)` verbatim (`:376`). Without this the Step 1 absence would be vacuous.
- Both steps: no `archon-search.toml` exists at the `--config` path (`:462`).
- **Doc-gap reopening gate**: the "What Gets Configured" section of `20_wizard.md` (lines 597-669) still names no TOML key for code enrichment, and `:693` still states the wizard does not configure `[graph]`. If either changes, `--no-code` has gained a documented config observable and S552 must be re-implemented against it. **No bug is filed: a missing doc is not an app defect.**

### Steps to reproduce
1. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --no-code`
2. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --non-interactive --skip-preload --dry-run --code`

### Evidence
```
E   AssertionError: UserManual/20_wizard.md no longer states that the wizard does not configure the `[graph]` section — the worklist bullet's 'code graph disabled' claim may now be documented, so S552 must be re-implemented against it
E   assert 'The wizard does not configure the `[graph]` section' in '**Purpose**: Comprehensive guide to the `archon-search wizard` command — what it does, every prompt it asks, all CLI ... [`160_troubleshooting.md`](./160_troubleshooting.md) — detailed troubleshooting for service and ingestion failures.
'
E    +  where '**Purpose**: Comprehensive guide to the `archon-search wizard` command — what it does, every prompt it asks, all CLI ... [`160_troubleshooting.md`](./160_troubleshooting.md) — detailed troubleshooting for service and ingestion failures.
' = read_text()
E    +    where read_text = PosixPath('/Users/manczg/Documents/development/archon-search-test/docs/UserManual/20_wizard.md').read_text
```
