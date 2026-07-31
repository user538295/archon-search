# Feature Brief: Strategy Pattern for Dry-Run Enforcement

## Problem
Agent-generated code added an installation step that modifies the system (stops and deletes a service file) but forgot to wrap it in a dry-run safety check. The bug slipped through because the installer has ~37 scattered `if dry_run:` guards across 71 methods in one 2,648-line class — easy for a human or agent to miss one. Dry-run mode promised to be read-only, but it wasn't.

## Goal
Make it structurally impossible to accidentally run destructive commands in dry-run mode. If an agent writes a new installation step and forgets the safety check, the code shouldn't compile or the type system should catch it — not rely on code review spotting every missing guard.

## Users & Context
Developers (human and AI agents) writing new installation steps, and operators running the wizard or install command. They need confidence that `--dry-run` will never modify the system, regardless of who wrote the code or what they forgot to check.

## Core Flow

1. The CLI calls a factory function with the config path and dry-run flag (`create_installer(config_file, dry_run)`).
2. The factory returns one of two concrete classes — `DryRunInstaller` if the flag is true, `RealInstaller` otherwise — both implementing the same protocol.
3. Both classes inherit from `BaseInstaller` (which holds the ~30 read-only methods: config parsing, GPU detection, validation, derived values).
4. The CLI calls methods on the returned installer without knowing which concrete class it got.
5. When a destructive operation is called (file write, service start/stop, package install), the two classes diverge:
   - `DryRunInstaller` prints `[DRY RUN] Would <action>` and returns immediately.
   - `RealInstaller` executes the actual system command.
6. Both classes print a banner at the very start announcing their mode (`=== DRY-RUN MODE: No changes will be made ===` or `=== REAL MODE: System will be modified ===`).
7. Dry-run mode prints `[DRY RUN] Would...` before every action; real mode prints no prefix (just the action result like "Removed legacy service file...").
8. When something goes wrong (config missing, invalid profile, disk full), both modes fail identically — same error message, same exit code.

## In Scope

- Split `SearchInstaller` into three classes: `BaseInstaller` (abstract, holds shared read-only methods), `DryRunInstaller` (concrete), `RealInstaller` (concrete).
- Define `InstallerProtocol` (runtime-checkable Protocol listing the public methods both concrete classes must implement).
- Factory function `create_installer(config_file: str | None, dry_run: bool) -> InstallerProtocol` that returns the appropriate concrete class.
- Mode banners printed at the start of `run()` in both classes.
- Dry-run action prefix (`[DRY RUN] Would...`) on every destructive operation.
- Update all call sites (CLI `wizard` and `install` commands, plus tests) to use the factory instead of direct construction.
- Migrate the existing 553 lines of dry-run tests to work with the new structure.

## Out of Scope

- Automated AST-based test that fails if a destructive call is unguarded (decided against in favor of structural enforcement via the type system).
- Changing the behavior of dry-run mode (it still does the same thing — prints what would happen and exits with the same codes — just enforced differently).
- Adding new installation steps (this refactor only restructures the existing 71 methods).
- Three-way split (dry-run / interactive / non-interactive) — this is strictly dry-run vs. real, and the interactive flag stays orthogonal.

## Key Decisions

- **Strategy pattern over scattered guards**: Two separate classes (each only knows how to do one thing) instead of one class with 37 `if dry_run:` checks, because the type system can enforce separation but can't enforce that every new method remembers to check a flag.
- **Inheritance for shared code**: `BaseInstaller` holds the ~30 read-only methods (config parsing, GPU detection, validation) so we don't duplicate 1,200 lines across both concrete classes.
- **Factory hides the choice**: CLI calls `create_installer(config_file, dry_run)` and gets back "an installer" without knowing which concrete class — matches the existing `get_search_service()` pattern (lines 62-74 of `platform/runtime.py`).
- **Both modes print banners**: Dry-run announces itself loudly to prevent confusion; real mode also prints a banner as a last safety check (catches a forgotten `--dry-run` flag before any destructive work happens).
- **Identical error behavior**: Both modes exit with the same code and message when something goes wrong (dry-run is a rehearsal — if it would fail, it should fail, not print "Would fail" and exit 0).

## Edge Cases & Constraints

- **Read-only methods stay in the base class**: Methods like `detect_gpu()`, `validate_providers()`, and config parsing don't touch the filesystem, so they live in `BaseInstaller` once and both concrete classes inherit them.
- **Destructive methods split into both classes**: File writes (`atomic_write_text`, `atomic_write_bytes`, `.mkdir()`, `.unlink()`), service lifecycle calls (`svc.start()`, `svc.stop()`, `svc.register()`), and package installs (`subprocess.run(["uv", "pip", "install", ...])`). `DryRunInstaller` stubs all of these; `RealInstaller` executes them.
- **Service lifecycle already uses this pattern**: The `SearchServiceLifecycle` abstract base class (with `start(dry_run)`, `stop(dry_run)`, `register(dry_run)` abstract methods implemented by platform-specific subclasses) proves the pattern already works in this codebase. The installer refactor mirrors that structure.
- **Tests mock the factory, not the constructor**: The existing tests that write `SearchInstaller(dry_run=True)` will change to `create_installer(config_file, dry_run=True)` — tests that need to assert which concrete class was returned can check `isinstance(installer, DryRunInstaller)`.
- **Migration is one-way**: After this lands, the old `SearchInstaller(dry_run=flag)` constructor goes away. No backwards compatibility — every call site updates.

## Open Questions

