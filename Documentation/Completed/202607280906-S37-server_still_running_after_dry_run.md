## Bug: Wizard `--dry-run` does not modify system state

**ID**: 202607280906-S37-server_still_running_after_dry_run
**Scenario**: S37
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: /health not 200 after dry-run — server state was changed
assert 0 == 200

### What should happen
- Exits 0.
- Output describes what *would* happen (printed actions, no execution).
- If `~/.archon-search/` did not exist before, it still does not exist after.

### Steps to reproduce
1. `archon-search wizard --profile minimal --non-interactive --skip-preload --dry-run`
2. Compare system state to before: no new service registered, no TOML created if it was absent.

### Evidence
```
E   AssertionError: /health not 200 after dry-run — server state was changed
E   assert 0 == 200
E    +  where 0 = http_status('http://127.0.0.1:8765/health')
```
