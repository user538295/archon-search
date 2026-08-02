## Bug: `start` / `stop` / `status` lifecycle

**ID**: S04-health_unreachable_after_stop
**Scenario**: S04
**Severity**: medium
**Version**: archon-search, version 26.7.1738

### What happened
AssertionError: Expected non-200 from /health after stop, got 200
assert 200 != 200

### What should happen
- `stop` exits 0; subsequent `status` reports `stopped`.
- `start` exits 0; subsequent `status` reports `running (PID <n>...)`.
- `GET /health` returns 200 after `start`; returns connection refused (or non-200) after `stop`.

### Steps to reproduce
1. `archon-search status` — note PID.
2. `archon-search stop`
3. `archon-search status` — expect stopped.
4. `archon-search start`
5. `archon-search status` — expect running.

### Evidence
```
E   AssertionError: Expected non-200 from /health after stop, got 200
E   assert 200 != 200
```
