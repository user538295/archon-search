## Bug: Sync picks up files added to a collection's source directory

**ID**: S287-doc_count_increases_after_sync
**Scenario**: S287
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: docs count did not increase after syncing a newly added file: before=0, after=0
assert 0 > 0

### What should happen
- Step 1: exits 0.
- Step 4: exits 0; stdout contains `Sync complete.`.
- Step 5: the `docs=<n>` count (N2) is strictly greater than the pre-sync count (N1) — the incrementally added file is indexed.

### Steps to reproduce
1. `archon-search collection add <dir> --wait`
2. `archon-search collection list`   # record docs=<n> for the derived name → N1
3. Add a new file to `<dir>`.
4. `archon-search sync --wait`
5. `archon-search collection list`   # record docs=<n> for the derived name → N2

### Evidence
```
E   AssertionError: docs count did not increase after syncing a newly added file: before=0, after=0
E   assert 0 > 0
```
