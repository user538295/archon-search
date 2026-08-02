## Bug: `key rotate --grace`: old key stays active during grace window

**ID**: S133-rotate_with_grace_keeps_old_active
**Scenario**: S133
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: old key rejected during grace window (grace not honored)
assert 401 == 200

### What should happen
- Step 2: Exit 0; stdout contains the new raw bearer token.
- Step 4: `old_key_status` is `active` (not `revoked`); `old_key_expires_at` is approximately `now + 1h`.
- Step 5: HTTP 200 — old key is still accepted during the grace window.
- Step 6: HTTP 200 — new key is accepted immediately.
- Step 7: Both the new key and the old key appear in the listing; old key shows as `active` with a future `expires_at`.

### Steps to reproduce
1. `OLD_KEY=$ARCHON_SEARCH_API_KEY`
2. `archon-search key rotate --grace 1h 2>/tmp/rotate_grace_stderr.txt`
3. `source ~/.archon-search/.search.env`
4. `cat /tmp/rotate_grace_stderr.txt`
5. `curl -sS -w "\n%{http_code}" http://127.0.0.1:8765/status \
     -H "Authorization: Bearer $OLD_KEY"`
6. `curl -sS -w "\n%{http_code}" http://127.0.0.1:8765/status \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"`
7. `archon-search key list --status all`

### Evidence
```
E   AssertionError: old key rejected during grace window (grace not honored)
E   assert 401 == 200
E    +  where 401 = http_status('http://127.0.0.1:60650/status', headers={'Authorization': 'Bearer [REDACTED]
```
