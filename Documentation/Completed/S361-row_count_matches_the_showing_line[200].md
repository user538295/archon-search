## Bug: `archon-search jobs list --limit <n>` prints at most n rows

**ID**: S361-row_count_matches_the_showing_line[200]
**Scenario**: S361
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
052e53d0  ingest              ttl_test_docs         DONE            2026-08-06 19:45:32   0s
441db74a  ingest              ttl_test_docs         DONE            2026-08-06 19:45:29   0s
22446133  ingest              s102-col              DONE            2026-08-06 19:43:09   0s
a04121ce  ingest              s101-col              DONE            2026-08-06 19:43:06   0s
2a94843a  ingest              s099-col              DONE            2026-08-06 19:43:03   0s
618d696b  ingest              s098-col              DONE            2026-08-06 19:43:00   1s
3ea4cb35  ingest              s097-col              DONE            2026-08-06 19:42:56   0s
68facf82  export              s093_col              DONE            2026-08-06 19:42:54   1s
547a0dab  ingest              s093_col              DONE            2026-08-06 19:42:51   0s
5e0dc4bf  import              s092_col              DONE            2026-08-06 19:42:47   3s
8883c78a  export              s092_col              DONE            2026-08-06 19:42:40   4s
3c1bff41  ingest              s092_col              DONE            2026-08-06 19:42:38   0s
4a7c4a11  import              s091_col              DONE            2026-08-06 19:42:31   4s
bb159384  export              s091_col              DONE            2026-08-06 19:42:12   18s
e4db2702  ingest              s091_col              DONE            2026-08-06 19:42:10   0s
808f107b  export              archon_test_docs      DONE            2026-08-06 19:42:09   15s
7a5f24a8  export              archon_test_docs      DONE            2026-08-06 19:42:09   10s
7623dbd2  export              archon_test_docs      DONE            2026-08-06 19:42:07   8s
6ecb7950  export              archon_test_docs      DONE            2026-08-06 19:42:07   3s
6d8244d4  export              archon_test_docs      DONE            2026-08-06 19:42:01   4s
cbab54ac  metadata_reindex    s054_col              DONE            2026-08-06 19:38:37   0s
40062a6a  ingest              s054_col              DONE            2026-08-06 19:38:35   0s
b6ee5e0b  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:34   0s
a02562c8  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:34   0s
bb08da5e  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:32   0s
1e02efe1  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:31   0s
ed927c5e  metadata_reindex    s052_col              DONE            2026-08-06 19:38:31   0s
429f724e  metadata_reindex    s052_col              DONE            2026-08-06 19:38:30   0s
a65c023e  metadata_reindex    s052_col              DONE            2026-08-06 19:38:30   0s
dd6bd0ca  metadata_reindex    s052_col              DONE            2026-08-06 19:38:30   0s
b44c11d5  ingest              s052_col              DONE            2026-08-06 19:38:27   0s
8efbc42f  ingest              s051_http_col         DONE            2026-08-06 19:38:25   0s
aab7dd5d  ingest              s051_cli_col          DONE            2026-08-06 19:38:22   0s
d7d60af0  sync                                      DONE            2026-08-06 19:36:10   3s
14f6e282  reindex                                   DONE            2026-08-06 19:36:07   1s
8b8cb218  ingest              archon_multitype      DONE            2026-08-06 19:36:03   1s
fafe3c2e  ingest              rest-docs             DONE            2026-08-06 19:35:47   0s
3039f68c  ingest              single-docs           DONE            2026-08-06 19:35:45   0s
74022d6b  ingest              archon_test_docs      DONE            2026-08-06 19:35:42   1s
020c0af7  ingest              archon_warmup_probe   DONE            2026-08-06 19:30:55   1s

assert None

### What should happen
- Step 1 reports a `total` **greater than 3**, so the limits in steps 2-3 are binding
  (100:122 — `total` counts across all pages).
- Steps 2, 3, 4 and 5 each exit **`0`**.
- Step 2 prints **at most 1** data row, step 3 **at most 3**, step 4 **at most 200** — 100:69
  documents `--limit` over the range `1..200`.
- Each of steps 2-4 prints **at least one** data row: more jobs exist than the limit, so a
  bounded listing is not an empty one.
