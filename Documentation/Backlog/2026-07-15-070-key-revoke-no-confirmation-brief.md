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
- **`abort=True` on the confirm call:** If the operator declines, `click.confirm(..., abort=True)` exits with code 0 cleanly — no error output, consistent with "I changed my mind."

## Edge Cases & Constraints
- **Non-interactive terminal (pipe, CI without `--yes`):** `click.confirm` raises `Abort` when stdin is not a TTY and no `--yes` is given — the command exits non-zero. Operators running in CI must add `--yes` explicitly. This is intentional: silent revocation in automation is also risky.
- **Already-revoked key:** The server returns success (idempotent). The prompt still fires — acceptable since the consequence is still irreversible from the user's perspective.
- **`key_id` typo:** If the ID doesn't exist, the server returns 404 after the prompt. The prompt does not validate the ID format — that's a server concern.

## Open Questions
- Should the prompt display the key's label (human-readable name) instead of its raw ID? The `GET /keys` response includes a `label` field — a pre-flight lookup before prompting would show `Revoke key "production-webhook" (id: abc123)?`, which is more informative. Adds one HTTP round-trip; worth it if labels are commonly set.

## Future Iterations
- Extend confirmation prompt to other destructive key operations if added (bulk revoke, namespace-wide wipe)

## References
- [[archon_search/cli/key_cmd.py]] `[code-agent]` — `revoke_subcommand` at line 338, no confirmation before DELETE
- [[archon_search/server/routes_keys.py]] `[code-agent]` — `DELETE /keys/{id}` endpoint

## Recommendation
Two lines of code (`--yes` option declaration + `click.confirm` call) that prevent an irreversible mistake. The open question about showing the label is worth the extra round-trip — implement it; the UX improvement is significant and the cost is a single cheap GET. Do not ship the prompt without the `--yes` flag or it will break any existing automation immediately.
