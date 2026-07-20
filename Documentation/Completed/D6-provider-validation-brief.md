# Feature Brief: D6 — Install-time / Background Provider Validation

> **Status update (2026-07-20):** The per-model provider configuration feature described in "Future Iterations" below has been implemented. See [ADR-10](../ADRs/10_coreml_split_providers.md) for the accepted design.

## Problem

When a user installs archon-search with a non-CPU provider (CoreML, CUDA) or a custom `reranker_model`, the first real search query triggers the ONNX session initialization — silently, 5–15 seconds into the user's request, with no prior warning that the configured model or provider is invalid. The reranker has no validation path at all; the embedder's provider check only runs during Metal GPU detection at install time and never again. Misconfigured providers cause search failures at query time rather than at install or startup.

## Goal

Provider and model configuration is validated before it fails a user query. A misconfigured reranker model or unavailable ONNX provider surfaces at install time (wizard), at server startup (background, non-blocking), and is visible in `/ready` and `/status` — while the server still boots in under 2 seconds regardless of model availability.

## Users & Context

Operators installing archon-search with GPU acceleration (CoreML on Apple Silicon, CUDA on Linux), or operators changing `reranker_model` or `providers` in `archon-search.toml` after the initial install. They discover the misconfiguration either via the install wizard output, a failed first search, or by polling `/status`. The worst case today is a silent failure on the first production query with no actionable error message.

## Core Flow

1. **Install wizard** — After model file pre-warm (`_prewarm_models`), the wizard runs `validate_providers()` against the reranker model in addition to the embedder. If the reranker provider is unavailable (e.g., CoreML not in `onnxruntime.get_available_providers()`), the wizard logs an actionable warning and falls back to CPU for the reranker — consistent with the existing embedder fallback behavior. Install does not fail hard.
2. **Server startup** — The FastAPI lifespan handler spawns a background task after the existing eager-preload step. The task calls a new `validate_models_async()` function that instantiates both the embedder (if not already warmed by eager-preload) and the reranker with a timeout guard. Errors are logged at WARNING level and stored in `app.state.validation_result`. Startup is never blocked — the background task runs concurrently with the server coming online.
3. **Validation result surfaces in `/status`** — `GET /status` returns a new `model_validation` sub-object: `{embedder_ok: bool, reranker_ok: bool, provider_warnings: list[str], validated_at: str|null}`. While the background task is running, `validated_at` is null and both flags are null (not false — unknown is distinct from failed).
4. **`/ready` gains optional model readiness check** — A new `models` check is added to `ReadinessChecks`. It is `OK` once validation completes with no errors, `WARN` if validation completed with warnings (fallback providers used), `FAIL` if a model could not be loaded at all, and `UNKNOWN` while validation is still in progress. The overall `ready` flag is not affected by model validation status — a failed model check does not return 503, because the server can still serve cached results and handle management API calls.
5. **Operator inspects via CLI** — `archon-search status` CLI command already calls `GET /status`; the new `model_validation` object renders as a human-readable block in the existing status output.

## In Scope

