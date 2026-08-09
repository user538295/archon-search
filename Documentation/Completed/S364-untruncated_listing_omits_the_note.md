## Bug: `jobs list` prints the `Showing N of M jobs` note only when the listing is truncated

**ID**: S364-untruncated_listing_omits_the_note
**Scenario**: S364
**Severity**: medium
**Version**: archon-search, version 26.8.1931

### What happened
AssertionError: `jobs list --status cancelled --limit 200` printed the `Showing N of M jobs` line although all 2 matching job(s) were returned; UserManual/100_jobs_and_async_operations.md:72 prints it only when more jobs exist than were returned
ID        TYPE                COLLECTION            STATUS          STARTED               ELAPSED
----------------------------------------------------------------------------------------------
ca1acbca  ingest              archon_s363_docs      CANCELLED       2026-08-09 09:39:12   28s
7f816fef  ingest              archon_s359_docs      CANCELLED       2026-08-09 09:37:23   57s
Showing 2 of 2 jobs — use --limit to see more (max: 200).

assert not <re.Match object; span=(381, 438), match='Showing 2 of 2 jobs — use --limit to see more (ma>

### What should happen
- Step 1 reports a `total` **greater than 50**, so step 2's listing is truncated at the
  documented default (100:69).
- Step 2 exits `0` and prints a line matching exactly
  `Showing N of M jobs — use --limit to see more (max: 200).` (100:72), with `N <= 50` and
  `M > N`.
- Step 2's `M` **equals** the `total` from step 1 — 100:122 defines `total` as the count of
  jobs matching the filters across all pages, which is precisely the "M jobs" that exist.
- Step 3 prints the same line with `N <= 5` and `M` again equal to the unfiltered `total`.
- Step 5: the filter matches **fewer jobs than the limit**, so nothing was withheld and the
  `Showing N of M jobs` line is **absent** — 100:72 prints it only "when more jobs exist than
  were returned". The listing itself still prints its rows and exits `0`.

### Steps to reproduce
1. `curl -s "http://127.0.0.1:8765/jobs?limit=1" -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])'`
2. `archon-search jobs list`
3. `archon-search jobs list --limit 5`
4. Find a status whose job count is small, e.g.:
   ```bash
   curl -s "http://127.0.0.1:8765/jobs?status=CANCELLED&limit=1" \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])'
   ```
5. `archon-search jobs list --status cancelled --limit 200`

### Evidence
```
E   AssertionError: `jobs list --status cancelled --limit 200` printed the `Showing N of M jobs` line although all 2 matching job(s) were returned; UserManual/100_jobs_and_async_operations.md:72 prints it only when more jobs exist than were returned
E     ID        TYPE                COLLECTION            STATUS          STARTED               ELAPSED
E     ----------------------------------------------------------------------------------------------
E     ca1acbca  ingest              archon_s363_docs      CANCELLED       2026-08-09 09:39:12   28s
E     7f816fef  ingest              archon_s359_docs      CANCELLED       2026-08-09 09:37:23   57s
E     Showing 2 of 2 jobs — use --limit to see more (max: 200).
E     
E   assert not <re.Match object; span=(381, 438), match='Showing 2 of 2 jobs — use --limit to see more (ma>
E    +  where <re.Match object; span=(381, 438), match='Showing 2 of 2 jobs — use --limit to see more (ma> = <built-in method search of re.Pattern object at 0x83be4de10>('ID        TYPE                COLLECTION            STATUS          STARTED               ELAPSED
------------------..._s359_docs      CANCELLED       2026-08-09 09:37:23   57s
Showing 2 of 2 jobs — use --limit to see more (max: 200).
')
E    +    where <built-in method search of re.Pattern object at 0x83be4de10> = re.compile('Showing\\s+(\\d+)\\s+of\\s+(\\d+)\\s+jobs\\s+—\\s+use\\s+--limit\\s+to\\s+see\\s+more\\s+\\(max:\\s*200\\)\\.').search
```
