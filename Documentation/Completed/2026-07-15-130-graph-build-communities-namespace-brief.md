# Feature Brief: graph build-communities Ignores Namespace

## Problem
Running `archon-search graph build-communities <collection>` always targets the "default" namespace, even when the collection belongs to a different namespace — so the command silently builds communities in the wrong place and exits 0 with no warning.

## Goal
The command must accept a `--namespace` flag and apply it when building communities, so what the user asks for matches what actually happens.

## Users & Context
Operators running multi-namespace deployments (e.g. separate namespaces per team or environment) who need to trigger a community rebuild for a non-default namespace via the CLI. Today they have no way to do this correctly — the wrong namespace is used silently.

## Core Flow
1. User runs `archon-search graph build-communities my_collection --namespace my_ns`
2. CLI passes `namespace=my_ns` to `CommunityBuilder.build(collection, ns)`
3. Communities are written to the correct namespace's graph tables
4. Command prints `"Built N communities for collection 'my_collection' (namespace: my_ns)"`

## In Scope
- Add `--namespace` / `-n` option to `graph build-communities`, defaulting to `"default"` for backward compatibility
- Thread the namespace value through to `builder.build(collection, ns)` (`graph_cmd.py:72–76`)
- Include the namespace in the success output so the user can confirm the right target was used

## Out of Scope
- Adding namespace flags to other graph commands — addressed separately if needed
- Changing the default namespace or validation logic — `DEFAULT_NAMESPACE = "default"` stays the default
- The broader CLI-bypasses-server architectural issue (`bug-008-cli-server-proxy-brief.md`) — the namespace fix is valid regardless of whether the CLI later proxies to a REST endpoint

## Key Decisions
- **Default to `"default"` not required**: Keeps backward compatibility — existing scripts that omit `--namespace` continue to work unchanged.
- **Print namespace in success output**: Costs nothing; prevents the silent-wrong-namespace confusion that caused this bug.

## Edge Cases & Constraints
- **Namespace does not exist**: `CommunityBuilder.build()` already handles this via the underlying `GraphStore` — no special handling needed at the CLI layer.
- **Collection exists in a different namespace than specified**: `GraphStore.ensure_graph_tables()` creates tables if absent — a wrong namespace silently creates an empty community set. This is pre-existing behavior, not introduced by this fix, but the added output (showing which namespace was targeted) makes the mistake visible.
- **Dependency on bug-008**: If `graph build-communities` is later migrated to proxy through a REST endpoint (`POST /graph/{collection}/rebuild-communities`), the `--namespace` flag must be forwarded as a query/body param. The fix here is designed so the flag is cleanly passable either way.

## Decisions

- **REST migration timing:** Add `--namespace` now; keep the REST proxy migration for bug-008. The fix is a one-liner; mixing it with the architectural swap makes the PR harder to review and risks bug-008 sequencing issues. The flag is designed to forward cleanly to the REST endpoint when bug-008 lands.
- **Flag name:** Use `--namespace`. Confirmed: `key_cmd.py` lines 113 and 227 already use `--namespace`; no CLI command uses `--ns`. Use the existing convention.

## Future Iterations
- Add `--namespace` to `graph inspect` and `graph view` commands if they have the same hardcoded-default limitation
- A `--all-namespaces` flag for operators who want to rebuild communities across every namespace in one pass

## References
- [[archon_search/cli/graph_cmd.py]] `[code-agent]` — lines 72–76, hardcoded `DEFAULT_NAMESPACE` with comment acknowledging the limitation
- [[archon_search/jobs/]] `[code-agent]` — `CommunityBuilder.build(collection, ns)` signature
- [[Documentation/Backlog/bug-008-cli-server-proxy-brief.md]] `[user]` — broader CLI proxy architecture this fix must not conflict with

## Recommendation
One of the cheapest fixes in the backlog: one `@click.option` declaration, one variable threaded two levels deep, one updated print statement. Do it in the same PR as any other `graph_cmd.py` touch to avoid a context-switch cost. The namespace default keeps it non-breaking. The only thing to confirm before writing the code is which flag name (`--namespace` vs `--ns`) matches the rest of the CLI — five minutes of grepping.
