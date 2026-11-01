// C1 — Shared LLM client seam (internal logical seam).
// Revised after implementation (2026-11-01): the text below is the plan as written; the shipped API differs as follows (`archon_search/llm_client/protocol.py`, `fit.py`, `budget.py`).
// - `call` and `input_budget` are async, `context_window` is sync; `input_budget(template_text, think = ThinkSetting.DEFAULT)` takes the thinking setting because a chat template can frame a prompt differently per setting. `ThinkSetting` members are `DEFAULT`, `ON`, `OFF`.
// - `LlmCallRequest` fields are `prompt`, `max_output_tokens`, `timeout_seconds`, `think`; callers send `ANSWER_RESERVE_TOKENS` (1024), and a description call that may think adds `DESCRIPTION_THINKING_HEADROOM_TOKENS` (3072) and pays for it out of its sample. `LlmCallFailure(provider, reason)` has the subclasses `LlmCallTimeout` and `LlmThinkingNotSupported`.
// - Two optional protocols carry counting: `TokenCountingClient.count_tokens` (llama_cpp, ollama) and `TokenBoundingClient.token_ceiling` (a request-free upper bound, so a text surely within budget skips the count).
// - The fit/budget API of the callers is `fit.py`: `fit_query_to_window(client, template, query, provider=)` (HyDE and RAG Fusion) and `fit_text_to_budget(text, budget, count)`, with `NoRoomForInput` when the window leaves no room, at most `MAX_FIT_COUNTS` (10) counts, and `TemplateCostCache` (in `budget.py`) memoising the template cost. The old fixed 2000-character query cut is gone; a cloud query is sent untrimmed.
// - The Ollama estimate counts digits and emoji; the llama_cpp budget counts the chat framing through `/apply-template` and honours `think`.
// - The `claude_cli` child's environment is an allowlist wider than the body lists: besides `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`, `TMPDIR` and `CLAUDE_CONFIG_DIR` it carries the Windows variables, proxy and CA variables, and `CLAUDE_PROXY_URL` and `CLAUDE_PROXY_TOKEN` (the dev-container `claude` wrapper); the server's API key, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` never reach it (`child_environment` in `llm_client/claude_cli_client.py`).
// - `LlmClientOptions` is realised as the arguments of `build_llm_client` in `server/llm_composition.py` (`rate_limit_rpm`, claude_cli retry flag), not as a model.
//
//
// Layer edge: Use Cases (HyDE generation, RAG Fusion generation, graph community enrichment,
// description generation) -> Interface Adapters (one wrapper per provider).
//
// What is new: one consumer-owned interface replaces the two parallel families that exist today
// (query-expansion providers that never raise, enrichment clients that raise). The old adapters,
// both protocols' old forms, the enrichment factory and the query-expansion builder are removed in
// the same change (clean cutover); prompt templates and variant parsing move to the consumers.
// What is fixed: one user message in, text out, no history; a failure is raised, never swallowed;
// each caller decides its own fallback and logs the failure once per process. Each feature keeps
// its own provider, model, host and context setting: the client is built per feature.
// Input is sized AHEAD of the call to the known window. There is no truncation detection after the
// call and no safety net: llama-server rejects an oversized prompt with an error (raised as a
// failure), and Ollama is protected by a generous spare margin plus a startup WARNING when the
// configured window exceeds the model's real maximum.
// Sizing by provider: `llama_cpp` counts exactly with its tokenize call and reads the window from
// its properties endpoint; `ollama` estimates with a gpt2-based token counter plus a 20% margin on
// top of the reserved answer space and the prompt template; cloud providers are not sized.
// The gpt2 token counter is a NEW component of this layer: the repository has none today (gpt2 only
// appears as the tokenizer name the chunker hands to its chunking library, chunker.py lines 20 and
// 121). The dev picks the tokenizer package and confirms its vocabulary is available offline through
// the existing model provisioning rather than a first-use download.
// Startup WARNINGs about the window belong to this layer and ship in group A for every feature that
// uses the provider: an Ollama window that is unset (4096 assumed, nothing sent), a failed
// `llama_cpp` probe (4096 assumed), and a configured Ollama window above the model's real maximum
// (read from Ollama's model-details call). An unset Ollama value is an assumption, not a guarantee:
// Ollama's own default window depends on the machine's VRAM and on the server environment
// (docs.ollama.com/context-length), so an unsent `num_ctx` means "whatever the server picked".
//
// `claude_cli` now serves graph enrichment too: the prompt goes on standard input for EVERY feature
// that uses it (HyDE, RAG Fusion, graph, description; the current adapter puts the prompt in argv,
// which shows in the process list and hits the argument-size limit on a large sample).
// Rate limit: the graph rate limit (`extraction_rate_limit_rpm`, default 60) is applied by the
// shared client layer, one limiter per graph client, to `anthropic` (as today, where the limiter
// lives only inside the Anthropic enrichment client that this change deletes) and to `claude_cli`
// (new: a Claude login has an account-level quota). `openai`, `ollama` and `llama_cpp` graph calls
// stay unthrottled as today; HyDE and RAG Fusion `claude_cli` stay unthrottled as today. An
// exhausted limiter raises like any other failure.
// `claude_cli` isolation: graph and description prompts carry untrusted document text, so the
// subprocess is locked down: prompt on standard input; every tool disabled (`--tools ""`); no session
// saved (`--no-session-persistence`); no slash commands or skills (`--disable-slash-commands`); no
// MCP servers (`--strict-mcp-config`); `--max-turns 1` (the old SDK path set `max_turns=1`,
// description_generator.py line 106; with every tool disabled one turn answers); `--safe-mode` so
// the operator's own CLAUDE.md, hooks, plugins, skills and custom agents neither change the output
// nor receive the excerpts; a neutral empty working directory, never the server's.
// Flags checked against the local `claude` 2.1.308 (`claude --version`): `--tools`,
// `--no-session-persistence`, `--disable-slash-commands`, `--strict-mcp-config`, `--safe-mode` and
// `--setting-sources` are listed in its `claude --help`; `--max-turns` is NOT listed there but the
// binary accepts it (`claude --max-turns 1 -p` with empty input fails with "Input must be provided",
// not "unknown option", while a made-up flag fails with "unknown option"). One real call with this
// exact flag set, under the allowlisted environment below, answered normally on the login (no API
// key). The dev re-checks the flags against the installed version, and the fake-binary test (S79)
// asserts the whole argv: `--tools ""`, `--no-session-persistence`, `--disable-slash-commands`,
// `--strict-mcp-config`, `--max-turns 1`, `--safe-mode`, the prompt on standard input and none of it in
// argv. A `claude` older than these flags fails with "unknown option": a raised failure the operator
// sees, not a silent one. `--bare` is NOT used: it reads only an API key, never the Claude login that
// `claude_cli` exists for.
// Environment: an ALLOWLIST, not a copy of the server's environment minus `CLAUDECODE`. The server
// may hold `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the bearer token, which the child must never
// see; and with `ANTHROPIC_API_KEY` set `claude -p` bills that key instead of the login that
// `claude_cli` is for. The child gets only `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`,
// `TMPDIR`, plus `CLAUDE_CONFIG_DIR` when set (and `SYSTEMROOT`, `USERPROFILE`, `APPDATA`,
// `LOCALAPPDATA` on Windows, and proxy and CA variables when set: [INFERENCE] not tested). Checked on
// macOS: with `env -i` and only `PATH`, `HOME` and `LANG` the child answered "Not logged in" (the
// keychain login was not found); adding `USER`, `LOGNAME` and `TMPDIR` made the same call succeed.
// `CLAUDECODE` is never in the allowlist, so a server started from inside a Claude Code session can
// still call, and no process-wide mutation or lock is needed (the old SDK path popped it under a
// lock). The fake-binary test asserts the child's environment holds none of the secrets.
// Process lifetime: the subprocess is killed and reaped on timeout AND on cancellation, so a server
// shutdown leaves no orphan (the current adapter kills only on timeout: the `proc.kill()` at
// claude_cli_provider.py line 130, inside the `except TimeoutError` at line 128).
// Transport retry: the current adapter retries once after a short backoff on a non-zero exit
// (`_MAX_ATTEMPTS = 2`, line 36; not on timeout), which would send the prompt a second time. The
// shared wrapper keeps that one retry for HyDE and RAG Fusion only (queries, today's behaviour, it
// absorbs a transient exit under session contention) and does NOT retry for graph and description:
// their prompts hold document excerpts, a provider error is not retried (S53), and the privacy INFO
// says excerpts are sent once per attempt, twice only after an output retry. It is not the
// description retry.
//
// Named constants (the rest of the plan quotes these instead of restating the numbers):
// `assumedContextTokens` (4096) and `ollamaEstimateMargin` (0.20) below; the description limits live
// in C4.
//
// Logical only: no signatures beyond what two roles must agree on, no vendor SDK API.

