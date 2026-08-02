## Bug: Ingest and search inside container

**ID**: S30-beta_in_results
**Scenario**: S30
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
AssertionError: beta.md not in sources: []
assert False

### What should happen
- Ingest exits 0 with success message.
- Search returns HTTP 200 with at least one result referencing `beta.md`.

### Steps to reproduce
```bash
# Create test documents inside container
docker exec archon-prod mkdir -p /tmp/testdocs
docker exec archon-prod sh -c 'printf "# Alpha\nThe quick brown fox.\n" > /tmp/testdocs/alpha.md'
docker exec archon-prod sh -c 'printf "# Beta\nPython language.\n" > /tmp/testdocs/beta.md'

# Ingest via CLI inside container
docker exec archon-prod archon-search collection add /tmp/testdocs \
  --wait \
  --api-url http://127.0.0.1:8765 \
  --api-key $ARCHON_SEARCH_API_KEY

# Search via REST from host
curl -s -X POST http://127.0.0.1:18765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"testdocs","query":"programming language"}'
```

### Evidence
```
E   AssertionError: beta.md not in sources: []
E   assert False
E    +  where False = any(<generator object TestS30IngestAndSearch.test_beta_in_results.<locals>.<genexpr> at 0x10bae72a0>)
```

---

### Analysis — Product defect, resolved (feature-level)

**Verdict:** confirmed product defect — now fixed.

Adding a folder as a collection failed inside the container because the server could not write its configuration file on first use, which returned a server error before anything was ingested; every downstream search and "data not persisted" failure followed from that single error. The configuration write now succeeds on first use, so the collection is created and search returns the expected results across restarts. Covered by a new automated regression test.
