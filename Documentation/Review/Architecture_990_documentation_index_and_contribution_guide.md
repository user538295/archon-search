# Review: Architecture/990_documentation_index_and_contribution_guide.md

## Summary

This doc is the navigation index plus contribution rules for the `/Documentation/` tree. All referenced files exist at the stated paths and the one-liners are consistent with the linked filenames/topics. However, the doc's central self-imposed invariant — "every doc under `/Documentation/` is listed here" — is violated: `Architecture/220_accessibility_and_internationalization.md` exists on disk but is missing from the Architecture table. The contribution guide example then compounds this by claiming `220_…` is still available as "the next testing-adjacent doc" slot. A few smaller inconsistencies in the numbering-range narrative and the metadata-header format also surface.

## Inaccuracies (numbered)

1. **Line 10 (and Architecture table, lines 20–36): "Every doc under `/Documentation/` is listed here. If a doc is not in this index, it does not exist as far as readers are concerned."** The file `Documentation/Architecture/220_accessibility_and_internationalization.md` exists on disk but is **not** listed in the Architecture table. This both falsifies the invariant and orphans a real doc. (CLAUDE.md's "Documentation map" table also references it under `Architecture/220_accessibility_and_internationalization.md`, confirming it is intended to be a first-class architecture doc.)

2. **Line 136 (Contribution guide step 2): "extend the existing numeric range with a 10-step gap (e.g. next testing-adjacent doc → `220_…`)."** `220` is already taken (see #1). The example slot is wrong — the next free testing-adjacent slot would be `230_…`.

3. **Line 11: "`000`–`099` are foundations, `100`–`299` are architecture, `500`–`599` are workflows, `600` is reference, `990` is meta."** Two narrow issues:
   - `530_technical_debt_refactoring_roadmap.md` sits in the `500`–`599` range labelled "workflows" but is in substance a debt register, not a workflow doc. The labelling table at lines 158–163 then re-classifies `500`–`699` together as "Technical reference" (bi-annual cadence) — i.e. the doc internally disagrees with itself about what `500`–`599` means.
   - The range `300`–`499` is unallocated and unmentioned, which is fine, but the bracketing of `100`–`299` as "architecture" is loose: only `100`–`220` are actually used and `200`–`220` are testing/perf/a11y, which the cadence table separates from architecture proper only by being in the same Quarterly bucket.

4. **Lines 1–4 (this doc's own metadata header) vs. line 137–143 (the rule it sets):** The contribution guide mandates a plain 4-line header:
   ```
   Purpose: <one sentence>
   Audience: <who reads this>
   Status: Draft | Active | Deprecated
   Last reviewed: YYYY-MM-DD / Next review: YYYY-MM-DD
   ```
   This file's own header uses bolded labels (`**Purpose**:` etc.) and places `Last reviewed` and `Next review` on the same line in a format that matches but with markdown bold. Either the example rule should permit bolding or this file should match the rule.

5. **Line 14: "Architecture quarterly, technical bi-annual, process annual — see [Maintenance and review cadence]."** The cadence table at lines 158–163 actually defines three buckets keyed by **numeric range** (`100`–`299` Architecture, `500`–`699` Technical reference, `000`–`099` + `990` Process and meta). The narrative summary at line 14 omits ADRs, which are listed in the same cadence table with the distinct rule "Reviewed only when superseded; never edited after acceptance". Minor — the principle gloss is incomplete relative to the table it cites.

6. **Line 144: "PRs that add docs without updating this index will not be merged."** This is a process claim with no enforcement mechanism visible in `.github/workflows/` (only `archon-search-pr.yml` and `archon-search-release.yml`, neither of which lint the index). Not factually false as a policy, but unverifiable as a hard gate — flagging as aspirational rather than enforced.

## Verified claims

- All 17 Architecture files listed (000, 010, 100, 110, 120, 130, 140, 150, 160, 200, 210, 500, 510, 520, 530, 600, 990) exist at the stated paths.
- All 7 UserManual files (`01_installation.md` … `07_troubleshooting.md`) exist.
- All 6 MigrationGuide files exist.
- All 7 DeveloperGuide files exist.
- All 6 OperatorGuide files exist.
- All 5 ADR files exist with the stated filenames.
- All 6 SecurityGuide files exist.
- All 3 Backlog files exist.
- Root-level `roadmap.md`, `quick_start.md`, and `../../BREAKING.md` (i.e. `/BREAKING.md` at repo root) all exist.
- UserManual `05_searching.md` one-liner mentions "the nine MCP tools": verified against `archon_search/server/mcp.py` — exactly 9 tool functions are registered (`search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`), matching the CLAUDE.md statement.
- `BREAKING.md` exists at the repo root and is in fact the "compatibility contract" — its own first heading and policy paragraph confirm the role described at line 129.
- The release tooling (`release.sh`) and CI workflow (`.github/workflows/archon-search-release.yml`) referenced indirectly by the `510_release_and_environment_strategy.md` row exist.
- Title-vs-one-liner topic match: each Architecture row's one-liner is consistent with the filename's slug. Same for ADRs, MigrationGuide, DeveloperGuide, OperatorGuide, SecurityGuide, UserManual, Backlog.
- ADR-append-only rule (line 163) is consistent with CLAUDE.md's "ADRs are append-only — supersede with a new ADR rather than editing accepted ones."

## Unverifiable / ambiguous

- **`Documentation/Completed/` directory** exists on disk and is empty. It is not mentioned by the index. Whether that is an orphan that should be listed, an intentional staging area, or vestigial is unclear from the doc alone.
- **Line 12: "Source of truth is code. Docs explain intent and trade-offs; they never replace `/openapi.json`, `BREAKING.md`, or the test suite."** Consistent with CLAUDE.md but stated as a normative principle, not a verifiable fact about the codebase.
- **Owners in the cadence table** ("Maintainer of the affected module", "Whoever last shipped a related change", "Project lead") are role labels with no CODEOWNERS file in the repo to bind them — unverifiable.
- **The `Status: Draft | Active | Deprecated` enum** prescribed at line 141 is asserted as the only allowed values; whether every existing doc obeys it requires a separate sweep and is out of scope here.
- **"3–5 principles up front" (line 137)** — the rule says "then 3–5 principles up front", but this index doc itself has 5 "Guiding principles" (lines 10–14), which fits. Whether all other Architecture docs comply is unverifiable from this file alone.
