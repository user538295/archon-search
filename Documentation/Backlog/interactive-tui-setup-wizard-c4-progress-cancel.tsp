// C4 — Progress + cancellation seam (internal logical seam; Use Cases ↔ Presentation).
// The slow work (_prewarm_models, _wait_for_service, extra installs) runs off the
// UI thread and streams progress into the cockpit gauges; cancel must unwind so the
// install lock is released. Logical shape of the progress event and cancel handle.
namespace ProgressCancelContract;

// D7 (X10): there are TWO slow stretches the worker drives, both represented
// as phases: the pre-Proceed CoreML probe (Step 9, install.py:2376,
// coremlProbe) and the post-Proceed prewarm/service-start tail
// (modelDownload / serviceStart / serviceWait).
//
// Y13: enum values are ordered to match RUNTIME order, and every value is
// classified pre- or post-Proceed (the Proceed gate is install.py:2414):
//   coremlProbe    -- install.py:2376 -- PRE-Proceed  (GPU provider config)
//   diskCheck      -- install.py:2390 -- PRE-Proceed  (_check_disk_space)
//   [ -- Proceed confirm, install.py:2414 -- ]
//   extraInstall   -- install.py:2428/2436 -- POST-Proceed (_install_code_extra / _install_graph_extra)
//   modelDownload  -- install.py:2505 -- POST-Proceed (_prewarm_models)
//   serviceStart   -- install.py:2525 -- POST-Proceed (write_service_file)
//   [ -- load_service, install.py:2526, runs here; no dedicated phase -- ]
//   serviceWait    -- install.py:2534 -- POST-Proceed (_wait_for_service)
enum InstallPhase {
  coremlProbe,
  diskCheck,
  extraInstall,
  modelDownload,
  serviceStart,
  serviceWait,
}

// D8 (X17): spinnerFrame is DROPPED — spinner animation is the cockpit's
// concern; the worker reports what's happening (phase, fraction, message),
// the UI decides how it spins.
//
// `fraction`: _prewarm_models (install.py:487) is a blocking fastembed
// download with NO progress-callback surface. `fraction` is COARSE /
// phase-based and may be indeterminate during model download — it can be
// step-granular rather than smooth. Live-gauge fidelity during that stretch
// is a known, manual-only limitation, not a contract the worker can honor.
model ProgressEvent {
  phase: InstallPhase;
  fraction: float32;
  message: string;
}

interface ProgressSink {
  emit(event: ProgressEvent): void;
}

// D5 (X3): the worker requests a blocking modal decision from the UI thread
// and awaits the result. run() executes wholly on the worker thread
// (Q4, @work(thread=True)) and Q10 keeps both confirms mid-flow — the
// overwrite confirm (install.py:2308) and the Proceed confirm
// (install.py:2414). This seam models that call honestly: the worker blocks
// on requestDecision until the UI thread answers.
//
// Y10 NOTE: requestDecision is the cockpit's DELEGATION to C2
// DecisionAskerContract.Asker.askYesNo — askYesNo is THE single boolean-ask
// primitive; requestDecision is its mid-flow/worker-thread entry point. See
// C2's Asker.askYesNo note for the authoritative asymmetry discussion (this
// is a pointer, not a duplicate). Signatures are unchanged.
interface DecisionRequester {
  requestDecision(prompt: string): boolean;
}

// D5 (X9): CORRECTED cancellation model. Textual @work(thread=True) workers
// CANNOT be force-cancelled — worker.cancel() sets a flag the body polls
// cooperatively (is_cancelled); it does NOT raise an exception into a
// blocking native call. cancel() therefore requests cooperative unwind: the
// worker polls is_cancelled at checkpoints and returns/raises on its own: the
// `with lock_cm` block (install.py:2107; 2106 is the assignment) releases the
// lock via its `finally` (install.py:126-134) when run() unwinds normally.
// No rollback; nothing-durable-then-reverted (see D6) — cancel does not
// force-unwind a blocking call mid-flight.
interface InstallWorker {
  start(sink: ProgressSink, decisions: DecisionRequester): void;
  cancel(): void;
  isCancelled(): boolean;
}
