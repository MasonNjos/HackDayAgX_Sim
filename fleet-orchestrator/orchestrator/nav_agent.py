"""Navigation Agent: shortest routes over the road graph, avoiding blocked edges.

Owner: Person A. Person B relies on the signature of route() and travel_ticks().
"""
from __future__ import annotations

import heapq
import math

from .models import Tractor, WorldState


def route(state: WorldState, from_node: str, to_node: str) -> tuple[list[str], float] | None:
    """Shortest path from_node -> to_node. Returns (path incl. both ends, km), or None if unreachable."""
    if from_node == to_node:
        return [from_node], 0.0
    adj: dict[str, list[tuple[str, float]]] = {}
    for e in state.edges.values():
        if e.id in state.blocked_edges:
            continue
        adj.setdefault(e.a, []).append((e.b, e.length))
        adj.setdefault(e.b, []).append((e.a, e.length))

    dist = {from_node: 0.0}
    prev: dict[str, str] = {}
    heap = [(0.0, from_node)]
    while heap:
        d, n = heapq.heappop(heap)
        if n == to_node:
            break
        if d > dist.get(n, math.inf):
            continue
        for m, w in adj.get(n, []):
            nd = d + w
            if nd < dist.get(m, math.inf):
                dist[m], prev[m] = nd, n
                heapq.heappush(heap, (nd, m))
    if to_node not in dist:
        return None
    path = [to_node]
    while path[-1] != from_node:
        path.append(prev[path[-1]])
    return path[::-1], dist[to_node]


def edge_between(state: WorldState, a: str, b: str) -> str | None:
    for e in state.edges.values():
        if {e.a, e.b} == {a, b}:
            return e.id
    return None


def travel_ticks(tractor: Tractor, km: float) -> int:
    return math.ceil(km / tractor.speed) if km > 0 else 0
