## Bug: wizard --force --delete-db leaves phantom collections in 'collection list' that cannot be removed

**ID**: S38-ghost_collections_after_wizard_force_delete_db
**Scenario**: S38
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
After 'archon-search wizard --force --delete-db' (a supposedly clean database), 'archon-search collection list' still shows collections from prior use (e.g. archon_test_docs, s051_cli_col, s052_col, ...), each with docs=0. --delete-db wipes the LanceDB store, but the [collections].collections registrations in ~/.archon-search/archon-search.toml survive; on restart the startup collection sync cannot rematerialize them (their source paths were removed and/or the crash-loop guard suppressed the sync), so they persist as phantom entries. They cannot be cleaned up: 'collection remove <name>' returns 'not found' (see companion report S630-collection_list_shows_collections_remove_reports_not_found). The user believes they have a clean install but sees stale collections with no supported way to remove them.

### What should happen
The docs describe --delete-db as 'Delete the existing database on reinstall. All indexed data will be lost.' (20_wizard.md:560) and say the wizard 'does not configure them [collections]' (20_wizard.md:791). The documentation does NOT state that a --delete-db reinstall leaves collection registrations that 'collection list' shows but that cannot be removed. Either --delete-db should also clear the now-dataless registrations so the DB is truly clean, or those registrations must stay removable via 'collection remove' — so the user can reach a clean, self-consistent state. As-is there is no CLI path to reconcile 'collection list' with the wiped store.

### Steps to reproduce
1. archon-search wizard --force --delete-db
2. archon-search collection list      # still lists archon_test_docs docs=0, s051_cli_col docs=0, ... (collections whose data was wiped)
3. archon-search collection remove archon_test_docs   # Error: collection 'archon_test_docs' not found.

### Evidence
```
collection list after --delete-db shows 9 stale collections (archon_test_docs, archon_multitype, s051_cli_col, s052_col, s054_col, ttl_test_docs, s187_col, archon_s221_embed, s265_docs), each docs=0 chunks=0.
config [collections].collections has 11 paths; ~/.archon-search/search/ contains only swiftcompress.lance + _archon_collection_meta.lance (the wiped collections have no table).
log: 'startup sync SUPPRESSED for 11 collection(s): the previous run died mid-ingest (job marked process_restart).'
collection remove archon_test_docs -> 'not found' (exit 1).
```
