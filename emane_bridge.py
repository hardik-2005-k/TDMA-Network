#!/usr/bin/env python3
"""Bridge from the Part 1 TDMA schedule to EMANE input files (Part 2).

Purpose
-------
Turn the Slot x Node matrix produced by ``tdma_scheduler.py`` into the files
EMANE needs in order to enforce that schedule:

* ``schedule_<variant>.xml``: an ``emane-tdma-schedule`` full schedule that is
  sent to every NEM with ``emaneevent-tdmaschedule``.
* ``pathloss.eel``: pathloss events for the EEL event generator, so EMANE
  models the same 500 m unit-disk connectivity that Part 1 scheduled for.

Model
-----
* One frame per multiframe. The frame has as many slots as the Part 1 frame
  length, so Part 1 slot ``s`` becomes EMANE slot index ``s``.
* The NEM id is the trailing number of the node name (``Node_07`` is NEM 7).
* Each ``<slot>`` lists the NEMs that transmit in it. Slots that are not
  listed default to receive for every NEM in the schedule (EMANE TDMA guide,
  "Working with TDMA Schedules", item 4), so receive slots are never written.
* Spatial reuse shows up as several NEMs in one ``<slot nodes="...">`` entry.

Schedule variants
-----------------
* ``part1``: the distance-2 schedule from Part 1. This is the real output.
* ``distance1``: a colouring of the 1-hop graph only. It avoids direct
  conflicts but lets hidden terminals share a slot. It is the main negative
  control because it isolates exactly what distance-2 colouring adds.
* ``allslot0``: every NEM transmits in slot 0. A crude control: loss here is
  caused by half-duplex radios that never listen, not only by collisions.

Usage
-----
    python emane_bridge.py --schedule schedule.json --coords sample_nodes.json
    python emane_bridge.py --coords sample_nodes.json   # runs Part 1 first

References
----------
* EMANE TDMA radio model guide: https://emane.io/tdma-radio-model
* Schedule schema: adjacentlink/emane, src/python/emane/events/schema/
  tdmaschedule.xsd (vendored in ``emane/schema/``).
* EEL pathloss sentence: https://emane.io/eel-event-generator
* Free-space pathloss: Friis transmission equation, H. T. Friis (1946),
  "A Note on a Simple Transmission Formula".
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

import tdma_scheduler

# ---------------------------------------------------------------------------
# Radio and schedule parameters. Each value is written into the schedule XML
# or the EEL file, so this block is the single place to change them.
# ---------------------------------------------------------------------------

# The brief asks for 1 ms slots. EMANE expects microseconds.
SLOT_DURATION_US = 1000

# The guide says slot overhead must be at least the maximum propagation delay
# ("TDMA Schedule" section). 500 m takes 1.67 us, so 10 us covers it with
# margin for timer jitter in a software emulator. Cost: 1 % of each slot.
SLOT_OVERHEAD_US = 10

# Part 1 produces one recurring frame, so the multiframe holds one frame.
FRAMES_PER_MULTIFRAME = 1

# 2.4 GHz ISM band, the same band the EMANE guide examples use. With
# precomputed pathloss the frequency only matters for in-band overlap, since
# every NEM shares it, every concurrent transmission is co-channel.
FREQUENCY_HZ = 2_400_000_000

# Receiver bandwidth. Sets the noise floor:
# -174 dBm/Hz + 10*log10(10 MHz) + 4 dB noise figure = -100 dBm.
BANDWIDTH_HZ = 10_000_000

# 100 mW, a typical tactical handheld level.
TX_POWER_DBM = 20.0

# Chosen from the official TDMA PCR curves (tdmabasemodelpcr.xml): at 9 Mbps
# the probability of reception is 99.7 % at 5 dB SINR and 0.3 % at 0 dB.
# A clean link here has 26 dB SNR, so it always succeeds. One equal power
# interferer pulls SINR to about 0 dB, so a collision always fails. That
# makes collisions visible instead of being hidden by a forgiving curve.
DATARATE_BPS = 9_000_000

# Queue 0 (best effort). Matches the default DSCP of test traffic.
SERVICE_CLASS = 0

# Pathloss for pairs beyond radio range. Receive power becomes
# 20 - 200 = -180 dBm, 80 dB under the noise floor, so the pair neither
# communicates nor adds measurable interference, as Part 1 assumes.
OUT_OF_RANGE_PATHLOSS_DB = 200.0

SPEED_OF_LIGHT_M_S = 299_792_458.0

# Pathloss is static, so every event is published at scenario time 0.
EEL_START_TIME_S = 0.0

VARIANTS = ("part1", "distance1", "allslot0")
DEFAULT_XSD = Path(__file__).resolve().parent / "emane" / "schema" / "tdmaschedule.xsd"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "emane" / "generated"


# ---------------------------------------------------------------------------
# Part 1 input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Part1Result:
    """The parts of the Part 1 output that the bridge needs.

    Attributes:
        node_order: Node names in matrix column order.
        matrix: Slot x Node matrix, ``matrix[s][j] == 1`` when node ``j``
            transmits in slot ``s``.
        links: 1-hop links (pairs of node names) of the connectivity graph.
        radio_range_m: Radio range that Part 1 used, in metres.
    """

    node_order: list[str]
    matrix: list[list[int]]
    links: list[tuple[str, str]]
    radio_range_m: float


def load_schedule_json(path: Path) -> Part1Result:
    """Read the file written by ``tdma_scheduler.py --json-out``.

    Args:
        path: Path to ``schedule.json``.

    Returns:
        The parsed and validated Part 1 result.

    Raises:
        ValueError: If a required key is missing or the matrix is malformed.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        result = Part1Result(
            node_order=list(data["node_order"]),
            matrix=[list(row) for row in data["matrix"]],
            links=[tuple(link) for link in data["links"]],
            radio_range_m=float(data["radio_range_m"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing key {exc}") from exc
    validate_matrix(result.matrix, result.node_order)
    return result


def result_from_coords(coords: dict[str, tuple[float, float]]) -> Part1Result:
    """Run Part 1 directly and keep only what the bridge needs.

    Args:
        coords: Node name to (x, y) in metres, as from
            ``tdma_scheduler.parse_coordinates``.

    Returns:
        The Part 1 result for these coordinates.

    Raises:
        ValueError: If Part 1 reports a conflict, which would make the
            schedule unsafe to emulate.
    """
    r = tdma_scheduler.compute_schedule(coords)
    if r["problems"]:
        raise ValueError(f"Part 1 schedule failed verification: {r['problems']}")
    links = sorted(tuple(sorted(e)) for e in r["graph"].edges)
    return Part1Result(r["order"], r["matrix"], links, r["radio_range"])


def validate_matrix(matrix: list[list[int]], order: list[str]) -> None:
    """Check the Part 1 contract: binary values, every node in exactly one slot.

    Args:
        matrix: Slot x Node matrix.
        order: Node names in column order.

    Raises:
        ValueError: On any violation, naming the offending node or slot.
    """
    if not matrix:
        raise ValueError("matrix has no slots")
    for s, row in enumerate(matrix):
        if len(row) != len(order):
            raise ValueError(f"slot {s} has {len(row)} columns, expected {len(order)}")
        if any(v not in (0, 1) for v in row):
            raise ValueError(f"slot {s} contains a value other than 0 or 1")
    for j, name in enumerate(order):
        count = sum(row[j] for row in matrix)
        if count != 1:
            raise ValueError(f"{name} transmits in {count} slots, expected 1")


def nem_ids(order: list[str]) -> dict[str, int]:
    """Map node names to EMANE NEM ids using the trailing number.

    ``Node_07`` becomes NEM 7. The schema only accepts ids from 1 upward
    (``NodeType`` pattern ``[1-9][0-9]*``).

    Args:
        order: Node names.

    Returns:
        Node name to NEM id.

    Raises:
        ValueError: If a name has no trailing number, maps to 0, or two
            names map to the same id (for example ``Node_1`` and ``Node_01``).
    """
    ids: dict[str, int] = {}
    for name in order:
        m = re.search(r"(\d+)$", name)
        if not m or int(m.group(1)) == 0:
            raise ValueError(f"cannot derive a NEM id (>= 1) from node name {name!r}")
        ids[name] = int(m.group(1))
    if len(set(ids.values())) != len(ids):
        raise ValueError("two node names map to the same NEM id")
    return ids


# ---------------------------------------------------------------------------
# Schedule variants
# ---------------------------------------------------------------------------

def graph_from_links(order: list[str], links: list[tuple[str, str]]) -> nx.Graph:
    """Rebuild the Part 1 connectivity graph from its node list and links."""
    g = nx.Graph()
    g.add_nodes_from(order)
    g.add_edges_from(links)
    return g


def distance1_matrix(result: Part1Result) -> list[list[int]]:
    """Return a schedule that only separates 1-hop neighbours.

    Colours the connectivity graph G instead of G squared. Direct neighbours
    still get different slots, but two nodes that share a neighbour may
    collide at it. Reuses Part 1 helpers so slot numbering follows the same
    rules.

    Args:
        result: The Part 1 result.

    Returns:
        Slot x Node matrix for the distance-1 schedule.
    """
    g = graph_from_links(result.node_order, result.links)
    colour = nx.coloring.greedy_color(g, strategy="largest_first")
    slots = tdma_scheduler.canonicalise(colour, result.node_order)
    return tdma_scheduler.build_matrix(slots, result.node_order)


def allslot0_matrix(order: list[str], frame_length: int) -> list[list[int]]:
    """Return a schedule where every node transmits in slot 0.

    The frame length is kept equal to Part 1 so only the slot assignment
    differs between the two runs.
    """
    return [[1 if s == 0 else 0 for _ in order] for s in range(frame_length)]


def variant_matrix(result: Part1Result, variant: str) -> list[list[int]]:
    """Return the Slot x Node matrix for one of ``VARIANTS``.

    Raises:
        ValueError: For an unknown variant name.
    """
    if variant == "part1":
        return result.matrix
    if variant == "distance1":
        return distance1_matrix(result)
    if variant == "allslot0":
        return allslot0_matrix(result.node_order, len(result.matrix))
    raise ValueError(f"unknown variant {variant!r}, expected one of {VARIANTS}")


# ---------------------------------------------------------------------------
# Index lists ("1,4,7:9")
# ---------------------------------------------------------------------------

def compress_ids(ids: list[int]) -> str:
    """Write ids in EMANE index syntax, using ``a:b`` for runs of 3 or more.

    Runs of two stay as ``a,b`` because that is easier to read than ``a:b``.
    """
    values = sorted(set(ids))
    parts: list[str] = []
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[j + 1] == values[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{values[i]}:{values[j]}")
        else:
            parts.extend(str(v) for v in values[i:j + 1])
        i = j + 1
    return ",".join(parts)


def expand_ids(text: str) -> list[int]:
    """Expand EMANE index syntax into a sorted list of ids.

    Mirrors ``expandIndex`` in emane/src/python/emane/events/tdmaschedule.py,
    with stricter checks so that malformed input fails loudly.

    Raises:
        ValueError: On an empty token, a non-numeric token or a reversed range.
    """
    result: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        m = re.fullmatch(r"(\d+):(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi < lo:
                raise ValueError(f"reversed range {token!r}")
            result.update(range(lo, hi + 1))
        elif token.isdigit():
            result.add(int(token))
        else:
            raise ValueError(f"bad index token {token!r}")
    return sorted(result)


# ---------------------------------------------------------------------------
# Schedule XML
# ---------------------------------------------------------------------------

def max_propagation_delay_us(radio_range_m: float) -> float:
    """Return the one-way propagation delay across the radio range."""
    return radio_range_m / SPEED_OF_LIGHT_M_S * 1e6


def build_schedule_xml(matrix: list[list[int]], order: list[str],
                       radio_range_m: float, label: str) -> str:
    """Return a full ``emane-tdma-schedule`` document for the matrix.

    Args:
        matrix: Slot x Node matrix (validated by ``validate_matrix``).
        order: Node names in column order.
        radio_range_m: Used to check that the slot overhead covers the
            propagation delay.
        label: Short description written into the header comment.

    Returns:
        The XML document as a string.

    Raises:
        ValueError: If the slot overhead is shorter than the propagation
            delay, which the guide warns causes frames to be discarded.
    """
    if SLOT_OVERHEAD_US < max_propagation_delay_us(radio_range_m):
        raise ValueError("SLOT_OVERHEAD_US is shorter than the propagation delay")

    ids = nem_ids(order)
    root = ET.Element("emane-tdma-schedule")
    ET.SubElement(root, "structure", {
        "frames": str(FRAMES_PER_MULTIFRAME),
        "slots": str(len(matrix)),
        "slotoverhead": str(SLOT_OVERHEAD_US),
        "slotduration": str(SLOT_DURATION_US),
        "bandwidth": str(BANDWIDTH_HZ),
    })
    multiframe = ET.SubElement(root, "multiframe", {
        "frequency": str(FREQUENCY_HZ),
        "power": f"{TX_POWER_DBM:.1f}",
        "class": str(SERVICE_CLASS),
        "datarate": str(DATARATE_BPS),
    })
    frame = ET.SubElement(multiframe, "frame", {"index": "0"})
    for s, row in enumerate(matrix):
        transmitters = [ids[order[j]] for j, v in enumerate(row) if v]
        if not transmitters:
            continue  # An unlisted slot is a receive slot for every NEM.
        slot = ET.SubElement(frame, "slot", {"index": str(s),
                                             "nodes": compress_ids(transmitters)})
        # An empty <slot> already means transmit; <tx/> makes that explicit.
        ET.SubElement(slot, "tx")

    ET.indent(root, space="  ")
    header = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        f"<!-- Generated by emane_bridge.py ({label}). Do not edit by hand. -->\n"
    )
    return header + ET.tostring(root, encoding="unicode") + "\n"


def parse_schedule_xml(text: str, order: list[str]) -> tuple[dict[str, int], list[list[int]]]:
    """Read a schedule back into a structure dict and a Slot x Node matrix.

    Used by the tests and by the live verification step to prove the XML
    carries exactly the Part 1 matrix. Only frame 0 is read, matching
    ``FRAMES_PER_MULTIFRAME``.

    Args:
        text: Schedule XML.
        order: Node names in the column order wanted for the matrix.

    Returns:
        ``(structure, matrix)`` where ``structure`` holds the integer
        attributes of ``<structure>``.

    Raises:
        ValueError: If the schedule is not a full schedule, refers to an
            unknown NEM, or uses a slot index outside the frame.
    """
    root = ET.fromstring(text)
    node = root.find("structure")
    if node is None:
        raise ValueError("no <structure>: this is an update, not a full schedule")
    structure = {k: int(v) for k, v in node.attrib.items() if v.isdigit()}
    column = {nem: j for j, nem in enumerate(nem_ids(order)[n] for n in order)}
    matrix = [[0] * len(order) for _ in range(structure["slots"])]
    for frame in root.iter("frame"):
        if expand_ids(frame.get("index", "")) != [0]:
            raise ValueError("expected a single frame with index 0")
        for slot in frame.iter("slot"):
            child = next(iter(slot), None)
            if child is not None and child.tag != "tx":
                continue  # rx and idle entries are not transmit assignments.
            for s in expand_ids(slot.get("index", "")):
                if s >= structure["slots"]:
                    raise ValueError(f"slot index {s} outside frame of {structure['slots']}")
                for nem in expand_ids(slot.get("nodes", "")):
                    if nem not in column:
                        raise ValueError(f"NEM {nem} is not one of the scheduled nodes")
                    matrix[s][column[nem]] = 1
    return structure, matrix


def validate_xsd(text: str, xsd_path: Path) -> list[str]:
    """Validate schedule XML against the official schema.

    EMANE flushes its whole schedule when it receives one with an error
    (guide item 11), so invalid XML must never be sent.

    Args:
        text: Schedule XML.
        xsd_path: Path to ``tdmaschedule.xsd``.

    Returns:
        Schema error messages. Empty means valid.

    Raises:
        ImportError: If lxml is not installed (the stdlib cannot do XSD).
    """
    from lxml import etree  # Imported here so the rest works without lxml.

    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    doc = etree.fromstring(text.encode("utf-8"))
    if schema.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log]


# ---------------------------------------------------------------------------
# Topology: pathloss events
# ---------------------------------------------------------------------------

def free_space_pathloss_db(distance_m: float, frequency_hz: float) -> float:
    """Return free-space pathloss, 20*log10(4*pi*d*f/c), in dB."""
    return 20 * math.log10(4 * math.pi * distance_m * frequency_hz / SPEED_OF_LIGHT_M_S)


def in_range_pathloss_db(radio_range_m: float) -> float:
    """Return the pathloss used for every in-range pair.

    Every in-range link gets the free-space loss at the range edge, which
    gives a unit-disk model: a link either exists at full quality or does not
    exist. That matches Part 1, which has no notion of link quality. A
    distance based value would let a near transmitter capture the receiver
    over a far interferer, and then a hidden-terminal collision might go
    unnoticed in the emulation even though Part 1 counts it as a conflict.
    At 500 m and 2.4 GHz this is 94.0 dB, giving -74 dBm received and
    26 dB SNR.
    """
    return free_space_pathloss_db(radio_range_m, FREQUENCY_HZ)


def in_range_pairs(coords: dict[str, tuple[float, float]],
                   radio_range_m: float) -> set[tuple[str, str]]:
    """Return sorted name pairs within range, using Part 1's inclusive rule."""
    names = list(coords)
    return {
        tuple(sorted((a, b)))
        for i, a in enumerate(names) for b in names[i + 1:]
        if math.dist(coords[a], coords[b]) <= radio_range_m
    }


def check_links_match(coords: dict[str, tuple[float, float]], result: Part1Result) -> None:
    """Make sure the coordinates describe the same graph Part 1 scheduled.

    Catches a stale ``schedule.json`` or the wrong coordinates file, either
    of which would make the emulation and the schedule model different
    networks.

    Raises:
        ValueError: If the node sets or the link sets differ.
    """
    if set(coords) != set(result.node_order):
        raise ValueError("coordinates and schedule list different nodes")
    expected = {tuple(sorted(link)) for link in result.links}
    actual = in_range_pairs(coords, result.radio_range_m)
    if expected != actual:
        diff = sorted(expected ^ actual)
        raise ValueError(f"coordinates do not reproduce the schedule links: {diff}")


def build_pathloss_eel(coords: dict[str, tuple[float, float]], order: list[str],
                       radio_range_m: float) -> str:
    """Return EEL pathloss sentences for every ordered NEM pair.

    One sentence per NEM lists its pathloss to every other NEM, in the form
    ``<time> nem:<id> pathloss nem:<other>,<dB> ...`` from the EEL guide.
    Both directions are written, so the file does not depend on which end
    of the pair EMANE treats as the transmitter. Pathloss is symmetric here.

    Args:
        coords: Node name to (x, y) in metres.
        order: Node names in NEM order.
        radio_range_m: Range limit, inclusive.

    Returns:
        EEL file contents.
    """
    ids = nem_ids(order)
    near = in_range_pairs(coords, radio_range_m)
    near_db = in_range_pathloss_db(radio_range_m)
    lines = []
    for a in order:
        entries = []
        for b in order:
            if a == b:
                continue
            db = near_db if tuple(sorted((a, b))) in near else OUT_OF_RANGE_PATHLOSS_DB
            entries.append(f"nem:{ids[b]},{db:.1f}")
        lines.append(f"{EEL_START_TIME_S:.1f} nem:{ids[a]} pathloss " + " ".join(entries))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def write_outputs(result: Part1Result, coords: dict[str, tuple[float, float]] | None,
                  out_dir: Path, xsd_path: Path | None, source: str) -> list[str]:
    """Generate, check and write all schedule variants and the pathloss file.

    Args:
        result: The Part 1 result.
        coords: Coordinates for the pathloss file, or None to skip it.
        out_dir: Output directory, created if missing.
        xsd_path: Schema for validation, or None to skip validation.
        source: Where the schedule came from, for the XML header comment.

    Returns:
        Human readable summary lines.

    Raises:
        ValueError: If any generated schedule fails the schema or does not
            round-trip to the matrix it came from.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    g = graph_from_links(result.node_order, result.links)
    summary = []
    for variant in VARIANTS:
        matrix = variant_matrix(result, variant)
        validate_matrix(matrix, result.node_order)
        xml = build_schedule_xml(matrix, result.node_order, result.radio_range_m,
                                 f"variant {variant}, source {source}")
        if xsd_path is not None:
            errors = validate_xsd(xml, xsd_path)
            if errors:
                raise ValueError(f"{variant} schedule fails the XSD: {errors}")
        _, back = parse_schedule_xml(xml, result.node_order)
        if back != matrix:
            raise ValueError(f"{variant} schedule does not round-trip to its matrix")
        (out_dir / f"schedule_{variant}.xml").write_text(xml, encoding="utf-8")
        conflicts = tdma_scheduler.verify_schedule(g, matrix, result.node_order)
        summary.append(f"schedule_{variant}.xml: {len(matrix)} slots, "
                       f"{len(conflicts)} two-hop conflicts")
    if coords is not None:
        check_links_match(coords, result)
        eel = build_pathloss_eel(coords, result.node_order, result.radio_range_m)
        # Force Unix line endings. EMANE's EEL pathloss loader parses each line
        # as fields and treats a trailing CR as part of the last number, which
        # aborts the event service. Writing on Windows would otherwise turn
        # every \n into \r\n.
        (out_dir / "pathloss.eel").write_text(eel, encoding="utf-8", newline="\n")
        summary.append(f"pathloss.eel: {len(result.links)} in-range links at "
                       f"{in_range_pathloss_db(result.radio_range_m):.1f} dB, "
                       f"others at {OUT_OF_RANGE_PATHLOSS_DB:.1f} dB")
    return summary


def main(argv: list[str] | None = None) -> int:
    """Run the bridge from the command line. Return a process exit code."""
    ap = argparse.ArgumentParser(description="Convert the Part 1 schedule to EMANE input.")
    ap.add_argument("--schedule", type=Path, help="schedule.json from tdma_scheduler.py")
    ap.add_argument("--coords", type=Path,
                    help="node coordinates JSON; needed for pathloss.eel, and used "
                         "to run Part 1 when --schedule is not given")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--xsd", type=Path, default=DEFAULT_XSD)
    ap.add_argument("--skip-xsd", action="store_true",
                    help="skip schema validation (only if lxml is unavailable)")
    args = ap.parse_args(argv)

    try:
        coords = None
        if args.coords:
            coords = tdma_scheduler.parse_coordinates(args.coords.read_text(encoding="utf-8"))
        if args.schedule:
            result, source = load_schedule_json(args.schedule), args.schedule.name
        elif coords is not None:
            result, source = result_from_coords(coords), "compute_schedule()"
        else:
            ap.error("give --schedule, --coords or both")
        xsd = None if args.skip_xsd else args.xsd
        for line in write_outputs(result, coords, args.out_dir, xsd, source):
            print(line)
    except (OSError, ValueError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote files to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
