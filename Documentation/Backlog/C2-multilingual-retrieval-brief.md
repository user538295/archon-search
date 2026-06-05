# Feature Brief: Multilingual Retrieval

## Problem
Users with non-English or mixed-language corpora get degraded retrieval quality and cannot filter by language, because the system defaults to English-only embeddings and the language filter is hard-blocked in `filters.py` and `store_filters.py` pending C2.

## Goal
Users can ingest multilingual documents that are automatically tagged with their detected language, search results can be filtered by language via the `language` filter field, and non-English corpora receive better embedding quality through multilingual models. Success on the embedding quality goal requires measurable retrieval lift: recall@5 on non-English eval fixtures must exceed the English-only model baseline on the same fixtures by a documented margin defined during the spike/eval phase.

## Users & Context
Operators running archon-search on corpora that contain documents in languages other than English — technical docs in German, support tickets in French, research papers across languages — who want either filtered results (`language=fr`) or simply better ranking quality on non-English text.

## Core Flow
1. Operator installs archon-search with `--multilingual` flag (or sets it in TOML); this requires `--force --delete-db` if switching from an existing English profile, which destroys and requires re-ingestion of all indexed data
2. Install downloads `lid.176.ftz` fasttext model to `~/.archon-search/models/` and prompts for CC-BY-SA 3.0 model license acceptance (consistent with the existing Jina license gate pattern)
3. Operator ingests documents — `pipeline.py` (after parsing, before chunking) detects language per document using fasttext, propagates the tag to all chunks from that document; both `pipeline.py` and `chunker.py` need changes for propagation
4. fasttext returns `__label__xx` codes; the `__label__` prefix is stripped and codes are normalized to ISO 639-1 where available (ISO 639-3 otherwise); the normalized code is stored in `ChunkRecord.language`
5. Operator queries with optional `language=fr` filter on a **single-collection** query — returns only French-tagged chunks; without the filter, all language states are returned. Multi-collection fan-out queries do not support filters in v1 — language filter has no effect there.
6. For existing collections on a fresh install (or after re-ingest), language tags are populated automatically during normal ingest
7. FTS tokenization is language-aware if the LanceDB/Tantivy spike (first implementation task) confirms Python API support; otherwise FTS ships unchanged

## In Scope
- fasttext `lid.176.ftz` model download at `archon-search install` when `multilingual = true`; CC-BY-SA 3.0 license acceptance required at install (interactive prompt with `--accept-fasttext-license` flag for non-interactive mode, consistent with `_prompt_jina_license` pattern in `install.py`)
- Extend `_prewarm_models` in `install.py` to download `lid.176.ftz` to `~/.archon-search/models/` — this download hook does not currently exist
- Use `fasttext-wheel` (pre-built wheels, avoids C++ compilation) as the dependency; add as a new `[multilingual]` optional extra in `pyproject.toml`: `pip install archon-search[multilingual]`
- Document-level language detection in `pipeline.py` (after parse, before chunk); language detection runs via `run_in_executor` to avoid blocking the asyncio event loop; language tag propagated to chunks by passing it through `DocumentChunker.chunk()` (requires adding a `language` parameter to `chunker.py`) and assigning it in `pipeline.py:ingest_file` post-chunk loop
- fasttext `__label__` prefix stripped; code normalized to ISO 639-1 (2-letter) where fasttext maps it; ISO 639-3 (3-letter) used for languages with no ISO 639-1 equivalent
- Confidence-score threshold (not character count) for `unknown` tagging: documents whose fasttext top-1 confidence is below a configurable threshold (default 0.7) are tagged `unknown`
- Populate the `language` field in `ChunkRecord`; three-state contract: `""` = never processed (legacy), `"unknown"` = processed but confidence below threshold, specific code = detected. **The `store.py` read path currently coerces `"" → None` — it must be updated to preserve `""` as a distinct value so legacy data is distinguishable from `None`/missing.**
- Unlock `SearchFilters.language` in `filters.py` **and** add SQL clause generation to `build_where` in `store_filters.py` (both files currently block the filter)
- `language=fr` returns only chunks tagged `fr`; excludes `unknown` and `""`; `language=unknown` is a valid filter value; no language filter returns all three states
- Add startup check: if `multilingual = true` and `fasttext-wheel` is not installed, server must fail at startup with a clear error distinguishing "package missing" from "model file missing"
- Update MCP tool descriptions for `search` and `search_with_context` in `mcp.py` to reflect that `language` is now a valid, filterable parameter (single-collection only)
- Add `language_filter_used: bool` to `FilterFlags` in `telemetry/entry.py`
- Language-aware FTS tokenization — **gated on spike**: first implementation task must confirm whether LanceDB's Python API exposes Tantivy's language tokenizer configuration; if it does not, this sub-feature is dropped without workarounds
- When `multilingual = true`, language detection is active; when `multilingual = false`, language detection is skipped — no separate config key
- Multilingual eval fixtures (minimum one non-English language, e.g., French or German) and updated `thresholds.toml` required before landing; a before/after comparison on non-English recall@5 must be documented in the PR

