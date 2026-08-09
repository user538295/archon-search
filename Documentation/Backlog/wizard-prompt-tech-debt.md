## Tech Debt: Wizard prompt infrastructure

**Type**: Tech debt (not a bug — wizard works correctly with documented inputs)
**Discovered**: 2026-08-09, during S03 fix + iterative review
**Severity**: Medium — no production failure mode, but demonstrated cost: this debt
made S03 harder to diagnose and creates a class of defect (prompt misalignment)
that is easy to reintroduce.

---

### Background

`archon-search wizard` is a sequential interactive prompt flow spread across
three files: `archon_search/install/wizard.py`, `installer.py`, and `prewarm.py`.
The prompting logic grew organically with no shared primitive. The result is six
independent yes/no handlers with inconsistent rules.

---

### Issue 1 — Six prompt parsers, four different EOF policies

When stdin runs out mid-wizard (e.g. a script provides too few answers), each
prompt handles it differently:

| Prompt | Location | EOF behaviour |
|---|---|---|
| Multilingual? | `wizard.py:648` | Prints "No input received. Using English." → False |
| GPU confirm? | `wizard.py:673` | Silent → True (auto-enable) |
| Reranker / watch / etc. | `wizard.py:402` (`_ask_yn`) | Returns configured `default` (True or False) |
| Database delete confirm | `prewarm.py:163` | `""` → treated as "no" → abort + SystemExit(1) |
| Proceed? | `installer.py:771` | Sets `answer = "n"` → abort + return 1 |

There is no single rule. The consequence of a missing input depends entirely on
which prompt receives EOF. S03 was caused by a test supplying N answers for N+1
prompts — the wrong prompt silently got `"yes"` and the wizard aborted.

**Ideal fix**: one shared helper with an explicit `on_eof` parameter
(`default` vs `abort`), called from all six sites. Decision is declared at each
call site, implemented once.

---

### Issue 2 — Typo handling is inconsistent between prompt types

If a user types something unrecognised:

- **`[Y/n]` prompts** (`_ask_yn` with `default=True`): anything that is not
  literally `"n"` or `"no"` is treated as yes. So `"nope"` → yes. No feedback.
- **`[y/N]` prompts** (`_ask_yn` with `default=False`): anything that is not
  `"y"` or `"yes"` is treated as no. So `"yep"` → no. No feedback.
- **Multiple-choice prompts** (`_ask_choice`): prints `"Invalid value. Valid
  options: …"` and retries once before falling back to the default.

The choice prompt already has the right UX. `_ask_yn` silently coerces junk.
The same typo (`"yep"`) means yes at one prompt and no at the next.

**Ideal fix**: give `_ask_yn` a single retry on unrecognised input, matching
`_ask_choice`. `default` then means only "what to return on empty/EOF", not
also "how permissive is parsing".

---

### Issue 3 — No isatty() check; non-interactive mode requires an explicit flag

The wizard decides whether to show prompts based on the `--non-interactive`
CLI flag. It never checks `sys.stdin.isatty()`.

In practice: piping into the wizard without `--non-interactive` causes it to
prompt anyway, consuming piped data as answers. If data runs out, one of the
four EOF behaviours above kicks in depending on which prompt was hit.

**Ideal fix**: one check at startup —
`if not sys.stdin.isatty(): non_interactive = True` — before any prompting
begins. Callers in CI/automation no longer need to know the flag.

---

### Relationship to the existing TUI backlog item

`Documentation/Backlog/interactive-tui-setup-wizard-brief.md` (`status: planned`)
describes a full Textual TUI wizard that would eventually replace this text flow.
That item is unscheduled. These three issues are independent of it: they are
fixable in the current plain-text wizard without any TUI work, and they should
be fixed regardless of whether the TUI item ever lands.

---

### Files involved

- `archon_search/install/wizard.py` — `_ask_yn`, `_ask_choice`, `_prompt_multilingual`, `_prompt_gpu_confirm`
- `archon_search/install/installer.py` — Step 13 Proceed? prompt (~line 768)
- `archon_search/install/prewarm.py` — delete-db confirmation (~line 160)
