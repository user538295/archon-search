// C4 — Generic model-artifact provisioning (internal logical seam).
//
// Layer edge: Presentation (wizard prompts) -> Frameworks & Drivers (installer package).
// Replaces the spaCy-wheel-specific step (compatibility-table lookup -> GitHub wheel URL ->
// unzip -> wheel-layout assert -> smoke-load), which verifies no digest at all.
//
// SCOPE, decided 2026-08-24 (team plan, Scope -> Provisioning): this seam now governs
// `lid.176.ftz` ALONE. The graph model is fetched the way the existing embedder already
// fetches its own model — `GLiNER.from_pretrained(model_name, revision=...)`, lazy behind a
// lock (C1), mirroring `embedder.py:37-45` — never through `ArtifactProvisioner`. Every member
// below that existed to serve the graph model's own hosted-tarball design is retained for
// interface stability and marked unreachable where it has lost its producer, following the
// same pattern this file already used for `conflicting_onnx_runtimes` and (as an excluded
// candidate) `license_declined` — this is an edit, not a deletion.
//
// Layering constraint inherited from the code it replaces, stated as a MODULE-level rule, not
// a layer-level one (M3): `ProseExtractionBackend` sits in Frameworks & Drivers, the plan's
// Layer map — the same layer the installer package itself occupies — so a layer-level
// justification could not discriminate between the two modules the installer imports and the
// one it must not. The installer package must NOT import `archon_search.graph_extractor` — the
// Interface Adapters orchestrator that wraps the backend (`GraphExtractor` is Interface
// Adapters, per the plan's Layer map — not Use Cases). **This rule has no live import left to
// protect within THIS seam** (2026-08-24): `provision()` no longer touches the graph model, so
// nothing in `ArtifactProvisioner` imports `ProseExtractionBackend` any more — a pinned-digest
// byte artifact needs neither it nor `graph_extractor`. Retained here because this file is
// where the module-level distinction was first drawn; its live enforcement now sits with C1's
// `ProvisioningBackendFactory`, whose sole remaining caller is the graph engine's own
// provider-validation probe (C5), not this seam.
//
// TWO descriptor shapes, not one (Q12) — see `ArtifactSpec` and `SizeEstimateSpec` below.
//
// The pinned model+revision SINGLE-OWNER guarantee below is now MOOT within this seam's scope
// (2026-08-24): it protected `ArtifactSpec.revision` for the graph model, which this seam no
// longer provisions. `lid.176.ftz` — the sole remaining `ArtifactSpec` consumer — has no
// revision concept at all (`install/licenses.py:90`, `FASTTEXT_MODEL_URL`, a single fixed URL
// with no version dimension), so `revision` is unpopulated / unused for it. Retained on the
// descriptor for interface stability and as provenance for a future provisioned artifact with
// a versioned upstream, not as live guidance for `lid.176.ftz` today. `url`, `sha256` and
// `sizeBytes` are, and remain, separate hand-pinned constants on the installer side (K8).

namespace ModelArtifactProvisioning;

