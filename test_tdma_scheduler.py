"""Tests for tdma_scheduler.py.

Run with:  python -m unittest -v test_tdma_scheduler
"""

import itertools
import json
import math
import random
import subprocess
import sys
import unittest

import tdma_scheduler as t


def hop_distance(coords, radio_range, a, b):
    """Reference BFS written from scratch, so the tests do not trust networkx."""
    names = list(coords)
    seen, frontier, d = {a}, [a], 0
    while frontier and d < 3:
        d += 1
        nxt = []
        for u in frontier:
            for v in names:
                if v not in seen and math.dist(coords[u], coords[v]) <= radio_range:
                    seen.add(v)
                    nxt.append(v)
                    if v == b:
                        return d
        frontier = nxt
    return 99 if b not in seen else d


def assert_conflict_free(tc, coords, radio_range, slots):
    for a, b in itertools.combinations(coords, 2):
        if slots[a] == slots[b]:
            tc.assertGreater(hop_distance(coords, radio_range, a, b), 2,
                             f"{a} and {b} share slot {slots[a]} but are within 2 hops")


def brute_force_min_slots(conflict_pairs, names):
    """Exhaustive minimum colouring, only used on tiny inputs."""
    for k in range(1, len(names) + 1):
        for assign in itertools.product(range(k), repeat=len(names)):
            col = dict(zip(names, assign))
            if all(col[a] != col[b] for a, b in conflict_pairs):
                return k
    return len(names)


def random_coords(rng, n, size):
    return {f"Node_{i + 1:02d}": (rng.uniform(0, size), rng.uniform(0, size))
            for i in range(n)}


class ScheduleCorrectness(unittest.TestCase):
    def test_demo_grid(self):
        coords = t.parse_coordinates(json.dumps(t.DEMO_NODES))
        r = t.compute_schedule(coords)
        self.assertEqual(r["problems"], [])
        assert_conflict_free(self, coords, 500.0, r["slots"])
        self.assertEqual(len(r["matrix"]), 9)
        self.assertTrue(r["proven_optimal"])

    def test_random_topologies_many_densities(self):
        rng = random.Random(1)
        for size in (300, 600, 900, 1500, 3000):
            for _ in range(40):
                coords = random_coords(rng, 16, size)
                r = t.compute_schedule(coords, restarts=30)
                self.assertEqual(r["problems"], [])
                assert_conflict_free(self, coords, 500.0, r["slots"])

    def test_matches_brute_force_on_tiny_graphs(self):
        rng = random.Random(7)
        for _ in range(25):
            coords = random_coords(rng, 6, 900)
            names = list(coords)
            pairs = [(a, b) for a, b in itertools.combinations(names, 2)
                     if hop_distance(coords, 500.0, a, b) <= 2]
            expected = brute_force_min_slots(pairs, names)
            r = t.compute_schedule(coords)
            self.assertEqual(len(r["matrix"]), expected)

    def test_two_hop_hidden_terminal(self):
        # A and C are 800 m apart (no link) but both reach B: must differ.
        coords = {"Node_01": (0, 0), "Node_02": (400, 0), "Node_03": (800, 0)}
        r = t.compute_schedule(coords)
        self.assertEqual(len(r["matrix"]), 3)

    def test_spatial_reuse_beyond_two_hops(self):
        # A chain of 5 nodes 400 m apart: ends are 4 hops apart and may share.
        coords = {f"Node_{i + 1:02d}": (i * 400.0, 0.0) for i in range(5)}
        r = t.compute_schedule(coords)
        self.assertEqual(len(r["matrix"]), 3)
        self.assertEqual(r["slots"]["Node_01"], r["slots"]["Node_04"])

    def test_exact_range_boundary_is_a_link(self):
        coords = {"Node_01": (0, 0), "Node_02": (500, 0)}
        r = t.compute_schedule(coords)
        self.assertEqual(len(r["matrix"]), 2)
        coords = {"Node_01": (0, 0), "Node_02": (500.01, 0)}
        r = t.compute_schedule(coords)
        self.assertEqual(len(r["matrix"]), 1)

    def test_duplicate_coordinates_are_adjacent(self):
        coords = {"Node_01": (10, 10), "Node_02": (10, 10)}
        self.assertEqual(len(t.compute_schedule(coords)["matrix"]), 2)

    def test_fully_disconnected_uses_one_slot(self):
        coords = {f"Node_{i + 1:02d}": (i * 2000.0, 0.0) for i in range(16)}
        r = t.compute_schedule(coords)
        self.assertEqual(len(r["matrix"]), 1)

    def test_fully_connected_needs_one_slot_per_node(self):
        coords = {f"Node_{i + 1:02d}": (i * 10.0, 0.0) for i in range(16)}
        self.assertEqual(len(t.compute_schedule(coords)["matrix"]), 16)

    def test_deterministic_for_same_seed(self):
        rng = random.Random(3)
        coords = random_coords(rng, 16, 900)
        a = t.compute_schedule(coords, seed=5)["slots"]
        b = t.compute_schedule(coords, seed=5)["slots"]
        self.assertEqual(a, b)

    def test_verifier_catches_a_bad_schedule(self):
        coords = {"Node_01": (0, 0), "Node_02": (400, 0), "Node_03": (800, 0)}
        g = t.build_connectivity_graph(coords, 500.0)
        bad = [[1, 0, 1], [0, 1, 0]]  # Node_01 and Node_03 collide at Node_02
        self.assertTrue(t.verify_schedule(g, bad, list(coords)))


class InputHandling(unittest.TestCase):
    def test_rejects_bad_json(self):
        with self.assertRaises(ValueError):
            t.parse_coordinates("{not json")

    def test_rejects_wrong_shapes(self):
        for raw in ('[]', '{}', '{"a": [1]}', '{"a": [1, "x"]}',
                    '{"a": [1, 2, 3]}', '{"a": [true, 1]}', '{"a": [1e999, 0]}'):
            with self.assertRaises(ValueError, msg=raw):
                t.parse_coordinates(raw)

    def test_natural_sort_order(self):
        coords = t.parse_coordinates('{"Node_10":[0,0],"Node_2":[1,1],"Node_1":[2,2]}')
        self.assertEqual(list(coords), ["Node_1", "Node_2", "Node_10"])


class CommandLine(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "tdma_scheduler.py", *args],
                              capture_output=True, text=True)

    def test_json_argument(self):
        p = self.run_cli(json.dumps(t.DEMO_NODES))
        self.assertEqual(p.returncode, 0)
        self.assertIn("Schedule verified conflict-free", p.stdout)

    def test_bad_input_exits_2_without_traceback(self):
        p = self.run_cli("nonsense")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
