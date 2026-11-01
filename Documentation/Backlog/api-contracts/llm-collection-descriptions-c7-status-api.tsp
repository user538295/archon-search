import "@typespec/http";
using Http;

@service(#{ title: "Archon Search Status API (description block)" })
namespace StatusDescriptionApi;

// C7 — `GET /status` -> `description`.
// Revised after implementation (2026-11-01): `DescriptionStatusDetail` in `schemas.py` keeps `provider`, `key_available` and `backfill_on_startup` required; every other field is optional with a null default (not required-nullable), and `provider` is a plain string on the wire. `effective_context_tokens` and `context_window_source` are null for any provider that trims nothing (cloud and `claude_cli`). `key_available` for `claude_cli` means the client found the `claude` program. The block reads `app.state` only (no provider call), the probe's free-text reason and install command stay log-only, and the last attempt is process-wide and visible to every authenticated caller. The probe runs whenever descriptions are enabled. `describe --all` preflights `GET /status`: a null `description` prints the setup message and exits 1.
//
//
// HTTP/API seam. `GET /openapi.json` is the authoritative HTTP contract for this project, so this
// file models only the sub-object this feature adds. The field is additive and nullable, like
// `hyde`, `rag_fusion` and `graph`: it is null when the feature is off.
//
// Rules:
//   - Description health appears in `/status` and the startup log only. It does NOT appear in
//     `GET /ready`, and it never uses `provider_warnings`: `/ready` grades only `embedder_ok`,
//     `reranker_ok` and `provider_warnings`.
//   - `reachable` comes from a startup probe that runs as its own startup task, not inside the
//     `/ready` model validation; it is null until probed.
//   - The existing `llama_cpp_ok` is unchanged (it still reflects the first `llama_cpp` URL of
//     hyde, rag_fusion and graph); it is not fed by the description provider.
//   - `last_attempt_ok` and `last_attempt_at` are held in memory and reset on restart.

enum LlmProvider {
  anthropic,
  openai,
  ollama,
  claude_cli,
  llama_cpp,
}

enum ContextWindowSource {
  configured,
  probed,
  assumed,
}

model DescriptionStatusDetail {
  provider: LlmProvider;
  /// Whether the provider's key (or binary) is available.
  key_available: boolean;
  /// Null until the startup probe has run.
  reachable: boolean | null;
  /// Null for cloud providers (nothing is trimmed).
  effective_context_tokens: int32 | null;
  context_window_source: ContextWindowSource | null;
  backfill_on_startup: boolean;
  /// Null until the first attempt since start.
  last_attempt_ok: boolean | null;
  last_attempt_at: utcDateTime | null;
}

/// Only the changed part of the response is modelled.
model StatusDescriptionFragment {
  description: DescriptionStatusDetail | null;
}

@route("/status")
interface Status {
  @get status(): StatusDescriptionFragment;
}
