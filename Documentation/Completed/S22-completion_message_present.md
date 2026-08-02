## Bug: `archon-search collection reindex`

**ID**: S22-completion_message_present
**Scenario**: S22
**Severity**: medium
**Version**: archon-search, version 26.8.1800

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
E    +  where False = any(<generator object TestS22Reindex.test_completion_message_present.<locals>.<genexpr> at 0x107b250e0>)
```

---

### Analysis — Not a product defect (feature-level)

**Verdict:** environmental cascade, not a product defect.

Reindex reported "collection not found" only because the collection was never created in this run — the earlier add step (S07) was rejected due to leftover state from a previous run. This is a knock-on effect of that environmental condition, not a reindex defect.

**Verified:** starting from a clean setup, reindex completes with a completion message and the collection remains listed afterwards.

**Recommendation:** run each smoke scenario against a fresh, empty data directory so the collection exists before reindex runs.
