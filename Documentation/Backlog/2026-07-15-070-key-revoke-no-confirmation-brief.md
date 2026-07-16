# Feature Brief: Key Revoke Confirmation Prompt

## Problem
Running `archon-search key revoke <id>` immediately and permanently revokes the key — no prompt, no warning. A typo in the key ID silently revokes the wrong key, and there is no way to undo it.

## Goal
The command asks "Are you sure?" before deleting a key. Scripts and automation can skip the prompt with a flag.

## Users & Context
Operators managing API keys — rotating credentials, cleaning up old keys, or revoking compromised keys. They run the command from a terminal, sometimes copying a key ID from a list. A mistype or wrong paste should not silently cause an irreversible action.

## Core Flow
1. Operator runs `archon-search key revoke <key_id>`.
2. The CLI prints: `Revoke key <key_id>? This cannot be undone. [y/N]:` and waits.
3. Operator types `y` — the key is revoked; CLI prints confirmation.
4. Operator types `n` (or presses Enter) — command exits cleanly with no change.
5. For scripted/CI use: `archon-search key revoke <key_id> --yes` skips the prompt and revokes immediately.

## In Scope
- Confirmation prompt on `key revoke` before the `DELETE /keys/{id}` request is sent
- `--yes` / `-y` flag to bypass the prompt for non-interactive use

## Out of Scope
- Confirmation on `key rotate` (rotation creates a new key before revoking the old one — lower risk; can be added separately)
- Soft-delete / undo / key recovery — the server has no such mechanism

## Key Decisions
- **Prompt by default, flag to skip:** Protects interactive users; doesn't break existing scripts that add `--yes`. The alternative (no prompt, document it) keeps the status quo and leaves the footgun in place.
- **Plain `click.confirm` (NOT `abort=True`) — corrected during implementation:** The original brief said to use `click.confirm(..., abort=True)` and claimed it "exits with code 0 cleanly." That is factually wrong: verified against click 8.3.3, `abort=True` on decline raises `Abort`, prints `Aborted!` to stderr, and exits **code 1**. To honor the intended behavior (interactive decline is not an error → exit 0, no traceback), the implementation uses plain `click.confirm(...)`: an interactive "no"/Enter returns `False`, prints `Aborted.`, and returns (exit 0); a non-interactive stdin (pipe/CI) with no `--yes` still raises `Abort` on EOF and exits non-zero, preserving the CI-safety requirement below.

## Edge Cases & Constraints
- **Non-interactive terminal (pipe, CI without `--yes`):** `click.confirm` raises `Abort` when stdin is not a TTY and no `--yes` is given — the command exits non-zero. Operators running in CI must add `--yes` explicitly. This is intentional: silent revocation in automation is also risky.
- **Already-revoked key:** The server returns success (idempotent). The prompt still fires — acceptable since the consequence is still irreversible from the user's perspective.
- **`key_id` typo:** If the ID doesn't exist, the server returns 404 after the prompt. The prompt does not validate the ID format — that's a server concern.

## Open Questions
_(none — resolved below)_

## Resolved Decisions
- **Prompt shows the key's label, with fallback (resolved 2026-07-16):** Before prompting, fetch `GET /keys` and match the target ID to display `Revoke key "production-webhook" (id: abc123)?`. A confirmation that only echoes the typed ID protects almost nothing — a wrong paste would just be confirmed as-is; showing the recognizable label is what makes the prompt an actual safety check, and the cost is one cheap read before an irreversible action. Fallbacks: if the ID is not in the list or the key has no label, prompt with `Revoke key <id>?` and proceed (the server still returns 404 on a bad ID after confirmation, as today). The `--yes` path skips the lookup entirely — automation never reads the prompt, so the fetch would be pure waste there. Note: there is no `GET /keys/{id}`, only the list endpoint, so the lookup fetches all keys and filters client-side.

## Future Iterations
- Extend confirmation prompt to other destructive key operations if added (bulk revoke, namespace-wide wipe)

## Status
Implemented 2026-07-16. `revoke_subcommand` now prompts (with a best-effort label lookup via `GET /keys?status=all`) and accepts `--yes`/`-y`. Tests: `tests/test_key_revoke_confirm.py` (unit + integration), `tests/integration/test_key_revoke_confirm_e2e.py` (e2e). Docs updated: `Documentation/Architecture/600_api_reference_or_public_interface.md`, `Documentation/SecurityGuide/02_authentication_and_keys.md`.

## References
- [[archon_search/cli/key_cmd.py]] `[code-agent]` — `revoke_subcommand` + `_lookup_key_label` helper
- [[archon_search/server/routes_keys.py]] `[code-agent]` — `DELETE /keys/{id}` endpoint, `GET /keys` (label lookup source)

## Recommendation
Two lines of code (`--yes` option declaration + `click.confirm` call) that prevent an irreversible mistake. The open question about showing the label is worth the extra round-trip — implement it; the UX improvement is significant and the cost is a single cheap GET. Do not ship the prompt without the `--yes` flag or it will break any existing automation immediately.