namespace SharedLlmClientContract;

/// Window assumed when an Ollama value is unset or a `llama_cpp` probe fails.
const assumedContextTokens = 4096;

/// Spare share added on top of the gpt2 estimate for `ollama` (20%).
const ollamaEstimateMargin = 0.20;

enum LlmProvider {
  anthropic,
  openai,
  ollama,
  claude_cli,
  llama_cpp,
}

/// Thinking control. `useDefault` sends no thinking control at all. Only the description feature
/// exposes this setting; HyDE, RAG Fusion and graph are unchanged. Providers with no verified
/// switch (`anthropic`, `openai`, `claude_cli`) ignore a non-default value (startup WARNING in C5).
enum ThinkSetting {
  useDefault,
  on,
  off,
}

/// One stateless call: exactly one user message, no history, no session.
model LlmCallRequest {
  prompt: string;
  /// Generous for every model so that `useDefault` works on reasoning models; the exact ceiling is
  /// the implementer's call.
  maxOutputTokens: int32;
  /// Per attempt.
  timeoutSeconds: float32;
  think: ThinkSetting;
}

model LlmCallResult {
  text: string;
}

/// Raised on every failure: unreachable provider, missing key, timeout, provider error, a prompt
/// the server rejects as too long, an exhausted graph rate limit, or `think = on` on a model
/// without thinking support.
/// Callers catch it, apply their own fallback and log a WARNING (rate limited to once per process).
/// The failure text never contains the prompt, a query, a hypothetical document or an excerpt.
@error
model LlmCallFailure {
  provider: LlmProvider;
  reason: string;
}

