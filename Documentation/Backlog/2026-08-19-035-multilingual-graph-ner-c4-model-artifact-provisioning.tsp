// C4 — Generic model-artifact provisioning (internal logical seam).
//
// Layer edge: Presentation (wizard prompts) -> Frameworks & Drivers (installer package).
// Replaces the spaCy-wheel-specific step (compatibility-table lookup -> GitHub wheel URL ->
// unzip -> wheel-layout assert -> smoke-load), which verifies no digest at all.
//
// Layering constraint inherited from the code it replaces, stated as a MODULE-level rule, not
// a layer-level one (M3): `ProseExtractionBackend` sits in Frameworks & Drivers, the plan's
// Layer map — the same layer the installer package itself occupies — so a layer-level
// justification could not discriminate between the two modules the installer imports and the
// one it must not. The installer package must NOT import `archon_search.graph_extractor` — the
// Interface Adapters orchestrator that wraps the backend (`GraphExtractor` is Interface
// Adapters, per the plan's Layer map — not Use Cases). The backend module itself
// (`ProseExtractionBackend`, `archon_search/prose_extraction_backend.py`, C1) IS importable
// from the installer; the provisioner calls it directly for the smoke-load and capability
// assert.
//
// TWO descriptor shapes, not one (Q12) — see `ArtifactSpec` and `SizeEstimateSpec` below.
//
// The pinned model+revision has a SINGLE OWNER: `paths.py` holds the pinned revision as a
// module constant alongside `get_graph_models_dir()` (Q6). `ArtifactSpec.revision` is
// populated FROM that same constant, not declared as its own copy — imported, not duplicated.
// That single-owner guarantee covers the revision element only (K8): `url`, `sha256` and
// `sizeBytes` are separate hand-pinned constants on the installer side, not derived from the
// revision constant, so S54 additionally asserts `url` structurally contains the pinned
// revision string — the four move together by construction, not by discipline alone.

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

  /// Upstream revision the URL was pinned at. Imported from `paths.py`'s single-owner module
  /// constant (Q6) — never declared as an independent copy on this descriptor.
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

  /// SPDX license identifier of the checkpoint, surfaced before download.
  /// `Apache-2.0` for the pinned artifact (Q15), which under the rule below is
  /// disclosure-only — there is no accept prompt and therefore no way to decline into a
  /// deployment with no graph engine at all.
  license: string;

  /// How the license is handled at install time. Set from the ONE rule that now lives in
  /// `install/licenses.py` (Q15), replacing the per-model ad-hoc gates: a license that is
  /// RESTRICTIVE ON USE (e.g. CC-BY-NC-4.0, non-commercial) prompts for acceptance;
  /// everything else is disclosed and nothing is gated.
  licenseDisposition: LicenseDisposition;

  /// Attribution text we are obliged to ship next to the artifact because we REDISTRIBUTE it
  /// (we self-export the ONNX and host the bytes). The LICENSE file and this attribution
  /// travel with the placed artifact, not just with the source tree.
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
  smoke_load_failed,

  relations_not_supported,

  /// Two ONNX runtimes are importable at once (`onnxruntime` and `onnxruntime-gpu`). This is
  /// a probe FAILURE, never a swallowed exception (Q26) — the resolved session is
  /// unpredictable and any capability result from it is meaningless. Detected only; closing
  /// it at the source is explicitly out of scope for this feature (Q26). Occurs AFTER
  /// placement, during the same combined smoke-load/capability-assert step as
  /// `smoke_load_failed` — the capability assert never ran for this category either.
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

  /// Whether the placed artifact's forward pass returned relations. False is a failure,
  /// not a warning — a checkpoint that silently ignores relation labels must never be
  /// accepted as provisioned. Null, not false, when the capability assert never ran — a
  /// WIDER null set than `capabilityStatusPath`'s below: that field is non-null for
  /// `smoke_load_failed` and `conflicting_onnx_runtimes` because the sentinel still gets
  /// written for both; this field is null for them, because the assert that would populate
  /// it never ran. Non-null only whenever the assert actually executed and returned an answer —
  /// which requires BOTH that the outcome reached the assert step (`provisioned`,
  /// `already_present`, or a `failed` outcome whose `failureKind` is `relations_not_supported`)
  /// AND that the artifact has an ONNX execution-provider dimension for the assert to run
  /// against (the graph model). Null for the five pre-placement failures, for
  /// `smoke_load_failed` / `conflicting_onnx_runtimes` alike, and for `lid.176.ftz` regardless
  /// of outcome — the assert is a no-op for that shape (see the provisioning-seam doc, above).
  /// See `CapabilityStatusRecord` below for the sentinel's own copy of this question, which
  /// stays non-nullable and `false`-by-convention for those same two never-asserted categories
  /// — a different field, on disk, with a different tradeoff.
  relationsSupported: boolean | null;

  /// Absolute path to the capability-assert sentinel file (`capability_status.json`, Data),
  /// written or refreshed by this call, beside the graph model under the same
  /// `get_graph_models_dir()` path — the durable record the background probe (S13, Q9) reads
  /// without re-running the assert. Null when `outcome` is `failed` before placement
  /// (`download_failed`, `digest_mismatch`, `size_mismatch`, `insufficient_disk`,
  /// `extract_failed`) — no artifact directory exists yet to write it beside — and null for
  /// `lid.176.ftz` regardless of outcome, since the assert is a no-op for an artifact with no
  /// ONNX execution-provider dimension (see the provisioning-seam doc, above) and there is
  /// nothing to record. Non-null for the graph model's `smoke_load_failed` and
  /// `conflicting_onnx_runtimes` too, even though the capability assert never ran for either,
  /// and non-null for `already_present` — re-verification re-runs the assert and rewrites this
  /// file exactly as `provisioned` does — see `CapabilityStatusRecord`.
  capabilityStatusPath: string | null;

  /// The ONNX execution providers the stage-2 smoke-load actually resolved to (Q7) — a
  /// step-down to CPU must be visible here rather than inferred, mirroring
  /// `EngineCapability.activeProviders` (C1). The wizard reads this to decide whether to
  /// offer the accelerator (`ProviderOffer.accelerator_offered_default_on`, C5) and what to
  /// write into `[graph].providers`. Null, not empty, when no session resolved: the five
  /// pre-placement failures plus `smoke_load_failed` / `conflicting_onnx_runtimes`, where no
  /// load completed — the same seven categories `relationsSupported` above is null for.
  /// Non-null (a real resolved-provider list) for the graph model's `provisioned`,
  /// `already_present` and `relations_not_supported` outcomes. Empty (`[]`), not a fabricated
  /// provider name, for `lid.176.ftz` at any of those same three outcomes — it has no ONNX
  /// session to resolve providers for (see the provisioning-seam doc, above), and
  /// `candidateProviders` (below) is likewise always `[]` for it.
  activeProviders: string[] | null;
}

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

  /// True only when the capability assert actually executed and returned relations. When
  /// `failureKind` is `smoke_load_failed` or `conflicting_onnx_runtimes`, the assert never
  /// ran at all and this field is written `false` by convention, NOT as an authoritative
  /// "checkpoint has no relation heads" result — the background probe MUST render the
  /// recorded `failureKind` for those two categories, never infer from this boolean alone.
  relationsSupported: boolean;

  /// Mirrors `ProvisionResult.activeProviders` for the same call, null set included — read by
  /// the background probe (S13), which renders an execution-provider step-down entry in
  /// `provider_warnings` (C6) only when this recorded set is a step *down* from configured
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

