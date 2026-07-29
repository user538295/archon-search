## Bug: `archon-search collection reindex`

**ID**: 202607282036-S22-completion_message_present
**Scenario**: S22
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: Expected completion message in reindex output:
Error: collection 'archon_test_docs' not found.

assert False

### What should happen
- Step 1 exits 0; output contains a completion message.
- Step 2 still lists the collection (reindex does not remove it).

### Steps to reproduce
1. `archon-search collection reindex archon_test_docs --wait`
2. `archon-search collection list`

### Evidence
```
E   AssertionError: Expected completion message in reindex output:
E     Error: collection 'archon_test_docs' not found.
E     
E   assert False
E    +  where False = any(<generator object TestS22Reindex.test_completion_message_present.<locals>.<genexpr> at 0x10abfda80>)
```
