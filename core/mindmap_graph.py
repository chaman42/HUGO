# ═══════════════════════════════════════════════════════════════════════════
# MINDMAP GRAPH — NetworkX wrapper over data/mind_map_connections.json.
#
# Upgrades core/memory_select.py's old _expand_with_connections, which only
# ever walked a single hop from each already-matched fact (a plain Python
# loop over the raw edge list). That misses a real multi-hop chain: if A-B
# and B-C are both genuine strong connections but A and C were never
# directly compared to each other by the reflective phase, the old code
# could never surface C when the user's message only matched A. A real
# graph structure makes that a 2-hop (or deeper) traversal instead of a
# structural blind spot.
#
# Undirected — connections.json's "from"/"to" only records which fact was
# newer at write time (core.reflective._add_connection), not a directional
# relationship; "A relates to B" and "B relates to A" mean the same thing
# here, same symmetric treatment the old one-hop code already gave it.
#
# Edge weight = 1 - strength, so NetworkX's shortest-path machinery (which
# treats lower weight as "closer") naturally prefers strong-connection paths
# over weak ones — a strong 2-hop chain can rank above a weak 1-hop edge.
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging

import networkx as nx

logger = logging.getLogger(__name__)

CONNECTIONS_PATH = "data/mind_map_connections.json"


def _load_connections() -> list[dict]:
    try:
        with open(CONNECTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def build_graph(min_strength: float = 0.0) -> nx.Graph:
    """Builds an undirected weighted graph from every edge in
    connections.json with strength >= min_strength. When the same pair
    appears more than once (e.g. two separate reflective sessions both drew
    an edge between the same two facts), the strongest one wins — a graph
    edge can't hold two weights, and the strongest connection found is the
    more trustworthy one to keep. Never raises; an empty/missing file just
    yields an empty graph."""
    graph = nx.Graph()
    for edge in _load_connections():
        strength = edge.get("strength", 0)
        if strength < min_strength:
            continue
        a, b = edge.get("from"), edge.get("to")
        if not a or not b or a == b:
            continue
        if graph.has_edge(a, b) and graph[a][b].get("strength", 0) >= strength:
            continue
        graph.add_edge(a, b, weight=1.0 - strength, strength=strength, relationship=edge.get("relationship", ""))
    return graph


def expand_multi_hop(
    seed_texts: list[str],
    candidate_pool: set[str],
    max_hops: int = 2,
    max_results: int = 2,
    min_strength: float = 0.3,
) -> list[tuple[str, float]]:
    """From each seed fact, finds the strongest-path-weighted facts within
    max_hops steps, restricted to candidate_pool (the actual pool of loaded
    fact texts — a connection can reference a fact that's since been marked
    outdated/deleted, which resolving against the pool naturally excludes).
    Returns up to max_results (text, strength) pairs, sorted by combined
    path strength descending, excluding the seeds themselves.

    strength here is the geometric-mean edge strength along the best path
    to that node — a single strong hop (0.8) beats two medium hops
    (0.5, 0.5 -> ~0.5), but a strong 2-hop chain (0.7, 0.7 -> ~0.7) still
    beats a single weak hop (0.4), which is the actual point of doing this
    with a real graph instead of the old one-hop-only walk.
    """
    if not seed_texts:
        return []
    graph = build_graph(min_strength=min_strength)
    if graph.number_of_nodes() == 0:
        return []

    seeds_in_graph = [s for s in seed_texts if s in graph]
    if not seeds_in_graph:
        return []

    best_strength: dict[str, float] = {}
    for seed in seeds_in_graph:
        # Plain unweighted BFS for the hop cutoff — keeps "how many hops
        # away" and "which path we score" consistent (both the fewest-hop
        # path), rather than mixing a hop-count cutoff with a
        # weight-shortest path that could legitimately be a different,
        # longer route.
        hop_lengths = nx.single_source_shortest_path_length(graph, seed, cutoff=max_hops)
        for node, hops in hop_lengths.items():
            if node == seed or hops == 0:
                continue
            if node not in candidate_pool or node in seed_texts:
                continue
            path = nx.shortest_path(graph, seed, node)
            edge_strengths = [graph[path[i]][path[i + 1]]["strength"] for i in range(len(path) - 1)]
            path_strength = 1.0
            for s in edge_strengths:
                path_strength *= s
            path_strength = path_strength ** (1.0 / len(edge_strengths))  # geometric mean
            if path_strength > best_strength.get(node, 0.0):
                best_strength[node] = path_strength

    ranked = sorted(best_strength.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:max_results]
