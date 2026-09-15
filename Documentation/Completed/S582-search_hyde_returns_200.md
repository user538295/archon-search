## Bug: llama_cpp HyDE provider starts without extra install

**ID**: S582-search_hyde_returns_200
**Scenario**: S582
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search with hyde=true returned 0, expected 200; body: None
assert 0 == 200

### What should happen
- Server starts successfully — `llama_cpp` needs no extra pip install; `httpx` is a core dependency (60_searching.md:214).
- `POST /search` with `hyde=true` returns HTTP 200, not 500 — the provider code path exists.
- `hyde_applied` is `false` — the llama-server at port 19999 is unreachable, so the server falls back silently (60_searching.md:238).
- `expansion_used` is `false` — no expansion applied (60_searching.md:233).
- `expansion_warning` is `"HyDE expansion failed"` — the documented fallback string (60_searching.md:234).

### Steps to reproduce
1. Start an isolated server with:
   ```toml
   [hyde]
   enabled = true
   provider = "llama_cpp"
   model = "test-model"
   llama_cpp_base_url = "http://127.0.0.1:19999"
   ```
2. Ingest a small document into a test collection.
3. `POST /search` with `{"collection":"<col>","query":"test query","hyde":true}`.

### Evidence
```
E   AssertionError: POST /search with hyde=true returned 0, expected 200; body: None
E   assert 0 == 200
```
