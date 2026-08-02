## Bug: Ingest a directory via CLI (`collection add`)

**ID**: S07-success_message_in_output
**Scenario**: S07
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
AssertionError: Expected success/ingest in output, got:
Error: collection already registered

assert ('success' in 'error: collection already registered

### What should happen
- Step 1 exits 0; output contains `Collection '<name>' ingested successfully.`
- Step 2 shows a line for the collection (name derived from `archon-test-docs`) with `docs=3` (or `chunks=<n>`).

### Steps to reproduce
Setup:
```bash
mkdir -p /tmp/archon-test-docs
echo "# Alpha\nThe quick brown fox jumps over the lazy dog." > /tmp/archon-test-docs/alpha.md
echo "# Beta\nPython is a programming language created by Guido van Rossum." > /tmp/archon-test-docs/beta.md
echo "# Gamma\nDocker containers are lightweight isolated environments." > /tmp/archon-test-docs/gamma.md
```

1. `archon-search collection add /tmp/archon-test-docs --wait`
2. `archon-search collection list`

### Evidence
```
E   AssertionError: Expected success/ingest in output, got:
E     Error: collection already registered
E     
E   assert ('success' in 'error: collection already registered
' or 'ingest' in 'error: collection already registered
')
E    +  where 'error: collection already registered
' = <built-in method lower of str object at 0x107b6d840>()
E    +    where <built-in method lower of str object at 0x107b6d840> = 'Error: collection already registered
'.lower
E    +  and   'error: collection already registered
' = <built-in method lower of str object at 0x107b6d840>()
E    +    where <built-in method lower of str object at 0x107b6d840> = 'Error: collection already registered
'.lower
```

---

### Analysis — Not a product defect (feature-level)

**Verdict:** environmental (leftover state from a previous run), not a product defect.

When a folder is added as a collection, the server refuses to register it a second time — that is the intended behaviour to avoid duplicate collections. In this run the "already registered" message appeared because a collection for this folder still existed from an earlier run; the server was correctly rejecting a duplicate, not failing.

**Verified:** starting from a clean setup (empty data directory), adding this folder succeeds and reports the success message as expected.

**Recommendation:** run each smoke scenario against a fresh, empty data directory so no collection survives from a prior run.