/// The pinned identity of one downloadable, checksum-verified, placed artifact — the graph
/// model and `lid.176.ftz`, both pinned URLs with a fixed byte count. Everything here is a
/// constant in the installer package (barring `revision`, imported from `paths.py`) — nothing
/// is resolved from a remote compatibility table at run time. Distinct from `SizeEstimateSpec`
/// (below), which covers pip/uv-installed extras that have neither a pinned URL nor a stable
/// byte count — the tree-sitter bundle does NOT get this descriptor shape.
model ArtifactSpec {
  /// Stable logical name, used for the on-disk directory and for operator-facing messages.
  name: string;

  /// Fully pinned download URL. NOT derived from `revision` by construction — S54's
  /// structural assertion is what keeps the two from drifting apart (K8).
  url: string;

  /// Upstream revision the artifact was pinned at, where the artifact has a revision concept.
  /// Originally imported from `paths.py`'s single-owner module constant for the graph model
  /// (Q6) — moot now that the graph model no longer provisions through this seam (see header,
  /// 2026-08-24). `lid.176.ftz`, the sole remaining consumer, has no revision at all
  /// (`install/licenses.py:90`, a single fixed URL) — unpopulated for it, retained for
  /// interface stability.
  revision: string;

  /// Expected digest of the downloaded bytes, verified before anything is placed.
  sha256: string;

  /// Exact download size in bytes (Q12). Three rules, all load-bearing:
  ///   - GENERATED from the artifact we host, alongside the digest — never hand-typed and
  ///     never read off a model card.
  ///   - ASSERTED against the bytes actually received, exactly like the digest. A size that
  ///     does not match is a provisioning failure, not a rounding difference.
  ///   - Read by `ArtifactProvisioner.plannedDownloadBytes` as ONE of the inputs summed across
  ///     the whole install selection (alongside every `SizeEstimateSpec.declaredMb` in the
  ///     selection) — that sum, `totalBytes`, is what actually feeds the disk guard's
  ///     `requiredFreeBytes`, the service-ready timeouts and the wizard's display (three named
  ///     quantities, NOT one shared field — see `ArtifactProvisioner.plannedDownloadBytes`).
  sizeBytes: int64;

  /// SPDX license identifier, surfaced before download. `Apache-2.0` was the pinned graph
  /// checkpoint's value (Q15) — MOOT now that the graph model no longer provisions through
  /// this seam (see header, 2026-08-24). `lid.176.ftz` — the sole remaining consumer — is
  /// CC-BY-SA 3.0, but its license handling stays on the separate, untouched
  /// `_prompt_fasttext_license` gate (`install/licenses.py:60`), not on this field; converting
  /// it to this unified rule is a named follow-up, not scoped here (Q15, team plan Scope ->
  /// Provisioning). Retained on the descriptor for interface stability, unpopulated in
  /// practice for `lid.176.ftz` today.
  license: string;

  /// How the license is handled at install time. Set from the ONE rule that now lives in
  /// `install/licenses.py` (Q15) for a NEWLY-provisioned artifact under this rule — the graph
  /// model was to be its first and, absent a future artifact, only real user; that design was
  /// superseded (see header, 2026-08-24) before it provisioned anything. `lid.176.ftz`'s
  /// license gate (`_prompt_fasttext_license`) predates this rule and stays on its own legacy
  /// path rather than converging onto it (Q15) — so this field, too, is retained for interface
  /// stability but unpopulated in practice for the sole remaining `ArtifactSpec` consumer.
  licenseDisposition: LicenseDisposition;

  /// Attribution text shipped next to an artifact this project REDISTRIBUTES. Written for the
  /// graph checkpoint's now-superseded hosted-tarball design (see header, 2026-08-24) — we
  /// never redistribute `lid.176.ftz`'s bytes under a repackaged tarball either; it is fetched
  /// straight from its own pinned URL and placed as-is, with no LICENSE file of ours to ship
  /// beside it. Retained on the descriptor for interface stability, unpopulated in practice.
  attribution: string;
}

/// The SECOND descriptor shape (Q12): a declared-size-only estimate for a pip/uv-resolved
/// extra such as the tree-sitter bundle (ten wheels, no pinned URL, no stable byte count
/// across platforms) — display only, never digest-checked, never placed by this seam.
model SizeEstimateSpec {
  /// Stable logical name, for operator-facing messages only.
  name: string;

  /// Declared install size in megabytes — an estimate, not a measured byte count.
  declaredMb: int32;

  /// SPDX license identifier, surfaced in the pre-download disclosure. Unset for estimates
  /// whose license is not disclosed here (e.g. the tree-sitter bundle); set for the graph
  /// model estimate (`Apache-2.0`). Disclose-only — this shape carries no `licenseDisposition`.
  license?: string;
}

/// The license rule, applied to every artifact descriptor rather than decided per model.
enum LicenseDisposition {
  /// Restrictive on use — the operator must accept before any bytes move.
  prompt_for_acceptance,

  /// Permissive — disclose the identifier before download, gate nothing.
  disclose_only,
}

enum ProvisionOutcome {
  /// Artifact was downloaded, verified, placed, smoke-loaded and asserted capable.
  provisioned,

