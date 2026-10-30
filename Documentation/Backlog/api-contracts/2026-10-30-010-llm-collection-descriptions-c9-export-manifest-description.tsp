// C9 — Export manifest version 2, description fields (internal logical seam).
//
// Layer edge: export job (writes the manifest) and import job (reads it) -> the archive format.
//
// The export schema version goes from 1 to 2 (a decision recorded in the brief;
// only `description_source` is actually new, because version 1 manifests already carry `description`,
// routes_export.py line 244). Manifest v2 carries `description` and
// `description_source`. Version-1 archives, including every existing backup (the automatic backup
// loop writes the same archives), are refused on import
// unless `ignore_schema_version` is set (REST import, `routes_export.py` lines 334 and 1049, and the
// MCP import, `mcp.py` line 1872, all compare for strict equality); that flag only skips the version
// comparison, the embedding-model check is not bypassable. Restoring a pre-upgrade backup therefore
// needs the flag; this goes into BREAKING.md and the backup and restore operator guide. No
// dual-format code.
// Import rules: a description without a source in a bypassed v1 archive imports as `generated`; an
// archive with source `manual` stays manual and protected. An imported description gets status `ok`.
// Validation: an archive is untrusted input and a v1 archive carries legacy text, so the imported
// description passes the same limits as a manual one BEFORE the write: one line of at most 600
// characters (`maxDescriptionChars`, C4). Because refusing would make an old backup unrestorable,
// import normalises instead of rejecting: control characters and every run of whitespace (line
// breaks included) collapse to one space, the text is trimmed to the last full sentence within 600
// characters (the generation rule, C2), and a result that is empty after that imports as no
// description (the collection restores without one, with a WARNING naming the collection). The
// router additionally escapes `<` and `>` and collapses whitespace when it renders any description,
// so legacy rows written before this rule cannot forge a `- name:` row either (S77, S83).
// Import writes the description through the shared write operation (C11): that write refreshes the
// routing vector itself with the COLLECTION's embedder (the import already holds it), before the
// metadata recompute for a non-empty archive (the recompute then carries the vector as read, C11) or
// on its own for an empty one.
// The baseline stays unset, so a later qualifying ingest may regenerate `generated` text, never
// `manual`. Older servers ignore unknown manifest keys (by reading the code; untested).
//
// Logical only.

import "./2026-10-30-010-llm-collection-descriptions-c3-description-state.tsp";
using DescriptionStateContract;

namespace ExportManifestDescriptionContract;

/// The only supported value on import without the bypass flag.
const manifestSchemaVersion = 2;

model ManifestDescription {
  schema_version: int32;
  description: string;
  /// Absent in a bypassed v1 archive, which then imports as `generated`.
  description_source?: DescriptionSource;
}

enum ImportVersionOutcome {
  accepted,
  /// A v1 archive without `ignore_schema_version`.
  refusedOldVersion,
  /// A v1 archive with the flag: version comparison skipped, other checks stay.
  acceptedWithBypass,
}
