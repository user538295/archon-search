## Bug: `language` filter: valid ISO accepted, non-ISO value rejected with 422

**ID**: S303-literal_unknown_accepted
**Scenario**: S303
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: expected 200 for accepted 'unknown' sentinel, got 0: None
assert 0 == 200

### What should happen
- Step 1 (non-ISO `language`): HTTP `422` — a non-ISO `language` is an invalid filter (60:136). Note: the docs specify no error body/code for this rejection, so the assertion keys on the `422` status only and cannot distinguish a language-rejection `422` from an unrelated one (accepted coverage limit).
- Step 2 (`language=unknown`): HTTP `200`, not `422` — `"unknown"` is an accepted value (60:133, 100:184). Strict equality means `language=unknown` returns only sub-threshold chunks (60:152); on the `minimal` baseline (all chunks `""`-tagged, 60:155) this excludes every seeded chunk, so `results` is expected empty and any returned result must carry `language == "unknown"`.
- Step 3 (valid ISO 639-1 `en`): HTTP `200` — the filter accepts valid ISO codes (100:184); `applied_filters.language` is present and echoes the parsed/normalised filter (60:138–145) — the docs specify no 639-1→639-3 canonicalisation, so the value is asserted as `"en"` **or** its 639-3 form `"eng"`, not a fixed literal; every returned result carries `language == "en"` (strict equality, 60:149–151). On the `minimal` baseline all chunks are `""`-tagged and `language=en` excludes `""` (60:151, 60:155), so `results` is expected empty and the per-result equality check holds vacuously — the "returns only docs tagged with that language" positive path with real language tags requires `multilingual = true` (fasttext extra + license), which is out of the baseline scope and therefore not exercised here.
- Step 4 (valid ISO 639-3 `eng`): HTTP `200` — `language` accepts an ISO 639-3 code too (60:133); acceptance only is asserted (no `applied_filters` value, since the docs specify no 639-3 normalisation).

### Steps to reproduce
1. Ingest an English collection `s303_docs` (the fixture posts each file to `POST /ingest`; equivalently, drop `.md` files in a directory and ingest it):
   ```bash
   mkdir -p /tmp/s303 && printf '# Guide\nPython is a popular programming language for data work.\n' > /tmp/s303/guide.md
   curl -s -X POST http://127.0.0.1:8765/ingest \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H "Content-Type: application/json" \
     -d '{"collection":"s303_docs","path":"/tmp/s303"}'
   ```
   Then POST a search with a clearly non-ISO language value:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H "Content-Type: application/json" \
     -d '{"collection":"s303_docs","query":"programming language","filters":{"language":"notalanguage"}}'
   ```
2. POST a search with the documented `"unknown"` sentinel:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H "Content-Type: application/json" \
     -d '{"collection":"s303_docs","query":"programming language","filters":{"language":"unknown"}}'
   ```
3. POST a search with a valid ISO 639-1 code:
   ```bash
   curl -s -X POST http://127.0.0.1:8765/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H "Content-Type: application/json" \
     -d '{"collection":"s303_docs","query":"programming language","filters":{"language":"en"}}'
   ```
4. POST a search with a valid ISO 639-3 code (a distinct code registry, also documented as accepted):
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H "Content-Type: application/json" \
     -d '{"collection":"s303_docs","query":"programming language","filters":{"language":"eng"}}'
   ```

### Evidence
```
E   AssertionError: expected 200 for accepted 'unknown' sentinel, got 0: None
E   assert 0 == 200
```
