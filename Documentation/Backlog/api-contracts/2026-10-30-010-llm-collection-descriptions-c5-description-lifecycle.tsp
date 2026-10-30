// C5 — Description lifecycle seam (internal logical seam).
//
// Layer edge: Frameworks & Drivers (server startup and shutdown in `app.py` host the tasks and
// build the lifecycle service) -> Use Cases (the lifecycle service: startup validator, backfill and
// generation orchestration, called by the ingest pipeline and the describe job). The validator and the
// backfill are Use Cases; only their hosting is Frameworks & Drivers. The lifecycle reaches the store
// only through the `DescriptionStore` port (C3); the in-memory `pending` overlay is owned here, not by
// the store.
//
// Generation runs off the ingest path. Three triggers: the existing 20% rule (or first ingest),
// the describe job (`collection describe`, REST 202 plus a job, C6/C10), and startup backfill.
// The describe job FORCES generation (it skips the 20% rule) through `generateNow`; the other two go
// through `requestGeneration` and do not force. The force path is the lifecycle's own entry point: the
// `force_regenerate_description` parameter of `ingest_directory` (pipeline.py line 1232) lives on an
// ingest and cannot be reached from a job, so it is removed.
// Ownership of in-flight work: the lifecycle service owns EVERY generation task (ingest-triggered,
// describe job, backfill), the in-memory `pending` and last-attempt state, the per-collection
// single-flight map and the process-wide generation slot. At shutdown it refuses new requests, then
// cancels and awaits all of its tasks (and the claude subprocess, C1) before the stores disconnect;
// today the lifespan drains only `app.state._background_tasks`, which the pipeline's tasks would not
// be in. Shutdown order in `app.py` (the finally block, lines 1061-1092): watcher stop, telemetry
// drain, cancel and await `_background_tasks` (the ingest and backfill tasks, so no producer is left),
// THEN `shutdown()` here, THEN the store disconnects. `shutdown()` sets a closed flag first: from then
// on `requestGeneration` and `generateNow` are refused (`closed` / `shuttingDown`), so a request that
// races the drain can neither start a task nor write to a closed store; an ingest task that is
// cancelled concurrently may still call in and is refused the same way.
// Cost control and slot discipline: one generation per collection at a time (key: collection name,
// names are globally unique; a second request settles `alreadyRunning` at once and never queues) and at
// most `maxConcurrentGenerations` (C4, 1) generations holding the slot in the whole process. The slot
// covers exactly the sample read and the provider call(s) including the output retry; it is RELEASED
// before the embedding and the guarded write (C11: up to 30 s of collection-lock wait, the embedder's
// bounded wait, and `recordFailure`'s own lock wait), so one busy or slow collection never stalls
// every other generation. Waiting requests are served by class, then in arrival order: describe
// (an operator is waiting for it), then ingest rule, then backfill; the queue holds at most
// `maxQueuedGenerations` (C4) requests: a describe over the bound settles `failed` with a detail that
// says the queue is full, an ingest-rule or backfill request over the bound is dropped with one WARNING
// per process (its baseline stays unset, so the next qualifying trigger retries). The sample is read
// AFTER the slot is acquired (C2), so a describe that waited does not describe a stale sample. A
// describe loop by any namespace key is therefore serial; there is no per-key rate limit (see the
// plan's Known limitations).
// Startup validation never blocks boot: an unusable provider is a WARNING and a `/status` finding,
// and backfill is skipped for that run. Description health never enters `/ready` or
// `provider_warnings`; the reachability probe runs as its own startup task, and the backfill AWAITS
// that probe's result before it starts (a down provider means backfill is skipped, not a pile of
// failed attempts). Reachability is as of that startup probe and is not refreshed while the server
// runs; a later outage shows in the last attempt and on the collection (`failed`).
// Startup findings of the description feature (all WARNING unless noted): provider unreachable or key
// missing; provider package or binary missing (C12); `think` non-default for a provider with no
// verified switch; `include_restricted_chunks` ignored for a cloud provider, or enabled (naming
// provider and host, saying that restricted text may then appear in descriptions every reader of the
// collection can see, with a second WARNING when the host is not loopback); INFO that excerpts leave
// the machine for `anthropic`, `openai`, `claude_cli`, and also for `ollama` or `llama_cpp` when the
// configured host is not loopback (a retry sends them again); the off-notice; hybrid routing without
// descriptions. The window findings (unset Ollama window, failed `llama_cpp` probe, Ollama window
// above the model's real maximum) belong to the shared client layer (C1, group A) and are not
// repeated here.
// Loopback test (`isLoopbackHost`, one function used by the privacy INFO and both restricted-chunk
// WARNINGs; a provider counts as local only when its host is loopback, not by its name):
// `urllib.parse.urlsplit` the configured URL; when it has no `//` (for example `localhost:11434`,
// which `urlsplit` reads as scheme `localhost` and no hostname), split `"//" + value` instead; take
// `hostname` (already lower case), strip one trailing dot (`localhost.` is `localhost`); loopback
// when the host is `localhost`, or `ipaddress.ip_address(host)` parses and is loopback, where an
// IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) is unwrapped to its IPv4 form first. Everything
// else is NOT loopback, on purpose: `0.0.0.0` (a wildcard, not a loopback address),
// `host.docker.internal` and every other name (DNS is never consulted), an empty or unparseable host
// (conservative: the operator gets the INFO). Table test: `127.0.0.1`, `127.1.2.3`, `[::1]`,
// `localhost`, `localhost.`, `LOCALHOST`, `localhost:11434` (scheme-less), `::ffff:127.0.0.1` are
// loopback; `0.0.0.0`, `192.168.1.5`, `host.docker.internal`, `example.com`, the empty string and
// `http://` are not.
// Backfill: a background task started after the startup sync has ended (in any state: done or failed;
// when no sync task exists because no collection is configured it starts at once), registered with the
// other background tasks and cancelled and drained at shutdown before the stores disconnect. It is
// SKIPPED for the whole run, with one INFO naming the reason, when the startup sync is SUPPRESSED
// (`StartupSyncResult.SUPPRESSED`, app.py lines 1030-1050): that state means the previous run died
// mid-ingest and the corpus may be stale until an operator runs `POST /sync`, so a mass LLM run over
// it would defeat the crash-loop guard the same way the watcher's skip (app.py line 219) prevents.
// One collection at a time across all namespaces (the port's `readAll`). It fills only collections
// whose description is empty AND whose STORED status is not `failed` (the rule lives on the stored
// value, so it survives a restart); it skips a collection already being generated, never touches
// described collections, and never touches manual ones. A collection left `failed` by an outage stays
// skipped until its next qualifying ingest or a `collection describe` (`describe --all` fills it
// too: its client-side skip rule is "empty description OR stored status `failed`", C10), because
// ingest and describe still retry failed collections.
//
// Logical only.

