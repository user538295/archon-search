## Bug: `archon-search collection reindex`

**ID**: 202607280906-S22-reindex_exits_zero
**Scenario**: S22
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: collection reindex exited 1:

Error: collection 'archon_test_docs' not found.

assert 1 == 0

### What should happen
- Step 1 exits 0; output contains a completion message.
- Step 2 still lists the collection (reindex does not remove it).

### Steps to reproduce
1. `archon-search collection reindex archon_test_docs --wait`
2. `archon-search collection list`

### Evidence
```
E   AssertionError: collection reindex exited 1:
E     
E     Error: collection 'archon_test_docs' not found.
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'collection', 'reindex', 'archon_test_docs', '--wait'), returncode=1, stdout='', stderr="Error: collection 'archon_test_docs' not found.
").returncode
```
