## Bug: `archon-search status --json` produces JSON output

**ID**: S209-status_json_exits_zero
**Scenario**: S209
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: `status --json` exit=2
stdout:

stderr:
Usage: archon-search status [OPTIONS]
Try 'archon-search status --help' for help.

Error: No such option '--json'.

assert 2 == 0

### What should happen
- Exits 0.
- Output is valid JSON (parseable with `python3 -m json.tool` or `jq .`).
- JSON contains a field indicating the running state (e.g. `"status": "running"` or equivalent).

### Steps to reproduce
1. `archon-search status --json`

### Evidence
```
E   AssertionError: `status --json` exit=2
E     stdout:
E     
E     stderr:
E     Usage: archon-search status [OPTIONS]
E     Try 'archon-search status --help' for help.
E     
E     Error: No such option '--json'.
E     
E   assert 2 == 0
E    +  where 2 = CompletedProcess(args=('archon-search', 'status', '--json'), returncode=2, stdout='', stderr="Usage: archon-search status [OPTIONS]
Try 'archon-search status --help' for help.

Error: No such option '--json'.
").returncode
```
