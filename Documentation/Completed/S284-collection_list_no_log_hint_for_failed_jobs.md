## Bug: collection list gives no hint where to find the reason for failed ingest jobs

**ID**: S284-collection_list_no_log_hint_for_failed_jobs
**Scenario**: S284
**Severity**: low
**Version**: archon-search, version 26.9.2111

### What happened
After 'collection add', jobs failed (jobs list shows FAILED). 'collection list' shows each collection as '<name>  docs=0  chunks=0' with no indication that ingestion failed and no pointer to where the reason can be checked. To find the cause the user must already know to run 'archon-search jobs status <id>' (surfaces a terse code like 'process_restart') and to open ~/.archon-search/logs/archon-search.log. Nothing in the 'collection list' output guides them there.

### What should happen
collection list output should give the user a hint where to look when a collection is empty/failed — e.g. print the server log path (~/.archon-search/logs/archon-search.log, per 160_troubleshooting.md:19) and/or a note to run 'archon-search jobs list/status <id>'. A user seeing docs=0 should not have to already know the diagnostic path.

### Steps to reproduce
1. archon-search collection add ~/Documents/development/n1ka
2. archon-search collection add ~/Documents/development/swiftcompress
3. (wait) archon-search jobs list   # both ingest jobs -> FAILED
4. archon-search collection list   # shows 'n1ka  docs=0  chunks=0' etc. with no reason and no log-path hint

### Evidence
```
collection list:
  n1ka  docs=0  chunks=0
  swiftcompress  docs=0  chunks=0
jobs list:
  b2574c97  ingest  swiftcompress  FAILED
  67254a79  ingest  n1ka           FAILED
jobs status 67254a79 -> error: process_restart
Actual failure detail only in ~/.archon-search/logs/archon-search.log — not referenced anywhere in 'collection list' output.
NOTE: enhancement/UX gap — the docs do not currently promise a log hint in 'collection list'.
```
