## Bug: file_path disambiguates same-named symbols

**ID**: S68-helpers_b_resolves_caller_b
**Scenario**: S68
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: caller_b missing: {'helpers_a', 'caller_a'}
assert 'caller_b' in {'caller_a', 'helpers_a'}

### What should happen
- Step 1: `callers.direct` contains `caller_a`; does not contain `caller_b`.
- Step 2: `callers.direct` contains `caller_b`; does not contain `caller_a`.
- Both return `depth_used` ≥ 1.

### Steps to reproduce
Setup:
```bash
mkdir -p /tmp/archon-code-graph-dup

cat > /tmp/archon-code-graph-dup/helpers_a.py << 'EOF'
def helper():
    return "a"

def caller_a():
    helper()
EOF

cat > /tmp/archon-code-graph-dup/helpers_b.py << 'EOF'
def helper():
    return "b"

def caller_b():
    helper()
EOF

archon-search ingest --path /tmp/archon-code-graph-dup --collection code-dup --wait
```

1. ```bash
   curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     "http://127.0.0.1:8765/graph/code-dup/impact/helper?file_path=helpers_a.py"
   ```
2. ```bash
   curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     "http://127.0.0.1:8765/graph/code-dup/impact/helper?file_path=helpers_b.py"
   ```

### Evidence
```
E   AssertionError: caller_b missing: {'helpers_a', 'caller_a'}
E   assert 'caller_b' in {'caller_a', 'helpers_a'}
```