  /// A usable artifact was already present; presence was re-verified by smoke-load and by
  /// re-running the capability assert.
  already_present,

  /// Download, digest, size, disk, extraction, smoke-load, capability assert, or
  /// execution-provider conflict failed. Non-fatal to the wizard run: graph ingest degrades to
  /// code symbols only until the wizard is re-run.
  failed,
}

/// Why a provision attempt failed. Sanitized categories, never an exception message —
/// operator remedies differ per category.
///
/// EIGHT members (M8 — widened back from an over-narrow five, which left three of the causes
/// now named in `ProvisionOutcome.failed`'s doc comment above with no category, no rendering
/// obligation and no revert disposition). Every cause named there — download, digest, size,
/// disk, extraction, smoke-load, capability assert, or execution-provider conflict failed — now
/// has a category, a revert disposition in S45 and a rendering obligation in the Frontend
/// Done-when.
/// `license_declined` alone stays OUT: it is unreachable for the pinned Apache-2.0 /
/// `disclose_only` graph artifact and no scenario or Done-when item names it. Widening this
/// enum again requires giving every new member both a revert disposition in S45 and a
/// rendering obligation in the Frontend Done-when, in the same change.
///
/// **THREE of the eight are now retained-but-unreachable, not just one** (2026-08-24 update —
/// was one, `conflicting_onnx_runtimes`, when this comment was last written).
/// `smoke_load_failed`, `relations_not_supported` and `conflicting_onnx_runtimes` all named
/// the post-placement smoke-load/capability-assert stage of `provision()`, which existed only
/// to serve the graph model's now-superseded hosted-tarball design (Scope -> Provisioning,
/// team plan, 2026-08-24). That stage never ran for `lid.176.ftz` even under the original
/// design and, with the graph model gone from this seam entirely, now has no artifact left to
/// run for at all — see each member's own doc, below, for its specific reasoning. All three
/// stay in the enum: removing any of them would break S45's revert-disposition switch and the
/// Frontend Done-when's per-category rendering obligation, both of which still enumerate all
/// eight — the same interface-stability reasoning `license_declined` and
/// `conflicting_onnx_runtimes` already used for a member with no live producer.
enum ProvisionFailureKind {
  /// The download did not complete — a network failure or a truncated transfer caught before
  /// any verification step runs. Nothing is placed; reverts `[graph].enabled` like the other
  /// four pre-placement categories below.
  download_failed,

  digest_mismatch,

  /// Bytes received did not match `sizeBytes`. Distinct from `digest_mismatch` so a
  /// truncated transfer reads differently from a tampered or re-pointed artifact.
  size_mismatch,

  insufficient_disk,

  /// The downloaded archive failed to extract. Occurs before `fsync`+atomic-rename, so
  /// nothing is placed; reverts `[graph].enabled` like the other pre-placement categories.
  extract_failed,

  /// The placed artifact failed its mandatory post-placement smoke-load, before the
  /// relation-capability assert could even run. Occurs AFTER atomic rename (Scope →
  /// Provisioning: smoke-load is ordered after publish) — the artifact IS on disk, so this
  /// does NOT revert `[graph].enabled`, the same disposition as `relations_not_supported` and
  /// `conflicting_onnx_runtimes` below: there is a placed tree an operator can retry against
  /// without re-provisioning from scratch. The capability assert never ran for this category
  /// (see `CapabilityStatusRecord`, below).
  ///
  /// UNREACHABLE, decided 2026-08-24 (Scope -> Provisioning, team plan): the "mandatory
  /// post-placement smoke-load" this doc describes was BE-13's, and BE-13 provisioned the
  /// graph model — the artifact whose smoke-load could plausibly fail on a corrupted or
  /// incompatible download. The graph model no longer reaches `provision()` at all; it loads
  /// via `GLiNER.from_pretrained` (C1), lazily, outside this seam. `lid.176.ftz` — the sole
  /// remaining `provision()` target — was never smoke-loaded even under the original design:
  /// its digest-plus-byte-count check (BE-3) is the whole of its verification, because a
  /// fastText binary has no load-time capability to assert beyond "the bytes are the bytes we
  /// pinned," which the digest already covers. Retained in the frozen enum for the same
  /// interface-stability reason as `conflicting_onnx_runtimes`, below.
  smoke_load_failed,