- Extend `validate_providers()` in `install.py` to test reranker instantiation (currently only tests the embedder). The function already imports `onnxruntime.get_available_providers()` and instantiates `TextEmbedding` with `embed(["archon search test"])`; extend it to also instantiate `TextCrossEncoder` with `rerank()` on the same test string, gated on `profile.reranker is not None`.
- New `validate_models_async(config: SearchConfig, timeout: float) -> ModelValidationResult` function in `archon_search/model_validation.py`. Returns a typed dataclass (or Pydantic model) with `embedder_ok`, `reranker_ok`, `provider_warnings: list[str]`, `validated_at: datetime`. Never raises — all exceptions are caught and mapped to `ok=False` + a warning string.
- `ModelValidationResult` stored on `app.state.model_validation` (`None` until the background task completes).
- Background validation task registered in `app.py` lifespan handler alongside the existing `eager_load_embedders` preload step, using `asyncio.create_task()` and tracked in `app.state._background_tasks`.
- `GET /status` extended with `model_validation` sub-object mirroring `ModelValidationResult`. While task is pending, all fields are `null`.
- `GET /ready` extended with a `models: CheckStatus` field in `ReadinessChecks` and `ReadinessResponse`. `PENDING` while validation is in progress, `OK` when both models pass, `WARN` when provider fallback occurred, `FAIL` when a model could not be loaded. The `ready: bool` field remains storage-only — model validation does not gate the 200/503 response code.
- Timeout for background validation: read `config.validation_timeout_seconds` (new `[database]` TOML key, default 60 s). The timeout guard pattern is already established in `model_validation.py` via `asyncio.wait_for`.
- `schemas.py` additions: `ModelValidationStatus` Pydantic model (for API response), `CheckStatus.PENDING` enum value (additive change; `BREAKING.md` entry required), updated `ReadinessChecks` and `StatusResponse` to include the new fields.
- Test coverage: unit tests for `validate_models_async()` covering (a) both models OK, (b) embedder fails, (c) reranker fails, (d) provider unavailable, (e) timeout. Integration test verifying the background task completes and `GET /status` reflects the result.
- Update `archon-search.toml.example` with a comment explaining that `providers` affects both embedder and reranker.
- Documentation updated: CLAUDE.md (architecture section), API reference (`routes_ready.py`, `routes_status.py`).

## Out of Scope

- Per-model provider configuration (separate `embedding_providers` vs `reranker_providers` config keys) — a single `providers` list is the current contract; splitting it is a config-breaking change deferred to a future ADR.
- Blocking server startup on model validation failure — the lazy-load contract must be preserved. The server always boots; validation is advisory.
- Retrying failed validation automatically after startup — operators fix config and restart. Auto-retry introduces complexity and can mask misconfiguration.
- Validation on config reload without restart — archon-search does not support live config reloads; this is out of scope.
- Reporting validation results via MCP tools — REST and CLI coverage is sufficient for v1; MCP addition is deferred.
- Provider validation for per-collection embedder models (the `EmbedderCache` pool) — validating each collection's model at startup scales poorly with collection count; the existing `eager_load_embedders` path already handles this with warning-on-failure semantics, which is sufficient.

## Key Decisions

- **Background task, not blocking startup**: the lazy-load contract is a hard requirement; validation fires asynchronously after the lifespan handler completes, not before the server is ready to accept requests. This is the same pattern used by the backup loop and watcher.
- **Warning-on-failure, not fatal**: a misconfigured reranker should not prevent ingest, collection management, or query operations that don't need the reranker. Degraded-but-running is always better than a refused startup in a server-side tool.
- **`PENDING` as a distinct check status**: distinguishing "validation not yet complete" from "validation failed" prevents false negatives in monitoring. A readiness probe polled immediately after startup should not report `FAIL` for models that simply haven't been checked yet.
- **Extend `validate_providers()` rather than duplicate it**: the function already handles embedder validation and ONNX provider availability; adding reranker support is 10 lines, not a new function. Reuse prevents divergence.
- **Store result on `app.state`**: consistent with how `app.state.backup_loop`, `app.state.job_store`, and `app.state.search_store` are surfaced. Route handlers read `app.state.model_validation` directly — no extra indirection needed.
- **`ready: bool` is not gated on model validation**: the `/ready` probe is used by load balancers and container orchestrators. Returning 503 because a GPU provider isn't available would take the service out of rotation unnecessarily — the service can still function on CPU fallback.

## Edge Cases & Constraints

