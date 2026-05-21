# Review: Architecture/200_testing_strategy.md

## Summary

The doc is largely accurate. Markers, default selector, coverage gate, fixtures, thresholds, and baseline numbers all match source. Two minor inaccuracies: (1) the doc claims "four pytest markers" but `pyproject.toml` registers only three (`benchmark`, `integration`, `eval`, `live` are four — recount: actually four are declared, so this is correct; see below), and (2) the doc references `tokenizer subprocesses are stubbed` which is a paraphrase of `_search_stubs.install_stubs()` and matches code. The main real issue is the PR-workflow guidance: the doc says split CI "MUST `coverage combine`", but the actual `archon-search-pr.yml` deliberately skips `coverage combine` (uses `--cov-append` into a single `.coverage` file and explains why). Strategy and CI are consistent in *outcome* (one combined dataset before threshold) but the doc's instruction is over-specific.

## Inaccuracies (numbered)

1. **"split across four pytest markers plus a default (unmarked) tier"** (line 9). Verified four markers in `pyproject.toml` lines 62–67: `benchmark`, `integration`, `eval`, `live`. Count is correct — **not an inaccuracy**. (Listed here only to record verification.)

2. **"Split / matrix CI runs MUST `coverage combine`"** (lines 17, 72). `.github/workflows/archon-search-pr.yml` does NOT run `coverage combine`. It explicitly comments: *"we skip `coverage combine` (it would error with 'No data to combine') and report directly on the single file"*. Both pytest steps use `--cov-append` into one `.coverage`, then `coverage report --fail-under=85`. The doc's prescriptive "MUST combine" is contradicted by the actual workflow's approach (single-file append). The semantic intent (one dataset before threshold) holds, but the mechanism described is wrong for this repo.

3. **"`tests/conftest.py` … injects `ARCHON_SEARCH_API_KEY = "0" * 64`"** (line 40). Correct in value, but the doc says it's set "so `create_app()` always sees a known key" — verified at `tests/conftest.py:23-24`. **Accurate.**

4. **Eval invocation example** (line 47): `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`. CI uses `tests/eval/` (directory), not `tests/eval/test_eval_suite.py` (single file). The single-file form works but is narrower than CI's eval slice. Minor scope mismatch — the doc under-represents what the eval marker covers in CI.

5. **"per-run `runtime.toml`"** (line 52). `tests/eval/runtime.toml` exists; "per-run" is misleading — it's a single committed file, not generated per run. Minor wording issue.

6. **Routing accuracy floor "≈ 0.926"** (line 53). Actual value in `thresholds.toml`: `0.9259259259259259`. The "≈" is accurate.

7. **`ndcg_at_5 ≈ 0.976`, `ndcg_at_10 ≈ 0.979`** (line 53). Actual: `0.9756955953575489` and `0.9793677493892313`. Rounded values accurate.

8. **"flagship file is `tests/benchmark_routing_latency.py`"** (line 61). File exists at that path; contains `test_routing_latency_harness_runs` with `@pytest.mark.benchmark`. **Accurate.**

9. **"compares … `MultiCollectionRouter.get_pre_context` against `POST /route` over 100 iterations after 3 warmups"** (line 61). Verified: `_ITERATIONS = 100`, `_WARMUP = 3`, both branches measured. **Accurate.**

10. **"auto-skips when the server is not reachable (`pytest.skip` on a failed `GET /health` probe)"** (line 61). Verified at `_is_server_running()` (lines 31-36) calling `GET /health`. **Accurate.**

11. **"Unmarked eval units … run under the default selector"** (line 57). Verified: `tests/eval/` contains `test_backends.py`, `test_metrics.py`, `test_fixtures.py`, `test_types.py`, `test_runner.py`, `test_docs_contract.py`, `test_baseline_contract.py`, `test_corpus_contract.py`, `test_pytest_integration.py` — none are marker-gated by default. **Accurate.**

12. **`addopts` claim** (line 69): `--cov=archon_search --cov-report=term-missing --cov-fail-under=85`. Verified verbatim in `pyproject.toml:61`, plus `--strict-markers --strict-config` and the marker exclusion. **Accurate.**

## Verified claims

- Marker names and meanings (`pyproject.toml:62-67`).
- Default selector `-m 'not live and not eval and not benchmark and not integration'` (line 61 of `pyproject.toml`).
- Coverage gate `--cov-fail-under=85`.
- `tests/conftest.py` calls `install_stubs()` at module import time before pytest discovery.
- `connected_store` fixture is module-scoped; `col_name` uses `uuid.uuid4().hex[:8]`.
- `auth_headers` fixture exists and uses the test API key.
- Eval fixtures present: `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`, `routing/`, `runtime.toml`.
- `archon_search/eval/backends.py` defines `EvalEmbedderBackend` (SHA-256-based) and `EvalRerankerBackend` (BM25-inspired lexical).
- Thresholds floor values match `tests/eval/thresholds.toml` exactly.
- `policy.max_floor_drop_without_waiver = 0.05` verified.
- Baseline JSON contains `waiver_ids` (empty dict) — referencing path is correct.
- Latency ceilings intentionally unset — confirmed by `thresholds.toml` header comment.
- Benchmark file path, marker, iteration counts, and skip behaviour.

## Unverifiable / ambiguous

- **"Read `tests/eval/README.md` before changing thresholds, fixtures, or baselines"** — `tests/eval/README.md` exists but its content wasn't reviewed here; the cross-reference is valid as a file pointer.
- **"latency p50/p95 is a regression guard, not a production SLA"** (intent statement) — non-falsifiable from code alone, consistent with `baseline.json` where latency metrics are `null`.
- **"Real LanceDB + real pipeline end-to-end … `integration`"** — no actual `@pytest.mark.integration` usage was sampled in tests/; the marker is registered and excluded but its adoption in current tests was not verified.
- **`live` marker usage** — registered, but no in-tree test using `@pytest.mark.live` was sampled.
- **Discrepancy between doc's "MUST coverage combine" and CI's `--cov-append` single-file approach** (Inaccuracy #2): the doc may be intentionally prescribing a general principle while CI chose a simpler equivalent. Whether to update doc or CI is an editorial call.
