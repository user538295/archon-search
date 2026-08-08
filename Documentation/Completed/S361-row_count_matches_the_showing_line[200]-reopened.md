## Bug: `archon-search jobs list --limit <n>` prints at most n rows

**ID**: S361-row_count_matches_the_showing_line[200]
**Scenario**: S361
**Severity**: medium
**Version**: archon-search, version 26.8.1916

### What happened
6bcdc841  ingest              ttl_test_docs         DONE            2026-08-08 13:37:40   0s
0681f65d  ingest              s102-col              DONE            2026-08-08 13:36:27   0s
bd725522  ingest              s101-col              DONE            2026-08-08 13:36:25   0s
b2603d93  ingest              s099-col              DONE            2026-08-08 13:36:22   0s
6db6aa55  ingest              s098-col              DONE            2026-08-08 13:36:19   0s
06a3bebf  ingest              s097-col              DONE            2026-08-08 13:36:16   0s
b6f29fdc  export              s093_col              DONE            2026-08-08 13:36:13   1s
db7123d2  ingest              s093_col              DONE            2026-08-08 13:36:10   0s
ee2ca489  import              s092_col              DONE            2026-08-08 13:36:06   3s
bd58334c  export              s092_col              DONE            2026-08-08 13:35:59   4s
98577390  ingest              s092_col              DONE            2026-08-08 13:35:57   0s
2a109153  import              s091_col              DONE            2026-08-08 13:35:50   4s
a38955c3  export              s091_col              DONE            2026-08-08 13:35:31   17s
071cade1  ingest              s091_col              DONE            2026-08-08 13:35:29   0s
b3fa42b4  export              archon_test_docs      DONE            2026-08-08 13:35:29   15s
b93f905f  export              archon_test_docs      DONE            2026-08-08 13:35:29   10s
209212af  export              archon_test_docs      DONE            2026-08-08 13:35:26   7s
707bfd0e  export              archon_test_docs      DONE            2026-08-08 13:35:26   3s
f81dd576  export              archon_test_docs      DONE            2026-08-08 13:35:24   0s
9ff4f335  metadata_reindex    s054_col              DONE            2026-08-08 13:34:51   0s
082e069b  ingest              s054_col              DONE            2026-08-08 13:34:49   0s
af88e92f  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:48   0s
2fa1a344  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:46   0s
fc05a27d  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:44   0s
86ef9b24  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:43   0s
64aa1f18  metadata_reindex    s052_col              DONE            2026-08-08 13:34:43   0s
8b4f8d4c  metadata_reindex    s052_col              DONE            2026-08-08 13:34:42   0s
39aab115  metadata_reindex    s052_col              DONE            2026-08-08 13:34:41   0s
483dd9f5  metadata_reindex    s052_col              DONE            2026-08-08 13:34:41   0s
d6ab5951  ingest              s052_col              DONE            2026-08-08 13:34:39   0s
6c49b271  ingest              s051_http_col         DONE            2026-08-08 13:34:36   0s
5bf8900b  ingest              s051_cli_col          DONE            2026-08-08 13:34:33   0s
ac64a74e  sync                                      DONE            2026-08-08 13:33:08   1s
d32ce651  reindex                                   DONE            2026-08-08 13:33:06   0s
76d6ccd8  ingest              archon_multitype      DONE            2026-08-08 13:32:57   1s
b0e12b2c  ingest              rest-docs             DONE            2026-08-08 13:32:37   0s
59d37321  ingest              single-docs           DONE            2026-08-08 13:32:34   0s
b954f1a3  ingest              archon_test_docs      DONE            2026-08-08 13:32:31   0s
b5a596bf  ingest              archon_warmup_probe   DONE            2026-08-08 13:30:52   0s
b1ee0f64  ingest              path2                 FAILED          2026-08-08 11:57:51   0s

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
 13:37:43   0s
E     6bcdc841  ingest              ttl_test_docs         DONE            2026-08-08 13:37:40   0s
E     0681f65d  ingest              s102-col              DONE            2026-08-08 13:36:27   0s
E     bd725522  ingest              s101-col              DONE            2026-08-08 13:36:25   0s
E     b2603d93  ingest              s099-col              DONE            2026-08-08 13:36:22   0s
E     6db6aa55  ingest              s098-col              DONE            2026-08-08 13:36:19   0s
E     06a3bebf  ingest              s097-col              DONE            2026-08-08 13:36:16   0s
E     b6f29fdc  export              s093_col              DONE            2026-08-08 13:36:13   1s
E     db7123d2  ingest              s093_col              DONE            2026-08-08 13:36:10   0s
E     ee2ca489  import              s092_col              DONE            2026-08-08 13:36:06   3s
E     bd58334c  export              s092_col              DONE            2026-08-08 13:35:59   4s
E     98577390  ingest              s092_col              DONE            2026-08-08 13:35:57   0s
E     2a109153  import              s091_col              DONE            2026-08-08 13:35:50   4s
E     a38955c3  export              s091_col              DONE            2026-08-08 13:35:31   17s
E     071cade1  ingest              s091_col              DONE            2026-08-08 13:35:29   0s
E     b3fa42b4  export              archon_test_docs      DONE            2026-08-08 13:35:29   15s
E     b93f905f  export              archon_test_docs      DONE            2026-08-08 13:35:29   10s
E     209212af  export              archon_test_docs      DONE            2026-08-08 13:35:26   7s
E     707bfd0e  export              archon_test_docs      DONE            2026-08-08 13:35:26   3s
E     f81dd576  export              archon_test_docs      DONE            2026-08-08 13:35:24   0s
E     9ff4f335  metadata_reindex    s054_col              DONE            2026-08-08 13:34:51   0s
E     082e069b  ingest              s054_col              DONE            2026-08-08 13:34:49   0s
E     af88e92f  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:48   0s
E     2fa1a344  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:46   0s
E     fc05a27d  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:44   0s
E     86ef9b24  metadata_reindex    archon_test_docs      DONE            2026-08-08 13:34:43   0s
E     64aa1f18  metadata_reindex    s052_col              DONE            2026-08-08 13:34:43   0s
E     8b4f8d4c  metadata_reindex    s052_col              DONE            2026-08-08 13:34:42   0s
E     39aab115  metadata_reindex    s052_col              DONE            2026-08-08 13:34:41   0s
E     483dd9f5  metadata_reindex    s052_col              DONE            2026-08-08 13:34:41   0s
E     d6ab5951  ingest              s052_col              DONE            2026-08-08 13:34:39   0s
E     6c49b271  ingest              s051_http_col         DONE            2026-08-08 13:34:36   0s
E     5bf8900b  ingest              s051_cli_col          DONE            2026-08-08 13:34:33   0s
E     ac64a74e  sync                                      DONE            2026-08-08 13:33:08   1s
E     d32ce651  reindex                                   DONE            2026-08-08 13:33:06   0s
E     76d6ccd8  ingest              archon_multitype      DONE            2026-08-08 13:32:57   1s
E     b0e12b2c  ingest              rest-docs             DONE            2026-08-08 13:32:37   0s
E     59d37321  ingest              single-docs           DONE            2026-08-08 13:32:34   0s
E     b954f1a3  ingest              archon_test_docs      DONE            2026-08-08 13:32:31   0s
E     b5a596bf  ingest              archon_warmup_probe   DONE            2026-08-08 13:30:52   0s
E     b1ee0f64  ingest              path2                 FAILED          2026-08-08 11:57:51   0s
E     
E   assert None
```
