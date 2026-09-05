---
title: Core-module complexity refactor (defref_extractor, graph_extractor, config_writer, model_validation)
status: draft        # draft → in-progress → done
source: surfaced by the BE-21 (2026-08-19-035 MGE) clean-code review pass
---

# Core-module complexity refactor

**Why this is its own task, not folded into BE-21.** These findings were raised by the
clean-code catalog pass while reviewing BE-21 (the structural meta-guards). BE-21 only
*reworded comments* in these files; it introduced none of the complexity below. The review
scans whole changed files, so it flagged pre-existing structure. Every item is a **style /
maintainability threshold** (cyclomatic complexity, function length, file length) — **none is
a functional defect**, and all the code is working and covered by the existing suite.
Refactoring intricate, well-tested parsing/config code for a style metric is deferred here so
it lands as a reviewable, independently-tested change rather than riding a guard commit — the
surgical-changes and one-task-one-commit rules both point this way.

Do this only with the module's tests green before and after each extraction; treat any metric
change in `tests/eval/` (the code lane indexes real source) as a signal to re-verify, not to
edit baselines blindly.

## Findings (from the clean-code catalog, severity as reported)

### `archon_search/defref_extractor.py` — highest concentration
- [Moderate · file length] 1312 lines. Extract the per-language walkers and their
  import-name helpers (`_walk_python`/`_walk_typescript`/`_walk_javascript`/`_walk_go`/
  `_walk_rust`/`_walk_java`/`_walk_bash`/`_walk_swift`/`_walk_csharp`, ~lines 445–1312) into a
  separate module.
- [Major · cyclomatic complexity, >10 branches] each of the 9 `_walk_<lang>` functions above,
  plus `_build_result` (`:234`).
- [Moderate · function length] `_build_result` (`:234`, ~211 lines).

### `archon_search/graph_extractor.py`
- [Major · cyclomatic complexity] `extract` (`:278`, >10 branches).
- [Moderate · function length] `extract` (`:278`, ~219 lines).

### `archon_search/install/config_writer.py`
- [Major · cyclomatic complexity] `_apply_wizard_features_to_toml` (`:147`) and
  `_detect_config_hand_edits` (`:311`).
- [Moderate · function length] `_apply_wizard_features_to_toml` (`:147`, ~163 lines).

### `archon_search/model_validation.py`
- [Major · cyclomatic complexity] `validate_providers_shared` (`:375`).

## Approach (suggested, not prescriptive)
- Per language, extract the walker into a `defref_walkers/` submodule keyed by language, behind
  the existing dispatch — behaviour identical, one file per language, each independently
  testable.
- For the long functions, extract cohesive branch-groups into named private helpers; keep the
  public signature and return type stable (there are signature-stability guards elsewhere).
- Line numbers above are from the state at BE-21; re-grep before starting.
