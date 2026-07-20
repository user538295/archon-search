# Feature Brief: CoreML Split Providers — Embedder on GPU, Reranker on CPU

## Problem

On a MacBook with Apple Silicon, the setup wizard tries to enable hardware acceleration (CoreML) for both models it uses — one that converts text to searchable numbers (the embedder) and one that ranks results by relevance (the reranker). When either model fails under CoreML, the wizard gives up on hardware acceleration entirely. The reranker model currently bundled with the "Max" and "Multilingual" profiles fails at inference time under CoreML, so MacBook users end up with CPU-only performance for everything — including the embedder, which works fine on CoreML.

## Goal

When the embedder works under CoreML but the reranker doesn't, the wizard automatically configures the best available split: CoreML for the embedder, CPU for the reranker. The wizard clearly communicates this in the install summary. The user gets GPU-accelerated text search, and ranking falls back gracefully to CPU — which is correct and correct-by-design, not a failure.

## Users & Context

Mac users on Apple Silicon who run `archon-search wizard` to set up or reconfigure the server. They're doing a one-time setup and expect the wizard to choose the best hardware configuration automatically. They should not need to understand ONNX Runtime execution providers to get good performance.

## Core Flow

1. The wizard detects Apple Silicon and offers to enable CoreML acceleration (existing behaviour).
2. The user confirms (existing behaviour).
3. The wizard probes **both** models under CoreML (existing behaviour).
4. **New:** If the combined probe fails, the wizard probes the embedder alone under CoreML.
5. **New:** If the embedder passes but the reranker doesn't, the wizard writes a split configuration — CoreML for the embedder, CPU for the reranker — instead of falling back to full CPU.
6. **New:** The install summary shows "CoreML — text search; CPU — result ranking" so the user understands what was configured.
7. The TOML config gains a `reranker_providers` entry (empty list = CPU) alongside the existing `providers` entry (CoreML).
8. At runtime, the server loads the embedder with CoreML and the reranker with CPU — no further user action needed.

## In Scope

- Wizard: detect the split case and write the split config automatically.
- Config: add `reranker_providers` as a new optional `[database]` field; when absent, it falls back to `providers` (backward-compatible).
- Pipeline and server startup: read `reranker_providers` and pass it to the reranker; embedder continues to use `providers`.
- Install summary: display string updated to reflect partial CoreML use.
- TOML example file and configuration docs: document `reranker_providers`.
- Wizard re-runs: a second `archon-search wizard` call upgrades an existing full-CPU config to the split config if the split case is now detectable.
- Post-download re-validation (FE-1 step in the wizard): skip the combined CoreML re-probe when already in split mode — the result is already known.

## Out of Scope

- Adding `embedding_providers` as a separate field to mirror `reranker_providers` — the existing `providers` field already fulfils that role; introducing `embedding_providers` now would be a breaking change for every existing TOML config. Deferred to a future config version.
- Asking the user to choose between the split and full-CPU — the split is strictly better; no prompt needed.
- CUDA path — CoreML/Metal only; CUDA validation is already out of scope in D6.
- Automatically fixing existing TOML configs that have `providers = ["CoreMLExecutionProvider"]` without a `reranker_providers` entry — those will encounter the CoreML reranker failure at runtime. A wizard re-run will apply the fix; a startup warning is a future improvement.

## Key Decisions

- **`reranker_providers` not `embedding_providers`**: Adding only `reranker_providers` is backward-compatible — existing configs need no migration. Adding `embedding_providers` as an alias for `providers` would break every config that sets `providers`. The asymmetry (`providers` = embedder default, `reranker_providers` = reranker override) is a minor papercut compared to a breaking migration.
- **Silent split, one summary line**: No confirmation prompt. The split is always better than full-CPU fallback. The wizard communicates what it configured in the summary screen, matching the existing pattern for other partial states.
- **`None` sentinel means "inherit from `providers`"**: A `reranker_providers` absent from the TOML (value `None` in code) falls back to `providers`. An explicit empty list (`[]`) means "use CPU". This keeps old configs working without code changes.
- **Consolidate provider injection before adding the new field**: `app.py` (lines 587, 620) and `pipeline.py` (lines 3481/3486) both construct the embedder and reranker with provider settings. This PR consolidates both into a single construction path via `pipeline.py` first, then adds `reranker_providers` once. This prevents the new field from being silently ignored on one path, and ensures every future provider setting is a one-place change.

