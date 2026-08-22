// C4 — Generic model-artifact provisioning (internal logical seam).
//
// Layer edge: Presentation (wizard prompts) -> Frameworks & Drivers (installer package).
// Replaces the spaCy-wheel-specific step (compatibility-table lookup -> GitHub wheel URL ->
// unzip -> wheel-layout assert -> smoke-load), which verifies no digest at all.
//
// Layering constraint inherited from the code it replaces: the installer package must NOT
// import the graph layer. Artifact name, pinned URL, revision, and digest therefore live on
// the installer side, duplicated deliberately rather than imported.

namespace ModelArtifactProvisioning;

/// The pinned identity of one downloadable artifact. Everything here is a constant in the
/// installer package — nothing is resolved from a remote compatibility table at run time.
model ArtifactSpec {
  /// Stable logical name, used for the on-disk directory and for operator-facing messages.
  name: string;

  /// Fully pinned download URL.
  url: string;

  /// Upstream revision the URL was pinned at.
  revision: string;

  /// Expected digest of the downloaded bytes, verified before anything is placed.
  sha256: string;

  /// Exact download size in bytes (Q12). Three rules, all load-bearing:
  ///   - GENERATED from the artifact we host, alongside the digest — never hand-typed and
  ///     never read off a model card.
  ///   - ASSERTED against the bytes actually received, exactly like the digest. A size that
  ///     does not match is a provisioning failure, not a rounding difference.
  ///   - Feeds the disk-space guard, the service-ready timeout AND the display, from this
  ///     one field. The install-time planned-download total is the sum over the profile plus
  ///     every selected extra, which is what closes the existing undercount — the tree-sitter
  ///     bundle and `lid.176.ftz` gain the same descriptor treatment in this change.
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

  /// A usable artifact was already present; presence was re-verified by smoke-load.
  already_present,

  /// Download, digest, extraction, smoke-load or capability assert failed. Non-fatal to the
  /// wizard run: graph ingest degrades to code symbols only until the wizard is re-run.
  failed,
}

/// Why a provision attempt failed. Sanitized categories, never an exception message —
/// operator remedies differ per category.
enum ProvisionFailureKind {
  download_failed,
  digest_mismatch,

  /// Bytes received did not match `sizeBytes`. Distinct from `digest_mismatch` so a
  /// truncated transfer reads differently from a tampered or re-pointed artifact.
  size_mismatch,

  extract_failed,
  smoke_load_failed,
  relations_not_supported,
  insufficient_disk,

  /// Reachable only for a `prompt_for_acceptance` artifact. The pinned graph artifact is
  /// Apache-2.0 / `disclose_only`, so this category can never fire for it — kept because the
  /// provisioner is generic and the fasttext and Jina gates share the same rule.
  license_declined,

  /// Two ONNX runtimes are importable at once (`onnxruntime` and `onnxruntime-gpu`). This is
  /// a probe FAILURE, never a swallowed exception (Q26) — the resolved session is
  /// unpredictable and any capability result from it is meaningless.
  conflicting_onnx_runtimes,
}

model ProvisionResult {
  outcome: ProvisionOutcome;
  failureKind: ProvisionFailureKind | null;

  /// Absolute placement path under the data directory, when the outcome is not `failed`.
  placedAt: string | null;

  /// Whether the placed artifact's forward pass returned relations. False is a failure,
  /// not a warning — a checkpoint that silently ignores relation labels must never be
  /// accepted as provisioned.
  relationsSupported: boolean;
}

/// The provisioning seam. Steps are ordered and none may be skipped: announce size and
/// license, download to a staging directory inside the target filesystem, verify the digest
/// AND the byte count, extract, fsync the tree, atomically publish by rename, fsync the
/// parent directory, smoke-load, then assert relation-extraction capability.
///
/// Placement root is `paths.get_graph_models_dir()` (Q6) — a ROLE-named accessor in
/// `paths.py`, not a vendor-named one — laid out as
/// `<data>/models/graph/<model>-<revision>/`. The per-artifact layout assert runs INSIDE the
/// smoke-load, not as a separate step.
interface ArtifactProvisioner {
  /// Free-space requirement for this artifact, so the disk guard accounts for it.
  /// Derived from `spec.sizeBytes`; never estimated independently.
  requiredBytes(spec: ArtifactSpec): int64;

  /// Human-readable pre-download disclosure: real size and license.
  disclosure(spec: ArtifactSpec): string;

  /// Total planned download for a whole install — the profile's models plus every selected
  /// extra's artifacts. This is the number the disk guard, both timeouts and the wizard's
  /// display all read (Q12).
  plannedDownloadBytes(specs: ArtifactSpec[]): int64;

  /// Idempotent. Re-verifies an already-present artifact rather than trusting presence,
  /// and re-provisions when the present tree fails its smoke-load.
  provision(spec: ArtifactSpec): ProvisionResult;
}
