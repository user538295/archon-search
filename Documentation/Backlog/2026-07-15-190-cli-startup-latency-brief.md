# Feature Brief: CLI Startup Latency Fix

## Problem
Every `archon-search` command — including lightweight ones like `config show` and `status` — takes 1–2 seconds to start because the entire ML stack loads at startup, even when it is not needed.

## Goal
Lightweight commands (`config show`, `config get`, `status`, `stop`, `key list`) start in under 0.2 seconds. Heavy commands (`serve`, `collection add`, `ingest`) are unaffected — they still load the ML stack, but only when actually invoked.

## Users & Context
Any operator or developer who runs `archon-search` CLI commands regularly — checking config, querying status, or managing keys. Today every command has ~1.4 seconds of waiting before any output appears. On first install (before model files are cached), some commands wait for a GPT-2 tokenizer download from the internet.

## Core Flow
No user-visible flow change. The only observable difference:

1. User runs `archon-search config show` (or any lightweight command).
2. Output appears immediately — under 0.2 seconds.
3. Heavy commands (`serve`, `collection add`) are unchanged; they load the ML stack as before, on demand.

## In Scope
- Move `from archon_search.server.app import run_server` inside the `serve()` command function body (`cli/serve.py` line 25).
- Move `from archon_search.pipeline import create_pipeline` inside each command function body in `cli/collection.py`, `cli/ingest.py`.
- Move `from archon_search.install import SearchInstaller` (or equivalent heavy import) inside command bodies in `cli/install_cmd.py`.
- Move `from claude_agent_sdk import ...` inside the function that uses it in `description_generator.py` (currently line 9 — fires on every CLI command that touches the pipeline).
- All other imports in those files stay at module level — only the imports that trigger fastembed, onnxruntime, or the Claude SDK move.

## Out of Scope
- Making `serve` or `collection add` faster — those commands need the ML stack; that is a separate concern.
- Lazy-initialising chunkers inside `SearchPipeline` — valid long-term refactor but wider blast radius; deferred.
- A separate lightweight entry point — adds maintenance burden with no additional gain once imports are lazy.
- Changing any CLI output, flags, or behavior.

## Key Decisions
- **Lazy imports over a lazy-loading Click group:** Moving imports inside functions is a 1–2 line change per file, fully reversible, and directly targets the two measured hot paths. A Click lazy-group wrapper would achieve the same result with more indirection.
- **`description_generator.py` fixed at the source:** The Claude SDK import fires on every pipeline import, not just on serve. Fixing it at the source eliminates the cost for all callers, not just the CLI.

## Edge Cases & Constraints
- **Import-time side effects:** None of the moved imports have documented module-level side effects. Each initialises resources only when its public API is called, not on import.
- **Type checkers:** Moving imports inside functions means static type checkers will not resolve those symbols at the call sites without a `TYPE_CHECKING` guard. Add `if TYPE_CHECKING:` blocks where needed to keep mypy/pyright clean.
- **Test isolation:** Tests that monkeypatch `archon_search.server.app` or `archon_search.pipeline` at the module level in the CLI files may need to move their patch target. Grep `patch("archon_search.cli.serve.*")` etc. before committing.

## Open Questions
- Does `cli/install_cmd.py` import `SearchInstaller` at module level or only inside functions? Confirm before scoping the exact change — the investigation found it imports `SearchInstaller` at line 11, but the heavy cost is from `pipeline`, which `install_cmd` may or may not pull in transitively.
- Are there other CLI modules (e.g. `sync.py`, `graph_cmd.py`) that eagerly import `pipeline` or `server.app`? A `grep -rn "^from archon_search.pipeline\|^from archon_search.server.app" archon_search/cli/` should confirm the full list before implementation.
- Does moving `claude_agent_sdk` import inside `description_generator.py`'s function body break any existing test that patches it at module level?

## Future Iterations
- Lazy chunker initialisation inside `SearchPipeline` — eliminates ~1s of GPT-2 tokenizer loading for `collection list` and other store-only commands (companion fix tracked in bug-003).
- Baseline import-time CI assertion (e.g. `python -c "import time; t=time.time(); import archon_search.cli.main; assert time.time()-t < 0.3"`) to prevent regression.

## References
- [[archon_search/cli/main.py]] `[code-agent]` — all 14 subcommand imports at lines 5–18; root cause
- [[archon_search/cli/serve.py]] `[code-agent]` — `from archon_search.server.app import run_server` at line 25
- [[archon_search/description_generator.py]] `[code-agent]` — `from claude_agent_sdk import ...` at line 9; fires on every pipeline import
- [[archon_search/cli/collection.py]] `[code-agent]` — `from archon_search.pipeline import create_pipeline` at line 18
- [[archon_search/cli/ingest.py]] `[code-agent]` — `from archon_search.pipeline import create_pipeline` at line 16
- **Team plan:** [2026-07-15-190-cli-startup-latency-team-plan.md](./2026-07-15-190-cli-startup-latency-team-plan.md)

## Recommendation
Build this. It is a 5-file, ~10-line change with a direct, measured impact: lightweight commands go from 1.4 seconds to under 0.2 seconds. The risk is low — moving an import inside a function changes nothing about how the code runs, only when the module is first loaded. The hardest part is not the code change; it is auditing every test that patches these modules and ensuring type-checker annotations stay intact. Do both in the same PR.
