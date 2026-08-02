## Bug: `archon-search status --json` produces JSON output

**ID**: S209-status_json_reports_running_state
**Scenario**: S209
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
AssertionError: `status --json` stdout is not valid JSON (Expecting value: line 1 column 1 (char 0))
stdout:

stderr:
Usage: archon-search status [OPTIONS]
Try 'archon-search status --help' for help.

Error: No such option '--json'.

### What should happen
- Exits 0.
- Output is valid JSON (parseable with `python3 -m json.tool` or `jq .`).
- JSON contains a field indicating the running state (e.g. `"status": "running"` or equivalent).

### Steps to reproduce
1. `archon-search status --json`

### Evidence
```
E   json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

During handling of the above exception, another exception occurred:
E   AssertionError: `status --json` stdout is not valid JSON (Expecting value: line 1 column 1 (char 0))
E   stdout:
E   
E   stderr:
E   Usage: archon-search status [OPTIONS]
E   Try 'archon-search status --help' for help.
E   
E   Error: No such option '--json'.
```
