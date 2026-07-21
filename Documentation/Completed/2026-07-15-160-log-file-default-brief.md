# Feature Brief: Fix Wizard Erasing the Log File Setting

## Problem
After running the setup wizard and choosing JSON log format with "log to stderr only," all application logs silently disappear when the terminal closes — the wizard writes an empty log file path that turns off file logging permanently, even though the user never asked to disable it.

## Goal
After the wizard completes, logs are always written to `~/.archon-search/logs/archon-search.log` unless the user explicitly chose to disable file logging. Existing users whose TOML already has `log_file = ""` receive a clear one-time instruction to remove that line.

## Users & Context
Any operator who ran the setup wizard, chose JSON log format, and answered "yes" to "Log to stderr only?" — their install silently has no log file. They discover this only when they need to debug a problem and find there are no logs on disk.

## Core Flow

**For new installs (wizard fix):**
1. User runs the wizard and chooses their log format (JSON or plain text).
2. User answers "Yes" to "Log to stderr only?" — meaning they want logs mirrored to stderr in addition to the log file, or instead of it.
3. Wizard now treats "stderr only" as a separate, explicit question: "Also write logs to a file? [Y/n]"
4. If user says yes (or presses Enter, which defaults to yes): wizard writes nothing for `log_file` — the code default (`~/.archon-search/logs/archon-search.log`) takes effect.
5. If user explicitly says no: wizard writes `log_file = ""` to disable file logging intentionally.

**For existing affected installs (immediate remediation):**
1. User opens `~/.archon-search/archon-search.toml`.
2. User removes or comments out the line `log_file = ""`.
3. On next server start, logs are written to `~/.archon-search/logs/archon-search.log` automatically.

## In Scope
- Fix the wizard (`install.py`) to stop writing `log_file = ""` as a side-effect of choosing stderr output
- Decouple the "log to stderr" flag from the "disable file logging" flag in the wizard
- Document the remediation step for existing users in the release notes or upgrade guide

## Out of Scope
- Changing the default log path (`~/.archon-search/logs/archon-search.log`) — the default is correct, no change needed
- Auto-migrating existing broken TOMLs on startup — this adds complexity for an edge case; a one-time user action is sufficient
- Adding a `load_config()` guard that treats `""` as "use default" — this removes the ability to intentionally disable file logging, which is a valid advanced use case

## Key Decisions
- **Wizard decouples stderr and file logging:** These are independent outputs. Enabling stderr should not imply disabling the file. The user must explicitly opt out of file logging.
- **No auto-migration on startup:** Auto-migrating a user's TOML without their knowledge is worse than asking them to remove one line. The remediation is documented and trivially reversible.
- **Default remains a tilde path, not absolute:** `"~/.archon-search/logs/archon-search.log"` is expanded at runtime by `logging_setup.py`. This is consistent with the rest of the config and respects `ARCHON_SEARCH_DATA_DIR` override. No change needed here.

## Edge Cases & Constraints
- **User intentionally wants no log file:** After the fix, they must explicitly answer "no" to the new "Also write logs to a file?" prompt. Their choice is now intentional rather than accidental.
- **`ARCHON_SEARCH_DATA_DIR` override:** When this environment variable is set (e.g., Docker), `_apply_env_overrides()` in `config.py` already sets `log_file` to an absolute path under the data dir. The wizard fix does not affect this path — it only fixes the case where the wizard explicitly writes `""`.
- **User has already edited their TOML by hand:** If a user manually set `log_file = ""` for a real reason, the wizard fix does not affect them (wizard only runs at install/re-install time). Their intent is preserved.
- **`test_config_defaults.py` snapshot:** The code default (`"~/.archon-search/logs/archon-search.log"`) is already correct and pinned in this test. No snapshot update needed.

## Decisions

- **WARNING in `load_config()` for `log_file = ""`:** Add it. The wizard fix only helps new installs; operators with already-broken configs (from before this fix) have no signal without the warning. Word it to say "remove this line or set it to a path to re-enable file logging" so even unintentional viewers know exactly what to do. The cost (a spurious warning for operators who intentionally set `log_file = ""`) is low and acceptable.
- **Second wizard prompt ("Also write logs to a file?"):** Drop it; always keep the log file when stderr logging is enabled. The entire bug is that the wizard silently disabled file logging as a side-effect. The fix should be "file logging stays on unless you say otherwise" — and the right moment to say otherwise is a manual TOML edit, not a wizard prompt almost no one needs. Intentional opt-out (e.g. container deployments) remains possible via `log_file = ""` in TOML.

## Future Iterations
- `archon-search doctor` command that checks for common misconfigurations like `log_file = ""` and suggests fixes
- Log rotation settings exposed in the wizard (max file size, backup count) — currently hardcoded in `logging_setup.py`

## References
- [[archon_search/cli/install.py]] `[code-agent]` — wizard logic at lines 284–286 where `log_file = ""` is written
- [[archon_search/config.py]] `[code-agent]` — `load_config()` at lines 546–547 where TOML value unconditionally overwrites the default
- [[archon_search/logging_setup.py]] `[code-agent]` — line 105: `if config.log_file:` guard that silently disables file logging when value is empty

## Recommendation
Fix this. It's a silent data-loss bug — operators running in production have no logs on disk and won't know until they need them. The wizard fix is a small, targeted change to one prompt in `install.py`. The immediate remediation (remove `log_file = ""` from TOML) should be communicated in the next release notes. Neither change touches the config loading logic, keeping blast radius minimal.