## Out of Scope
- **Per-chunk language detection** — all chunks in a document share the document's detected language; mixed-language documents are an accepted v1 limitation
- **Per-query model routing** — embedding model stays at collection level; language detection does not dynamically switch models at search time
- **Chunk-level model routing** — requires restructuring the LanceDB schema (breaks vector dimension consistency across chunks in a table); multi-sprint rewrite, not this feature
- **Operator UI language / i18n** — archon-search remains English-only for all operator-facing strings, logs, CLI, and error messages
- **Language-based collection routing** — using the multi-collection router to prefer language-matched collections for a given query; natural follow-up once language tags are populated
- **Automatic reindex trigger on profile switch** — auto-detection of config drift is new infrastructure; out of scope for this feature
- **Language filter for multi-collection fan-out queries** — filters are blocked for multi-collection search in v1 (`routes_search.py`); language filter inherits this limitation

## Key Decisions
- **Per-document detection, not per-chunk**: More text = more accurate detection. All chunks in a document share the majority language. Keeps collection-level model assumption intact. Mixed-language documents are an accepted v1 limitation.
- **Detection in `pipeline.py` after parse, before chunk**: `parser.py` returns plain `str` — adding language to its return type would break every caller. Detection belongs in the orchestration layer where metadata can be attached before the chunk pipeline runs.
- **`fasttext-wheel` as optional dependency**: Avoids C++ compilation requirement across platforms. Optional extra (`[multilingual]`) avoids imposing the dependency on English-only users.
- **fasttext downloaded at install, stored at `~/.archon-search/models/lid.176.ftz`**: Consistent with fastembed model management pattern. Server runtime stays air-gappable after install — model file must be copied manually for air-gapped deployments.
- **Confidence-score threshold, not character count**: fasttext returns a probability with each prediction. A configurable confidence threshold (default 0.7) is more accurate than a fixed character-count heuristic.
- **Profile switch = re-install + re-ingest, not reindex**: The existing `_check_reinstall_guard` in `install.py` requires `--force --delete-db` for embedding model changes. This is destructive: all existing indexed data is deleted and must be re-ingested. There is no incremental re-embedding path. Operators must be warned of this at install time.
- **FTS tokenization gated on spike**: LanceDB exposes Tantivy under the hood but the Python API may not surface language tokenizer configuration. Spike is the first implementation task. If it fails, FTS ships unchanged — do not spend time on workarounds.
- **Three-state language contract**: `""` = legacy untagged (never processed), `"unknown"` = processed but confidence below threshold, specific ISO code = detected. The `store.py` read path must preserve `""` as a distinct value (currently collapsed to `None`). `language=fr` excludes both `""` and `"unknown"`.
- **No separate `language_detection` config key**: Language detection is active when `multilingual = true`. Coupling is intentional — operators who want multilingual embeddings almost universally want language tagging.

