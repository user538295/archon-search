## Bug: install --dry-run output carries no dry-run marker — indistinguishable from a real registration

**ID**: S197-dry_run_describes_registration
**Scenario**: S197
**Severity**: low
**Version**: archon-search, version 26.8.1751

### What happened
`archon-search install --dry-run` prints:

    archon-search service registered and running.

That is a bare state report in the past tense, with nothing marking it as a simulation. It reads exactly like the output of a real `archon-search install` that just succeeded. A user cannot tell from the output whether anything was actually changed.

By contrast the wizard's dry-run marks itself explicitly — `docs/UserManual/20_wizard.md:46` shows `[DRY RUN] Would remove ...`: a `[DRY RUN]` prefix plus conditional phrasing ("Would").

The underlying behaviour is correct — we verified the service is genuinely untouched (same PID before and after). This is a UX/documentation defect in the output, not a functional one.

### What should happen
`install --dry-run` should mark its output as a simulation the way the wizard's dry-run does (`docs/UserManual/20_wizard.md:46`: `[DRY RUN] Would remove ...`) — a `[DRY RUN]` prefix and/or conditional phrasing describing what WOULD be done, rather than a past-tense statement of current state.

EXPLICIT CAVEAT, stated for honesty: the documentation does NOT specify any output format for `install --dry-run`, only for the wizard's. We therefore did NOT assert the presence of a marker in the test — asserting an undocumented format would be inventing a spec. The test asserts only the documented invariant that the service is not restarted (PID unchanged), which passes. This report records the inconsistency for the maintainers to rule on; it may equally be resolved by documenting the intended `install --dry-run` output.

### Steps to reproduce
1. `archon-search status`   # note the reported PID
2. `archon-search install --dry-run`
3. Read the output — it says `archon-search service registered and running.` with no dry-run marker
4. `archon-search status`   # PID unchanged, confirming nothing was actually done

### Evidence
```
$ archon-search install --dry-run
archon-search service registered and running.

Compare, docs/UserManual/20_wizard.md:46 (the wizard's dry-run):
[DRY RUN] Would remove ...

PID invariant verified (this part is CORRECT and is what the test asserts):
  PID before install --dry-run: <pid>
  PID after  install --dry-run: <pid>   (identical)

Guard test: tests/test_s197_install_dry_run.py::TestS197InstallDryRun::test_dry_run_does_not_restart_the_service
```
