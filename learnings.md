# Learnings

## What Has Worked

**2026-06-15 — Parallel iterative review of already-merged commits**
- Observation: `git reset --soft <parent>` inside a worktree exposes a commit's diff as staged changes, which `/iterative-review` can then inspect without needing an open branch.
- Action: Use this pattern when spawning review agents on commits that are already merged to main; it avoids checking out detached HEAD and keeps the worktree clean.
- Confidence: high

**2026-06-15 — Coroutine leak prevention pattern in threaded async code**
- Observation: `asyncio.run_coroutine_threadsafe` raises `RuntimeError` when the loop is closed, but can raise other exceptions (e.g. `ValueError`, `TypeError`) for other failure modes. Only catching `RuntimeError` leaves the coroutine unawaited on those paths, emitting `RuntimeWarning: coroutine never awaited`.
- Action: Always add `except BaseException: coro.close(); raise` after `except RuntimeError` whenever a coroutine is created before being handed to `run_coroutine_threadsafe`.
- Confidence: high

**2026-06-15 — Graduating plans to Completed/**
- Observation: Verify that (a) all plan tasks are `[x]`, and (b) key production symbols exist in the codebase (`grep` or `ls`) before moving to `Documentation/Completed/`. Plans without a paired brief (e.g. E1) are moved as plan-only.
- Action: Follow this two-step verification before any move; never move based on plan checkbox state alone.
- Confidence: high

## What Has Failed

**2026-06-15 — Mocking `asyncio.wait_for` to simulate timeout**
- Observation: Patching `asyncio.wait_for` directly (e.g. `side_effect=asyncio.TimeoutError`) leaves the inner coroutine (the `embed` AsyncMock) unawaited, producing `RuntimeWarning`. The mock intercepts the call before the real `wait_for` can await the coroutine.
- Action: Never patch `asyncio.wait_for` to simulate timeouts. Instead, make the coroutine itself raise the target exception (`AsyncMock(side_effect=asyncio.TimeoutError)`) so the real `wait_for` propagates it cleanly.
- Confidence: high

**2026-06-15 — Agent names with dots**
- Observation: The Agent tool name parameter rejects dots. `ReviewC18-2.1` fails; `ReviewC18-21` works. The regex is `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.
- Action: Never use dots in agent names; replace with nothing or an underscore.
- Confidence: high

**2026-06-15 — Generating commit message without committing**
- Observation: When a user says "commit X using the /commit-message format", running the skill and stopping at the message is incomplete — the user expects the full action: generate + commit.
- Action: "commit X using /commit-message" → `git add X` → `Skill("commit-message")` → `git commit` with the generated message, all in one flow. No pause.
- Confidence: high

## Patterns and Preferences

**2026-06-15 — Merge strategy for review branches diverged from different parents**
- Observation: When review agents work from different parent commits, `git merge` risks conflicts from both the review diffs and the intervening main commits. The safer pattern is: `git diff <original-sha> <review-branch-tip> -- <files> | git apply` to extract only the incremental fix delta and apply it to main.
- Action: Use the patch-diff merge strategy (not `git merge`) when integrating review branches that diverged from commits already in main's history.
- Confidence: high

## Open Questions
- (Nothing recorded yet)
