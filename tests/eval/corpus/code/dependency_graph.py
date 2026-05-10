"""Dependency graph with topological sort (Kahn's algorithm)."""
from __future__ import annotations
from collections import defaultdict, deque


def topological_sort(deps: dict[str, list[str]]) -> list[str]:
    """Return nodes in topological order given a dependency dict.

    Args:
        deps: Mapping from node to its list of dependencies.

    Returns:
        Topologically sorted list of nodes.

    Raises:
        ValueError: If the graph has a cycle.
    """
    # Build in-degree and adjacency
    in_degree: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    nodes = set(deps)
    for node, predecessors in deps.items():
        for pred in predecessors:
            nodes.add(pred)
            adj[pred].append(node)
            in_degree[node] += 1

    queue = deque(n for n in nodes if in_degree[n] == 0)
    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for successor in adj[node]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if len(result) != len(nodes):
        raise ValueError("Cycle detected in dependency graph")
    return result
