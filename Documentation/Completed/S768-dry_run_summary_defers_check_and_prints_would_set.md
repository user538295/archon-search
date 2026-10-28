## Bug: The wizard's accelerator check runs only after the summary and `Proceed?`: the summary `Device:` line says `checked after you confirm; CPU if it fails`, `n` writes no device, and `--dry-run` prints `Would set [database] device = 'auto'.`

**ID**: S768-dry_run_summary_defers_check_and_prints_would_set
**Scenario**: S768
**Severity**: low
**Version**: archon-search, version 26.10.2267

### What happened
With --dry-run --non-interactive on Apple Silicon (GPU accepted), the Step 6 summary prints 'Device:     Apple GPU (mps)' — without the documented 'checked after you confirm; CPU if it fails' suffix. Without --dry-run (interactive, answering n at Proceed?) the same summary prints 'Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails'. The dry run also prints '[DRY RUN] Would set [database] device = 'auto'.' as documented. Command: archon-search wizard --config $TMP/archon-search.toml --db-path $TMP/search --profile minimal --no-multilingual --skip-preload --dry-run --non-interactive. Doc: UserManual/20_wizard.md:453 (GPU accepted -> the Device line reads '... — checked after you confirm; CPU if it fails').

### What should happen
- Step 1: exit `0`; the summary has `Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails`; the output contains `[DRY RUN] Would set [database] device = 'auto'.` (20:453, 20:144).
- Step 2: the summary has the same `Device:` line; after `n` the output contains `Installation aborted.`; the output carries no `Device:     Apple GPU (mps)` outcome line (the check never ran) and no `validation failed` warning; and the written config has no `[database].device` key (20:155 "no device written", 20:483).

### Steps to reproduce
1. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --no-multilingual --skip-preload --dry-run --non-interactive`
2. Interactive run (stdin: `y` to the GPU prompt, defaults for the optional-feature prompts, `n` at `Proceed?`): `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --no-multilingual --skip-preload`; then read the TOML.

### Evidence
```
{{EVIDENCE}}
```
