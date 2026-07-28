## Bug: `POST /route` returns collection suggestions

**ID**: 202607280906-S18-routable_names_non_empty
**Scenario**: S18
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: Expected at least one routable collection, got: {'pre_context': None, 'pinned_names': [], 'routable_names': [], 'decomposer_invoked': False}
assert 0 > 0

### What should happen
- HTTP 200.
- Response contains `pre_context`, `pinned_names` (array), `routable_names` (array), `decomposer_invoked` (bool).
- `routable_names` contains at least one collection name.

### Steps to reproduce
```bash
curl -s -X POST http://127.0.0.1:8765/route \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"container deployment"}'
```

### Evidence
```
E   AssertionError: Expected at least one routable collection, got: {'pre_context': None, 'pinned_names': [], 'routable_names': [], 'decomposer_invoked': False}
E   assert 0 > 0
E    +  where 0 = len([])
```
