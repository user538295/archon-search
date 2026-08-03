## Bug: archon-search serve removes launchd plist on malformed-TOML startup failure

**ID**: S138-serve_exits_non_zero
**Scenario**: S138
**Severity**: high
**Version**: 26.8.1815

### What happened
After S138's test_serve_exits_non_zero ran, ~/Library/LaunchAgents/com.archon.search.plist was gone. archon-search start returned 'Plist not installed'; archon-search install timed out. All subsequent server-dependent tests errored (68 total cascade).

### What should happen
archon-search serve must never modify or remove the launchd plist. A config-parse failure is a startup error; the OS service registration is owned by install/uninstall only.

### Steps to reproduce
1. Run archon-search install (registers plist)
2. Run: ARCHON_SEARCH_CONFIG=/tmp/malformed.toml archon-search serve (where malformed.toml contains invalid TOML)
3. Verify plist is gone: ls ~/Library/LaunchAgents/com.archon.search.plist

### Evidence
```
conftest self-heal log: start exit 1 'Plist not installed at ~/Library/LaunchAgents/com.archon.search.plist'; install exit 1 timed out; service registered after attempt: True. 68 downstream setup errors from S139 onward.
```
