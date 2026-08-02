## Bug: Multiple file types ingested

**ID**: S14-ingest_exits_zero
**Scenario**: S14
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
AssertionError: collection add (multitype) exited 1:

Error: collection already registered

assert 1 == 0

### What should happen
- Ingest completes with exit 0.
- The collection appears in the list with `docs=4`.

### Steps to reproduce
Setup:
```bash
mkdir -p /tmp/archon-multitype
echo "Name,Age\nAlice,30\nBob,25" > /tmp/archon-multitype/data.csv
echo '{"key": "value", "items": [1, 2, 3]}' > /tmp/archon-multitype/config.json
echo "# Title\nSome text here." > /tmp/archon-multitype/notes.md
printf "[section]\nkey = value\n" > /tmp/archon-multitype/settings.toml
```

1. `archon-search collection add /tmp/archon-multitype --wait`
2. `archon-search collection list`

### Evidence
```
E   AssertionError: collection add (multitype) exited 1:
E     
E     Error: collection already registered
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'collection', 'add', '/tmp/archon-multitype', '--wait'), returncode=1, stdout='', stderr='Error: collection already registered
').returncode
```

---

### Analysis — Not a product defect (feature-level)

**Verdict:** environmental (leftover state from a previous run), not a product defect.

When a folder is added as a collection, the server refuses to register it a second time — that is the intended behaviour to avoid duplicate collections. In this run the "already registered" message appeared because a collection for this folder still existed from an earlier run; the server was correctly rejecting a duplicate, not failing.

**Verified:** starting from a clean setup (empty data directory), adding this folder succeeds and reports the success message as expected.

**Recommendation:** run each smoke scenario against a fresh, empty data directory so no collection survives from a prior run.