- Which of the 71 methods are destructive and need to split into both classes, vs. which are read-only and stay in the base? (A quick grep for file writes, service calls, and subprocess runs will enumerate them — estimate ~40 destructive, ~30 read-only.)
- Should `BaseInstaller` be an abstract base class (`ABC`) or just a plain parent class? (Likely `ABC` with abstract methods for the destructive operations, mirroring `SearchServiceLifecycle`.)
- Where does the protocol definition live — same file as the factory, or its own `installer_protocol.py`? (Same file is simpler for a single protocol; follow the existing pattern in `platform/service.py` which defines the ABC and factory in one file.)
- Do the concrete classes need separate files (`dry_run_installer.py`, `real_installer.py`), or can all three live in `install.py`? (Keeping them in `install.py` avoids churn on imports; split only if the file grows past ~3,500 lines.)

## Future Iterations

- Automated guard-checking test as a second layer (if agents keep introducing bugs in the base class's shared methods, add an AST-based test that verifies every call to a destructive helper is inside a class that handles dry-run correctly).
- Extract the banner printing into a decorator or helper so adding a new mode (like a "validate-only" mode that checks config without installing anything) doesn't require updating every method.
- Protocol-level enforcement via `@runtime_checkable` so the type checker can verify at call sites that only methods defined in the protocol are invoked.

## References

- [archon_search/install.py](archon_search/install.py) `[user+docs-agent+code-agent]` — main installer with 37 dry_run checks scattered throughout
- [tests/test_install_dry_run.py](tests/test_install_dry_run.py) `[user+code-agent]` — 553 lines of dry-run test coverage
- [Documentation/Completed/202607280906-S37-server_still_running_after_dry_run.md](Documentation/Completed/202607280906-S37-server_still_running_after_dry_run.md) `[docs-agent]` — bug report documenting wizard --dry-run modifying system state when it should be read-only
- [Documentation/Completed/202607282036-S37-server_still_running_after_dry_run.md](Documentation/Completed/202607282036-S37-server_still_running_after_dry_run.md) `[docs-agent]` — updated bug report for same dry-run issue
- [Documentation/UserManual/20_wizard.md](Documentation/UserManual/20_wizard.md) `[docs-agent]` — wizard guide documenting --dry-run guarantees
- [Documentation/UserManual/10_installation.md](Documentation/UserManual/10_installation.md) `[docs-agent]` — installation guide listing --dry-run flag
- [Documentation/Completed/C14-wizard-ux-improvements-plan.md](Documentation/Completed/C14-wizard-ux-improvements-plan.md) `[docs-agent]` — plan for fixing dry-run bug with exact gates needed
- [Documentation/Completed/C14-wizard-ux-improvements-brief.md](Documentation/Completed/C14-wizard-ux-improvements-brief.md) `[docs-agent]` — feature brief identifying dry-run bug
- [Documentation/Completed/C8-wizard-optional-features-plan.md](Documentation/Completed/C8-wizard-optional-features-plan.md) `[docs-agent]` — plan referencing dry_run in install helpers
- [Documentation/Architecture/200_testing_strategy.md](Documentation/Architecture/200_testing_strategy.md) `[docs-agent]` — testing strategy covering markers, coverage rules, parallel isolation
- [Documentation/Completed/C17-install-lock-parallel-isolation-plan.md](Documentation/Completed/C17-install-lock-parallel-isolation-plan.md) `[docs-agent]` — plan for install-lock fixes including dry-run handling
- [Documentation/Completed/C17-install-lock-parallel-isolation-brief.md](Documentation/Completed/C17-install-lock-parallel-isolation-brief.md) `[docs-agent]` — brief mentioning dry-run and .bak creation gating
- [CLAUDE.md](CLAUDE.md) `[docs-agent]` — project overview with testing strategy
- [learnings.md](learnings.md) `[docs-agent]` — project learnings documenting dry-run repro pattern
- `archon_search/cli/install_cmd.py` `[docs-agent+code-agent]` — CLI command definitions where --dry-run flag is registered
- `archon_search/platform/service.py` `[code-agent]` — SearchServiceLifecycle ABC showing existing abstraction pattern with dry_run support in abstract methods
- `archon_search/platform/macos.py` `[code-agent]` — LaunchdSearchService concrete implementation of SearchServiceLifecycle ABC
- `archon_search/embedder.py` `[code-agent]` — EmbedderBackend Protocol showing another abstraction pattern
- `archon_search/reranker.py` `[code-agent]` — RerankerBackend Protocol example
- `archon_search/query_expansion_protocol.py` `[code-agent]` — QueryExpansionProvider Protocol
- `archon_search/graph_store_protocol.py` `[code-agent]` — GraphStoreProtocol example
- `archon_search/graph_enrichment_protocol.py` `[code-agent]` — LLMEnrichmentClientProtocol example
- [tests/test_install_run.py](tests/test_install_run.py) `[code-agent]` — extensive install flow tests including dry_run scenarios
- [tests/test_install.py](tests/test_install.py) `[code-agent]` — general install tests
- [tests/test_install_force_delete.py](tests/test_install_force_delete.py) `[code-agent]` — force reinstall tests including dry_run
- `archon_search/platform/linux.py` `[code-agent]` — likely Linux systemd implementation of SearchServiceLifecycle
- `archon_search/platform/windows.py` `[code-agent]` — likely Windows service implementation of SearchServiceLifecycle

## Recommendation

This is the right feature to build now. The bug that triggered it (agent forgot a dry-run guard, dry-run deleted a system file) is exactly the class of mistake this pattern prevents — if an agent writes a new installation step in `RealInstaller` and forgets to add the corresponding stub in `DryRunInstaller`, the code won't pass type checking or tests (both classes must implement the protocol). The hardest part is the initial split (cleanly separating the 40 destructive methods from the 30 read-only ones), but the existing `SearchServiceLifecycle` abstraction proves the pattern already works in this codebase. What must not be compromised: identical error behavior in both modes (if dry-run exits clean, the real run must succeed — no silent divergence).
