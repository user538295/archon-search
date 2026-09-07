# 13. Graph Device Probe Is Two-Stage, and the Memory Guardrail Polices Growth

**Status**: Accepted
**Date**: 2026-09-07
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR 12 — Local Prose Relations, Enrichment Narrowed to Summarisation](12_local_prose_relations_enrichment_narrowed.md) — specifically two claims in its Decision corollaries: the phrase "its own post-download validation" in §"`[graph].providers` posture and its non-inheritance" (which reads as a single mandatory post-download check), and the memory figure "The memory budget is sized for that one resident model (~3.65 GiB at the pinned sub-batch size)" in §"Single, unbounded, resident model — deviation". ADR 12's engine choice, its non-inheritance decision, its single-resident-model decision, and everything else in it are unchanged and still authoritative.

## Context

ADR 12 recorded, as umbrella corollaries, two facts about the local `gliner` graph
engine that were true in outline but imprecise in detail. Close-out review of the
`2026-08-19-035` multilingual-graph-NER feature found both worth correcting, and the
repository's ADR rule ("never edited after acceptance — supersede instead",
`Architecture/990_documentation_index_and_contribution_guide.md` §"Maintenance and
review cadence") makes that a new ADR rather than an edit.

1. **"Post-download validation" understates the shape.** The validation is not one
   check after the download. It is two stages with different costs, different failure
   semantics, and different points in the install sequence — and the second stage does
   not own a model load at all; it rides one that may not happen.

2. **"~3.65 GiB" conflated two different measurements and implied an assertion that
   does not exist.** The figure is a *peak*, not the resident size, and no test asserts
   either number. Quoting a single figure as "the memory budget" misdescribes what the
   lane actually guards.

Neither correction changes a decision. Both correct the record of what was built.

## Decision

**The device probe is recorded as two stages, and the memory guardrail is recorded as
a growth policer under a separate host-safety ceiling.**

### The two-stage device probe

`archon_search/install/device_probe.py`:

- **Stage 1 — `probe_device_availability(providers)`.** Free, runs before any
  download. It returns `resolve_torch_device(providers)`: the torch device the
  configured list *would* resolve to. Loads nothing, never fails
  (`device_probe.py:26-31`).

- **Stage 2 — `probe_device_validation(providers, prewarm_resolved_device)`.** The
  real check, and it **rides the optional pre-warm** rather than owning a mandatory
  smoke-load. `_prewarm_graph_model` loads the checkpoint, places it via
  `model.to(resolve_torch_device(providers))`, and returns `model.device.type` — the
  device it actually landed on — or `None` on any failure, since pre-warm is
  non-fatal by construction (`prewarm.py:178-212`).

Stage 2 grades that into exactly one of three states — resolved, failed, or the
neutral `not_yet_validated` sentinel — an invariant enforced structurally in
`DeviceProbeResult.__post_init__`, which raises if the three flags do not sum to
exactly one state (`device_probe.py:44-66`).

The load-bearing asymmetry: a configured accelerator with nothing back from pre-warm
grades as a **failure** (`ProvisionFailureKind.conflicting_onnx_runtimes`), not as
`not_yet_validated` (`device_probe.py:112-115`); so does a pre-warm that resolved to
CPU while an accelerator was configured (`device_probe.py:117-118`). That is what
stops an unvalidated accelerator from being offered. The intent check uses
`requests_accelerator`, not `resolve_torch_device`, precisely because the latter has
already stepped a configured-but-unavailable accelerator down to CPU and would
disguise "host lacks the device" as "nothing configured" (`torch_device.py:22-33`).

The wizard settles `[graph].providers` only when the extra is ready **and** pre-warm
was not skipped (`installer.py:962`, Step 14b). Absence of a pre-warm is absence of
evidence, not evidence of failure — with no stage 2 the probe cannot tell a skip from
a real failure, so a run without pre-warm leaves a hand-set list alone rather than
clobbering it. The settle step is auxiliary: it is wrapped in `try/except` that logs a
sanitized WARNING and leaves the config unchanged, and can never fail the install
(`installer.py:962-971`).

### ONNX vocabulary, torch consumer

The values in `[graph].providers` remain this repo's ONNX-Runtime execution-provider
vocabulary (`CUDAExecutionProvider`, `CoreMLExecutionProvider`, …) — the same strings
as `[database].providers`, never the literal torch names `cuda`/`mps`
(`torch_device.py:15-19`). Only the consumer differs: the graph engine loads through
torch, so `resolve_torch_device` matches the first entry against the `cuda` / `coreml`
markers and steps down to `cpu` with a **WARNING — never an exception** — when the
device is unavailable at runtime, so a stale providers list cannot latch a permanent
failure (`torch_device.py:36-87`).

