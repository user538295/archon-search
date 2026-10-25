# Runtime parity fixtures

Frozen reference outputs of the fastembed 0.9.0 runtime (ONNX Runtime, `CPUExecutionProvider`), captured so that a replacement inference runtime can be proven to reproduce them. Vectors already stored in users' indexes, and the reranker scores the API exposes, were produced by this behaviour. The fixtures cover every model in `archon_search/model_specs.py`: 19 embedders and 5 rerankers.

|File|Content|
|---|---|
|`corpus.json`|27 texts and 14 (query, document) pairs covering short and long input (up to 2000 words, beyond most models' limit), accents in composed and decomposed form, six scripts, code, URLs, whitespace and literal `query: ` / `passage: ` prefixes.|
|`embedder_golden.npz`|One float32 array per embedder, shape `(27, dim)`, key = registry name with `/` replaced by `__` (the key keeps the lower-case `snowflake/` name users configure). Raw `TextEmbedding.embed()` output.|
|`reranker_golden.json`|Raw `TextCrossEncoder.rerank()` scores per model, one list of 14 floats in `pairs` order.|
|`model_specs.json`|One entry per model with the parameters a PyTorch implementation needs (weights repo `torch_repo` and pinned `revision`, `max_length`, `pooling`, `normalize`, `output_dim`, `torch_weight_MB`), `fastembed_graph_uses_tanh_gelu` (embedders), and the traps behind them. The tier-2 entries also carry the evidence behind every value (`parity`, `pooling_variant_scan`, `sentence_transformers`, token-id agreement, `example_tokens`) and `notes`. `_provenance` holds the tool versions. A test (`tests/test_model_specs.py`) keeps the spec table equal to this file field by field.|

Do not regenerate these files from another runtime: they are the reference, not an output of it.

## Provenance

|Models|Frozen|Tools|
|---|---|---|
|The 6 embedders and 4 rerankers of the default profiles|2026-10-24|fastembed 0.9.0, onnxruntime 1.31|
|13 further embedders and `BAAI/bge-reranker-v2-m3`|2026-10-25|fastembed 0.9.0, onnxruntime 1.30.0, onnx 1.23.2, torch 2.13.0, transformers 5.8.1, tokenizers 0.22.2, numpy 2.5.1, Python 3.13.14|

Method: the shared corpus through fastembed exactly as archon calls it (`embed(texts)`, `rerank(query, documents)`), then reproduced with `tokenizers` plus stock `transformers` classes in fp32. For the second batch two complete runs, one in the project environment and one in a throwaway environment with the same package versions, produced bit-identical goldens. The freeze script was a one-off tied to fastembed and was deleted with it; the hub revisions it recorded are the pinned `revision` / `torch_revision_sha` values, so the weights stay reproducible only while those revisions remain downloadable.

## Thresholds and measured parity

ADR 18: embedders cosine at least 0.99994 with the vector norm unchanged (ratio within 1e-3 of 1); rerankers absolute score difference below 1e-4. `tests/runtime_parity/` checks every registry entry against these, batched and one text at a time, on every available device (`cpu`, `mps`, `cuda`), plus truncation on the corpus texts and pairs that exceed the model's `max_length`. Worst values measured with `ARCHON_RUNTIME_PARITY_DATA_DIR=/tmp/rt_data` on CPU and `mps` (identical to the digits shown unless two columns are given); the whole lane (144 test cases, 6 truncation cases skipped for lack of a text beyond the limit) takes about 2 minutes once the 15 GB of weights are cached.

|Model|fastembed class|Pooling|Normalize|`max_length`|Dim|Worst cosine|Weights MB|
|---|---|---|---|---|---|---|---|
|`BAAI/bge-small-en-v1.5`|OnnxTextEmbedding|cls|yes|512|384|0.9999981|133.5|
|`BAAI/bge-base-en-v1.5`|OnnxTextEmbedding|cls|yes|512|768|0.9999447|438.0|
|`BAAI/bge-large-en-v1.5`|OnnxTextEmbedding|cls|yes|512|1024|0.9999999|1340.6|
|`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`|PooledEmbedding|mean|**no**|128|384|0.9999921 (norm ratio 0.99933 to 1.00078)|470.6|
|`sentence-transformers/paraphrase-multilingual-mpnet-base-v2`|PooledEmbedding|mean|**no**|512|768|1.0000000|1112.2|
|`intfloat/multilingual-e5-large`|PooledEmbedding|mean|**no**|512|1024|1.0000000|2239.6|
|`BAAI/bge-base-en`|OnnxTextEmbedding|cls|yes|512|768|0.9999941|438.0|
|`BAAI/bge-small-en`|OnnxTextEmbedding|cls|yes|512|384|0.9999990|133.5|
|`BAAI/bge-small-zh-v1.5`|OnnxTextEmbedding|cls|yes|512|512|0.9999981|95.8|
|`mixedbread-ai/mxbai-embed-large-v1`|OnnxTextEmbedding|cls|yes|512|1024|1.0000000|670.3|
|`snowflake/snowflake-arctic-embed-xs`|OnnxTextEmbedding|cls|yes|512|384|1.0000000|90.3|
|`snowflake/snowflake-arctic-embed-s`|OnnxTextEmbedding|cls|yes|512|384|1.0000000|132.9|
|`snowflake/snowflake-arctic-embed-m`|OnnxTextEmbedding|cls|yes|512|768|1.0000000|435.6|
|`snowflake/snowflake-arctic-embed-l`|OnnxTextEmbedding|cls|yes|512|1024|1.0000000|1336.4|
|`sentence-transformers/all-MiniLM-L6-v2`|PooledNormalizedEmbedding|mean|yes|256|384|1.0000000|90.9|
|`thenlper/gte-base`|PooledNormalizedEmbedding|mean|yes|512|768|1.0000000|219.0|
|`thenlper/gte-large`|PooledNormalizedEmbedding|mean|yes|512|1024|1.0000000|670.3|
|`ibm-granite/granite-embedding-small-english-r2`|BuiltinSentenceEmbedding|cls|**no**|8192|384|0.9999999|95.3|
|`Qwen/Qwen3-Embedding-0.6B`|LastTokenNormalizedEmbedding|**last_token**|yes|32768|1024|1.0000000|1191.6|

|Reranker|Architecture|Token types|`max_length`|Worst abs diff (CPU / `mps`)|Weights MB|
|---|---|---|---|---|---|
|`Xenova/ms-marco-MiniLM-L-6-v2`|bert|yes|512|9.5e-06 / 7.6e-06|90.9|
|`Xenova/ms-marco-MiniLM-L-12-v2`|bert|yes|512|5.7e-06 / 6.7e-06|133.5|
|`BAAI/bge-reranker-base`|xlm_roberta|no|512|1.5e-05 / 9.1e-06|1112.2|
|`jinaai/jina-reranker-v2-base-multilingual`|jina_xlm_roberta|no|1024|5.6e-06 / 1.9e-06|556.9|
|`BAAI/bge-reranker-v2-m3`|xlm_roberta (pad id 1)|no|8192|1.5e-05 / 1.0e-05|2271.1|

## Why six embedders are below cosine 1

`bge-small-en-v1.5`, `bge-base-en-v1.5`, `paraphrase-multilingual-MiniLM-L12-v2`, `bge-base-en`, `bge-small-en` and `bge-small-zh-v1.5` are the six graphs whose cosine is below 1 (`fastembed_graph_uses_tanh_gelu: true`). fastembed runs an ORT-optimized graph that contains `FastGelu`, the tanh approximation of GELU, with fp32 weights. These graphs are not quantized (the operators were scanned). PyTorch's published exact GELU is the whole deviation: with `hidden_act='gelu_new'` (tanh GELU) the worst cosine rises to 0.9999995 (`bge-small-en-v1.5`), 0.9999995 (`bge-base-en-v1.5`) and 0.9999993 (`paraphrase-multilingual-MiniLM-L12-v2`), and for the three tier-2 models to 1.0 (`parity.cpu_one_by_one_with_tanh_gelu`). With the exact GELU it is 0.9999981, 0.9999447 and 0.9999921. The runtime keeps the exact GELU the model was trained with; `bge-base-en-v1.5` at 0.9999447 is the closest to the 0.99994 threshold.

Statements about quantization that remain true: the registry names `Qwen/Qwen3-Embedding-0.6B-Q` and `BAAI/bge-reranker-v2-m3-int8` are quantized ONNX builds of supported models (recorded as `fastembed_quantized_aliases`; their own parity was not measured). Quantization explains none of the cosine gaps above.

## What the table does not show

Measured and recorded per model in `model_specs.json`:

- `last_token` pooling (Qwen3) is a third pooling next to `cls` and `mean`. The tokenizer appends `<|endoftext|>`, the pooled vector is the hidden state of that last real token, `tokenizer.json` is byte-identical in the ONNX and PyTorch repos, and right padding with position `attention_mask.sum - 1` matches in batches (cosine 1.0). The weights are bfloat16 on disk, about 2.4 GB in fp32 memory.
- `ibm-granite/granite-embedding-small-english-r2` is a ModernBERT whose ONNX graph pools inside the graph (fastembed reads output 1). Pooling and normalization come from the repo's sentence-transformers files (`1_Pooling/config.json` says cls, `modules.json` has no Normalize module) and the scan in `pooling_variant_scan` proves it: cls without normalization reaches cosine 1.0 with norm ratio 1 ± 3.1e-07, mean and last_token stay at 0.83. The golden vectors are **not normalized** (norm 17.2 to 17.6). fastembed itself fails on this corpus at its default batch size of 256 (onnxruntime: the `global_attn_mask` Tile node would exceed 4 GiB), so its golden was produced at batch size 1.
- `mixedbread-ai/mxbai-embed-large-v1` and the four arctic-embed models define a query prompt in their sentence-transformers config, and Qwen3 an instruction; fastembed's `embed()` applies none and the goldens come from raw text, so the PyTorch side must not add one. fastembed L2-normalizes `mxbai` although its repo has no Normalize module: the golden has norm 1.
- `mxbai-embed-large-v1`, `gte-base` and `gte-large` store float16 weights, `granite` and Qwen3 bfloat16; transformers upcasts them to fp32 exactly and the goldens match.
- For `bge-small-en` and `arctic-embed-s` the hidden states of `[CLS]` and `[SEP]` are identical, so the pooling scan cannot separate cls from last_token; cls is what fastembed's class does.
- Truncation: the corpus reaches 2135 to 4733 tokens, so the 128, 256, 512 and 1024 limits are exercised by corpus texts and pairs; granite's and the reranker's 8192 are shown by a probe input just past the limit (cosine 1.0, score difference 7.2e-06; `tests/runtime_parity/` skips those truncation cases because no corpus text is that long); Qwen3's 32768 comes from the ONNX repo's `tokenizer_config.json` and is not exercised (a 32k-token probe would need tens of GB of attention scores in the ONNX graph and was not attempted). `bge-reranker-v2-m3` needs 8192: truncating at 512, 1024 or 2048 misses the golden by about 0.4.
- The ONNX and PyTorch repos' `tokenizer.json` files are byte-identical for ten of the thirteen second-batch embedders, differ only in the serialized truncation and padding sections for `all-MiniLM-L6-v2`, `gte-large` and `granite`, and in float digits and the Metaspace keys for the reranker; token ids agree on all 27 texts (second-batch embedders) and on all 14 pairs plus the 729 combinations of corpus texts as query and document (reranker).

Executable reading of this contract: `Documentation/Backlog/pytorch-runtime-spike/reference_impl.py`. Plan: `Documentation/Backlog/pytorch-runtime-plan.md`. Decision: `Documentation/ADRs/18_pytorch_runtime_replaces_fastembed.md`.
