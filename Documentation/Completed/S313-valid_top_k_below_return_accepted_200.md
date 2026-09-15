## Bug: `top_k` happy path: explicit valid value is accepted but does NOT set the result count

**ID**: S313-valid_top_k_below_return_accepted_200
**Scenario**: S313
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: top_k=3 should be accepted; got 0: None
assert 0 == 200

### What should happen
- Both requests: HTTP `200` (an explicit `top_k` in `1..top_k_max` is accepted — `60_searching.md:36`).
- Both requests return exactly `[database].top_k_return` = `5` results (`60_searching.md:12`, `30_configuration.md:63`) — the count does **not** track the requested `top_k`. In particular the `top_k=3` request returns `5` results (more than 3), and the `top_k=50` request also returns `5` (not 50): the per-request `top_k` does not set the returned count.
- The two counts are identical — the returned count is independent of `top_k`.

### Steps to reproduce
```bash
source ~/.archon-search/.search.env

# Step 1: explicit top_k BELOW top_k_return (3) — valid (1..top_k_max)
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"s313_docs","query":"aurora","top_k":3}'

# Step 2: explicit top_k ABOVE top_k_return but BELOW top_k_max (50) — also valid
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"s313_docs","query":"aurora","top_k":50}'
```

### Evidence
```
E   AssertionError: top_k=3 should be accepted; got 0: None
E   assert 0 == 200
```