### The memory guardrail

ADR 12's single-resident-model decision stands. What is corrected is its sizing claim.
The spike gate's PyTorch figures are a **pair**, not one number: **3172.9 MiB resident
(~3.10 GiB)** and **3737.0 MiB peak RSS at batch 8 (~3.65 GiB)**, recorded in the
provenance block of `tests/test_graph_ner_real_artifact_lane.py:118-121`. ADR 12
quoted only the peak, labelled it the budget, and dropped the resident figure.

The lane asserts **neither** figure. It polices steady-state **growth** —
`_RSS_GROWTH_BUDGET_MIB = 600` (`:138`), set with headroom over a local measurement by
this very test (137.0 MiB of growth over 1,000 chunks, baseline 2831.4 MiB resident,
which corroborates the spike's resident figure) — under a separate host-safety
ceiling, `_RSS_CEILING_MIB = 6000` (`:144`), which *is* informed by the spike's
absolute peak plus interpreter footprint so it trips only on a genuine leak.

Two scope limits are recorded with it. The growth budget is **provisional**: the
lane's first green CI run replaces it with a CI-measured figure (K9), which is why the
run records `rss_growth_mib` into the CI step's `--junitxml` (`:122-129`). And both
constants **bind the CPU configuration only** — an accelerator (CUDA/MPS) is permitted
but unmeasured, with no scenario asserting a ceiling for it (`:132-133`).

## Consequences

### Positive

- The install-time device story is now documented at the granularity an operator
  debugging a "why is my GPU not offered?" report needs: the offer depends on pre-warm
  having run, and a skipped pre-warm is deliberately not a validation.
- The memory record no longer implies a test assertion that does not exist. A reader
  sizing a host gets the resident figure (~3.10 GiB, the number that matters for
  capacity) alongside the peak (~3.65 GiB, the number that informed the ceiling).
- The provisional status of `_RSS_GROWTH_BUDGET_MIB` is now visible in the decision
  record, not only in a test comment, so K9's follow-up has a documented home.

### Negative / Tradeoffs

- Two ADRs now describe one subsystem; a reader must follow the supersession link from
  ADR 12 to get the corrected detail. This is the cost the append-only ADR rule
  deliberately accepts in exchange for an unrewritten decision history.
- The accelerator path remains permitted but unmeasured. This ADR records that gap
  rather than closing it.

## Alternatives Considered

- **Edit ADR 12 in place.** Rejected: `CLAUDE.md` and
  `990_documentation_index_and_contribution_guide.md` both state ADRs are append-only
  and superseded rather than edited. The corrections are real but not urgent enough to
  justify breaking the one rule that keeps decision history trustworthy.
- **Fold the corrections into the operator/architecture docs only, with no ADR.**
  Rejected: ADR 12 would keep asserting "~3.65 GiB" as *the* memory budget, leaving a
  live contradiction between an accepted ADR and the reference docs. A superseding ADR
  resolves it at the source.
- **Assert the absolute resident/peak figures in the lane so the ADR's original claim
  becomes true.** Rejected: absolute RSS is machine- and allocator-dependent, so such
  an assertion would flake on any runner unlike the dev machine. Policing growth under
  a generous host-safety ceiling is the testable property; the fix belongs in the
  documentation, not in a brittle new assertion.

## Cross-References

- [ADR 12 — Local Prose Relations, Enrichment Narrowed to Summarisation](12_local_prose_relations_enrichment_narrowed.md):
  the umbrella record this ADR corrects in two specifics; its engine-choice,
  non-inheritance, and single-resident-model decisions are untouched.
- `archon_search/install/device_probe.py`: both probe stages and the
  three-state `DeviceProbeResult` invariant.
- `archon_search/install/prewarm.py`: `_prewarm_graph_model`, the non-fatal load
  stage 2 rides.
- `archon_search/torch_device.py`: `requests_accelerator` (config intent) and
  `resolve_torch_device` (warn-and-step-down placement).
- `archon_search/install/installer.py`: Step 14b, the auxiliary
  `[graph].providers` settle.
- `tests/test_graph_ner_real_artifact_lane.py`: `_RSS_GROWTH_BUDGET_MIB`,
  `_RSS_CEILING_MIB`, and the provenance block carrying the measured figures.
