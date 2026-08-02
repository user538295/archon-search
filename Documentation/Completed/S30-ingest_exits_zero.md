## Bug: Ingest and search inside container

**ID**: S30-ingest_exits_zero
**Scenario**: S30
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
AssertionError: collection add inside container exited 1:

Error: server returned 500: Internal Server Error

assert 1 == 0

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
E   AssertionError: collection add inside container exited 1:
E     
E     Error: server returned 500: Internal Server Error
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=['docker', 'exec', 'archon-prod', 'archon-search', 'collection', 'add', '/tmp/testdocs', '--wait...a5545417221f42293d1d9e6d5d48e'], returncode=1, stdout='', stderr='Error: server returned 500: Internal Server Error
').returncode
```

---

### Analysis — Product defect, resolved (feature-level)

**Verdict:** confirmed product defect — now fixed.

Adding a folder as a collection failed inside the container because the server could not write its configuration file on first use, which returned a server error before anything was ingested; every downstream search and "data not persisted" failure followed from that single error. The configuration write now succeeds on first use, so the collection is created and search returns the expected results across restarts. Covered by a new automated regression test.
