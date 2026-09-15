## Bug: config set → config get → serve picks up new top_k_max

**ID**: S290-serve_accepts_top_k_within_new_ceiling
**Scenario**: S290
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: Expected 200 for top_k=5 at/below the new ceiling of 10, got 0: None
assert 0 == 200

### What should happen
- Step 1: exits 0 (`config set` writes the value to the TOML).
- Step 2: exits 0; stdout contains `10` (the value written is read back).
- Step 4: HTTP 422 — `top_k` (50) exceeds the new ceiling of 10, proving the restarted server picked up the value (50 is accepted under the default ceiling of 100).
- Step 5: HTTP 200 — `top_k` (5) is at/below the new ceiling of 10.

### Steps to reproduce
1. `archon-search config set search.top_k_max 10`  (env `ARCHON_SEARCH_CONFIG` = isolated config)
2. `archon-search config get search.top_k_max`  (env `ARCHON_SEARCH_CONFIG` = isolated config)
3. Restart the isolated `archon-search serve` so it re-reads the config.
4. `POST /search {"collection": "<name>", "query": "...", "top_k": 50}`
5. `POST /search {"collection": "<name>", "query": "...", "top_k": 5}`

### Evidence
```
E   AssertionError: Expected 200 for top_k=5 at/below the new ceiling of 10, got 0: None
E   assert 0 == 200
```