## Edge Cases & Constraints

- **Both models fail CoreML**: existing behaviour — fall back to full CPU, no split written.
- **Both models pass CoreML**: existing behaviour — CoreML for both, no `reranker_providers` written.
- **Reranker disabled** (`reranker_model = ""`): existing behaviour — `reranker_ok` is always True; split detection doesn't run.
- **User re-runs the wizard on an existing CPU install**: the wizard probes CoreML fresh and writes the split config if appropriate — the user gets the upgrade automatically.
- **Operator manually sets `reranker_providers`** in the TOML: respected at runtime. The wizard overwrites it on next run if it runs GPU detection.
- **`validate_providers_shared` reranker-skip path**: passing `reranker_model = ""` to this function already skips the reranker probe cleanly — the embedder-only probe in step 4 above uses this mechanism without any changes to the shared validation function.

## Key Decisions (continued)

- **Log a one-time WARNING at startup when `providers = ["CoreMLExecutionProvider"]` but `reranker_providers` is absent**: place the check in `model_validation.py` alongside the existing provider probe logic. Fires only in the narrow stale-config edge case; the only signal a pre-fix user gets before hitting a silent CoreML inference failure at runtime.
- **Write a new superseding ADR and append a "Superseded by" note to D6**: the append-only ADR rule is respected (D6 is not edited), and the new ADR records the non-breaking per-model provider approach. `BREAKING.md` still gets the `reranker_providers` schema note. Leaving D6 as "deferred" would actively mislead future readers.

## Future Iterations

- **`embedding_providers` field**: rename/alias `providers` to `embedding_providers` in a future config version (with a migration shim) for full naming symmetry with `reranker_providers`.
- **Startup warning for stale configs**: detect `providers = [CoreML]` without `reranker_providers` at startup and log a one-time advisory to re-run the wizard.
- **CUDA split**: if the reranker ever fails under CUDA on specific models, the same split mechanism would apply. Not in scope today — CUDA validation is already deferred in D6.

## References

- `archon_search/config.py` `[code-agent]` — `providers` field line 208, `_apply_toml` parse line 419
- `archon_search/model_validation.py` `[code-agent]` — `validate_providers_shared()` line 64, reranker-skip via `reranker_model=""`
- `archon_search/install.py` `[code-agent]` — `validate_providers()` line 1482, `configure_providers()` line 1516, Metal block line 1910, FE-1 post-download re-validation line 2042
- `archon_search/pipeline.py` `[code-agent]` — embedder/reranker both get `cfg.providers` at lines 3481/3486
- `archon_search/reranker.py` `[code-agent]` — `ModelReranker`, lazy init with providers
- `archon_search/server/app.py` `[code-agent]` — second provider injection point at lines 587/620
- `tests/test_config_defaults.py` `[code-agent]` — snapshot test, `providers: []` line 102; will need `reranker_providers: None` added
- `tests/path_home_allowlist.txt` `[code-agent]` — hash-pinned allowlist; check if line numbers shift after config.py edit
- `Documentation/Completed/D6-provider-validation-brief.md` `[code-agent+docs-agent]` — explicitly defers per-model provider split; names `embedding_providers`/`reranker_providers` in Future Iterations
- `Documentation/Completed/D6-provider-validation-team-plan.md` `[code-agent]` — same deferral, future iterations line 78
- `Documentation/ADRs/03_cross_encoder_reranker_second_stage.md` `[docs-agent]` — reranker ADR; documents shared providers assumption this feature changes
- `Documentation/UserManual/02_wizard.md` `[docs-agent]` — wizard GPU flow; needs split-case messaging added
- `Documentation/UserManual/02_configuration.md` `[docs-agent]` — `[database].providers` reference; needs `reranker_providers` documented
- `archon-search.toml.example` `[docs-agent]` — shows single `providers` key; needs `reranker_providers` example added

## Recommendation

This is the right fix to ship now. The problem is real and repeatable — every MacBook user who picks the Max or Multilingual profile loses all GPU acceleration because of a single model's CoreML incompatibility. The fix is narrow, backward-compatible, and requires no user-visible decision. The hardest part is the two-location provider injection (`pipeline.py` and `app.py`) — consolidating those before adding the new field will make the change cleaner and prevent the new field from being silently ignored on one path. Don't skip the test snapshot and path-home allowlist updates; missing either will break CI immediately.
