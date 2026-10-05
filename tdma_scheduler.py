#!/usr/bin/env python3
"""TDMA schedule optimizer (Part 1: the "Brain").

Takes static 2D coordinates for a set of radios and produces a collision-free
TDMA schedule: one time slot per node, with slots reused between nodes that
are far enough apart.

Model
-----
* Two radios can hear each other when their distance is <= RADIO_RANGE
  (500 m by default).  That gives the connectivity graph G.
* A transmission from A collides at receiver B if another transmitter C is
  also heard by B.  So two nodes must NOT share a slot when they are
  1 hop apart (direct link) or 2 hops apart (hidden terminal).
* Collecting those pairs gives the conflict graph G^2 (the square of G).
  A proper vertex colouring of G^2 is a valid schedule, and each colour is
  one slot in the recurring frame.

Why this is hard
----------------
Finding the minimum number of colours (the chromatic number) is NP-hard, so
the solver works in three layers:
  1. Fast greedy colourings (several orderings) plus randomized DSATUR
     restarts.  These give a good upper bound.
  2. A clique lower bound.  Any clique in G^2 needs one slot per member, so
     the largest clique is a floor on the frame length.
  3. An exact DSATUR-style branch and bound that tries to prove that
     (best - 1) slots is impossible.  With 16 nodes this finishes instantly
     and lets the report say whether the frame length is provably optimal.

Usage
-----
    python tdma_scheduler.py '{"Node_01":[0,0],"Node_02":[300,0], ...}'
    python tdma_scheduler.py --file nodes.json --json-out schedule.json
    python tdma_scheduler.py --demo

References
----------
* Distance-2 colouring in wireless networks: Ramanathan and Lloyd (1993),
  "Scheduling algorithms for multihop radio networks".
* DSATUR: Brelaz (1979), "New methods to color the vertices of a graph".
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time

import networkx as nx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RADIO_RANGE = 500.0          # metres; nodes at exactly this distance CAN talk
EXPECTED_NODE_COUNT = 16     # the brief specifies 16 nodes
DEFAULT_RESTARTS = 300       # randomized DSATUR runs
EXACT_TIME_LIMIT_S = 10.0    # cap on the exact search so large inputs still return
HEADER_LEAD = "Slot \\ Node"   # left column label of the matrix
DEFAULT_SEED = 42            # fixed so repeated runs give identical output

# 4x4 grid with 300 m spacing, used by --demo.  Matches the layout hinted at
# in the brief (Node_01 at 0,0 and Node_16 at 900,900).
DEMO_NODES = {
    f"Node_{row * 4 + col + 1:02d}": [col * 300.0, row * 300.0]
    for row in range(4)
    for col in range(4)
}


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def natural_key(name: str):
    """Sort key so Node_2 comes before Node_10 (plain string sort gets this wrong)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def parse_coordinates(raw: str) -> dict[str, tuple[float, float]]:
    """Parse and validate the JSON coordinate string.

    Raises ValueError with a readable message on any problem, so the CLI can
    print it and exit instead of dumping a traceback.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"input is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or not data:
        raise ValueError("input must be a non-empty JSON object of name -> [x, y]")

    coords: dict[str, tuple[float, float]] = {}
    for name, pos in data.items():
        ok = (
            isinstance(pos, (list, tuple))
            and len(pos) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pos)
            and all(math.isfinite(v) for v in pos)
        )
        if not ok:
            raise ValueError(f"{name!r} must map to two finite numbers [x, y], got {pos!r}")
        coords[name] = (float(pos[0]), float(pos[1]))

    # Sorted order keeps node indexing and the printed matrix deterministic.
    return {n: coords[n] for n in sorted(coords, key=natural_key)}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_connectivity_graph(coords: dict[str, tuple[float, float]],
                             radio_range: float) -> nx.Graph:
    """Nodes are radios; an edge means the two radios are within range.

    The comparison is inclusive (<=) so a pair exactly at the range limit is
    treated as connected.  That is the conservative choice: assuming a link
    that does not exist only wastes a slot, while missing one causes a
    real collision.
    """
    g = nx.Graph()
    g.add_nodes_from(coords)
    names = list(coords)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if math.dist(coords[a], coords[b]) <= radio_range:
                g.add_edge(a, b)
    return g


def build_conflict_graph(g: nx.Graph) -> nx.Graph:
    """Return G^2: an edge joins every pair at hop distance 1 or 2.

    Colouring this graph properly is exactly the distance-2 colouring
    problem.  Distance-1 edges stop direct link interference.  The extra
    distance-2 edges stop the hidden terminal case (A -> B <- C).
    """
    return nx.power(g, 2)


# ---------------------------------------------------------------------------
# Heuristic colouring
# ---------------------------------------------------------------------------

def dsatur_randomized(conflict: nx.Graph, rng: random.Random) -> dict[str, int]:
    """DSATUR with random tie-breaking.

    At each step colour the uncoloured node whose neighbours already use the
    most distinct colours (highest "saturation"), since it has the fewest
    options left.  Ties go to higher degree, then to a random pick.  Running
    this many times with different seeds explores different tie-break paths.
    """
    colour: dict[str, int] = {}
    nodes = list(conflict.nodes)
    while len(colour) < len(nodes):
        best_key, candidates = None, []
        for n in nodes:
            if n in colour:
                continue
            sat = len({colour[m] for m in conflict[n] if m in colour})
            key = (sat, conflict.degree(n))
            if best_key is None or key > best_key:
                best_key, candidates = key, [n]
            elif key == best_key:
                candidates.append(n)
        node = rng.choice(candidates)
        used = {colour[m] for m in conflict[node] if m in colour}
        c = 0
        while c in used:  # smallest free colour keeps the frame short
            c += 1
        colour[node] = c
    return colour


def heuristic_colouring(conflict: nx.Graph, restarts: int, seed: int) -> dict[str, int]:
    """Best colouring found across networkx strategies and random restarts."""
    best: dict[str, int] | None = None

    def consider(candidate: dict[str, int]):
        nonlocal best
        if best is None or len(set(candidate.values())) < len(set(best.values())):
            best = candidate

    for strategy in ("largest_first", "smallest_last", "DSATUR",
                     "independent_set", "connected_sequential_bfs"):
        consider(nx.coloring.greedy_color(conflict, strategy=strategy))

    rng = random.Random(seed)
    for _ in range(restarts):
        consider(dsatur_randomized(conflict, rng))

    assert best is not None
    return best


# ---------------------------------------------------------------------------
# Exact search (used to tighten and to prove optimality)
# ---------------------------------------------------------------------------

class SearchTimeout(Exception):
    """Raised inside the backtracking when the time budget runs out."""


def colour_with_k(conflict: nx.Graph, k: int, time_limit: float) -> dict[str, int] | None:
    """Try to colour with at most k colours.

    Returns a colouring, or None if k colours are provably not enough.
    Raises SearchTimeout if the budget runs out before an answer.

    Branch and bound on DSATUR order.  Symmetry breaking: a node may only
    open the next unused colour, never skip ahead, which stops the search
    from revisiting the same partition under relabelled colours.
    """
    nodes = list(conflict.nodes)
    adj = {n: set(conflict[n]) for n in nodes}
    colour: dict[str, int] = {}
    deadline = time.monotonic() + time_limit

    def pick() -> str:
        # Most constrained node first: fail early, prune more.
        return max(
            (n for n in nodes if n not in colour),
            key=lambda n: (len({colour[m] for m in adj[n] if m in colour}), len(adj[n])),
        )

    def solve(used: int) -> bool:
        if time.monotonic() > deadline:
            raise SearchTimeout
        if len(colour) == len(nodes):
            return True
        node = pick()
        blocked = {colour[m] for m in adj[node] if m in colour}
        for c in range(min(used + 1, k)):
            if c in blocked:
                continue
            colour[node] = c
            if solve(max(used, c + 1)):
                return True
            del colour[node]
        return False

    return dict(colour) if solve(0) else None


def optimise(conflict: nx.Graph, restarts: int, seed: int, time_limit: float):
    """Run heuristics, then try to shrink with the exact search.

    Returns (colouring, lower_bound, proven_optimal).
    """
    best = heuristic_colouring(conflict, restarts, seed)

    # Lower bound: every clique in G^2 needs distinct slots.
    clique, _ = nx.max_weight_clique(conflict, weight=None)
    lower = len(clique)

    proven = len(set(best.values())) == lower
    while not proven:
        target = len(set(best.values())) - 1
        try:
            better = colour_with_k(conflict, target, time_limit)
        except SearchTimeout:
            break  # could not decide; keep the heuristic answer, not proven
        if better is None:
            proven = True  # target slots impossible, so current best is optimal
        else:
            best = better
            proven = len(set(best.values())) == lower
    return best, lower, proven


# ---------------------------------------------------------------------------
# Schedule formatting and verification
# ---------------------------------------------------------------------------

def canonicalise(colour: dict[str, int], order: list[str]) -> dict[str, int]:
    """Renumber slots by first appearance so Node_01 always sits in slot 0."""
    remap: dict[int, int] = {}
    for n in order:
        remap.setdefault(colour[n], len(remap))
    return {n: remap[colour[n]] for n in order}


def build_matrix(slots: dict[str, int], order: list[str]) -> list[list[int]]:
    """Slot x Node boolean matrix: matrix[s][j] == 1 when node j transmits in slot s."""
    frame = max(slots.values()) + 1
    matrix = [[0] * len(order) for _ in range(frame)]
    for j, n in enumerate(order):
        matrix[slots[n]][j] = 1
    return matrix


def verify_schedule(g: nx.Graph, matrix: list[list[int]], order: list[str]) -> list[str]:
    """Independent check that does not reuse the colouring code.

    Works from the matrix alone and measures hop distance with BFS on the
    original connectivity graph, so a bug in the G^2 construction or the
    solver would still be caught here.  Returns a list of problems (empty
    means the schedule is clean).
    """
    problems: list[str] = []

    # Every node must transmit in exactly one slot.
    for j, n in enumerate(order):
        count = sum(row[j] for row in matrix)
        if count != 1:
            problems.append(f"{n} is scheduled in {count} slots (expected 1)")

    # No two nodes in the same slot may be within 2 hops.
    for s, row in enumerate(matrix):
        members = [order[j] for j, v in enumerate(row) if v]
        for i, a in enumerate(members):
            near = nx.single_source_shortest_path_length(g, a, cutoff=2)
            for b in members[i + 1:]:
                if b in near:
                    problems.append(
                        f"slot {s}: {a} and {b} are {near[b]} hop(s) apart"
                    )
    return problems


def short_label(name: str) -> str:
    """Column header: use the trailing number of 'Node_07' -> '07'."""
    m = re.search(r"(\d+)$", name)
    return m.group(1).zfill(2) if m else name


def render_report(order, radio_range, slots, matrix, lower, proven,
                  g, problems, elapsed) -> str:
    frame = len(matrix)
    width = max(len(short_label(n)) for n in order)
    heads = " | ".join(short_label(n).rjust(width) for n in order)
    header = f"{HEADER_LEAD} | {heads}"
    rule = "-" * len(header)
    bar = "=" * 64

    lines = [
        bar,
        " TDMA TOPOLOGY OPTIMIZATION REPORT",
        bar,
        f"Total Nodes Processed : {len(order)}",
        f"Configured Radio Range : {radio_range:.1f} meters",
        f"Optimized Frame Length : {frame} unique timeslots (Lower is better)",
        "-" * 65,
        "",
        "NODE -> SLOT ASSIGNMENTS:",
    ]
    lines += [f" {n}: Slot {slots[n]}" for n in order]

    lines += ["", "STRUCTURAL TDMA SCHEDULE MATRIX (Slot x Node Boolean Matrix):",
              header, rule]
    for s, row in enumerate(matrix):
        cells = " | ".join(str(v).rjust(width) for v in row)
        lines.append(f"Slot {s:02d}".ljust(len(HEADER_LEAD)) + f" | {cells}")
    lines.append(rule)

    # Extra diagnostics that back up the heuristic claims.
    reuse = {s: sum(row) for s, row in enumerate(matrix)}
    lines += [
        "",
        "OPTIMIZATION DETAILS:",
        f" Links (1-hop edges)        : {g.number_of_edges()}",
        f" Lower bound (max clique)   : {lower} slots",
        f" Proven optimal             : {'yes' if proven else 'no (time limit or gap to bound)'}",
        f" Spatial reuse (nodes/slot) : "
        + ", ".join(f"slot {s}={c}" for s, c in reuse.items()),
        f" Reuse factor               : {len(order) / frame:.2f} nodes per slot",
        f" Solve time                 : {elapsed * 1000:.1f} ms",
        "",
    ]
    if problems:
        lines.append("VERIFICATION FAILED:")
        lines += [f" - {p}" for p in problems]
    else:
        lines.append("Execution finalized cleanly. Schedule verified conflict-free.")
    lines.append(bar)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point (also what Part 2 imports)
# ---------------------------------------------------------------------------

def compute_schedule(coords: dict[str, tuple[float, float]],
                     radio_range: float = RADIO_RANGE,
                     restarts: int = DEFAULT_RESTARTS,
                     seed: int = DEFAULT_SEED,
                     time_limit: float = EXACT_TIME_LIMIT_S) -> dict:
    """Run the whole pipeline and return everything as plain data."""
    start = time.monotonic()
    order = list(coords)
    g = build_connectivity_graph(coords, radio_range)
    conflict = build_conflict_graph(g)
    colour, lower, proven = optimise(conflict, restarts, seed, time_limit)
    slots = canonicalise(colour, order)
    matrix = build_matrix(slots, order)
    problems = verify_schedule(g, matrix, order)
    return {
        "order": order, "graph": g, "slots": slots, "matrix": matrix,
        "lower_bound": lower, "proven_optimal": proven, "problems": problems,
        "elapsed": time.monotonic() - start, "radio_range": radio_range,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Distance-2 colouring TDMA schedule optimizer.")
    ap.add_argument("coords", nargs="?",
                    help='JSON string, e.g. \'{"Node_01":[0,0],"Node_02":[300,0]}\'')
    ap.add_argument("--file", help="read the JSON coordinates from a file instead")
    ap.add_argument("--demo", action="store_true", help="run on a built-in 4x4 grid")
    ap.add_argument("--range", type=float, default=RADIO_RANGE, dest="radio_range",
                    help="radio range in metres (default 500)")
    ap.add_argument("--restarts", type=int, default=DEFAULT_RESTARTS,
                    help="randomized DSATUR restarts (default 300)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--json-out", help="also write the schedule as JSON (input for Part 2)")
    args = ap.parse_args(argv)

    try:
        if args.demo:
            raw = json.dumps(DEMO_NODES)
        elif args.file:
            with open(args.file, encoding="utf-8") as fh:
                raw = fh.read()
        elif args.coords:
            raw = args.coords
        else:
            ap.error("give a JSON string, --file or --demo")
        if args.radio_range <= 0:
            raise ValueError("--range must be positive")
        coords = parse_coordinates(raw)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if len(coords) != EXPECTED_NODE_COUNT:
        print(f"warning: brief expects {EXPECTED_NODE_COUNT} nodes, got {len(coords)}",
              file=sys.stderr)

    r = compute_schedule(coords, args.radio_range, args.restarts, args.seed)
    print(render_report(r["order"], args.radio_range, r["slots"], r["matrix"],
                        r["lower_bound"], r["proven_optimal"], r["graph"],
                        r["problems"], r["elapsed"]))

    if args.json_out:
        payload = {
            "radio_range_m": args.radio_range,
            "frame_length": len(r["matrix"]),
            "node_to_slot": r["slots"],
            "matrix": r["matrix"],
            "node_order": r["order"],
            "links": sorted([sorted(e) for e in r["graph"].edges]),
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    return 1 if r["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