  /// The placed artifact's forward pass accepted the relation labels and returned none —
  /// S12's checkpoint state. Reverted by the spike gate to its originally designed meaning
  /// (Spike gate — RESOLVED 2026-08-24, team plan): the checkpoint's PyTorch weights ARE
  /// relation-capable — measured `archon-search --uses--> LanceDB` @ 0.9822 and
  /// `reranker --depends_on--> fastembed library` @ 0.9591 — only the ONNX conversion lost
  /// that capability, and the resolution is to ship the PyTorch checkpoint instead, never the
  /// ONNX export (see the provisioning-seam doc, below). This category is therefore a genuine
  /// correctness check catching a bad revision or a corrupted download for THIS installation,
  /// not the permanent universal-steady-state failure the entities-only branch evaluated in
  /// the team plan's Spike gate section would have made it — that branch was not taken. Same
  /// post-placement disposition as `smoke_load_failed` above: occurs AFTER atomic rename, so
  /// does NOT revert `[graph].enabled`.
  ///
  /// UNREACHABLE, decided 2026-08-24 (Scope -> Provisioning, team plan) — superseding the
  /// paragraph above, which described the design that would have made this category fire in
  /// practice. The relation-capability assert this member names was always specific to the
  /// graph model (`lid.176.ftz` has no relations to assert), and the graph model no longer
  /// provisions through this seam at all: it is fetched the way the embedder already fetches
  /// its own model (`GLiNER.from_pretrained`, C1), with no post-download capability assert of
  /// any kind. Retained in the frozen enum for the same interface-stability reason as
  /// `conflicting_onnx_runtimes`, below — S45's revert-disposition switch and the Frontend
  /// Done-when's rendering obligation both still enumerate it.
  relations_not_supported,

  /// Two ONNX runtimes are importable at once (`onnxruntime` and `onnxruntime-gpu`). This is
  /// a probe FAILURE, never a swallowed exception (Q26) — the resolved session is
  /// unpredictable and any capability result from it is meaningless. Detected only; closing
  /// it at the source is explicitly out of scope for this feature (Q26). Occurs AFTER
  /// placement, during the same combined smoke-load/capability-assert step as
  /// `smoke_load_failed` — the capability assert never ran for this category either.
  ///
  /// Scope disposition post-spike-gate (Spike gate — RESOLVED 2026-08-24, team plan): this
  /// category is UNREACHABLE for both artifacts this seam provisions, mirroring how
  /// `license_declined` above stays a retained-but-unreachable member rather than a removed
  /// one. The graph model's smoke-load/capability-assert step loads through torch, not ONNX
  /// Runtime, and never creates an `onnxruntime.InferenceSession` — so there is no "resolved
  /// session" for two importable runtimes to make unpredictable, which is the only failure
  /// mechanism this category describes. `lid.176.ftz` never had an ONNX dimension either (see
  /// the provisioning-seam doc, below). `onnxruntime` itself is NOT dropped from the
  /// dependency set — `gliner` hard-depends on it, and `fastembed`'s own embedder still uses
  /// it — so a real two-runtime conflict remains a live environment state; it is simply not
  /// one either of THIS seam's two `provision()` targets can any longer surface, since neither
  /// creates the ONNX session this check exists to protect. Retained in the frozen enum for
  /// interface stability, not removed — widening or narrowing it still requires the S45 revert
  /// disposition and Frontend Done-when rendering obligation described above.
  ///
  /// UNREACHABLE FOR A SECOND, STRONGER REASON as of 2026-08-24: the paragraph above reasoned
  /// from "the graph model loads through torch, not ONNX" — true, but it also still assumed
  /// the graph model reached this seam's smoke-load/capability-assert step at all. It no
  /// longer does (Scope -> Provisioning, team plan): the graph model is fetched via
  /// `GLiNER.from_pretrained` (C1), entirely outside `provision()`. The conclusion is
  /// unchanged — still unreachable for both artifacts — but is now overdetermined rather than
  /// resting on the torch-vs-ONNX distinction alone.
  conflicting_onnx_runtimes,
}

