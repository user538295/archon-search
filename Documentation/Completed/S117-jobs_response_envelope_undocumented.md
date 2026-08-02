## Bug: GET /jobs response envelope is undocumented (next_cursor appears in no doc file)

**ID**: S117-jobs_response_envelope_undocumented
**Scenario**: S117
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
GET /jobs returns the cursor-paginated object {"items": [...], "next_cursor": ..., "total": N}, verified on 26.8.1751 for both GET /jobs and GET /jobs?status=FAILED_EXPIRED. None of those three field names is specified anywhere in docs/. A grep for 'next_cursor' across docs/UserManual and docs/OperatorGuide returns ZERO matches. UserManual/100_jobs_and_async_operations.md:111 documents only the QUERY parameters (status, kind, source, limit 1..200, cursor); it never states what the response looks like.

### What should happen
The jobs guide should specify the GET /jobs response envelope alongside the query parameters it already documents: the container object, the name of the job array, the continuation-token field, and the total count. A client cannot page through GET /jobs from the documentation alone -- the doc names the 'cursor' request parameter but never says which response field supplies its value.

### Steps to reproduce
1. archon-search status   # confirm the server is running
2. curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" 'http://127.0.0.1:8765/jobs' | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)))'
3. cd docs/UserManual && grep -rn next_cursor --include='*.md' .
4. cd ../OperatorGuide && grep -rn next_cursor --include='*.md' .

### Evidence
```
Step 2 stdout: ['items', 'next_cursor', 'total']  (identical for /jobs and /jobs?status=FAILED_EXPIRED)
Step 3 stdout: (no matches)
Step 4 stdout: (no matches)
Only doc reference to pagination on this endpoint: UserManual/100_jobs_and_async_operations.md:111 -- query parameters only.

Operational cost, not hypothetical: because the envelope is unspecified, every test of this endpoint in the suite had to be written against observed behaviour. Three of them (S129, S154, S173) were written to expect a BARE ARRAY and filed false bug reports when the product correctly returned the envelope.

Counter-argument, stated fairly: the same section (100_jobs_and_async_operations.md:107) says
"`GET /openapi.json` is the authoritative schema", so the shape IS machine-discoverable and
this is not a total documentation void. That is why this is filed LOW and as a documentation
defect rather than a product defect. It does not close the gap, though: the narrative doc
introduces the `cursor` REQUEST parameter without ever saying which RESPONSE field supplies
its value, so the one thing a reader needs in order to use the documented parameter is the
one thing the page omits. A single row naming the envelope would fix it.
```