/// The provisioning seam, generic across artifact shapes (Q12: the graph model and
/// `lid.176.ftz`). Steps are ordered and none may be skipped: announce size and license,
/// download to a staging directory inside the target filesystem, verify the digest AND the
/// byte count, extract, fsync the tree, atomically publish by rename under the artifact's own
/// directory, fsync the parent directory, smoke-load (the per-artifact layout assert runs
/// INSIDE the smoke-load, not as a separate step). The relation-extraction capability assert,
/// and the `capability_status.json` sentinel it writes, apply only to an artifact with an ONNX
/// execution-provider dimension — the graph model. `lid.176.ftz` has none, so both are no-ops
/// for it: `provision()` returns `relationsSupported: null`, `activeProviders: []` and
/// `capabilityStatusPath: null` for it regardless of outcome; smoke-load alone (already
/// existing behaviour) re-verifies its presence.
///
/// For the graph model, placement root is `paths.get_graph_models_dir()` (Q6) — a ROLE-named
/// accessor in `paths.py`, not a vendor-named one — laid out as
/// `<data>/models/graph/<model>-<revision>/`; `lid.176.ftz` publishes under
/// `get_fasttext_models_dir()` instead (Scope → Paths).
interface ArtifactProvisioner {
  /// Human-readable pre-download disclosure: real size and license.
  disclosure(spec: ArtifactSpec): string;

  /// Total planned download for a whole install — every provisioned-artifact spec in the
  /// profile plus every size-estimate spec from a selected extra. Every `declaredMb` is
  /// converted at `* 1024 * 1024` (binary megabytes, matching `prewarm.py:36`'s existing
  /// factor) before summing with every `sizeBytes`, which is bytes throughout — the two units
  /// never mix past this point. The sum, `totalBytes`, is NOT itself one of the three
  /// consumers (Q12) — it feeds three DERIVED, distinct quantities computed elsewhere: the
  /// disk guard's `requiredFreeBytes = totalBytes * 2` (no `ceil()` — `totalBytes` is already
  /// an integer sum, so rounding a whole number is a no-op), the service-ready timeouts, and
  /// the wizard's displayed figure. `plannedDownloadBytes` returns `totalBytes` only; it does
  /// not compute the three derived quantities itself.
  plannedDownloadBytes(artifactSpecs: ArtifactSpec[], sizeEstimates: SizeEstimateSpec[]): int64;

  /// Idempotent. Re-verifies an already-present artifact rather than trusting presence: for
  /// the graph model, both the smoke-load and the capability assert re-run, and
  /// `capability_status.json` is rewritten, on every call that reaches the post-placement
  /// steps — `already_present` included — so a recorded `relations_not_supported` or
  /// `conflicting_onnx_runtimes` outcome is not permanent by construction; re-running the
  /// wizard is a real remedy. For `lid.176.ftz`, only the smoke-load re-runs — the capability
  /// assert is a no-op for it (see the provisioning-seam doc, above). Re-provisions
  /// (re-downloads) only when the present tree fails its smoke-load.
  /// `candidateProviders` is the accelerator candidate list stage 2 of the provider probe
  /// (Q7) validates against — the same smoke-load this call already performs; the result's
  /// `activeProviders` reports what actually resolved. Empty (`[]`) for `lid.176.ftz`, which
  /// has no ONNX session to validate a provider candidate against.
  provision(spec: ArtifactSpec, candidateProviders: string[]): ProvisionResult;
}
