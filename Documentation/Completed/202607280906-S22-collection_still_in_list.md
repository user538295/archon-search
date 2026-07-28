## Bug: `archon-search collection reindex`

**ID**: 202607280906-S22-collection_still_in_list
**Scenario**: S22
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: 'archon_test_docs' missing from list after reindex:
archon_multitype  docs=0  chunks=4

assert 'archon_test_docs' in 'archon_multitype  docs=0  chunks=4

### What should happen
- Step 1 exits 0; output contains a completion message.
- Step 2 still lists the collection (reindex does not remove it).

### Steps to reproduce
1. `archon-search collection reindex archon_test_docs --wait`
2. `archon-search collection list`

### Evidence
```
E   AssertionError: 'archon_test_docs' missing from list after reindex:
E     archon_multitype  docs=0  chunks=4
E     
E   assert 'archon_test_docs' in 'archon_multitype  docs=0  chunks=4
'
```