model ProvisionResult {
  outcome: ProvisionOutcome;
  failureKind: ProvisionFailureKind | null;

  /// Absolute placement path under the data directory. Non-null whenever the artifact was
  /// actually published by atomic rename — including a POST-placement failure
  /// (`smoke_load_failed`, `relations_not_supported`, `conflicting_onnx_runtimes`), which
  /// leaves the placed tree in place (Data → "One durable record," K9). Null only for a
  /// failure that occurs BEFORE placement (`download_failed`, `digest_mismatch`,
  /// `size_mismatch`, `insufficient_disk`, `extract_failed`) — mirrors
  /// `capabilityStatusPath`'s null set below exactly (M12: the two fields previously
  /// disagreed on where the boundary sits).
  placedAt: string | null;

  /// ALWAYS `null` as of 2026-08-24 (Scope -> Provisioning, team plan): the capability assert
  /// this field reports on no longer runs for any artifact `provision()` handles — the graph
  /// model, the only artifact that ever had a device-placement dimension for it to run
  /// against, no longer reaches this seam at all. The paragraph below describes the design
  /// that would have populated it; retained as provenance and for interface stability, not as
  /// live guidance.
  ///
  /// Whether the placed artifact's forward pass returned relations. False is a failure,
  /// not a warning — a checkpoint that silently ignores relation labels must never be
  /// accepted as provisioned. Post-spike-gate (Spike gate — RESOLVED 2026-08-24, team plan),
  /// `true` is the expected steady-state result for this checkpoint — the assert reverts to
  /// its originally designed meaning, a correctness check catching a bad revision or a
  /// corrupted download, not a permanent universal failure (see `relations_not_supported`,
  /// above). Null, not false, when the capability assert never ran — a WIDER null set than
  /// `capabilityStatusPath`'s below: that field is non-null for `smoke_load_failed` and
  /// `conflicting_onnx_runtimes` because the sentinel still gets written for both; this field
  /// is null for them, because the assert that would populate it never ran. Non-null only
  /// whenever the assert actually executed and returned an answer — which requires BOTH that
  /// the outcome reached the assert step (`provisioned`, `already_present`, or a `failed`
  /// outcome whose `failureKind` is `relations_not_supported`) AND that the artifact has a
  /// device-placement dimension for the assert to run against (the graph model, loaded
  /// through torch — see the provisioning-seam doc, above). Null for the five pre-placement
  /// failures, for `smoke_load_failed` / `conflicting_onnx_runtimes` alike, and for
  /// `lid.176.ftz` regardless of outcome — the assert is a no-op for that shape.
  /// See `CapabilityStatusRecord` below for the sentinel's own copy of this question, which
  /// stays non-nullable and `false`-by-convention for those same two never-asserted categories
  /// — a different field, on disk, with a different tradeoff.
  relationsSupported: boolean | null;

  /// ALWAYS `null` as of 2026-08-24 (Scope -> Provisioning, team plan): the sentinel this
  /// field points at is never written any more — see `CapabilityStatusRecord`, below, which
  /// has no remaining producer. The paragraph below describes the design that would have
  /// populated it; retained as provenance and for interface stability, not as live guidance.
  ///
  /// Absolute path to the capability-assert sentinel file (`capability_status.json`, Data),
  /// written or refreshed by this call, beside the graph model under the same
  /// `get_graph_models_dir()` path — the durable record the background probe (S13, Q9) reads
  /// without re-running the assert. Null when `outcome` is `failed` before placement
  /// (`download_failed`, `digest_mismatch`, `size_mismatch`, `insufficient_disk`,
  /// `extract_failed`) — no artifact directory exists yet to write it beside — and null for
  /// `lid.176.ftz` regardless of outcome, since the assert is a no-op for an artifact with no
  /// device-placement dimension (see the provisioning-seam doc, above) and there is
  /// nothing to record. Non-null for the graph model's `smoke_load_failed` and
  /// `conflicting_onnx_runtimes` too, even though the capability assert never ran for either,
  /// and non-null for `already_present` — re-verification re-runs the assert and rewrites this
  /// file exactly as `provisioned` does — see `CapabilityStatusRecord`.
  capabilityStatusPath: string | null;

  /// ALWAYS `[]` as of 2026-08-24 (Scope -> Provisioning, team plan): this seam's sole
  /// remaining artifact, `lid.176.ftz`, never had a device-placement dimension to resolve, and
  /// the graph model — this field's real, non-empty producer under the design described
  /// below — no longer provisions through this seam at all; its own accelerator validation
  /// moved to a different mechanism (C5). The paragraph below describes the superseded design,
  /// retained for interface stability and provenance.
  ///
  /// The torch device (and, for CPU, thread configuration) the stage-2 smoke-load actually
  /// resolved to (Q7) — shape unchanged (`string[]`), but its source shifted post-spike-gate
  /// (Spike gate — RESOLVED 2026-08-24, team plan): ONNX Runtime execution providers gave way
  /// to torch device selection (`cpu` / `cuda` / `mps`) once the graph model shipped as a
  /// PyTorch checkpoint rather than an ONNX export. A step-down to CPU must still be visible
  /// here rather than inferred, mirroring `EngineCapability.activeProviders` (C1). The wizard
  /// reads this to decide whether to offer the accelerator
  /// (`ProviderOffer.accelerator_offered_default_on`, C5) and what to write into
  /// `[graph].providers`. Null, not empty, when no session resolved: the five pre-placement
  /// failures plus `smoke_load_failed` / `conflicting_onnx_runtimes`, where no load completed
  /// — the same seven categories `relationsSupported` above is null for. Non-null (a real
  /// resolved-device list) for the graph model's `provisioned`, `already_present` and
  /// `relations_not_supported` outcomes. Empty (`[]`), not a fabricated device name, for
  /// `lid.176.ftz` at any of those same three outcomes — it has no device-placement dimension
  /// to resolve against (see the provisioning-seam doc, above), and `candidateProviders`
  /// (below) is likewise always `[]` for it.
  activeProviders: string[] | null;
}