enum ContextWindowSource {
  /// Operator-set value (`ollama` only).
  configured,
  /// Read from the server (`llama_cpp`, from its properties endpoint).
  probed,
  /// Fallback of `assumedContextTokens` when no value is configured or the probe failed; startup
  /// logs a WARNING.
  assumed,
}

/// Cloud providers have no limit the client enforces, so they report `unbounded`.
union ContextWindow {
  tokens: ContextWindowTokens,
  unbounded: "unbounded",
}

/// `source` is what C7 shows on the wire as `context_window_source` (C5 carries it as
/// `contextWindowSource`).
model ContextWindowTokens {
  size: int32;
  source: ContextWindowSource;
}

/// How many input tokens a caller may send: window minus the reserved answer space minus the
/// prompt template, after the provider-specific counting rule (exact for `llama_cpp`, estimate plus
/// `ollamaEstimateMargin` for `ollama`).
model InputBudget {
  tokens: int32 | "unbounded";
}

/// Chosen once when a feature's composition builds its client.
model LlmClientOptions {
  provider: LlmProvider;
  /// Graph only: requests per minute (`extraction_rate_limit_rpm`). Honoured for `anthropic` and
  /// `claude_cli`; absent for every other provider and feature.
  maxRequestsPerMinute?: int32;
  /// `claude_cli` only: one retry after a short backoff on a non-zero exit. True for HyDE and RAG
  /// Fusion (today's behaviour), false for graph and description, whose prompts hold document
  /// excerpts and must not be sent twice by transport (S53).
  retryOnNonZeroExit?: boolean;
}

interface LlmClient {
  /// Stateless; two consecutive calls never share history.
  call(request: LlmCallRequest): LlmCallResult | LlmCallFailure;

  contextWindow(): ContextWindow;

  /// The budget callers size their input to, given the LONGEST form of the prompt they will send:
  /// the template WITH the retry reminder (S53), so a retry of a full sample still fits a small
  /// window and the retry never needs a second sizing.
  inputBudget(templateText: string): InputBudget;
}