- **Reranker disabled (`reranker_model = ""`)**: `validate_models_async()` sets `reranker_ok = true` and skips reranker validation. No warning emitted.
- **`eager_load_embedders = true`**: the embedder may already be warm before the background validation task runs. `validate_models_async()` should check `app.state.embedder.is_warm` and skip the embedder probe if already loaded (use the existing warm state as proof of validity).
- **Validation timeout fires**: both `embedder_ok` and `reranker_ok` are set to `False` if the respective model times out. `provider_warnings` includes `"validation timed out after {N}s"`. The background task does not retry.
- **ONNX provider list unavailable** (onnxruntime not installed or import error): `validate_providers()` already handles this with a try/except — it logs a warning and returns False. This behavior is preserved and the same fallback applies to the reranker path.
- **Server restart mid-validation**: the background task is cancelled as part of lifespan shutdown (standard asyncio task cancellation via `app.state._background_tasks`). On restart, validation runs fresh.
- **`/status` called before validation completes**: `model_validation` object is returned with all fields `null` and `validated_at: null`. Clients must handle null fields.
- **`GET /ready` called before validation completes**: `models` check returns `PENDING`. This must not cause monitoring systems to flag the service as unhealthy — operators deploying this version need to update their monitoring to treat `PENDING` as equivalent to healthy for the models check.
- **`CheckStatus.PENDING` addition**: if `CheckStatus` is an `Enum` used in Pydantic serialization, adding `PENDING` is an additive change. Verify no test asserts an exhaustive match on `CheckStatus` values. Requires a `BREAKING.md` entry.
- **Thread safety of `app.state.model_validation`**: the background task writes `app.state.model_validation` once (after completion). Route handlers read it. Python's GIL makes a single assignment to an attribute atomic for reads; no lock is required. This is the same pattern used by the existing `is_warm` properties.

## Open Questions

_All resolved._

- **Timeout source** → **Dedicated config key**: add `validation_timeout_seconds` to the `[database]` TOML section (default 60 s). Reusing `_prewarm_timeout` would create implicit coupling between install and runtime behavior; a hardcoded value gives no escape hatch for large custom models.
- **Shared validation code** → **Merge into `model_validation.py`**: refactor `validate_providers()` out of the install wizard class into a module-level function in `model_validation.py` called by both the wizard and the startup background task. Each caller handles its own error surfacing (terminal vs. `app.state`).
- **`/ready` on confirmed model failure** → **Always 200**: model validation never gates the `/ready` response. A broken reranker does not justify taking the server fully out of rotation — ingest, management APIs, and CPU-fallback search still work. Operators inspect `/status` for model health. An opt-in gate (`require_models_ready`) remains a future iteration.
- **"Not yet checked" label** → **`PENDING`**: use `CheckStatus.PENDING` (not `UNKNOWN`). `PENDING` unambiguously means "check in progress"; `UNKNOWN` is ambiguous between "not applicable" and "no data yet." Requires a `BREAKING.md` entry as an additive enum change to the public API.

## Future Iterations

- Per-model provider configuration: `embedding_providers` and `reranker_providers` as separate lists in `[database]`, allowing CoreML for embedder and CPU for reranker on a system where GPU reranker is unstable.
- Periodic re-validation: re-run `validate_models_async()` on a configurable interval (e.g., every 24 hours) to catch provider drift (e.g., CUDA driver update broke the ONNX session). Requires storing validation history, not just the last result.
- MCP tool: `get_model_status()` exposing the same `ModelValidationResult` over the MCP surface.
- Validation result in `GET /ready` as a soft gate: a config flag (`require_models_ready: bool = false`) that promotes the `models` check from advisory to gate-level, returning 503 when validation fails, for operators who need strict model availability guarantees.
- Auto-fallback recording: if provider validation falls back to CPU, persist the fallback decision to `archon-search.toml` automatically (or prompt the operator) so subsequent restarts don't re-discover the same incompatibility.

## Recommendation

Build this now. The core gap is narrow and well-defined: `validate_providers()` ignores the reranker today, and there is no post-install validation path. The fix to `validate_providers()` is 10–15 lines; `validate_models_async()` is a thin wrapper over patterns already in `model_validation.py`. The hardest part is the `/ready` endpoint semantics — specifically the `UNKNOWN` state and whether to document it as non-gating. Get that decision locked before implementation starts, because it drives the `CheckStatus` enum change and any monitoring documentation. Do not compromise on the non-blocking startup contract: validation is always advisory, never a startup gate.
