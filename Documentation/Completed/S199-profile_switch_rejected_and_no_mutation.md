## Bug: aborting wizard deletes the global launchd plist in Step 0 and leaves the service unrecoverable

**ID**: S199-profile_switch_rejected_and_no_mutation
**Scenario**: S199
**Severity**: high
**Version**: archon-search, version 26.8.1751

### What happened
A wizard invocation that ABORTS still removes the global launchd service first, and never restores it. Step 0's legacy-service cleanup runs BEFORE the profile-switch guard, so the wizard dismantles the running service and then refuses to do the work it dismantled it for.

The wizard announces the removal itself:

    === REAL MODE: System will be modified ===
    Removed legacy service file: /Users/manczg/Library/LaunchAgents/com.archon.search.plist

...then exits 1 with:

    Existing index uses BAAI/bge-small-en-v1.5 (chunk_size=512). Switching to
    BAAI/bge-base-en-v1.5 (chunk_size=512) requires re-indexing all documents.
    Run with --force --delete-db to proceed.

After the abort: plist MISSING, launchctl NOT LOADED, /health no response, `archon-search status` -> stopped. Unchanged 10s later, so it is not a transient race.

THE RESULTING STATE IS UNRECOVERABLE BY `start`. `archon-search start` exits 1 with "Error: Plist not installed" once the plist is gone, so nothing short of a fresh `archon-search install` brings the service back. An operator who runs a wizard that refuses its own change is left with a dead service and an error message that says nothing about it.

CRITICALLY, ARCHON_SEARCH_DATA_DIR/ARCHON_SEARCH_CONFIG DO NOT CONTAIN THIS. The run was fully isolated to a temp dir via both env vars plus --config, and the plist deleted was still the GLOBAL one under ~/Library/LaunchAgents. No env var redirects that path, so there is no way for a caller to sandbox this side effect.

This is the root cause of the 2026-08-01 suite cascade: 103 downstream tests lost and ~68 minutes of wall clock, because the plist went missing mid-run and self-heal could not restore it.

### What should happen
A wizard run that aborts without making its change must leave the system exactly as it found it. Either Step 0's legacy-service cleanup runs AFTER the profile/compatibility guards have passed, or an abort rolls back the removal.

`docs/UserManual/20_wizard.md:34-38` documents Step 0's legacy-service cleanup, and :49 shows the dry-run path announcing `[DRY RUN] Would remove legacy service file: ...` — i.e. the removal is a real, intended action on the real path. The docs do not state anywhere that a wizard which ABORTS will still have removed the service; an operator reading the profile-switch guard message ("Run with --force --delete-db to proceed") would reasonably conclude nothing was changed and simply re-run with the suggested flag.

At minimum, if the removal must precede the guards, the abort message must say the service was removed and tell the operator to run `archon-search install`.

### Steps to reproduce
1. Confirm the service is up: `ls ~/Library/LaunchAgents/com.archon.search.plist`, `launchctl list | grep com.archon.search`, `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health` (expect 200)
2. Seed an isolated minimal config in a temp dir (embedding_model = "BAAI/bge-small-en-v1.5") so the profile switch is rejected
3. Run the wizard fully isolated, so ONLY the global service can be affected:
   `env ARCHON_SEARCH_DATA_DIR=$TMP ARCHON_SEARCH_CONFIG=$CFG archon-search wizard --profile balanced --non-interactive --config $CFG`
4. Observe exit code 1 and the abort message about re-indexing
5. Re-check the same three things from step 1 — plist MISSING, launchctl NOT LOADED, /health no response
6. `archon-search start` -> exits 1, "Error: Plist not installed". Only `archon-search install` recovers.

### Evidence
```
FRESH REPRODUCTION (2026-08-01 11:39, archon-search 26.8.1751), wizard run RAW outside pytest
so no fixture teardown could repair the damage before observation:

--- state: BEFORE ---
plist: PRESENT  Aug  1 11:03:15 2026 1043 bytes
launchctl: 14601\t0\tcom.archon.search
health: 200
status: running (PID 14601)

--- wizard stdout (exit 1) ---
=== REAL MODE: System will be modified ===
Removed legacy service file: /Users/manczg/Library/LaunchAgents/com.archon.search.plist
[... profile descriptions ...]
Existing index uses BAAI/bge-small-en-v1.5 (chunk_size=512). Switching to
BAAI/bge-base-en-v1.5 (chunk_size=512) requires re-indexing all documents.
Run with --force --delete-db to proceed.

--- state: AFTER (immediate) ---
plist: ***MISSING***
launchctl: ***NOT LOADED***
health: 000NO RESPONSE
status: stopped

--- state: AFTER (+10s) ---
plist: ***MISSING***
launchctl: ***NOT LOADED***
health: 000NO RESPONSE
status: stopped

Recovery required a full `archon-search install` (plist restored, PID 16900, /health 200).

CORROBORATING EVIDENCE FROM THE 2026-08-01 08:36 SUITE RUN (log/20260801_083617.txt):
  line 1820-1824  S197, S199, S200, S205 execute in that order
  line 257        first SERVER SELF-HEAL, victim S207 — the server is down
  71 heals total, 103 tests lost, ~68 min wall clock, zero recoveries
  All 69 attributed heals named S197 — a STALE POINTER artifact, not the culprit:
  _LAST_HEALTHY_NODEID only advances for tests using the shared fixtures, and
  S199/S200/S205 use none, so the real killer could never be named. S197 was merely
  the last shared-fixture test to pass.

WHY THIS DID NOT REPRODUCE UNDER PYTEST TODAY (and why that is not exoneration):
  At git 70eef3b the wizard_tmp teardown was bare `shutil.rmtree(tmp, ignore_errors=True)`.
  The working tree has since added `restore_managed_service()` to that teardown
  (tests/conftest.py:621), which re-registers the plist. Running S199 through pytest today
  therefore REPAIRS the damage before it can be observed. That teardown line is LOAD-BEARING:
  it is currently the only thing preventing this suite from re-entering the cascade, and it
  must not be removed as defensive tidiness.

Experiment script: scratchpad/plist_experiment.sh (raw invocation, no pytest).
```
