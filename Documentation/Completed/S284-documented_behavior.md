## Bug: Add collection with a nonexistent path — behavior undocumented (errors at setup)

**ID**: S284-documented_behavior
**Scenario**: S284
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
Failed: S284: documentation gap — `collection add <nonexistent-path>` behavior is unspecified. Docs checked: UserManual/50_ingestion_and_collections.md (`collection add`, `--wait`), UserManual/100_jobs_and_async_operations.md (job lifecycle, `--wait`, `jobs status`). The docs never state whether a nonexistent path produces a FAILED job, a rejection at registration, or a DONE job over an empty source.

### What should happen
- Documentation insufficient to specify expected behavior — test errors at setup (owner to handle).

  The docs describe `collection add --wait` as "poll `GET /jobs/{id}` until terminal; prints `Collection '<name>' ingested successfully.` on DONE, exits `1` on FAILED" (`50_ingestion_and_collections.md`), and `--wait` more generally as "on `FAILED`/`FAILED_EXPIRED`/`CANCELLED` it prints `Job <STATUS>: <error>` and exits `1`" (`100_jobs_and_async_operations.md`). But nowhere do the docs state what a **nonexistent path** does: they never promise it produces a `FAILED` job (vs. a rejection at registration, or a `DONE` job over an empty source), and they never promise a leftover "zombie" collection entry with `docs=0` in `collection list`. A grep of `./docs/` for `zombie`/`orphan`/`docs=0`/registration-on-failure and for nonexistent/invalid-path handling of `collection add` returns nothing on point. Assert only what the docs state — so this scenario cannot be given real assertions without inventing behavior.

### Steps to reproduce
1. `archon-search collection add /nonexistent/archon/s284/path --wait`
2. `archon-search collection list`

### Evidence
```
E   Failed: S284: documentation gap — `collection add <nonexistent-path>` behavior is unspecified. Docs checked: UserManual/50_ingestion_and_collections.md (`collection add`, `--wait`), UserManual/100_jobs_and_async_operations.md (job lifecycle, `--wait`, `jobs status`). The docs never state whether a nonexistent path produces a FAILED job, a rejection at registration, or a DONE job over an empty source.
```