- In each of steps 2-4 the row count **equals the `N`** in the `Showing N of M jobs — use
  --limit to see more (max: 200).` line (100:72). The summary line describes the table above
  it; a mismatch means one of the two is wrong.
- Step 5 (no `--limit`) prints **at most 50** data rows — 100:69 "default 50".

### Steps to reproduce
1. `curl -s "http://127.0.0.1:8765/jobs?limit=1" -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])'`
2. `archon-search jobs list --limit 1`
3. `archon-search jobs list --limit 3`
4. `archon-search jobs list --limit 200`
5. `archon-search jobs list`

### Evidence
```
 19:45:35   0s
E     052e53d0  ingest              ttl_test_docs         DONE            2026-08-06 19:45:32   0s
E     441db74a  ingest              ttl_test_docs         DONE            2026-08-06 19:45:29   0s
E     22446133  ingest              s102-col              DONE            2026-08-06 19:43:09   0s
E     a04121ce  ingest              s101-col              DONE            2026-08-06 19:43:06   0s
E     2a94843a  ingest              s099-col              DONE            2026-08-06 19:43:03   0s
E     618d696b  ingest              s098-col              DONE            2026-08-06 19:43:00   1s
E     3ea4cb35  ingest              s097-col              DONE            2026-08-06 19:42:56   0s
E     68facf82  export              s093_col              DONE            2026-08-06 19:42:54   1s
E     547a0dab  ingest              s093_col              DONE            2026-08-06 19:42:51   0s
E     5e0dc4bf  import              s092_col              DONE            2026-08-06 19:42:47   3s
E     8883c78a  export              s092_col              DONE            2026-08-06 19:42:40   4s
E     3c1bff41  ingest              s092_col              DONE            2026-08-06 19:42:38   0s
E     4a7c4a11  import              s091_col              DONE            2026-08-06 19:42:31   4s
E     bb159384  export              s091_col              DONE            2026-08-06 19:42:12   18s
E     e4db2702  ingest              s091_col              DONE            2026-08-06 19:42:10   0s
E     808f107b  export              archon_test_docs      DONE            2026-08-06 19:42:09   15s
E     7a5f24a8  export              archon_test_docs      DONE            2026-08-06 19:42:09   10s
E     7623dbd2  export              archon_test_docs      DONE            2026-08-06 19:42:07   8s
E     6ecb7950  export              archon_test_docs      DONE            2026-08-06 19:42:07   3s
E     6d8244d4  export              archon_test_docs      DONE            2026-08-06 19:42:01   4s
E     cbab54ac  metadata_reindex    s054_col              DONE            2026-08-06 19:38:37   0s
E     40062a6a  ingest              s054_col              DONE            2026-08-06 19:38:35   0s
E     b6ee5e0b  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:34   0s
E     a02562c8  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:34   0s
E     bb08da5e  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:32   0s
E     1e02efe1  metadata_reindex    archon_test_docs      DONE            2026-08-06 19:38:31   0s
E     ed927c5e  metadata_reindex    s052_col              DONE            2026-08-06 19:38:31   0s
E     429f724e  metadata_reindex    s052_col              DONE            2026-08-06 19:38:30   0s
E     a65c023e  metadata_reindex    s052_col              DONE            2026-08-06 19:38:30   0s
E     dd6bd0ca  metadata_reindex    s052_col              DONE            2026-08-06 19:38:30   0s
E     b44c11d5  ingest              s052_col              DONE            2026-08-06 19:38:27   0s
E     8efbc42f  ingest              s051_http_col         DONE            2026-08-06 19:38:25   0s
E     aab7dd5d  ingest              s051_cli_col          DONE            2026-08-06 19:38:22   0s
E     d7d60af0  sync                                      DONE            2026-08-06 19:36:10   3s
E     14f6e282  reindex                                   DONE            2026-08-06 19:36:07   1s
E     8b8cb218  ingest              archon_multitype      DONE            2026-08-06 19:36:03   1s
E     fafe3c2e  ingest              rest-docs             DONE            2026-08-06 19:35:47   0s
E     3039f68c  ingest              single-docs           DONE            2026-08-06 19:35:45   0s
E     74022d6b  ingest              archon_test_docs      DONE            2026-08-06 19:35:42   1s
E     020c0af7  ingest              archon_warmup_probe   DONE            2026-08-06 19:30:55   1s
E     
E   assert None
```