## Edge Cases & Constraints
- **Confidence threshold for `unknown` tagging**: fasttext confidence below 0.7 (configurable) results in `unknown` tag. Short texts, heavily code-mixed text, and transliterated content are the primary cases.
- **Three-state filter behavior**: `language=fr` returns only `fr`-tagged chunks. `language=unknown` returns only `unknown`-tagged chunks. No filter returns all chunks including `""` legacy data.
- **Language filter only works for single-collection queries**: Multi-collection fan-out queries reject filters in v1 — document this clearly in operator-facing docs and MCP tool descriptions.
- **Pre-existing data**: Legacy collections have `language = ""` on all chunks. Language filter on such collections will return zero results for any specific language query. Operators must re-ingest (not reindex — the model switch requires `--force --delete-db`). The `/status` endpoint should surface a warning when `multilingual = true` but a collection contains chunks with `language = ""`.
- **Profile switch is destructive**: Switching from English to multilingual profile requires `archon-search install --multilingual --force --delete-db`. All indexed data is lost and must be re-ingested. This is not a reindex — it is a full re-install. Warn prominently at install time.
- **Mixed-language documents**: A single detected language is assigned to the document. Minority-language chunks will be tagged with the majority language. Accepted limitation for v1.
- **fasttext model absent at runtime**: If `lid.176.ftz` is missing and `multilingual = true`, ingest must fail immediately with a clear error message pointing to `archon-search install`.
- **fasttext package not installed**: If `archon-search[multilingual]` was not installed but `multilingual = true`, server startup must fail with a clear error distinguishing "package missing" from "model file missing."
- **fasttext in async hot path**: All fasttext inference must run in a thread via `asyncio.get_event_loop().run_in_executor()` — the same pattern used by `Embedder` in `embedder.py`. Failure to do this blocks the event loop during watched-file ingest.
- **FTS spike failure**: If LanceDB does not expose language tokenizer configuration via its Python API, drop FTS tokenization from this feature. Document it in `BREAKING.md` as future work. Do not ship a workaround.
- **Eval harness — English-only baseline**: Existing fixtures in `tests/eval/` are English-only. Multilingual fixtures (minimum one language) and updated `thresholds.toml` entries are required before the feature can land. A before/after recall@5 comparison on non-English content must be documented in the PR.

## Open Questions
- What confidence threshold (default 0.7 proposed) is the right cutoff for `unknown` tagging? Should it be per-language or global?
- Does LanceDB's Python API expose Tantivy's language tokenizer configuration? (Resolved by the spike — first implementation task.)
- What recall@5 improvement over the English-only model baseline is the minimum bar for the embedding quality goal? (Defined during spike/eval phase.)

## Future Iterations
- **Auto-reindex on profile switch**: Detect config drift by comparing `active_embedding_model` (from `collection_meta.py`) against configured model at startup; enqueue reindex jobs automatically with restart-safe checkpointing — requires incremental re-embedding infrastructure that does not currently exist
- **Language filter for multi-collection fan-out**: Requires lifting the v1 filter restriction on multi-collection search paths
- **Language-based collection routing**: Router prefers language-matched collections for a given query's detected language
- **Query language detection**: Auto-detect query language at search time; boost or filter results toward matching-language documents without requiring an explicit `language` filter
- **Language-specific chunking**: Character-based chunking for CJK scripts vs. word-based for Latin scripts (current GPT-2 BPE tokenizer under-segments non-Latin scripts, producing shorter semantic chunks)
- **Language-aware FTS tokenization** (if dropped from this iteration due to spike failure)
- **Per-chunk language detection**: Only worthwhile if mixed-language documents become a first-class use case

## Recommendation
This is the right feature to build now — the schema plumbing is done, multilingual model profiles exist, and the filter is hard-blocked with a C2 reference in both `filters.py` and `store_filters.py`. The key constraint operators must understand: switching to multilingual embeddings is destructive — it requires `--force --delete-db` and full re-ingestion, not a simple reindex. The language filter is also limited to single-collection queries in v1. The thing that must not be compromised is the eval gate: do not ship without multilingual fixtures, a passing harness run, and a documented before/after recall@5 comparison on non-English content.
