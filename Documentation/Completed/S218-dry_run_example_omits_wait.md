## Bug: reindex-metadata --dry-run example omits --wait, so the documented counts never appear

**ID**: S218-dry_run_example_omits_wait
**Scenario**: S218
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
`UserManual/50_ingestion_and_collections.md:146` shows `archon-search collection reindex-metadata docs --dry-run` with no `--wait`, and `:150` promises that flag will "report processed/updated/skipped counts without writing". Run exactly as documented, the command prints only a job ID and no counts at all.

This is a DOCUMENTATION defect, not a product defect: the behaviour is coherent and correctly described elsewhere. `UserManual/55_chunk_metadata_and_enrichment.md:178` states the route "runs asynchronously as a \`MetadataReindexJob\` and returns \`202\` with a job ID", so the counts cannot exist synchronously; `:165` documents `--wait` as "poll until the job finishes". Adding `--wait` produces the counts immediately. Only the example at `50_ingestion_and_collections.md:146` is misleading.

### What should happen
The example at `50_ingestion_and_collections.md:146` should read `archon-search collection reindex-metadata docs --dry-run --wait`, or `:150` should state that the counts are reported by the job and require `--wait` (or `archon-search jobs show <id>`) to observe. A reader following the page as written concludes the counts are missing.

### Steps to reproduce
1. `archon-search collection add /tmp/archon_s218_col --wait`
2. `archon-search collection reindex-metadata archon_s218_col --dry-run`      # as documented at 50_ingestion:146
3. `archon-search collection reindex-metadata archon_s218_col --dry-run --wait` # coherent form per 55_chunk:165/178

### Evidence
```
Step 2 — exactly as the doc example shows; no counts:
$ archon-search collection reindex-metadata archon_s218_col --dry-run
Reindex-metadata job submitted: 8a7d42fa-9bc2-43a1-817c-a09c7274ef30. Track progress with: archon-search jobs status 8a7d42fa-9bc2-43a1-817c-a09c7274ef30

Step 3 — with --wait, the documented counts appear:
$ archon-search collection reindex-metadata archon_s218_col --dry-run --wait
Reindex-metadata job submitted: 47379f57-f581-4baf-8e98-b3d9f3540ec1. Track progress with: archon-search jobs status 47379f57-f581-4baf-8e98-b3d9f3540ec1
Reindex-metadata complete for 'archon_s218_col'. processed=1, updated=0, skipped=0, ts_normalized=0

tests/test_s218_reindex_metadata_dry_run.py has been corrected to pass --wait and now passes (3 passed).
```
