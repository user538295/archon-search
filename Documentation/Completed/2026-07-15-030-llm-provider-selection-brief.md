# Feature Brief: LLM Provider Selection — Claude CLI + Wizard Surfacing

## Problem

AI-powered search improvements (query expansion via HyDE and RAG Fusion) already support three AI backends — Anthropic, OpenAI, and Ollama — but two of those options are invisible during setup and the fourth doesn't exist yet. An operator today has to discover, hand-edit a config file, and know to set a provider-specific environment variable with zero guidance. The result: practically everyone runs on `ANTHROPIC_API_KEY` or skips the feature entirely, even though cheaper and key-free alternatives are already wired in.

The specific missing piece: the `claude -p` path — using Claude Code's existing login to run Claude without a separate API key — has no provider implementation at all.

## Goal

Any operator setting up archon-search can pick their preferred AI provider during the guided setup wizard, get told exactly what they need to do to activate it, and see the feature work on first try. Developers already running Claude Code can enable HyDE and RAG Fusion with zero new accounts or API keys by choosing the `claude_cli` option.

## Users & Context

Developers and operators installing or reconfiguring archon-search. The typical moment: just enabled HyDE or RAG Fusion in the wizard and want it to actually work. Today the wizard selects the provider silently and offers no guidance on what to set up next. Operators who don't already know the config file format are stuck.

A second group: developers already using Claude Code who want query expansion without paying for a separate Anthropic API key — they have `claude` in their PATH and are already authenticated.

## Core Flow

