## Bug: collection list shows collections that 'collection remove' / 'collection info' report as not found (404)

**ID**: S630-collection_list_shows_collections_remove_reports_not_found
**Scenario**: S630
**Severity**: high
**Version**: archon-search, version 26.9.2111

### What happened
'archon-search collection list' lists collections (e.g. archon_test_docs, and 8 others), but 'archon-search collection info archon_test_docs' and 'archon-search collection remove archon_test_docs' both fail with "collection 'archon_test_docs' not found" (CLI exit 1; the underlying GET/DELETE /collections/{name} return HTTP 404). The user cannot remove or inspect a collection the tool itself shows in the list. ROOT CAUSE (observed): GET /collections/ lists collections registered in [collections].collections in ~/.archon-search/archon-search.toml, while GET/DELETE /collections/{name} require the collection to exist in the LanceDB meta store. Registrations present in config but absent from the store (e.g. after 'wizard --force --delete-db' wiped the store, or whose source path no longer exists while the startup sync is suppressed) are listed-but-unaddressable. Confirmed the split: a freshly re-added docs=0 collection (which IS written to the store) removes cleanly with exit 0, while the config-only entries 404. Companion report: S38-ghost_collections_after_wizard_force_delete_db (same root cause, different symptom).

### What should happen
A collection that 'collection list' shows must be addressable by 'collection info' and removable by 'collection remove'. Docs: 50_ingestion_and_collections.md:113 (list prints one line per collection), :126 ('The collection remains registered (with docs=0); use collection remove to clean it up'), :129-131 (remove proxies DELETE /collections/{name}). 'remove'/'info' must not report a name that 'list' just showed as 'not found' — the two surfaces must agree on which collections exist.

### Steps to reproduce
1. archon-search collection list        # shows 'archon_test_docs  docs=0  chunks=0' (among others)
2. archon-search collection remove archon_test_docs   # Error: collection 'archon_test_docs' not found.  (exit 1)
3. archon-search collection info archon_test_docs     # Collection 'archon_test_docs' not found.  (exit 1)

### Evidence
```
GET /collections/ (raw) includes: {"name":"archon_test_docs","path":"/private/tmp/archon-test-docs","doc_count":0,"status":"not_yet_indexed",...}
GET /collections/archon_test_docs  -> http=404 {"detail":"Collection 'archon_test_docs' not found"}
DELETE /collections/archon_test_docs -> http=404
collection remove archon_test_docs -> Error: collection 'archon_test_docs' not found. (exit=1)
config [collections].collections lists 11 paths; ~/.archon-search/search/ contains only swiftcompress.lance + _archon_collection_meta.lance
Control: a freshly re-added docs=0 collection ('repro_ghost_*') removed with exit 0 -> proves store-vs-config split.
```
