## Bug: Single file ingest via `archon-search ingest`

**ID**: 202607280906-S09-single_docs_in_list
**Scenario**: S09
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: 'single-docs' not in collection list:
archon_test_docs  docs=0  chunks=3

assert 'single' in 'archon_test_docs  docs=0  chunks=3

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
E   AssertionError: 'single-docs' not in collection list:
E     archon_test_docs  docs=0  chunks=3
E     
E   assert 'single' in 'archon_test_docs  docs=0  chunks=3
'
E    +  where 'archon_test_docs  docs=0  chunks=3
' = <built-in method lower of str object at 0x10752de30>()
E    +    where <built-in method lower of str object at 0x10752de30> = ('archon_test_docs  docs=0  chunks=3
' + '').lower
E    +      where 'archon_test_docs  docs=0  chunks=3
' = CompletedProcess(args=('archon-search', 'collection', 'list'), returncode=0, stdout='archon_test_docs  docs=0  chunks=3
', stderr='').stdout
E    +      and   '' = CompletedProcess(args=('archon-search', 'collection', 'list'), returncode=0, stdout='archon_test_docs  docs=0  chunks=3
', stderr='').stderr
```
