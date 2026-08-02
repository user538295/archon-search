## Bug: Two docs quote the server-not-running message differently; 50_ingestion_and_collections.md:16 is truncated

**ID**: S13-error_mentions_server
**Scenario**: S13
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
Two UserManual pages quote the same user-visible CLI string with different text.

  UserManual/50_ingestion_and_collections.md:16
    ... On a refused connection they exit `1` with `"archon-search serve is not running. Start it first."`

  UserManual/100_jobs_and_async_operations.md:11
    ... On connection refused they print `archon-search serve is not running. Start it first with: archon-search serve` (`cli/_helpers.py:_SERVER_NOT_RUNNING_MSG`).

The product emits the LONG form. 100_jobs:11 is therefore correct, and it is also the more precise of the two because it names the source constant (_SERVER_NOT_RUNNING_MSG). 50_ingestion:16 is stale/truncated -- it drops the trailing 'with: archon-search serve'.

This is a doc-vs-doc inconsistency, NOT a product defect. The application behaves correctly.

### What should happen
50_ingestion_and_collections.md:16 should quote the message in full, matching 100_jobs_and_async_operations.md:11 and the product: `archon-search serve is not running. Start it first with: archon-search serve`

The truncation is not signposted. It sits inside quote marks and ends in a period, so it reads as the complete string -- nothing tells a reader it is an excerpt. Anyone writing a client, a log matcher, or a test against that page gets a string the product never emits.

### Steps to reproduce
1. archon-search stop
2. archon-search ingest --path /tmp/single.md --collection test
3. Compare the printed message against both doc lines:
   cd docs/UserManual && grep -n 'Start it first' 50_ingestion_and_collections.md 100_jobs_and_async_operations.md
4. archon-search start   # restore

### Evidence
```
Step 2 actual stdout (exit 1):
  archon-search serve is not running. Start it first with: archon-search serve

Step 3 stdout:
  50_ingestion_and_collections.md:16:> **Write commands require a running server.** ... On a refused connection they exit `1` with `"archon-search serve is not running. Start it first."`
  100_jobs_and_async_operations.md:11:2. **The server must be running.** ... On connection refused they print `archon-search serve is not running. Start it first with: archon-search serve` (`cli/_helpers.py:_SERVER_NOT_RUNNING_MSG`).

Product output matches 100_jobs:11 exactly, character for character.

Demonstrated cost: tests/test_050_s07_s14_ingestion.py was tightened against the truncated 50_ingestion:16 quote and went red on correct product behaviour; scenarios/s013 carried the same truncated string. Both have been corrected to the long form and now pass. Verified on 26.8.1751.
```