/// UNREACHABLE as of 2026-08-24 (Scope -> Provisioning, team plan) — RETAINED for interface
/// stability, following the same pattern this file already applies to
/// `ProvisionFailureKind.conflicting_onnx_runtimes` and (as an excluded candidate)
/// `license_declined`. This model has NO remaining producer: the graph model — the only
/// artifact whose outcome could ever reach the post-placement steps this sentinel records —
/// no longer provisions through this seam at all (it loads via `GLiNER.from_pretrained`, C1,
/// with no capability assert of any kind), and `lid.176.ftz` never had one even under the
/// original design (see below). Nothing in the codebase should construct or read this model
/// going forward; it is documented rather than deleted so a future reader who finds
/// `capability_status.json` mentioned elsewhere (S13, the Data section) has a defined shape to
/// consult, and so a reintroduced second provisioned artifact with a device-placement
/// dimension has a schema ready rather than needing one invented from scratch.
///
/// The on-disk schema of `capability_status.json` (Data, above; M8 — declared explicitly so
/// the sentinel has a defined shape rather than an implied bare boolean). Written once, at the
/// end of a `provision()` call for the graph model whose outcome reached the post-placement
/// steps (`smoke_load_failed`, `relations_not_supported`, `conflicting_onnx_runtimes`, or
/// success); never written for a pre-placement failure, since there is no artifact directory
/// to write it beside, and never written for `lid.176.ftz` at all — the capability assert that
/// would populate it is a no-op for that artifact (see the provisioning-seam doc, below).
model CapabilityStatusRecord {
  /// Populated even when `relationsSupported` below is not authoritative. This is what lets
  /// the background probe (S13) report the REAL cause instead of defaulting every
  /// post-placement failure to `relations_not_supported` — the collapse this field exists to
  /// prevent (closes the C4-I-107 gap: `conflicting_onnx_runtimes` and `smoke_load_failed`
  /// both fail after placement, before the capability assert runs, so a schema carrying only
  /// a boolean has no honest value to write for either).
  failureKind: ProvisionFailureKind | null;

  /// True only when the capability assert actually executed and returned relations — expected
  /// `true` for this checkpoint post-spike-gate (Spike gate — RESOLVED 2026-08-24, team plan;
  /// see `ProvisionResult.relationsSupported` and `relations_not_supported`, above, for the
  /// full disposition). When `failureKind` is `smoke_load_failed` or
  /// `conflicting_onnx_runtimes`, the assert never ran at all and this field is written
  /// `false` by convention, NOT as an authoritative "checkpoint has no relation heads" result
  /// — the background probe MUST render the recorded `failureKind` for those two categories,
  /// never infer from this boolean alone.
  relationsSupported: boolean;

  /// Mirrors `ProvisionResult.activeProviders` for the same call, null set included, and the
  /// same post-spike-gate semantic shift to torch device selection (`cpu` / `cuda` / `mps`)
  /// in place of ONNX execution providers — see that field's doc, above, for the full
  /// disposition. Read by the background probe (S13), which renders a device step-down entry
  /// in `provider_warnings` (C6) only when this recorded set is a step *down* from configured
  /// `[graph].providers` — a configured accelerator absent from the recorded set — never on
  /// the inverse, so an operator who declined the accelerator (configured CPU, recorded CPU)
  /// does not trip it: the one `provider_warnings` category with no other producer, since the
  /// probe itself never loads the engine (Q9). The comparison reads this provision-time
  /// snapshot only; a step-down that first appears at a later load
  /// (`EngineCapability.activeProviders`, C1) is not visible to the probe until the wizard
  /// re-runs `provision()`.
  activeProviders: string[] | null;

  /// The pinned revision this record was written for (`paths.py`'s single-owner constant,
  /// Q6). Not reachable through the normal publish path — a superseded revision's sentinel
  /// lives in a different `get_graph_models_dir()` directory the probe never reads, since the
  /// revision is the directory name itself. What it actually guards: a hand-copied artifact
  /// tree, the container workaround an operator uses to mount a host-provisioned artifact
  /// (Known limitations) — the one case where a stale sentinel from a different revision
  /// could physically sit beside a newer artifact. No scenario exercises this case; it is
  /// review-enforced only.
  artifactRevision: string;

  assertedAt: utcDateTime;
}

