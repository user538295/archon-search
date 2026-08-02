## Bug: Data persists across container restart

**ID**: S31-search_still_works_after_restart
**Scenario**: S31
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
assert 404 == 200

### What should happen
- After restart, health returns 200.
- Search returns the same results (data persisted in named volume).
- Extras install is skipped (stamp matches); startup is noticeably faster than first start.

### Steps to reproduce
1. `docker restart archon-prod`
2. Poll health until 200.
3. Repeat the search from S30.

### Evidence
```
E   assert 404 == 200
```

---

### Analysis — Product defect, resolved (feature-level)

**Verdict:** confirmed product defect — now fixed.

Adding a folder as a collection failed inside the container because the server could not write its configuration file on first use, which returned a server error before anything was ingested; every downstream search and "data not persisted" failure followed from that single error. The configuration write now succeeds on first use, so the collection is created and search returns the expected results across restarts. Covered by a new automated regression test.
