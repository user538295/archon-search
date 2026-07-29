## Bug: Single file ingest via `archon-search ingest`

**ID**: 202607282036-S09-collection_info_exits_zero
**Scenario**: S09
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: collection info exited 1:

Collection 'single-docs' not found.

assert 1 == 0

### What should happen
- Step 1 exits 0; output contains `Ingest complete for 'single-docs'.`
- Step 2 shows `single-docs` in the list.
- Step 3 exits 0 and prints collection metadata (non-empty output containing `single-docs`).

### Steps to reproduce
Setup:
```bash
echo "# Single\nThis is a standalone document about semantic search." > /tmp/single.md
```

1. `archon-search ingest --path /tmp/single.md --collection single-docs --wait`
2. `archon-search collection list`
3. `archon-search collection info single-docs`

### Evidence
```
E   AssertionError: collection info exited 1:
E     
E     Collection 'single-docs' not found.
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'collection', 'info', 'single-docs'), returncode=1, stdout='', stderr="Collection 'single-docs' not found.
").returncode
```