/// The provisioning seam. SCOPED TO `lid.176.ftz` ALONE as of 2026-08-24 (Scope -> Provisioning,
/// team plan) — retained below for that artifact and as provenance for the graph-model design
/// that was designed, then not taken. Steps remain ordered and none may be skipped: announce
/// size and license, download to a staging directory inside the target filesystem, verify the
/// digest AND the byte count, extract, fsync the tree, atomically publish by rename under the
/// artifact's own directory, fsync the parent directory. `smoke-load` is RETAINED in this list
/// for interface stability only — see `ProvisionFailureKind.smoke_load_failed`, above:
/// `lid.176.ftz` has no load-time capability to assert beyond "the bytes are the bytes we
/// pinned," which the digest-plus-byte-count check already covers, so this step currently has
/// nothing left to do for the sole artifact that reaches it, and produces no failure a caller
/// could observe.
///
/// SUPERSEDED, kept as provenance (Spike gate — RESOLVED 2026-08-24, then re-decided the same
/// day, team plan Scope -> Provisioning): an earlier resolution of the Spike gate had this seam
/// hosting and downloading the graph model as a TARBALL of the checkpoint's 7-file PyTorch
/// snapshot, verified and published exactly like `lid.176.ftz` — the checkpoint's PyTorch
/// weights are relation-capable (measured `archon-search --uses--> LanceDB` @ 0.9822,
/// `reranker --depends_on--> fastembed library` @ 0.9591); only the ONNX conversion had lost
/// that capability. That design was superseded before any of it shipped — the graph model now
/// uses the house `GLiNER.from_pretrained` lazy-load pattern instead (C1), never reaching this
/// seam. The `extract` step above predates both designs and was never exercised by either: a
/// single `model.onnx` never used it, `lid.176.ftz` is one file needing no extraction, and the
/// abandoned tarball design never shipped. It stays in the step list for interface stability,
/// same disposition as `smoke-load` above.
///
/// The relation-extraction capability assert and the `capability_status.json` sentinel it wrote
/// are GONE from this seam's live behaviour, not merely no-ops for one artifact: `lid.176.ftz`
/// never had a device-placement dimension to assert against (see `CapabilityStatusRecord`,
/// above, for the full disposition), and the graph model — the only artifact that ever did — no
/// longer provisions here at all. `provision()` returns `relationsSupported: null`,
/// `activeProviders: []` and `capabilityStatusPath: null` unconditionally now, not merely "for
/// `lid.176.ftz`" as an earlier draft of this comment scoped it.
///
/// Placement root: `lid.176.ftz` publishes under `get_fasttext_models_dir()` (Scope -> Paths).
/// `paths.get_graph_models_dir()` (Q6) is NO LONGER this seam's concern — C1 now passes it
/// directly to `GLiNER.from_pretrained` as `cache_dir`, outside `provision()` entirely, so the
/// role-named-not-vendor-named accessor still exists but this interface neither reads nor
/// writes it.
interface ArtifactProvisioner {
  /// Human-readable pre-download disclosure: real size and license.
  disclosure(spec: ArtifactSpec): string;

