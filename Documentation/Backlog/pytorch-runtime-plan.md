# RT1 — PyTorch Runtime for Embedder and Reranker (replaces fastembed)
**Purpose**: Replace fastembed/ONNX Runtime with PyTorch as the one runtime for the embedder and reranker so Apple GPU (and CUDA) acceleration works, without changing any stored vector or exposed reranker score, and delete the ONNX provider layer it makes obsolete.
**Audience**: archon-search contributors implementing RT1; reviewers; operators who will see `[database].device` replace `providers`.
**Status**: In Progress — implemented (2026-10-25): every phase's code and tests exist and [ADR 18](../ADRs/18_pytorch_runtime_replaces_fastembed.md) is Accepted. Not done, and why: the Linux x86_64 / Windows CPU benchmark gate (Task 6.1) exists as a manual workflow and has not been run there; its only run, on Linux arm64 in the CPU image, failed for 3 of 10 models (0.77, 0.89, 0.73; cause measured: the fp16 ONNX Runtime GEMM fastembed's graphs use on arm64) and the decision is to keep PyTorch with the pass rule applied per platform (ADR 18); the NVIDIA/CUDA path (Task 7.3) is unit-tested but has not run on an NVIDIA host; the live eval and latency thresholds (Task 8.3) were not re-measured; Task 9.2's acceptance run is partial (see its notes).

---

## Background

On Apple Silicon the wizard writes `providers = ["CoreMLExecutionProvider"]`, which measured **slower** than CPU (ONNX Runtime cuts the dynamic-shape graph into 146 CoreML pieces). PyTorch on the Apple GPU gave 2–3× the CPU throughput with no compile step. PyTorch is already a declared dependency and already runs the graph model. Full evidence, alternatives and the list of unverified items are in [ADR 18](../ADRs/18_pytorch_runtime_replaces_fastembed.md).

Preparation already done (Phase 0) and used as ground truth below:

- Golden outputs of today's runtime: [tests/fixtures/runtime_parity/](../../tests/fixtures/runtime_parity/README.md).
- Executable reference implementation passing all ten models on CPU and `mps`: [pytorch-runtime-spike/reference_impl.py](pytorch-runtime-spike/reference_impl.py).
- Dependency check: with `constraint-dependencies = ["transformers>=4.51.3,<5"]` `uv lock` resolves (240 packages; only `transformers` 5.8.1 → 4.57.6 and `huggingface-hub` 1.33.0 → 0.36.2 change). The native Jina route makes that pin unnecessary; it is a fallback only.

## Goal

After this plan: `[database].device = "auto" | "cpu" | "mps" | "cuda"` selects where the 19 registry embedders and 5 registry rerankers run; every golden fixture is reproduced (embedder cosine ≥ 0.99994 with unchanged norm, reranker |Δ| < 1e-4); fastembed, `onnx_providers.py`, the CoreML/CUDA ONNX wizard probes and the `onnxruntime-gpu` Docker arm are gone; on Apple Silicon embedding is measurably faster than on CPU; the full default test suite passes at ≥ 85% coverage.

---

## Scope

### In Scope
- New modules: model specs, model store (download/cache), device resolution, batching, PyTorch embedder/reranker, Jina loader.
- Cutover of every construction site, config (`providers`/`reranker_providers` → `device`), startup validation, prewarm, wizard probes, graph device vocabulary.
- Packaging (direct deps, Docker, CI), docs, `BREAKING.md`, migration-guide index, superseding ADRs.

