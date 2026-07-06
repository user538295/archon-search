# Feature Brief: E2e — Deterministic Graph Eval Gate v2

## Problem
The existing graph quality gates always pass because they rely on fake stubs that never test real graph retrieval — a regression in graph recall can ship undetected.

## Goal
Replace all three fake graph gates (naive, local, global) with real ones: run honest multi-hop recall checks on frozen public datasets, add a negative control that proves graph mode doesn't hurt simple queries, and block CI on any regression.

## Users & Context
Developers merging changes to graph retrieval code. They need confidence that a change doesn't silently break graph recall or harm standard retrieval quality.

## Core Flow
1. During implementation, run the eval suite against frozen multi-hop fixtures to measure baseline Recall@5 and MRR for each graph mode.
2. Commit the baseline numbers and set hard floors in `thresholds.toml`.
3. On every subsequent CI run, the suite re-runs all three graph modes against the same frozen fixtures.
4. Any mode whose Recall@5 or MRR drops below its floor fails the build.
5. The negative control check (HotpotQA with `graph_mode=naive`) additionally fails the build if simple-query recall regresses.

## In Scope
- Frozen in-repo fixture subsets: **MuSiQue-Ans** (~100 questions, CC BY 4.0) and **2WikiMultiHopQA** (~100 questions, Apache-2.0) for multi-hop recall; **HotpotQA distractor** (~100 questions, CC BY 4.0) as negative control
- Supporting paragraph corpora for each dataset (the candidate documents each question retrieves from)
- New eval metrics: `graph_naive_recall_at_5`, `graph_local_recall_at_5`, `graph_global_recall_at_5`, `graph_negative_control_recall_at_5` (HotpotQA naive-mode)
- Real community eval backend: deterministic Leiden seed, community tables built from multi-hop corpus in the eval harness — replaces `CommunityStoreStub` for local/global eval
- Hard gates for all four new metrics, calibrated from a single baseline run and committed
- `EvalQualityFloors` extended with the four new optional float fields
- Baseline JSON and markdown updated with the new numbers
- Existing fake `graph_local_mrr` and `graph_global_mrr` floors (currently 1.0 via stub fallback) replaced by the new real gates; `graph_mrr` (naive, report-only) promoted to gated `graph_naive_recall_at_5`
- Two consecutive CI runs byte-identical on all new quality metrics (determinism verification)

## Out of Scope
- LLM-judged eval frameworks (RAGAS, BenchmarkQED) — deterministic label-based recall requires no paid API
- Live/real-model eval against production fastembed weights — stub embedder only (same SHA-256 backend as existing eval suite)
- RepoBench-R subset for code retrieval (deferred to E2g/E2h per roadmap)
- Full 300–500 question datasets — 100 questions per dataset is sufficient for a regression gate

## Key Decisions
- **Hard gate from day one**: floors set after a single calibration run; regressions block CI immediately. Report-only gates have historically stayed report-only indefinitely.
- **Negative control included (HotpotQA)**: published evidence shows graph retrieval hurts on simple factual questions; this is the failure mode the gate guards against.
- **All three modes gated in one delivery**: fake local/global gates (currently stub-fallback MRR=1.0) are replaced with real community eval infrastructure rather than left as lies.
- **100 questions per dataset**: statistical power is sufficient to detect Recall@5 drops of 5+ percentage points, which is what matters for a regression gate.
- **Absolute floors per mode** (not cross-config delta): same runner, two separate metric buckets (graph_mode=null and graph_mode=naive on HotpotQA questions) — no runner architecture changes required.

## Edge Cases & Constraints
- **Leiden non-determinism**: Leiden community detection must use a fixed random seed in the eval harness — vary the seed across runs and the gate becomes flaky. Pinned seed committed alongside the baseline.
- **Community rebuild in eval**: local/global modes require communities pre-built from the multi-hop corpus before the eval run. This build step must be part of the eval harness setup, not a manual prerequisite.
- **Fixture licensing**: MuSiQue (CC BY 4.0), 2WikiMultiHopQA (Apache-2.0), HotpotQA (CC BY 4.0) — all permit in-repo distribution with attribution. `LICENSE-DATASETS` file to be added alongside fixtures.
- **Graph extras required**: eval setup must install `archon-search[graph]` (spaCy, leidenalg, igraph); the eval harness already skips gracefully when extras are absent — new graph eval tests must follow the same skip pattern.
- **Corpus size**: ~100 questions × ~20 supporting paragraphs × 3 datasets ≈ 6,000 documents ingested per eval run with the stub embedder. Acceptable runtime; document if it grows.
- **Graph-mode queries excluded from standard retrieval metrics**: existing invariant — graph-mode queries must not affect `recall_at_1/3/5`, `mrr`, `ndcg` floors. Preserve this partition.

## Open Questions
- Exact subset selection strategy for the 100-question slices: random seed + stratified by question type (bridge vs. comparison for 2Wiki), or hand-curated? Planning will decide.
- Whether to run community build inside the eval fixture (per-test setup) or as a one-time conftest session fixture — depends on LanceDB temp-path isolation pattern already established in integration tests.
- Threshold values for the four new floors — set after first calibration run; not a design decision.

## Future Iterations
- RepoBench-R code retrieval subset for E2g/E2h graph code-symbol modes
- Recall@2 and Recall@10 in addition to Recall@5 (more granular gate; deferred to keep initial calibration simple)
- Real-model eval variant (live_benchmark marker) once production fastembed weights are cached in CI

## Recommendation
Build this now. The current graph gates are actively misleading — they pass on every run regardless of whether graph retrieval works. E2e closes that gap completely: three real gates, one honest negative control, all blocking. The hardest part is the community eval backend for local/global modes (deterministic Leiden + corpus setup), but the existing integration test patterns for `CommunityBuilder` make this tractable. Do not defer the negative control or the community backend — partial honesty is not an improvement over consistent fakery.