  /// Total planned download for a whole install — every provisioned-artifact spec in the
  /// profile plus every size-estimate spec from a selected extra. Every `declaredMb` is
  /// converted at `* BYTES_PER_MB` before summing with every `sizeBytes`, which is bytes
  /// throughout — the two units never mix past this point. AS SHIPPED that factor is
  /// `1_000_000`, DECIMAL megabytes, pinned once in `provisioning.py:42` and shared by the
  /// summing side (`prewarm.py:36-49`) and the rendering side (`render.py:107`), so
  /// `declared_mb * BYTES_PER_MB // BYTES_PER_MB` round-trips exactly. An earlier draft of
  /// this comment said `* 1024 * 1024` (binary MiB); it was wrong, and the two would disagree
  /// by ~5%. The sum, `totalBytes`, is NOT itself one of the three
  /// consumers (Q12) — it feeds three DERIVED, distinct quantities computed elsewhere: the
  /// disk guard's `requiredFreeBytes = totalBytes * 2` (no `ceil()` — `totalBytes` is already
  /// an integer sum, so rounding a whole number is a no-op), the service-ready timeouts, and
  /// the wizard's displayed figure. `plannedDownloadBytes` returns `totalBytes` only; it does
  /// not compute the three derived quantities itself.
  plannedDownloadBytes(artifactSpecs: ArtifactSpec[], sizeEstimates: SizeEstimateSpec[]): int64;

  /// Idempotent. Re-verifies an already-present artifact rather than trusting presence.
  /// SCOPED TO `lid.176.ftz` alone as of 2026-08-24 (Scope -> Provisioning, team plan; see the
  /// interface doc, above) — an earlier draft of this comment described a graph-model
  /// re-verification path (smoke-load plus capability assert re-run on every call reaching the
  /// post-placement steps, `capability_status.json` rewritten, `already_present` included)
  /// that never shipped: the graph model does not call `provision()`. For `lid.176.ftz`,
  /// re-verification is the digest-plus-byte-count check alone (BE-3) — there is no smoke-load
  /// or capability-assert stage left to re-run for it (see `ProvisionFailureKind`, above).
  /// Re-provisions (re-downloads) only when that check fails against the present file.
  /// `candidateProviders` (input) and `activeProviders` (on `ProvisionResult`) are RETAINED
  /// parameters with no non-empty producer left: they existed for stage 2 of the graph
  /// engine's provider probe (Q7), which validated an accelerator candidate against the graph
  /// model's own mandatory smoke-load — a mechanism this seam no longer performs for any
  /// artifact (see C5 for where that validation lives now). `lid.176.ftz` never had a device
  /// session to validate a candidate against, so both were already always `[]` for it; that is
  /// now the ONLY value either one ever takes, not one case among several. Retained on the
  /// signature for interface stability, not deleted.
  provision(spec: ArtifactSpec, candidateProviders: string[]): ProvisionResult;
}