import "./2026-10-30-010-llm-collection-descriptions-c1-shared-llm-client.tsp";
import "./2026-10-30-010-llm-collection-descriptions-c3-description-state.tsp";
using SharedLlmClientContract;
using DescriptionStateContract;

namespace DescriptionLifecycleContract;

/// The two triggers that go through `requestGeneration`. The describe job is not one of them: it
/// forces and waits for the outcome, so it uses `generateNow`.
enum GenerationTrigger {
  /// The existing 20% rule (or first ingest) fired at the end of an ingest. Does not force.
  ingestRule,
  /// `backfill_on_startup = true`; collections with no description only. Does not force.
  startupBackfill,
}

/// Order in which waiting requests get the generation slot (then arrival order inside a class).
enum SlotClass {
  describe,
  ingestRule,
  backfill,
}

/// What a generation request settles as.
enum GenerationResult {
  generated,
  /// The settled description outcome was `failed` (C2), including a describe over the full queue.
  failed,
  /// A generation for this collection was already running (single-flight).
  alreadyRunning,
  /// The collection has a manual description (REST answers 409 before a job starts; this covers a
  /// manual set that lands between that check and the run).
  refusedManual,
  /// A clear, set or other change landed during the run (`refusedStale`, C3): the result was discarded.
  superseded,
  /// The service is shutting down; nothing ran.
  shuttingDown,
}

/// What `requestGeneration` answers at once.
enum GenerationRequestOutcome {
  accepted,
  alreadyRunning,
  /// Over `maxQueuedGenerations` (C4): dropped, one WARNING per process.
  queueFull,
  /// After shutdown began: refused.
  closed,
}

model StartupFinding {
  provider: LlmProvider;
  /// Free of document text.
  message: string;
}

/// Produced once at startup. Findings are log lines and `/status` content; none stops boot.
model StartupReport {
  descriptionsEnabled: boolean;
  providerReachable?: boolean;
  keyPresent?: boolean;
  effectiveContextTokens?: int32;
  /// Shown on the wire as `context_window_source` (C7); C1 calls it `source`.
  contextWindowSource?: ContextWindowSource;
  findings: StartupFinding[];
  /// True when `ANTHROPIC_API_KEY` is set and no `[description]` section exists.
  offNoticeShown: boolean;
  /// True when `routing_strategy = "hybrid"` runs and at least one collection has no description.
  hybridWithoutDescriptionsWarned: boolean;
}

/// In-memory result of the latest generation attempt; shown in `/status` (C7), lost on restart.
model LastAttempt {
  ok: boolean;
  at: utcDateTime;
}

interface DescriptionLifecycle {
  /// Never blocks boot and never raises. Reachability is probed by its own startup task and reads
  /// null until probed.
  validateAtStartup(): StartupReport;

  /// Returns at once; the work runs in the background and settles as `ok` or `failed`. Used by the
  /// ingest rule and by backfill.
  requestGeneration(collection: string, collectionNamespace: string, trigger: GenerationTrigger): GenerationRequestOutcome;

  /// Used by the describe job: forces generation, takes the `describe` slot class, and returns how it
  /// settled.
  generateNow(collection: string, collectionNamespace: string): GenerationResult;

  /// The status a reader sees: the stored status (C3 port, no overlay) with `pending` laid over it
  /// while a generation of that collection is in flight. Called by the REST, MCP and CLI read paths.
  overlayStatus(collection: string, collectionNamespace: string, stored: DescriptionStoredStatus | null): DescriptionStatus | null;

  /// Startup backfill; waits for the startup sync and the reachability probe, is skipped when the sync
  /// is suppressed or the provider is down, then stops cleanly when the server shuts down.
  backfillMissing(): void;

  /// Refuses new requests, then cancels and awaits every generation task the service owns (and its
  /// provider calls), so no description is half written; called after the background tasks are
  /// cancelled and before the stores disconnect.
  shutdown(): void;
}
