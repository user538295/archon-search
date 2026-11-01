// C12 — Provider dependency guard (internal logical seam).
// Revised after implementation (2026-11-01): the guard is ONE shared function, `check_provider_package(provider)` in `server/provider_dependency.py`, returning a `PackageProblem` (package, detail, install hint) or `None`; there is no `GuardResult` type. Graph (`_missing_graph_package` in `app.py`) and description both use it. For graph the WARNING leaves `graph_client = None` while `graph.enabled` stays true (entity graph, PPR and communities still work). For description the probe reports the missing package as not reachable, so backfill is skipped. `claude_cli` and `llama_cpp` are never guarded here (the probe checks the `claude` program). HyDE and RAG Fusion keep `_check_provider_deps` and still refuse to start.
//
//
// Layer edge: Frameworks & Drivers (server startup composition) -> Interface Adapters (provider
// wrappers, resolved by import or by binary lookup).
//
// Packages each feature's provider needs after the migration: `anthropic` needs the `anthropic`
// package (extra `hyde`), `openai` the `openai` package (extra `openai-provider`), `ollama` the
// `ollama` package (extra `ollama`), `llama_cpp` nothing (httpx is core), `claude_cli` the `claude`
// binary on PATH, no package. The three extras are already declared in pyproject.toml; what is new is
// that graph enrichment (which used httpx for `openai` and `ollama`) and the description feature now
// need them.
// Difference between features: a missing package for HyDE or RAG Fusion keeps today's
// refuse-to-start error; for graph enrichment it is a WARNING with the install hint, graph
// enrichment stays off, and the server still starts. For the description feature it is also a
// WARNING with the install hint and the server still starts, but the feature is not silently off:
// every generation attempt fails visibly (status `failed`, one WARNING per process), `/status`
// shows the provider as not reachable, and startup backfill is skipped. The wizard installs the
// matching extra for the graph provider and for the description provider and reverts on failure.
// Owners: the graph entry of this guard ships in group A (S16), the description entry in group C
// with the startup validator (S75); HyDE and RAG Fusion keep the existing guard (S65).
//
// Logical only.

import "./2026-10-30-010-llm-collection-descriptions-c1-shared-llm-client.tsp";
using SharedLlmClientContract;

namespace ProviderDependencyGuardContract;

enum LlmFeature {
  hyde,
  ragFusion,
  graph,
  description,
}

model MissingDependency {
  provider: LlmProvider;
  /// The install extra that provides the package; absent for `claude_cli` (a binary).
  extra?: string;
  /// Operator-facing install hint.
  remedy: string;
}

enum MissingDependencyAction {
  /// HyDE and RAG Fusion: the server refuses to start with a clear error.
  refuseToStart,
  /// Graph: WARNING, the feature stays off, the server starts.
  warnAndDisableFeature,
  /// Description: WARNING, the server starts, every attempt fails visibly, backfill is skipped.
  warnAndFailAttempts,
}

model GuardResult {
  missing?: MissingDependency;
  action?: MissingDependencyAction;
}

interface ProviderDependencyGuard {
  check(feature: LlmFeature, provider: LlmProvider): GuardResult;
}
