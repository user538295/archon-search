// C4 — `[description]` configuration seam (internal logical seam).
// Revised after implementation (2026-11-01): `think` is stored as `bool | None` (`None` = `default`; quoted "true"/"false" are rejected). The wizard's "default answer is No" is not an Enter default: Enter at the description and backfill questions re-asks ("Please answer y or n."), only closed stdin means no. A blank model for openai/ollama/llama_cpp leaves the feature disabled (exit 0), and the shared-model check normalises hosts and models. The answer-space constants `ANSWER_RESERVE_TOKENS` (1024) and `DESCRIPTION_THINKING_HEADROOM_TOKENS` (3072) are in `constants.py`, not in this contract; `maxQueuedGenerations` is `MAX_QUEUED_GENERATIONS` (64).
//
//
// Layer edge: Presentation (install wizard answers, hand-edited TOML) -> Entities (the
// description settings read by the server at startup).
//
// A `[description]` section mirroring `[hyde]`. Off by default; API keys stay in the secrets file.
// Writing `enabled = false` silences the startup notice for an Anthropic-key installation with no
// `[description]` section. The wizard writes the section through the existing config writer with
// revert-on-failed-install. Every provider is in the single provider registry; the registry sync
// test stays green. Scripted installs use `config set`; there is no install flag, and `description`
// joins the `config` command's default sections.
// `ollama_context_tokens` appears in all four sections (`[hyde]`, `[rag_fusion]`, `[graph]`,
// `[description]`). Unset: `assumedContextTokens` (C1, 4096) is assumed, no context size is sent, and
// startup logs a WARNING (existing Ollama users of HyDE, RAG Fusion and graph see it after upgrade).
// The wizard asks per feature that uses Ollama, suggests 8192 and offers the previous value as the
// default. The three existing sections get the key in group A; `[description]` itself is group B.
// Differences from `[hyde]`: `timeout_seconds` defaults to 30 where `[hyde]` and `[rag_fusion]` use 10
// (`[graph]` 30); a cold local model (a 9 to 13 s load plus prefill of a large sample) can need more,
// and the operator raises the key; there is no `max_requests_per_minute`, because generations are
// capped by a process-wide concurrency limit instead (C5).
// `think` and `include_restricted_chunks` are accepted for every provider and, where the provider
// has no verified switch (or is not local), ignored with a startup WARNING rather than rejected, so
// one config file stays valid across provider changes (a recorded choice).
// Wizard gate: the description question is its own step and is asked whenever the wizard is truly
// interactive; `--enable-hyde` or `--enable-rag-fusion` pre-answering does NOT skip it (the existing
// provider prompts for HyDE, RAG Fusion and graph sit under one gate that is skipped in that case,
// wizard.py line 808). A non-interactive install leaves `[description]` unwritten (an unanswered
// question), so the off-notice can still show.
// Secrets file: the installer creates `.secrets.env` and writes the provider key only when the
// answers need one (installer.py lines 1010 and 1119-1129 gate it on `enable_hyde` or
// `enable_rag_fusion` today and its post-install tip names HyDE only); the description (and graph)
// provider answers join that gate and the tip names whichever feature needs the key.
//
// Logical only.

import "./2026-10-30-010-llm-collection-descriptions-c1-shared-llm-client.tsp";
using SharedLlmClientContract;

namespace DescriptionConfigContract;

/// Limits and defaults, named once; the rest of the plan quotes these.
/// Longest description, in characters (generated text is trimmed to it, manual text over it is rejected).
const maxDescriptionChars = 600;
/// Default `timeout_seconds`, per attempt.
const defaultTimeoutSeconds = 30;
/// Default `max_sample_chunks`.
const defaultMaxSampleChunks = 100;
/// Process-wide ceiling on generations running at the same time, across every trigger (C5).
const maxConcurrentGenerations = 1;
/// Most generation requests that may wait for the slot at once (C5). Not configurable: a loop over
/// hundreds of collections must not grow an unbounded queue. Chosen by the reviewer, not in the brief.
const maxQueuedGenerations = 64;

/// Wizard answers and the server's reading of the section share one shape.
model DescriptionSettings {
  /// Default false: nothing leaves the machine unless the operator chose it.
  enabled: boolean = false;
  provider: LlmProvider;
  modelName: string;
  /// Used when the provider is `ollama`.
  ollamaBaseUrl?: string;
  /// Used when the provider is `llama_cpp`.
  llamaCppBaseUrl?: string;
  /// Used when the provider is `ollama`; the wizard suggests 8192.
  ollamaContextTokens?: int32;
  /// Default false; the wizard's default answer is No.
  backfillOnStartup: boolean = false;
  /// Per attempt, in seconds; the operator may raise it on slower machines or cold local models.
  timeoutSeconds: float32 = defaultTimeoutSeconds;
  /// Ceiling on the chunks sampled for one description.
  maxSampleChunks: int32 = defaultMaxSampleChunks;
  /// Default `useDefault`: no thinking control is sent. Text values in the file: default, true, false.
  think: ThinkSetting = ThinkSetting.useDefault;
  /// Default false. Allowed only for `ollama` and `llama_cpp`; ignored with a startup WARNING for
  /// every other provider. Chunks with any ACL (including deny-all) are sampled only when true.
  includeRestrictedChunks: boolean = false;
}

/// The same context key in the three existing LLM sections.
model OllamaContextSetting {
  ollamaContextTokens?: int32;
}

/// What the wizard tells the operator before the provider question.
model PrivacyBanner {
  /// `anthropic`, `openai` and `claude_cli`: excerpts of document text leave the machine; the same
  /// holds for `ollama` or `llama_cpp` on a host that is not loopback (startup INFO, C5).
  providersThatSendExcerpts: LlmProvider[];
}
