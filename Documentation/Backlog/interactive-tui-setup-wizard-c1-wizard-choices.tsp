// C1 — WizardChoices answer-sheet (internal logical seam; Entities ↔ Use Cases).
// The single struct every front-end (cockpit / plain prompts / non-interactive)
// fills and SearchInstaller.run consumes. Grows today's WizardFeatures into the
// complete answer-sheet. Logical shape only — no code, no TOML; defaults are
// omitted except where semantically load-bearing (see disableGpu below).
namespace WizardChoicesContract;

enum ProfileName {
  minimal,
  balanced,
  max,
}

// D3: HyDE and RAG-Fusion are mutually exclusive (ADR-C5 invariant). A single
// enum makes "both on" unrepresentable in the type, instead of re-enforcing
// the invariant per front-end.
enum OptionalLlmFeature {
  none,
  hyde,
  ragFusion,
}

// D9: the answer-sheet is the Entity contract for the COMPLETE target sheet
// (Q8 rename; consolidating remap, not a literal superset) — the 23 real
// WizardFeatures fields at install.py:163-188 plus the core-flow answers
// currently passed as separate run() args or read inline. The 6 HyDE/RAG
// provider/model/base-url sub-fields fold under optionalLlmFeature + the
// provider/model/key/baseUrl sub-fields below.
//
// Z2 (inverse-obligation note): the fold above is one-directional at read
// time. The config writer (`_apply_wizard_choices_to_toml`, Interface
// Adapters) must DEMUX on optionalLlmFeature to route the provider/model/
// base-url triple (folded from the 6 real fields) PLUS a net-new llmApiKey
// (not in WizardFeatures; keys flow to .secrets.env) to the correct TOML
// section (`[hyde]` vs `[rag_fusion]`, written in
// `_apply_wizard_features_to_toml` install.py:264-373, which `_profile_toml`
// delegates to) — reading the sheet back out is a section-switch, not a
// field pass-through. The writer
// also writes the SELECTED section's provider details AND explicitly sets
// the OTHER section's `enabled=false` — both `enabled` flags are always
// written every run (install.py:351-353/371-373), so a Reconfigure switch
// (S19) can't leave a stale enabled flag. Writing to the wrong section
// silently breaks the feature.
//
// D9 (Brooks X12) INCREMENTAL-POPULATION NOTE: this is the frozen TARGET shape,
// not a slice-0 gate. Slice 0 fills only profileName (plus multilingual
// pre-filled to its default per D13/X19 — _prompt_multilingual runs before
// _select_profile at install.py:2116/2120, so slice 0 cannot literally defer
// multilingual). proceedConfirmed is NOT filled by slice 0 — it is obtained
// mid-flow, at Proceed time, via C4 requestDecision (see the field comment
// below). The remaining fields are filled by later slices / the plain flow.
// Change this contract only by team agreement (line ~22 of the plan), once a
// slice ratifies it — but C4 and C5, which govern fields slice 0 never
// touches, are "designed now, ratified when their slice starts," not frozen
// seams gating slice 0.
//
// Y9 NOTE: a struct with all-required fields can't be instantiated with only
// 1-2 fields set. Incremental population is only constructible because the
// real WizardFeatures dataclass gives EVERY field a default (install.py:163-188)
// — this .tsp omits those defaults for logical clarity only; it is not
// claiming the real struct is defaultless. (Header below: defaults are
// omitted except where semantically load-bearing, e.g. disableGpu.)
model WizardChoices {
  // Core flow (slice 0)
  profileName: ProfileName;
  // Y3 NOTE: proceedConfirmed (and overwriteConfirmed below) are NOT
  // pre-collected, front-end-filled inputs — they are run()-owned and
  // mid-flow-populated. The cockpit obtains them via C4 requestDecision
  // (transient inline input() reads inside run() at install.py:2308 and
  // :2414, never run() params) and writes the answer back to the App-held
  // sheet; the plain flow reads them inline; non-interactive derives them
  // from --non-interactive. Slice 0 fills profileName (+ multilingual
  // default) only — proceedConfirmed is obtained mid-flow, later.
  proceedConfirmed: boolean;

  // Optional-feature matrix
  multilingual: boolean;
  // D1: GPU is INTENT-ONLY (Q9) — a two-state toggle, not the split-CoreML
  // outcome. The split-CoreML result is a run()-computed value from
  // _probe_and_configure_coreml (defined at install.py:1943, called at
  // install.py:2376) and is never a sheet field. splitCoreml is deleted.
  disableGpu: boolean = false;
  acceptJinaLicense: boolean;
  acceptFasttextLicense: boolean;
  installCodeExtra: boolean;
  installGraphExtra: boolean;
  installMultilingualExtra: boolean;
  disableReranker: boolean;
  enableWatch: boolean;
  enableTelemetry: boolean;
  eagerLoadEmbedders: boolean;

  // HyDE / RAG-Fusion (D3: mutually exclusive via enum)
  optionalLlmFeature: OptionalLlmFeature;
  llmProvider: string | null;
  llmModel: string | null;
  llmApiKey: string | null;
  llmBaseUrl: string | null;

  // Routing / server config
  // Y11 NOTE: routingStrategy/logFormat/logLevel are intentionally free
  // strings, not enums — their value sets are validated at config load time
  // (config.py), not here; unlike profileName/optionalLlmFeature (closed,
  // stable, small sets), keeping these as strings avoids this contract
  // drifting out of sync with config.py's validation as it evolves.
  routingStrategy: string;
  logFormat: string;
  logLevel: string;
  host: string;
  port: int32;
  dbPath: string;
  topK: int32;
  telemetryRetentionDays: int32;

  // Install-flow decisions
  skipPreload: boolean;
  force: boolean;
  deleteDb: boolean;
  // Y3 NOTE: same as proceedConfirmed above — run()-owned / mid-flow, not
  // pre-collected (transient inline input() at install.py:2308 in the plain
  // flow; via C4 requestDecision in the cockpit).
  overwriteConfirmed: boolean;
  serverKey: string | null;
}

// Consumer seam: run() takes one WizardChoices and returns an exit code.
interface Installer {
  run(choices: WizardChoices): int32;
}
