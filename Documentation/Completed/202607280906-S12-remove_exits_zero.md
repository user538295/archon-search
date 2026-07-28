## Bug: Remove a collection

**ID**: 202607280906-S12-remove_exits_zero
**Scenario**: S12
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: collection remove exited 1:

Error: collection 'single-docs' not found.

assert 1 == 0

### What should happen
- Step 1 exits 0.
- Step 2 does not list `single-docs`.

### Steps to reproduce
1. `archon-search collection remove single-docs`
2. `archon-search collection list`

### Evidence
```
E   AssertionError: collection remove exited 1:
E     
E     Error: collection 'single-docs' not found.
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'collection', 'remove', 'single-docs'), returncode=1, stdout='', stderr="Error: collection 'single-docs' not found.
").returncode
```
