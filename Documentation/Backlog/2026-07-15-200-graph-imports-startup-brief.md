# Feature Brief: Graph Imports Slow Every CLI Command

## Problem
Every CLI command — including instant ones like `config show` — takes an extra ~0.31 seconds to start because the graph subsystem's code is loaded unconditionally at startup, even on machines where graph features are disabled.

## Goal
CLI commands that don't use the graph subsystem start without loading it. The ~0.31s overhead disappears for `config show`, `status`, `collection list`, and every other non-graph command.

## Users & Context
Any user running any archon-search CLI command. The overhead is invisible but cumulative — every invocation pays it, even on systems where `graph.enabled = false`.

## Core Flow
This is a code-only change with no user-facing steps. The fix is applied once and all CLI commands become faster automatically.

1. Developer moves `from archon_search.cli.graph_cmd import graph_cmd` in `main.py` (line 7) to a lazy registration block — same pattern applied to all other subcommands in bug-004.
2. ~~Optionally: move the three module-level imports in `graph_cmd.py` (lines 18–20: `CommunityBuilder`, `GraphStore`, `SearchStore`) inside the `build_communities_cmd` function body.~~ **Already done by GBC110 (BE-8, 2026-07-16):** `graph_cmd.py` was converted to a pure HTTP proxy — the old in-process `GraphStore`/`SearchStore`/`CommunityBuilder` call path was removed entirely, eliminating those heavy imports. The "optional step 2" is no longer applicable.
3. Verify: `time archon-search config show` no longer includes the ~0.31s graph loading overhead.

## In Scope
- Lazy-loading the graph CLI subcommand import in `main.py`
- ~~Optionally moving the three heavy imports inside `graph_cmd.py` function bodies~~ — **already completed by GBC110 BE-8** (CLI converted to HTTP proxy, removed in-process imports entirely)

## Out of Scope
- Changes to graph feature behaviour
- The broader CLI startup latency fix (tracked in bug-004 — this is a bundle item)

## Key Decisions
- **Bundle with bug-004**: Same file (`archon_search/cli/main.py`), same fix pattern (lazy subcommand registration), same PR. No reason to ship separately.

## Status
- **Optional step 2 (moving `graph_cmd.py` heavy imports)** was completed by **GBC110 BE-8 (2026-07-16)**: `graph_cmd.py` became a pure HTTP proxy and the old in-process `CommunityBuilder`/`GraphStore`/`SearchStore` imports were removed entirely. This brief's "step 2" is no longer applicable.
- **Step 1 (lazy-loading `graph_cmd` in `main.py`)**: **Done (2026-07-19)**. Added `_LazyGraphGroup` (a `click.Group` subclass) to `main.py` that overrides `get_command` and `list_commands` to defer the `graph_cmd` import until the `graph` subcommand is actually invoked. The module-level `from archon_search.cli.graph_cmd import graph_cmd` import and `main.add_command(graph_cmd)` call were removed. A regression guard (`test_lightweight_cmd_no_graph_cmd_module` in `tests/test_cli_startup_latency.py`) asserts `archon_search.cli.graph_cmd` is absent from `sys.modules` for lightweight commands.

## Edge Cases & Constraints
- If `main.py` uses a `LazyGroup` or similar Click pattern from bug-004, the graph_cmd import slots in automatically — no extra work.
- If bug-004 uses a simpler "move import inside function" approach, apply the same pattern to `main.py`'s graph_cmd registration line.
- Graph commands themselves are unaffected in behaviour — only their load timing changes.

## Open Questions
- Does the bug-004 fix use Click's `LazyGroup`, inline imports, or another mechanism? The graph_cmd import should follow whatever pattern bug-004 establishes rather than introducing a second pattern.

## Future Iterations
- If other optional subsystems (e.g. MCP, OpenAI shim) gain CLI subcommands, apply the same lazy-load pattern at the time they are added.

## References
- [[archon_search/cli/main.py]] `[code-agent]` — line 7: unconditional `from archon_search.cli.graph_cmd import graph_cmd`
- [[archon_search/cli/graph_cmd.py]] `[code-agent]` — lines 18–20: module-level `CommunityBuilder`, `GraphStore`, `SearchStore` imports
- [[Documentation/Backlog/bug-004-cli-startup-latency-brief.md]] `[user]` — parent fix this should bundle into

## Recommendation
Two-line fix in `main.py` once bug-004's lazy-loading pattern is established. No architectural decision needed — just apply the same pattern one more time. The only risk is accidentally leaving the graph_cmd registration out of the lazy block; a quick `time archon-search config show` before and after confirms the fix landed.