1. Operator runs the archon-search setup wizard.
2. Wizard asks: "Enable AI query expansion?" (HyDE, RAG Fusion, or both).
3. If yes: wizard asks "Which AI provider?" and shows all four options with a one-line description of each — Anthropic (API key required), OpenAI (API key required), Ollama (runs locally, no key), Claude CLI (uses Claude Code's login, no key).
4. Depending on the choice:
   - **Anthropic**: wizard tells them to set `ANTHROPIC_API_KEY` in their environment and shows where.
   - **OpenAI**: wizard tells them to set `OPENAI_API_KEY` and asks for the model name (e.g. `gpt-4o-mini`).
   - **Ollama**: wizard confirms the Ollama URL (defaults to `http://localhost:11434`) and verifies it's reachable.
   - **Claude CLI**: wizard checks that the `claude` command is installed and logged in; warns clearly if it isn't found. Then shows a curated list of current Claude model aliases (`haiku`, `sonnet`, `opus`, `fable`) as quick-pick options, with a free-text fallback for full model IDs (e.g. `claude-haiku-4-5-20251001`). Unlike Ollama, the Claude CLI has no `models` subcommand to query at runtime — the list is maintained in the wizard code and updated with each release.
5. Wizard writes `provider = "<choice>"` to the TOML config and shows a one-line confirmation.
6. Operator starts the server — query expansion works immediately with their chosen provider.

## In Scope

- New `claude_cli` provider: calls `claude -p "<prompt>"` as a subprocess; no API key; availability checked by whether `claude` is in the system PATH.
- Wizard update: show all four providers during HyDE/RAG Fusion setup; guide the operator to the right env var or tool check; warn on missing prerequisites.
- Wizard checks `claude` CLI is present and responds when `claude_cli` is chosen — surfaces a clear warning (not a crash) if it isn't.
- `claude_cli` skips rate-limiting (same as Ollama — local/free path, no API cap to respect).
- Both HyDE and RAG Fusion can independently use any of the four providers.

## Out of Scope

- Adding any provider beyond the four listed — this is the complete set for now.
- Changing how HyDE or RAG Fusion work functionally — only the provider swap changes.
- Storing API keys in the config file or anywhere except environment variables — keys stay in the operator's shell environment by design.
- A single "unified API key" across providers — Anthropic and OpenAI keys are issued by different companies and cannot be shared.
- An `ARCHON_LLM_PROVIDER` environment variable — the TOML `provider =` field already handles active-provider selection cleanly; a second mechanism would create conflicts.
- Wizard changes for anything other than the provider-selection and prerequisite-guidance flow.

## Key Decisions

- **Native per-provider env vars, not a unified key**: `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are the industry-standard names every tool, tutorial, and deployment guide already uses. Operators likely have them set already. A unified key is technically impossible — one string cannot authenticate with two different services.
- **TOML `provider =` field stays as the active-provider selector**: it already works, is per-feature (HyDE and RAG Fusion can use different providers), and lives in the config file alongside the rest of the setup. No new mechanism needed.
- **`claude_cli` checks PATH, not an env var**: the `claude` command either exists and is logged in or it isn't. The check is `shutil.which("claude")` — if it returns nothing, the provider is unavailable, and the wizard warns the operator to install Claude Code first.
- **`claude_cli` skips rate limiting**: subprocess calls are inherently slower than API calls; the natural throughput ceiling is lower than any rate-limit bucket would set. Treating it like Ollama (no rate limit) is correct.
- **`claude_cli` model list is curated, not queried**: the Claude CLI has no `models` subcommand. The wizard shows a hardcoded list of current model aliases (`haiku`, `sonnet`, `opus`, `fable`) with a free-text fallback — same UX as Ollama's dynamic list, different source. This list lives in the wizard code and must be updated when Anthropic releases new models.
- **Wizard guides, doesn't block**: if a prerequisite is missing (no API key set, `claude` not found), the wizard warns clearly but still writes the config. The server will surface the error on first use rather than refusing to start — same behaviour as today for missing `ANTHROPIC_API_KEY`.

## Edge Cases & Constraints

- **`claude` not in PATH at wizard time**: wizard shows a clear warning with install instructions (`claude.ai/code`) and writes the config anyway. Operator can install later before starting the server.
- **`claude -p` subprocess timeout**: the same `timeout_seconds` config field used by other providers applies. If the subprocess hangs, it is killed and the query expansion falls back silently (same fallback contract as Anthropic/OpenAI timeouts).
- **`claude -p` model selection**: reuses the existing `[hyde] model` / `[rag_fusion] model` TOML field. The provider passes it as `--model <value>` to the subprocess; when the value is the default or blank, the flag is omitted and Claude Code uses its own configured default. The wizard presents a curated alias list (`haiku`, `sonnet`, `opus`, `fable`) since `claude` has no runtime model-listing command — unlike `ollama list`, this list is hardcoded in the wizard and must be kept current with Claude releases.
- **OpenAI wizard**: model name is required (no sensible default applies across all OpenAI tiers). Wizard must ask for it; empty model must be rejected.
- **Existing operators upgrading**: operators who already have `provider = "anthropic"` in their TOML are unaffected — default unchanged, no migration needed.
- **Both HyDE and RAG Fusion enabled with different providers**: fully supported by the existing config structure (`[hyde] provider` and `[rag_fusion] provider` are independent fields). Wizard should ask per-feature if both are enabled.
- **`claude -p` output format**: the subprocess may include status lines or ANSI codes depending on Claude Code version. The provider implementation must strip non-text output before returning the generated text.

## Resolved Decisions (was Open Questions)

Resolved 2026-07-16. Verified against the installed Claude Code CLI and the current source; rationale recorded per decision.

- **Subprocess invocation — RESOLVED: `claude -p "<prompt>" --output-format text --model <model>`.** The prompt is passed as an argument via a subprocess list (no shell, so no escaping/injection concern). `-p/--print`, `--output-format`, and `--model` are all present in the installed CLI (`claude --help`). Choosing `--output-format text` yields clean output and largely dissolves the ANSI/status-line stripping worry in Edge Cases below — that mode is documented plain text. The spike's only job is to re-confirm these flags against the Claude Code version shipped against and hard-code the confirmed form; `-p` alone is the fallback if `--output-format` is ever renamed. Rejected: bare `claude -p` (forces manual output stripping); stdin pipe (solves a prompt-length ceiling the short HyDE/RAG-Fusion prompts never hit).
- **Model selection — RESOLVED (as previously proposed):** reuse the existing `[hyde] model` / `[rag_fusion] model` TOML field; pass it as `--model <value>`; omit the flag entirely when the value is blank or equals `DEFAULT_FAST_MODEL` (`"claude-haiku-4-5-20251001"`, `constants.py:28`), letting Claude Code use its own configured default. No new config field.
- **Availability check — RESOLVED: `shutil.which("claude")` only.** No check the wizard can run proves the CLI is logged in (`--version` reports fine when logged out; a real `claude -p` probe is slow, can hang, and may cost tokens). PATH presence matches the "wizard guides, doesn't block" decision — a logged-out CLI surfaces its error on first search, exactly as a missing `ANTHROPIC_API_KEY` does today. Revisit only if operators report confusion.
- **Wizard UX when both features enabled — RESOLVED: ask provider once, apply to both.** The config fields (`hyde_provider` / `rag_fusion_provider`, `install.py:168–172`) stay independent, so an operator wanting different providers per feature can still hand-edit the TOML. A per-feature prompt would double the questions for a rare case; add it only if that case proves common.
- **`_VALID_PROVIDERS` — RESOLVED: add `"claude_cli"` (safe).** Single-source check at `config.py:25` (rejects unknown names at `config.py:630`/`672`). Verified: no test hard-codes the current three-provider set as an assertion, so adding a fourth breaks no snapshot.

## Future Iterations

- Per-request provider override via the search API (`"provider": "openai"` in the request body) — today the provider is server-wide config only.
- Wizard health-check for OpenAI and Anthropic: attempt a minimal API call during setup to confirm the key is valid, not just present.
- A `claude_cli` provider that calls a locally running Anthropic-compatible server (LM Studio, llama.cpp) — currently out of scope since the OpenAI-compatible path via `ollama` already covers most local model use cases.

## References

- [[Documentation/Completed/C4-hyde-query-expansion-brief.md]] `[docs-agent]` — original HyDE feature brief; Anthropic-only design rationale
- [[Documentation/ADRs/C4-hyde-external-llm-dependency.md]] `[docs-agent]` — ADR explaining why local models were deferred in v1 and privacy trade-offs
- [[Documentation/ADRs/C5-rag-fusion-external-llm-dependency.md]] `[docs-agent]` — ADR for RAG Fusion's external LLM dependency and HyDE mutual exclusion
- [[Documentation/Completed/g10-llm-provider-matrix-brief.md]] `[docs-agent]` — G10 brief that introduced OpenAI and Ollama providers
- [[Documentation/Completed/g10-llm-provider-matrix-team-plan.md]] `[docs-agent]` — G10 team plan; full implementation detail for the provider protocol and factory wiring
- [[archon_search/hyde.py]] `[code-agent]` — HyDEGenerator; provider injection pattern and rate-limiting logic
- [[archon_search/rag_fusion.py]] `[code-agent]` — RAGFusionGenerator; same provider injection pattern
- [[archon_search/config.py]] `[code-agent]` — HyDEConfig and RAGFusionConfig; `provider` and `ollama_base_url` fields
- [[archon_search/query_expansion_protocol.py]] `[code-agent]` — QueryExpansionProvider protocol; `provider_key_available()` function
- [[archon_search/providers/anthropic_provider.py]] `[code-agent]` — reference implementation; lazy import and env-var key check pattern to follow
- [[archon_search/providers/openai_provider.py]] `[code-agent]` — OpenAI provider; response normalisation pattern
- [[archon_search/providers/ollama_provider.py]] `[code-agent]` — Ollama provider; no-key and no-rate-limit pattern that `claude_cli` should mirror
- [[archon_search/server/app.py]] `[code-agent]` — `_check_provider_deps()` and `_build_query_expansion_provider()` factory; where `claude_cli` wiring goes
- [[archon_search/install.py]] `[code-agent]` — wizard source; current provider-selection prompts and TOML write logic

## Recommendation

Build this. The `claude_cli` provider is the smallest possible unlock: one new file mirroring the Ollama pattern, plus subprocess plumbing. The wizard update is equally small but has outsized impact — it turns three already-working but invisible options into a guided first-run experience. The hardest part is the `claude -p` subprocess output normalisation (stripping status lines and ANSI codes) and confirming the exact CLI flag across Claude Code versions. Neither is a blocker, but both need a spike before writing the tests. Do not skip the wizard update — shipping the provider without surfacing it in setup repeats the G10 mistake of building capability that no operator can discover.