### Out of Scope
- Models in fastembed's registry that need the vendor's custom modeling code (13 embedders, 2 English Jina v1 rerankers) and the 7 static / ONNX-only / gated embedders (see ADR 18 and Task 1.3); each could be added later as one spec + golden vectors + the adapter it needs.
- fp16/bf16 inference (measured 10–15% faster on `mps`, 9e-5 similarity drift; revisit after cutover).
- Query/passage prefixes for e5 (archon sends none today; parity forbids adding them here).
- Changing the LanceDB distance metric (squared L2 today); see "Known limitations".
- Replacing onnxruntime for OCR (RapidOCR) and magika.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 9.2 — Final verification & documentation update (partial)](#task-92--final-verification--documentation-update-partial).

---

## What does NOT change
- `EmbedderBackend` / `RerankerBackend` protocols and the async `Embedder` / `Reranker` wrappers in [archon_search/embedder.py](../../archon_search/embedder.py) and [archon_search/reranker.py](../../archon_search/reranker.py) (warm-up contract, lock semantics, S184/S283/S290 behaviour).
- Model names users configure (`BAAI/bge-small-en-v1.5`, …), `collection.embedding_model`, stored vectors, REST/MCP schemas (only the wording of `provider_warnings` changes).
- Reranker score scale: raw logits, no sigmoid; stable sort and tie order (`reranker.py:182-185`).
- Install profiles' model choices ([archon_search/profiles.py](../../archon_search/profiles.py)); only displayed download sizes change.
- onnxruntime stays installed for RapidOCR and magika (`parser.py:47`).

---

## Known limitations / accepted trade-offs
- Supported models shrink from 41 embedders and 8 rerankers to 19 and 5 (ADR 18). A collection pinned to another model fails with a validation error; `BREAKING.md` says how to move it.
- Chunk search uses LanceDB's default squared-L2 metric with no vector index (`store.py:3306`), so multilingual vectors (not normalized, norm 2–30) behave differently from bge (norm 1). That is today's behaviour and is frozen by the parity fixtures; changing it is a separate decision.
- bge-small, bge-base and multilingual-MiniLM (and three more bge-family models) differ from the stored vectors by cosine ≥ 0.99994 (worst measured 0.9999447, `bge-base-en-v1.5`). The cause is not quantization: fastembed ran ORT-optimized graphs with FLOAT16 weights that use the tanh approximation of GELU (and execute fp16 MatMuls on arm64), and PyTorch's published exact GELU is the whole deviation ([fixtures README](../../tests/fixtures/runtime_parity/README.md)). No re-index is required.
- Linux x86_64 / Windows CPU speed is unmeasured: the gate of Task 6.1 exists as a manual workflow but has not been run there. The Linux arm64 run failed for `bge-small-en-v1.5` (0.77), `bge-base-en-v1.5` (0.89) and `paraphrase-multilingual-MiniLM-L12-v2` (0.73), because fastembed's fp16 ONNX graphs run fp16 MatMuls natively on arm64 (Task 6.1 notes); the other seven models are 1.04–1.61× faster. Decision: keep PyTorch, pass rule per platform, pending the x86_64 and Windows runs. fastembed is already removed from the product code, so a failed gate on those platforms reopens ADR 18's "two runtimes" alternative rather than being a quiet revert.

---

## Decisions needed (recommendation first; change these before Phase 6, not after)

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Supported model set | Ten verified models now, plus a **Tier 2** of 13 more embedders and `BAAI/bge-reranker-v2-m3` (plain architectures) once Task 1.3 has frozen their golden outputs; everything else → `422` `unsupported_model` | Of 41 embedders, 19 are plain BERT/XLM-R/ModernBERT/Qwen3-type models whose behaviour is reproducible and provable per model; 13 need the vendor's custom code; 7 are static tables, ONNX-only repos or gated. Parity per model is the whole safety case, and the golden outputs can only be produced while fastembed is still installed |
| D2 | Config surface | `[database].device` (`auto`/`cpu`/`mps`/`cuda`) replaces `providers` and `reranker_providers`; `auto` = `cuda` > `mps` > `cpu` | The old keys are ONNX names; a split embedder/reranker device is not needed (the CoreML split existed only for an ONNX failure) |
| D3 | Graph model device | `[graph].providers` → `[graph].device`, default `cpu` (unchanged behaviour) | One vocabulary; the accelerator offer in the wizard keeps its guardrail text |
| D4 | Model cache location | Keep `<data-dir>/models` (HF hub layout); drop `FASTEMBED_CACHE_PATH`; honour `HF_HUB_OFFLINE` | Same layout already used by the cache; removes a fastembed-specific env var. Old `models--Qdrant--*` / `models--Xenova--*` ONNX dirs become unused (documented, not auto-deleted) |
| D5 | Dependency pins | Declare `transformers>=4.57,<5.14`, `tokenizers`, `safetensors`, `huggingface-hub` as direct dependencies; keep the repo's `<5.14` constraint | They are transitive today; the code will import them directly; 4.57 is the oldest version verified |
| D6 | Interim stopgap | Before RT1 ships, optionally make the wizard write `providers = []` on Apple (slower today than CPU) | One change; independent of this plan |

### Decision outcomes (2026-10-25)

- **D1**: 19 embedders and 5 rerankers are in the registry; the Tier 2 golden outputs were frozen on 2026-10-25 while fastembed 0.9.0 was still installed and verified ([fixtures README](../../tests/fixtures/runtime_parity/README.md)). Every other name fails validation (`unsupported model '<name>'; supported: …`).
- **D2**, **D3**: as recommended (`[database].device`, `[graph].device`; wizard flag `--graph-device`).
- **D4**: as recommended; `paths.get_models_dir()` is the cache root and `model_store.py` writes the Hugging Face hub layout there.
- **D5**: `transformers>=4.57,<5.14`, `tokenizers`, `safetensors` and `huggingface-hub` are direct dependencies in `pyproject.toml`.
- **D6**: moot; the device probe of Task 7.1 writes `device = "cpu"` whenever no accelerator validates.

---

## Architecture

**Layering (all lazy-importing `torch`; `model_specs` imports nothing heavy so the CLI stays fast). There is no factory module: each construction site calls `ModelEmbedder(model, device=config.device)` / `ModelReranker(...)` directly.**

```
config.py ──device──▶ embedder.ModelEmbedder / reranker.ModelReranker  (implement EmbedderBackend / RerankerBackend)
                         │ uses
                       model_specs.py ◀──┤  model_store.py (download, revision pin)  torch_runtime.py (device, tokenizer, encoder loader)  text_batching.py (token-budget batches)
                                         └─ reranker_loading.py (cross-encoder loader; Jina fused-QKV → stock XLM-R)
```

**New modules**
- [archon_search/model_specs.py](../../archon_search/model_specs.py): frozen dataclasses and registries.
  ```python
  Pooling = Literal["cls", "mean", "last_token"]
  Architecture = Literal["bert", "xlm_roberta", "jina_xlm_roberta"]
  @dataclass(frozen=True) class EmbedderSpec: name; repo; revision; max_length; pooling; normalize; dim; weight_mb
  @dataclass(frozen=True) class RerankerSpec: name; repo; revision; max_length; architecture; uses_token_type_ids; weight_mb
  class UnsupportedModelError(ValueError)   # carries .name and .supported (tuple)
  EMBEDDER_SPECS, RERANKER_SPECS            # immutable mappings
  def get_embedder_spec(name) -> EmbedderSpec; def get_reranker_spec(name) -> RerankerSpec
  ```
  Values come from [tests/fixtures/runtime_parity/model_specs.json](../../tests/fixtures/runtime_parity/model_specs.json) (e.g. MiniLM-multilingual `max_length=128, mean, normalize=False`; mpnet `512, mean, False`; e5 `512, mean, False`; bge `512, cls, True`; Qwen3 `last_token`; rerankers `512`, Jina `1024`, `bge-reranker-v2-m3` `8192`; BERT MiniLM rerankers need token types); `tests/test_model_specs.py` keeps the table equal to that file.
- [archon_search/model_store.py](../../archon_search/model_store.py): `model_path(spec) -> Path | None` (cached snapshot at the pinned revision, no network); `ensure_model(spec) -> Path` (`huggingface_hub.snapshot_download(repo, revision=spec.revision, allow_patterns=["model.safetensors","config.json","tokenizer.json"], cache_dir=get_models_dir())`; raises `ModelDownloadError(repo, detail)` with fixed text; honours `HF_HUB_OFFLINE`).
- [archon_search/torch_runtime.py](../../archon_search/torch_runtime.py): `DEVICE_SETTINGS`; `resolve_device(setting) -> str` (`auto` → `cuda` if available, else `mps`, else `cpu`; a requested but unavailable accelerator steps down to `cpu` with one WARNING per process and setting); `load_tokenizer(path, max_length)`; `load_encoder(spec, path, device)`; `release_accelerator_cache(device)`; `disable_weight_progress_bar()`. It also serves the graph engine's device resolution (the former `torch_device.py` is gone).
- [archon_search/reranker_loading.py](../../archon_search/reranker_loading.py): `load_cross_encoder(spec, path, device) -> (Tokenizer, torch.nn.Module)` and `load_jina_reranker(path)` — in-memory conversion of the Jina checkpoint (`mixer.Wqkv` split into q/k/v; `emb_ln`, `norm1`/`norm2`, `mlp.fc1`/`mlp.fc2`, `mixer.out_proj` renamed; classifier head unchanged; fp32; strict key accounting). `trust_remote_code` is never passed.
- [archon_search/text_batching.py](../../archon_search/text_batching.py): `plan_batches(lengths, token_budget=DEFAULT_TOKEN_BUDGET, max_items=DEFAULT_MAX_ITEMS) -> list[list[int]]` — indices sorted by length ascending (stable); a batch holds items while `len(batch) * longest <= token_budget` (at least one item); the batches' concatenation is a permutation of `range(len(lengths))`. Default budget 4096 padded tokens.

**Changed modules**
- [archon_search/embedder.py](../../archon_search/embedder.py): `ModelEmbedder(model_name: str, device: str = "auto")`; the spec is resolved at construction (an unsupported name raises `UnsupportedModelError`); `encode(texts)`: raw-`tokenizers` encode with truncation at `spec.max_length`, `plan_batches`, pad, forward (`torch.inference_mode`), pool (`cls`, `mean` or `last_token`), L2-normalize only if `spec.normalize`, return in input order. Empty input → `[]`. `is_warm`, lock, lazy load unchanged; `make_embedder(model_name, device)` builds the async wrapper.
- [archon_search/reranker.py](../../archon_search/reranker.py): `ModelReranker(model_name: str, device: str = "auto")`; `predict(pairs)` encodes `(query, document)` as a pair (truncation `longest_first` at `spec.max_length`), passes `token_type_ids` only when `spec.uses_token_type_ids`, returns raw `logits[:, 0]` in input order; `make_reranker(model_name, device)`.
- Config ([archon_search/config.py](../../archon_search/config.py)): `providers`, `reranker_providers`, `resolve_reranker_providers` removed; `device: str = "auto"` validated against `DEVICE_SETTINGS`; `[graph].providers` → `[graph].device` (default `"cpu"`).
- [archon_search/model_validation.py](../../archon_search/model_validation.py): `validate_device(device, embedding_model, reranker_model)` replaced `validate_providers_shared`; `validate_embedding_model` takes the dimension from the spec.

**Config keys**: `[database].device` (str, default `"auto"`), `[graph].device` (str, default `"cpu"`); removed: `[database].providers`, `[database].reranker_providers`, `[graph].providers`, env `FASTEMBED_CACHE_PATH`. A removed key in an existing file is a `ConfigError` naming the replacement (no silent alias): `<section>.<key> was replaced by <section>.device (auto|cpu|mps|cuda); see BREAKING.md`.

**Data flow (search)**: query → `Embedder.embed` → `asyncio.to_thread(ModelEmbedder.encode)` (unchanged) → LanceDB → rerank → `ModelReranker.predict` (unchanged).

**Thread policy**: the process-global `torch.set_num_threads(1)` pin in [archon_search/prose_extraction_backend.py](../../archon_search/prose_extraction_backend.py) is gone (measured: PyTorch CPU 131 → 44 passages/s with it; `mps` unaffected).

---

## Task breakdown

### Phase 0 — Preparation (done during planning)
> **Releasable**: nothing user-visible; every later task depends on these artifacts.

- [x] Golden fixtures, spec file and README: [tests/fixtures/runtime_parity/](../../tests/fixtures/runtime_parity/README.md)
- [x] Reference implementation validated on CPU and `mps`: [pytorch-runtime-spike/reference_impl.py](pytorch-runtime-spike/reference_impl.py)
- [x] [ADR 18](../ADRs/18_pytorch_runtime_replaces_fastembed.md) (Proposed; Accepted 2026-10-25)
- [x] `uv lock` resolution test for `transformers<5`; LanceDB metric check (squared L2); wrong "cosine distance" claim fixed in [Documentation/Architecture/600_api_reference_or_public_interface.md](../Architecture/600_api_reference_or_public_interface.md)
- [x] Token-budget sweep (4096 best on `mps`; on CPU a smaller budget only reduces padding in the 32-item benchmark workload, and larger batches are equal or faster with equal-length inputs), thread-pin and GPU-memory measurements (ADR 18)

### Phase 1 — Model specs (pure data)
> **Releasable**: after Task 1.2 — no behaviour change; specs importable and consistent with the fixtures.

#### Task 1.1 — Spec dataclasses and registries
- [x] **File**: [archon_search/model_specs.py](../../archon_search/model_specs.py)
- **Depends on**: nothing
- **Description**: dataclasses and `get_*_spec` exactly as in Architecture; 24 entries (19 embedders, 5 rerankers); `UnsupportedModelError.args[0]` is `unsupported model <name!r>; supported: <comma list>`; no `torch`/`transformers` import.
- **Releasable**: spec lookup usable by validation code.
- **Tests (TDD)** — [tests/test_model_specs.py](../../tests/test_model_specs.py):
  - Unit: `test_every_profile_model_has_a_spec` — each embedder/reranker named in `profiles.py` resolves.
  - Unit: `test_embedder_specs_match_the_frozen_fixture_json`, `test_reranker_specs_match_the_frozen_fixture_json` — every field equals `model_specs.json` (guards drift).
  - Unit: `test_multilingual_models_are_not_normalized_and_keep_their_truncation` — MiniLM 128, mpnet 512, e5 512, `normalize=False`.
  - Unit: `test_unknown_model_raises_with_supported_list`.
  - Unit: `test_importing_specs_does_not_import_torch` — `sys.modules` check in a subprocess.
  - Checkpoint: `uv run pytest tests/test_model_specs.py --no-cov -q`

#### Task 1.2 — Dimension and validation without loading a model
- [x] **File**: [archon_search/model_validation.py](../../archon_search/model_validation.py) (`validate_embedding_model`)
- **Depends on**: Task 1.1
- **Description**: return `spec.dim` from the spec (no `TextEmbedding.list_supported_models`, no instantiation, no 30 s timeout path); unknown name → `ModelValidationError("unsupported model …; supported: …")` (REST `422`, MCP validation error, as today).
- **Releasable**: `PATCH /collections/{name}` validates against the 19 supported embedders.
- **Tests (TDD)** — [tests/test_model_validation.py](../../tests/test_model_validation.py): `test_dimension_comes_from_spec_without_loading`, `test_unsupported_model_is_a_validation_error`, `test_route_returns_422_for_unsupported_model`.
  - Checkpoint: `uv run pytest tests/test_model_validation.py --no-cov -q`

#### Task 1.3 — Tier 2: freeze golden outputs for the plain-architecture models (done)
- [x] **File**: [tests/fixtures/runtime_parity/](../../tests/fixtures/runtime_parity/README.md)
- **Depends on**: Task 1.1
- **Description**: a one-off freeze script (run once on 2026-10-25 while fastembed 0.9.0 was installed, then deleted with fastembed; its provenance is in the fixtures README) ran the shared corpus through fastembed for each Tier 2 model, derived the spec from the fastembed class (CLS+normalize, mean+normalize, mean, last-token+normalize), the ONNX repo's `tokenizer_config.json` length and the torch repo's configuration, and wrote the golden array plus a spec entry. Tier 2 embedders: `BAAI/bge-base-en`, `BAAI/bge-small-en`, `BAAI/bge-small-zh-v1.5`, `mixedbread-ai/mxbai-embed-large-v1`, `snowflake/snowflake-arctic-embed-xs|s|m|l`, `sentence-transformers/all-MiniLM-L6-v2`, `thenlper/gte-base`, `thenlper/gte-large`, `ibm-granite/granite-embedding-small-english-r2`, `Qwen/Qwen3-Embedding-0.6B`; reranker `BAAI/bge-reranker-v2-m3`. All 13 embedders and the reranker passed the ADR 18 thresholds and joined the registry. Quantized ONNX variants (`…-Q`, `-int8`) are not separate models: they map to their base model (same weights, fp32 in PyTorch); their own parity was not measured.
- **Releasable**: the supported set grew without fastembed.
- **Tests (TDD)** — [tests/test_model_specs.py](../../tests/test_model_specs.py): `test_every_embedder_spec_has_a_golden_array_of_its_dimension`, `test_every_reranker_spec_has_golden_scores_for_every_pair`; Live: the Task 4.3 / 5.3 parity tests parametrize over every registry spec.
  - Checkpoint: `uv run pytest tests/test_model_specs.py --no-cov -q`

### Phase 2 — Model store
> **Releasable**: after Task 2.2 — weights downloadable and locatable; unused by runtime until Phase 6.

#### Task 2.1 — Cache path and presence check
- [x] **File**: [archon_search/model_store.py](../../archon_search/model_store.py), [archon_search/paths.py](../../archon_search/paths.py)
- **Depends on**: Task 1.1
- **Description**: `model_path(spec) -> Path | None` resolves `<models>/models--<org>--<name>/snapshots/<revision>` and returns it only if `model.safetensors`, `config.json`, `tokenizer.json` exist. `get_fastembed_cache_dir` is gone; `paths.get_models_dir()` is the cache root (decision D4).
- **Tests (TDD)** — [tests/test_model_store.py](../../tests/test_model_store.py): `test_returns_none_when_snapshot_missing`, `test_returns_path_when_all_files_present`, `test_returns_none_when_weights_missing`, `test_wrong_revision_is_not_a_hit`.
  - Checkpoint: `uv run pytest tests/test_model_store.py --no-cov -q`

#### Task 2.2 — Download at the pinned revision
- [x] **File**: [archon_search/model_store.py](../../archon_search/model_store.py)
- **Depends on**: Task 2.1
- **Description**: `ensure_model(spec) -> Path` via `snapshot_download(... allow_patterns=[model.safetensors, config.json, tokenizer.json], revision=spec.revision, cache_dir=get_models_dir())`; `ModelDownloadError(repo, detail)` with fixed, sanitized text; offline mode (`HF_HUB_OFFLINE=1`) with a cache miss raises the same error; never downloads `pytorch_model.bin`, ONNX or README files; progress goes to stderr as today.
- **Tests (TDD)** — [tests/test_model_store.py](../../tests/test_model_store.py): `test_download_requests_only_the_three_files_at_the_pinned_revision` (patch `snapshot_download`), `test_offline_miss_raises_download_error`, `test_error_text_names_repo_not_exception_text`; Live: `test_ensure_model_downloads_bge_small`.
  - Checkpoint: `uv run pytest tests/test_model_store.py --no-cov -q`

### Phase 3 — Pure runtime helpers
> **Releasable**: after Task 3.2 — helpers tested without weights.

#### Task 3.1 — Token-budget batching
- [x] **File**: [archon_search/text_batching.py](../../archon_search/text_batching.py)
- **Depends on**: nothing
- **Description**: `plan_batches` as in Architecture; `token_budget <= 0` or `max_items <= 0` → `ValueError`; empty input → `[]`; an item longer than the budget gets its own batch.
- **Tests (TDD)** — [tests/test_text_batching.py](../../tests/test_text_batching.py): `test_batches_are_a_permutation_of_the_input`, `test_each_batch_respects_the_budget_except_single_oversize_items`, `test_short_items_are_grouped_before_long_ones`, `test_max_items_caps_batch_size`, `test_empty_input`, `test_invalid_budget_raises`; property test with Hypothesis if already a dev dependency, else a seeded random loop.
  - Checkpoint: `uv run pytest tests/test_text_batching.py --no-cov -q`

#### Task 3.2 — Device resolution and loaders seam
- [x] **File**: [archon_search/torch_runtime.py](../../archon_search/torch_runtime.py)
- **Depends on**: nothing
- **Description**: `resolve_device` and `release_accelerator_cache` in `torch_runtime.py`; `resolve_device("mps")` on a host without it returns `"cpu"` and logs one WARNING per process and setting; `"auto"` never warns; unknown value → `ValueError`. The former `torch_device.py` is deleted and the graph code resolves its device through `torch_runtime.resolve_device` (Task 7.4). The loader functions are `load_encoder` (`torch_runtime.py`) and `load_cross_encoder` (`reranker_loading.py`).
- **Tests (TDD)** — [tests/test_torch_runtime.py](../../tests/test_torch_runtime.py): `test_auto_prefers_cuda_then_mps_then_cpu` (patch availability), `test_unavailable_accelerator_steps_down_once`, `test_unknown_device_raises`, `test_torch_is_imported_lazily`.
  - Checkpoint: `uv run pytest tests/test_torch_runtime.py --no-cov -q`

### Phase 4 — PyTorch embedder
> **Releasable**: after Task 4.3 — the new embedder exists and is proven; not yet wired.

#### Task 4.1 — Loader for encoders
- [x] **File**: [archon_search/torch_runtime.py](../../archon_search/torch_runtime.py)
- **Depends on**: Tasks 1.1, 2.1, 3.2
- **Description**: `load_encoder`: `Tokenizer.from_file(path/"tokenizer.json")` with `no_padding()` and `enable_truncation(spec.max_length)`; `AutoModel.from_pretrained(path, dtype=torch.float32).to(device).eval()`; returns `(tokenizer, model)`. `pad_token_id` read from `model.config` (BERT 0, XLM-R 1).
- **Tests (TDD)** — [tests/test_torch_runtime.py](../../tests/test_torch_runtime.py): `test_loads_a_tiny_synthetic_bert_in_fp32` (fixture builds a 1-layer BERT + WordLevel `tokenizer.json` under `tmp_path`), `test_truncation_is_set_from_the_spec`.

#### Task 4.2 — `ModelEmbedder.encode`
- [x] **File**: [archon_search/embedder.py](../../archon_search/embedder.py)
- **Depends on**: Tasks 3.1, 4.1
- **Description**: as in Architecture. Pooling `cls`/`mean`/`last_token`, optional normalize, original order restored, `torch.inference_mode`, accelerator cache released only when the batch plan peaked above the budget (never per call otherwise). Load failure never latches (same as today). Keep `_WARMUP_*` and `Embedder` untouched.
- **Tests (TDD)** — [tests/test_embedder.py](../../tests/test_embedder.py) (rewritten onto the tiny-model loader seam): `test_output_order_matches_input_order_across_batches`, `test_cls_and_mean_pooling_differ`, `test_last_token_pooling_differs_from_cls_and_mean`, `test_normalize_only_when_spec_says_so`, `test_text_longer_than_max_length_is_truncated`, `test_padding_does_not_change_a_vector`, `test_empty_input_returns_empty_list_without_loading`, `test_lazy_load_and_is_warm`, `test_load_failure_is_not_latched`.
  - Checkpoint: `uv run pytest tests/test_embedder.py --no-cov -q`

#### Task 4.3 — Embedder parity against the golden vectors
- [x] **File**: [tests/runtime_parity/test_embedder_parity.py](../../tests/runtime_parity/test_embedder_parity.py)
- **Depends on**: Task 4.2
- **Description**: marker `runtime_parity` (excluded from the default run via `norecursedirs` + `-m` like `live_eval`; documented in `pyproject.toml`). For each of the 19 registry embedders: encode the corpus texts through `ModelEmbedder` on every available device (`cpu`, `mps`, `cuda`) and compare with `embedder_golden.npz`. Result (Apple M4 Pro, CPU and `mps`): all models pass; the worst cosine per model is in the [fixtures README](../../tests/fixtures/runtime_parity/README.md).
- **Tests (TDD)**: Live: `test_embedder_matches_golden[model]` — cosine ≥ 0.99994 per text, norm ratio within 1e-3 of 1; `test_long_text_truncation_matches`; batched against one-by-one.
  - Checkpoint (from the module docstring): `ARCHON_RUNTIME_PARITY_DATA_DIR=/tmp/rt_data uv run pytest -m runtime_parity tests/runtime_parity/test_embedder_parity.py --no-cov -q -p no:cacheprovider -o addopts= -rP`

### Phase 5 — PyTorch reranker
> **Releasable**: after Task 5.3 — reranker proven; not yet wired.

#### Task 5.1 — Cross-encoder loader
- [x] **File**: [archon_search/reranker_loading.py](../../archon_search/reranker_loading.py)
- **Depends on**: Tasks 1.1, 2.1, 3.2
- **Description**: `load_cross_encoder`: tokenizer as for encoders; `bert`/`xlm_roberta` → `AutoModelForSequenceClassification.from_pretrained(path, dtype=torch.float32)`; `jina_xlm_roberta` → `load_jina_reranker(path)` in the same module. `trust_remote_code` is never passed.
- **Tests (TDD)** — [tests/test_reranker_loading.py](../../tests/test_reranker_loading.py): `test_cross_encoder_loader_never_trusts_remote_code` (assert call kwargs), `test_loads_a_tiny_bert_cross_encoder_in_fp32_eval_mode`, `test_loads_a_tiny_xlm_roberta_cross_encoder`.

#### Task 5.2 — Jina checkpoint conversion
- [x] **File**: [archon_search/reranker_loading.py](../../archon_search/reranker_loading.py) (`load_jina_reranker`)
- **Depends on**: nothing
- **Description**: `load_jina_reranker` per Architecture: fused `Wqkv` layout `(three h d)`: rows `[0:H)` query, `[H:2H)` key, `[2H:3H)` value; raises `ValueError` naming any missing/unexpected key (strict accounting); casts bf16 → fp32.
- **Tests (TDD)** — [tests/test_reranker_loading.py](../../tests/test_reranker_loading.py): `test_wqkv_split_gives_equal_thirds_in_query_key_value_order`, `test_every_source_key_is_consumed_exactly_once`, `test_missing_tensor_raises_value_error_naming_it`, `test_output_dtype_is_float32_although_the_checkpoint_is_bf16`, `test_converted_tiny_model_matches_fused_reference` (tiny random fused checkpoint vs a hand-written fused attention forward).
  - Checkpoint: `uv run pytest tests/test_reranker_loading.py --no-cov -q`

#### Task 5.3 — `ModelReranker.predict` and parity
- [x] **File**: [archon_search/reranker.py](../../archon_search/reranker.py), [tests/runtime_parity/test_reranker_parity.py](../../tests/runtime_parity/test_reranker_parity.py)
- **Depends on**: Tasks 3.1, 5.1, 5.2
- **Description**: as in Architecture; all pairs share the query (`pairs[0][0]`, as today); `token_type_ids` only for `uses_token_type_ids`; batches by `plan_batches` on pair lengths; raw `logits[:, 0]`.
- **Tests (TDD)** — [tests/test_reranker.py](../../tests/test_reranker.py): `test_scores_are_raw_logits_not_sigmoid`, `test_token_type_ids_are_passed_when_the_spec_asks_for_them`, `test_token_type_ids_are_omitted_when_the_spec_does_not_ask_for_them`, `test_order_preserved_across_batches`, `test_rerank_candidates_stable_sort_on_equal_scores`. Live — [tests/runtime_parity/test_reranker_parity.py](../../tests/runtime_parity/test_reranker_parity.py): `test_reranker_matches_golden[model]` (|Δ| < 1e-4 on all 14 pairs for the 5 registry rerankers, CPU and `mps`: worst 1.5e-05, see the fixtures README), `test_long_pair_truncation_matches_golden`.
  - Checkpoint (from the module docstring): `ARCHON_RUNTIME_PARITY_DATA_DIR=/tmp/rt_data uv run pytest -m runtime_parity tests/runtime_parity/test_reranker_parity.py --no-cov -q -p no:cacheprovider -o addopts= -rP`

### Phase 6 — Gate and cutover
> **Releasable**: after Task 6.8 — fastembed is no longer used by any code path; the default suite is green.

#### Task 6.1 — GATE: CPU benchmark on Linux x86_64 and Windows
- [ ] **File**: [scripts/bench_runtime.py](../../scripts/bench_runtime.py), [.github/workflows/archon-search-runtime-bench.yml](../../.github/workflows/archon-search-runtime-bench.yml) — **implemented; run once on Linux arm64 only (failed, see below)** (left unticked on purpose: the pass rule needs a `workflow_dispatch` run on `ubuntu-latest` and `windows-latest`)
- **Depends on**: Tasks 4.2, 5.3
- **Description**: `workflow_dispatch` job on `ubuntu-latest` and `windows-latest` that runs `scripts/bench_runtime.py --runtime both` through `uv run --with fastembed==0.9.0` (the only place fastembed still runs): PyTorch on CPU and the fastembed 0.9.0 baseline alternate in one process, run by run, with the first runtime flipping each round, so a load burst on a shared host hits both (best of 10 timed rounds after one warm-up each); `compare` then writes the table. One row per profile model (the ten models of the six install profiles, `MODEL_KINDS`; passages and rerank pairs as in `scripts/bench_runtime.py`). **Pass rule**: PyTorch CPU throughput ≥ 0.9× fastembed for every compared model, per platform. If it fails, reopen ADR 18's "two runtimes" alternative.
- **Tests (TDD)**: Unit — [tests/test_bench_runtime.py](../../tests/test_bench_runtime.py): `test_report_marks_regression_below_threshold`, `test_benchmarked_models_are_exactly_the_models_of_the_six_profiles`, `test_interleaved_alternates_runtimes_each_round_after_one_warmup_each`, `test_interleaved_load_burst_hitting_both_runtimes_leaves_the_ratio_intact`. Live: the workflow run itself (attach the table to ADR 18) — not run on x86_64 or Windows.
  - Checkpoint: `uv run pytest tests/test_bench_runtime.py --no-cov -q`

- **Linux arm64 run (2026-10-25; FAILED for 3 of 10 models; x86_64 and Windows still not run)**: inside the `linux/arm64` CPU image (`docker build -t archon-search:local .`; Docker Desktop VM on an M4 Pro, 14 vCPUs; torch 2.14.1+cpu, transformers 5.13.1, onnxruntime 1.31.0, fastembed 0.9.0, installed with `python3 -m venv --system-site-packages fe-venv && fe-venv/bin/pip install fastembed==0.9.0`; own ONNX downloads, about 7.3 GB). The host was shared with other work (load average 13–20 on 14 cores), so absolute numbers are 20–40% below a quiet machine. This is not the x86_64 gate the task asks for; it says nothing about ONNX Runtime's x86 kernels.

  Final run, `--runtime both` (alternating, best of 10 rounds; throughput in passages/s or pairs/s):

  | Model | torch | fastembed | torch / fastembed | Result |
  |---|---:|---:|---:|---|
  | BAAI/bge-small-en-v1.5 | 63.1 | 81.9 | 0.77 | FAIL |
  | BAAI/bge-base-en-v1.5 | 22.8 | 25.7 | 0.89 | FAIL |
  | BAAI/bge-large-en-v1.5 | 5.8 | 5.5 | 1.04 | PASS |
  | sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 | 105.7 | 145.6 | 0.73 | FAIL |
  | sentence-transformers/paraphrase-multilingual-mpnet-base-v2 | 17.7 | 14.4 | 1.23 | PASS |
  | intfloat/multilingual-e5-large | 4.1 | 3.4 | 1.20 | PASS |
  | Xenova/ms-marco-MiniLM-L-6-v2 | 111.8 | 86.1 | 1.30 | PASS |
  | Xenova/ms-marco-MiniLM-L-12-v2 | 59.9 | 43.3 | 1.38 | PASS |
  | BAAI/bge-reranker-base | 14.8 | 10.4 | 1.42 | PASS |
  | jinaai/jina-reranker-v2-base-multilingual | 15.6 | 9.7 | 1.61 | PASS |

  Earlier runs with one runtime per process (the torch run, then the fastembed run, minutes apart) disagreed with each other: the first run gave `bge-base-en-v1.5` 0.81 and `paraphrase-multilingual-MiniLM-L12-v2` 0.66 (`bge-small-en-v1.5` 1.14, eight passes), three repeats of those two gave 0.87–0.89 and 0.78–0.81, and a ten-model gate of three repeats under host load 13–20 gave `paraphrase-multilingual-MiniLM-L12-v2` 0.81–0.90 and `bge-small-en-v1.5` 0.94–1.11. Identical configurations differed by 20–60% under load, and ORT suffered more than torch, so a ratio next to 0.9 is a coin flip on a shared host; this is why the script now alternates the runtimes, and why the table above is the one to cite. Even so, the three small models stay below 0.9 in the alternating run.

  **Cause (measured)**: the `Qdrant/*-onnx-Q` files fastembed downloads for these models (`model_optimized.onnx`, 208 MB and 225 MB against 418 MB of fp32 `bge-base` weights) hold 149 FLOAT16 initializers and no FLOAT weights, with ORT-fused `Attention`, `SkipLayerNormalization` and `FastGelu` nodes and a final fp16 to fp32 output cast; they contain no integer-quantization ops. An ONNX Runtime profile of `bge-base` puts 39% of the time in MatMuls with float16 inputs. A GEMM micro-benchmark (4096×768 by 768×3072, all 14 vCPUs) gave 1409 GFLOP/s for ORT's fp16 MatMul against 803 for PyTorch fp32 `linear`, 658 for ORT fp32, 880 for PyTorch fp16 and 538 for PyTorch bf16: PyTorch fp32 is already faster than ORT fp32, and ORT's native fp16 GEMM on arm64 NEON is the gap, largest for the smallest models. fp32 is a binding rule of ADR 18, so no code change followed. Levers measured with interleaved A/B runs and not adopted: `hidden_act` `gelu_pytorch_tanh` (0.98–1.0× speed; it improves the parity of the six tanh-GELU models but worsens `gte-base` below the 0.99994 threshold) and `gelu_new` (0.86–0.93×), eager attention (0.95–0.97×), thread counts (`torch.set_num_threads` changes only the ATen ops, the OpenBLAS GEMM ignores it; 10 threads helped one model by 11% and hurt another by 10%), token budgets (a smaller budget helped only because the 32-item benchmark workload pads less; with equal-length inputs larger batches are equal or faster for every model) and `torch.backends.mkldnn` off. PyTorch fp16/bf16, `torch.compile` and int8 were out of scope. On x86_64 ORT has no native fp16 MatMul kernel [INFERENCE, not measured], so this result may not transfer.

  **Decision (2026-10-25, recorded in [ADR 18](../ADRs/18_pytorch_runtime_replaces_fastembed.md))**: keep PyTorch as the only runtime. The pass rule is evaluated per platform: not met on Linux arm64 for the three models above (accepted: 11–27% slower for those three, 1.04–1.61× faster for the other seven), pending on Linux x86_64 and Windows. The threshold was not changed. The task stays unticked until the `workflow_dispatch` run on `ubuntu-latest` and `windows-latest` exists; if either fails the rule, ADR 18's "two runtimes" alternative reopens.

#### Task 6.2 — Backend factories (dropped)
- [x] **Dropped by decision during implementation**: no `backends.py` was written. Each construction site builds `ModelEmbedder(model, device=config.device)` / `ModelReranker(...)` itself (`embedder.make_embedder`, `reranker.make_reranker`, `pipeline.create_pipeline`, `server/app.py`, `server/routes_route.py`, `model_validation.py`, `install/prewarm.py`, `eval/runner.py`); the reranker is disabled by an empty `reranker_model` at those sites. The tests of this task were not written; `tests/test_backends.py` does not exist.

#### Task 6.3 — Config: `device` replaces providers
- [x] **File**: [archon_search/config.py](../../archon_search/config.py), [archon-search.toml.example](../../archon-search.toml.example)
- **Depends on**: Task 3.2
- **Description**: add `device`; remove `providers`, `reranker_providers`, `resolve_reranker_providers`, `[graph].providers` (→ `[graph].device`); a file still holding a removed key fails `load_config` with `ConfigError("<section>.<key> was replaced by <section>.device (auto|cpu|mps|cuda); see BREAKING.md")`.
- **Tests (TDD)** — [tests/test_config.py](../../tests/test_config.py): `test_device_default_is_auto`, `test_invalid_device_rejected`, the removed-key tests, and the graph device default; `tests/test_config_reranker_providers.py` is deleted (its subject no longer exists).
  - Checkpoint: `uv run pytest tests/test_config.py --no-cov -q`

#### Task 6.4 — Replace every construction site
- [x] **File**: [archon_search/server/app.py](../../archon_search/server/app.py), [archon_search/server/routes_route.py](../../archon_search/server/routes_route.py), [archon_search/pipeline.py](../../archon_search/pipeline.py), [archon_search/embedder_cache.py](../../archon_search/embedder_cache.py), [archon_search/sync.py](../../archon_search/sync.py), [archon_search/eval/runner.py](../../archon_search/eval/runner.py)
- **Depends on**: Task 6.3
- **Description**: each site passes `config.device` (`EmbedderCache` and `SearchCollectionSync` take a `device`; `make_embedder`/`make_reranker` take `device`). With Task 6.2 dropped there is no factory to call.
- **Tests (TDD)**: existing tests for each module updated onto the new signature. The AST guard `tests/test_no_direct_backend_construction.py` was not written (it belonged to the dropped factory module); `tests/test_no_fastembed.py` guards the absence of fastembed.
  - Checkpoint: `uv run pytest tests/test_embedder_cache.py tests/pipeline --no-cov -q`

#### Task 6.5 — Startup validation and warm-up
- [x] **File**: [archon_search/model_validation.py](../../archon_search/model_validation.py), [archon_search/server/warmup.py](../../archon_search/server/warmup.py)
- **Depends on**: Task 6.4
- **Description**: `validate_providers_shared` → `validate_device(device, embedding_model, reranker_model)`: loads both on the resolved device and runs one inference each; warnings are fixed text (`device unavailable: <requested>; models loaded on cpu`, `embedder probe failed; see the log for details`, `reranker probe failed; see the log for details`); the stale-split CoreML advisory is deleted. `GET /status` `model_validation.provider_warnings` keeps its shape.
- **Tests (TDD)** — [tests/test_model_validation.py](../../tests/test_model_validation.py): `test_probe_runs_one_inference_per_model`, `test_unavailable_device_warns_and_uses_cpu`, `test_probe_failure_text_is_fixed`; [tests/test_routes_ready.py](../../tests/test_routes_ready.py): `test_ready_reports_warn_when_device_stepped_down`.

#### Task 6.6 — Prewarm
- [x] **File**: [archon_search/install/prewarm.py](../../archon_search/install/prewarm.py)
- **Depends on**: Tasks 2.2, 6.4
- **Description**: `download=` → `ensure_model(spec)`; `load=` unchanged in spirit (`ModelEmbedder(...).encode([probe])`); `_planned_total_bytes` uses spec weight sizes; the 5–30 min timeout rule is kept.
- **Tests (TDD)** — [tests/test_install_prewarm.py](../../tests/test_install_prewarm.py): update the 31 tests; add `test_download_failure_names_phase_and_repo`.

#### Task 6.7 — Remove the GLiNER thread pin
- [x] **File**: [archon_search/prose_extraction_backend.py](../../archon_search/prose_extraction_backend.py)
- **Depends on**: Task 4.2
- **Description**: deleted `torch.set_num_threads(1)` / `set_num_interop_threads(1)` and the `_TORCH_*_THREADS` constants. The "within 10% of without the graph model" benchmark was not run as a separate gate; the unit test pins the thread count.
- **Tests (TDD)** — [tests/test_prose_extraction_backend.py](../../tests/test_prose_extraction_backend.py): `test_loading_the_graph_model_does_not_change_torch_thread_count`.

#### Task 6.8 — Delete fastembed and ONNX provider code
- [x] **File**: `archon_search/onnx_providers.py` (deleted), [archon_search/paths.py](../../archon_search/paths.py), [archon_search/__init__.py](../../archon_search/__init__.py), [tests/_search_stubs.py](../../tests/_search_stubs.py), [pyproject.toml](../../pyproject.toml)
- **Depends on**: Tasks 6.4–6.7
- **Description**: remove `fastembed` from dependencies and every import; delete `onnx_providers.py` and its 8 test files; remove `get_fastembed_cache_dir` / `FASTEMBED_CACHE_PATH`; replace the fastembed fake in `_search_stubs.py` with fakes on the loader seam (zeros of the spec dimension, `[0.5]*n` scores as before); keep `ORT_DISABLE_TELEMETRY` (onnxruntime stays for OCR/magika) and fix its comment; declare `transformers>=4.57,<5.14`, `tokenizers`, `safetensors`, `huggingface-hub` as direct dependencies (D5); `uv lock`.
- **Tests (TDD)**: full default suite; [tests/test_no_fastembed.py](../../tests/test_no_fastembed.py): `test_no_module_imports_fastembed`, `test_pyproject_does_not_depend_on_fastembed`.
  - Checkpoint: `uv run pytest -q` (full default suite, coverage gate on)

### Phase 7 — Wizard and installer
> **Releasable**: after Task 7.4 — `archon-search wizard` configures `device` and verifies it.

#### Task 7.1 — Device probe replaces CoreML/CUDA ONNX probes
- [x] **File**: [archon_search/install/installer.py](../../archon_search/install/installer.py) (`_probe_and_configure_device`, `_accelerator_validated`)
- **Depends on**: Task 6.5
- **Description**: one method `_probe_and_configure_device(gpu) -> list[str]`: accepted GPU → `validate_device("auto", …)`; passes on an accelerator → write `device = "auto"`; falls back to CPU → write `device = "cpu"` with `Warning: <Apple GPU|CUDA> validation failed — falling back to CPU.`; declined/none → `device = "cpu"`. Still runs after `Proceed?` (the 2026-10-23 ordering). Summary `Device:` labels: `Apple GPU (mps)`, `CUDA`, `CPU`, with the pending note `— checked after you confirm; CPU if it fails` before the probe.
- **Tests (TDD)** — [tests/test_install_device_probe_step.py](../../tests/test_install_device_probe_step.py) (replaced `test_install_split_coreml.py`, `test_install_providers.py`); [tests/test_install_confirm_before_probe.py](../../tests/test_install_confirm_before_probe.py) updated.

#### Task 7.2 — Config writer and wizard keys
- [x] **File**: [archon_search/install/config_writer.py](../../archon_search/install/config_writer.py), [archon_search/cli/install_cmd.py](../../archon_search/cli/install_cmd.py)
- **Depends on**: Task 6.3
- **Description**: `configure_device` writes `[database].device` (or `[graph].device`); `configure_providers`, `configure_reranker_providers`, `clear_reranker_providers`, `write_gpu_providers_disabled` are removed; `--disable-gpu` writes `device = "cpu"`; the hand-edit detector learned `device`.
- **Tests (TDD)** — [tests/test_install_config_writer.py](../../tests/test_install_config_writer.py), [tests/test_install_overwrite_detection.py](../../tests/test_install_overwrite_detection.py), [tests/test_install_device_probe_step.py](../../tests/test_install_device_probe_step.py).

#### Task 7.3 — CUDA torch handling
- [ ] **File**: [archon_search/install/extras.py](../../archon_search/install/extras.py) (`_install_cuda_torch`, `_torch_cuda_available`, `_cuda_visible_in_subprocess`), [archon_search/install/installer.py](../../archon_search/install/installer.py) (`_probe_and_configure_device`) — **implemented and unit-tested, not run on an NVIDIA host** (left unticked on purpose)
- **Depends on**: Task 7.1
- **Description**: the `+cu126` swap is the enabling step for GPU embedding: on linux/x86_64 with CUDA accepted and `torch.cuda.is_available()` false the wizard swaps torch/torchvision, then verifies in a fresh interpreter (this process keeps the torch it already imported). The "Keeping the installed PyTorch build" branch and every `onnxruntime-gpu` mention are gone. **Requires an NVIDIA host to verify (unverified so far).**
- **Tests (TDD)** — [tests/test_install_cuda_torch.py](../../tests/test_install_cuda_torch.py): `test_swap_runs_only_when_cuda_unavailable_after_accepting_gpu`, `test_cuda_swap_runs_on_linux_x86_64_when_torch_lacks_cuda`, the subprocess probe tests.

#### Task 7.4 — Graph device vocabulary
- [x] **File**: `archon_search/torch_device.py` (deleted, folded into [archon_search/torch_runtime.py](../../archon_search/torch_runtime.py)), [archon_search/install/device_probe.py](../../archon_search/install/device_probe.py), [archon_search/install/provisioning.py](../../archon_search/install/provisioning.py), [archon_search/prose_extraction_backend.py](../../archon_search/prose_extraction_backend.py)
- **Depends on**: Tasks 3.2, 6.3
- **Description**: graph extraction resolves `[graph].device` through `torch_runtime.resolve_device`; `--graph-providers` became `--graph-device` (choices `auto|cpu|mps|cuda`); the accelerator offer text keeps its guardrail wording and names device names instead of ONNX providers.
- **Tests (TDD)** — [tests/test_install_graph_accelerator.py](../../tests/test_install_graph_accelerator.py), [tests/test_install_device_probe.py](../../tests/test_install_device_probe.py): updated.

### Phase 8 — Packaging and CI
> **Releasable**: after Task 8.3 — images and pipelines build without fastembed.

#### Task 8.1 — Docker
- [x] **File**: [Dockerfile](../../Dockerfile), [scripts/check-gpu-cuda-majors.sh](../../scripts/check-gpu-cuda-majors.sh), [scripts/docker-entrypoint.sh](../../scripts/docker-entrypoint.sh) — code and unit tests done; the GPU image build itself was not run
- **Depends on**: Task 6.8
- **Description**: GPU arm keeps PyPI CUDA torch and drops the `onnxruntime` uninstall, the `onnxruntime-gpu[cuda,cudnn]` install and the ORT part of the CUDA-major check; the entrypoint drops the extras-volume ORT exclusion; `FASTEMBED_CACHE_PATH` is removed from `Dockerfile.test` and the compose files (`git grep` finds neither `onnxruntime` nor `fastembed` in them).
- **Tests (TDD)**: [tests/test_docker_gpu_image.py](../../tests/test_docker_gpu_image.py) updated; Live: GPU image build in the release workflow (best-effort, as today) — not run.

#### Task 8.2 — CI model cache and lanes
- [x] **File**: [.github/workflows/archon-search-pr.yml](../../.github/workflows/archon-search-pr.yml), [.github/workflows/archon-search-eval-live.yml](../../.github/workflows/archon-search-eval-live.yml), [.github/workflows/archon-search-release.yml](../../.github/workflows/archon-search-release.yml), [.github/workflows/archon-search-runtime-parity.yml](../../.github/workflows/archon-search-runtime-parity.yml), [tests/eval/live_benchmark/conftest.py](../../tests/eval/live_benchmark/conftest.py) — workflows written and linted by tests; no workflow run was observed
- **Depends on**: Task 6.8
- **Description**: prefetch the torch weights of the live-lane models at their pinned revisions into the models cache; cache key derived from the pinned revisions; `HF_HUB_OFFLINE=1` in the benchmark step; the `runtime_parity` lane runs nightly and on `workflow_dispatch` on CPU (Tasks 4.3 and 5.3); the live-benchmark skip guard looks for the new cache layout.
- **Tests (TDD)**: [tests/test_workflow_model_cache_lint.py](../../tests/test_workflow_model_cache_lint.py) (replaced the fastembed cache lint) and [tests/test_live_model_cache.py](../../tests/test_live_model_cache.py) (replaced `test_live_fastembed_cache.py`).

#### Task 8.3 — Eval thresholds
- [ ] **File**: [tests/eval/live_thresholds.toml](../../tests/eval/live_thresholds.toml) — left unticked: only comments changed; the live eval and benchmark lanes were not re-run on the new runtime, so no threshold was re-measured
- **Depends on**: Task 8.2
- **Description**: re-run the live eval and benchmark lanes on the new runtime; accept the new latency numbers only if quality metrics are unchanged (per [tests/eval/README.md](../../tests/eval/README.md) waiver policy); never lower a quality threshold.
- **Tests (TDD)**: Live: `uv run pytest tests/eval/live/ --no-cov` and the `live_benchmark` lane — not run.

### Phase 9 — Documentation
> **Releasable**: after Task 9.2 — plan complete.

#### Task 9.1 — BREAKING, migration index, ADRs
- [x] **File**: [BREAKING.md](../../BREAKING.md), [Documentation/MigrationGuide/03_breaking_changes_index.md](../MigrationGuide/03_breaking_changes_index.md), [Documentation/ADRs/18_pytorch_runtime_replaces_fastembed.md](../ADRs/18_pytorch_runtime_replaces_fastembed.md)
- **Depends on**: Task 6.8
- **Description**: `BREAKING.md` entry (removed keys and env var, supported model set, `provider_warnings` wording, download sizes, orphaned ONNX cache directories and how to delete them, wizard prompts) plus index row NR-42 and the heading/row counts (the S52 test enforces it); ADR 18 status → Accepted, header lists the superseded sentences exactly as ADRs 14–16 do.
- **Tests (TDD)** — [tests/test_removed_engine_repo_guard.py](../../tests/test_removed_engine_repo_guard.py): the S52 tests pass with the new entry and row.

#### Task 9.2 — Final verification & documentation update (partial)
- [ ] **File**: N/A (agent task) — the documentation sweep is done; the acceptance run is partial (status per criterion below)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation (README, ADRs, Architecture docs 000/100/110/130/140/150/160/200/210/500/510, user and operator guides, `archon-search.toml.example`, CLAUDE.md/AGENTS.md) and update every file affected; do not touch unrelated docs. Known sets: Architecture `100:31,65,169,184-186`, `110:52-57,73,155,229`, `130:15,29,39,44,48,335`, `200:14,30,41,47,57,111-113,120-127,142,177-179`; 18+ user-manual files; `README.md:38`.
  - Verify every acceptance criterion below; verify every file reference in this plan is a valid markdown link to an existing file (new files created by this plan are exempt).
- **Acceptance criteria** (must all pass):
  - `uv run pytest` (default suite) passes with coverage ≥ 85%; `uv run pytest -m runtime_parity` passes on CPU and on Apple `mps`.
  - Golden parity: every embedder cosine ≥ 0.99994 and norm ratio within 1e-3 of 1 (0.99 for MiniLM); every reranker |Δ| < 1e-4.
  - `git grep -i fastembed -- archon_search pyproject.toml` returns nothing; `onnx_providers.py` does not exist; `[database].providers` is rejected with a message naming `device`.
  - On Apple Silicon the wizard writes `device = "auto"`, `GET /ready` reports `checks.models: "ok"`, and the measured embedding throughput for `multilingual-e5-large` is higher on `mps` than on `cpu`.
  - Task 6.1's table shows PyTorch CPU ≥ 0.9× fastembed for every compared model on Linux x86_64 and Windows.
  - `archon-search wizard --dry-run`, a real wizard run on macOS, and `archon-search serve` + one ingest + one search complete without a model-load warning.
  - No test asserts on fastembed wording, and no code path imports `fastembed`.
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: confirm each criterion; scan this plan for bare file paths not wrapped in a markdown link.

**Status of the criteria (2026-10-25)**
- Met and evidenced in the repo: golden parity (every registry model, CPU and `mps` on an Apple M4 Pro; worst values in the [fixtures README](../../tests/fixtures/runtime_parity/README.md)); `onnx_providers.py` does not exist; `[database].providers` is rejected with a message naming `device` (`tests/test_config.py`); `git grep -i fastembed -- archon_search` is empty. `pyproject.toml` still names fastembed once, in the `runtime_parity` marker description (it describes the frozen reference outputs); no dependency on it remains (`tests/test_no_fastembed.py`).
- Not verified by the documentation task: the full default suite and its 85% coverage gate; the Apple wizard / `GET /ready` / `multilingual-e5-large` `mps` versus `cpu` throughput check; the wizard `--dry-run`, real wizard and serve + ingest + search runs.
- Not run: Task 6.1's table (Linux x86_64 and Windows CPU). It stays open, together with the NVIDIA/CUDA path (Task 7.3) and the eval thresholds (Task 8.3).

---

## References
- [Documentation/ADRs/18_pytorch_runtime_replaces_fastembed.md](../ADRs/18_pytorch_runtime_replaces_fastembed.md) `[decision]` — evidence, alternatives, unverified items.
- [tests/fixtures/runtime_parity/README.md](../../tests/fixtures/runtime_parity/README.md) `[fixtures]` — frozen reference outputs and per-model specs.
- [Documentation/Backlog/pytorch-runtime-spike/reference_impl.py](pytorch-runtime-spike/reference_impl.py) `[spike]` — executable reference for tokenization, pooling, Jina conversion.
- [archon_search/embedder.py](../../archon_search/embedder.py), [archon_search/reranker.py](../../archon_search/reranker.py) `[source]` — the seams that stay.
- [archon_search/embedder.py](../../archon_search/embedder.py), [archon_search/reranker.py](../../archon_search/reranker.py), [archon_search/torch_runtime.py](../../archon_search/torch_runtime.py), [archon_search/reranker_loading.py](../../archon_search/reranker_loading.py), [archon_search/model_validation.py](../../archon_search/model_validation.py), [archon_search/install/installer.py](../../archon_search/install/installer.py), [archon_search/install/prewarm.py](../../archon_search/install/prewarm.py) `[source]` — the new runtime. The replaced `onnx_providers.py` and `torch_device.py` no longer exist.
- [Documentation/ADRs/02_fastembed_for_dense_embeddings.md](../ADRs/02_fastembed_for_dense_embeddings.md), [Documentation/ADRs/15_unavailable_onnx_providers_fall_back_to_cpu.md](../ADRs/15_unavailable_onnx_providers_fall_back_to_cpu.md), [Documentation/ADRs/16_wizard_validates_cuda_before_writing.md](../ADRs/16_wizard_validates_cuda_before_writing.md) `[ADR]` — superseded by ADR 18.
