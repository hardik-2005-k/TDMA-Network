#!/usr/bin/env python3
"""Compare what EMANE delivered with what the TDMA schedule predicts.

For every ordered pair (transmitter A, receiver B) the schedule alone
decides whether B can decode A, given that every radio always has traffic
queued (the traffic generator saturates every transmit slot):

* A and B are not 1-hop neighbours      -> never (out of range).
* B transmits in A's slot              -> never (half duplex: B is talking).
* another neighbour C of B shares the slot -> never (collision at B; this is
  the hidden-terminal case distance-2 colouring exists to prevent).
* otherwise                            -> always.

The measured packet delivery ratio (PDR) is ``unique datagrams B got from A
/ datagrams A sent``. A pair *agrees* when the prediction is "always" and
PDR >= 0.9, or the prediction is "never" and PDR <= 0.1.

Usage::

    python emane/analyze.py results/part1 results/distance1 results/allslot0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DELIVER_MIN = 0.9
BLOCK_MAX = 0.1


def predict(order, matrix, links):
    """Return {(a, b): (bool delivered, reason)} for every ordered pair."""
    slot = {name: next(s for s, row in enumerate(matrix) if row[j])
            for j, name in enumerate(order)}
    nbrs = {n: set() for n in order}
    for a, b in links:
        nbrs[a].add(b)
        nbrs[b].add(a)
    out = {}
    for a in order:
        for b in order:
            if a == b:
                continue
            if b not in nbrs[a]:
                out[(a, b)] = (False, "out of range")
            elif slot[b] == slot[a]:
                out[(a, b)] = (False, "half duplex")
            else:
                hidden = sorted(c for c in nbrs[b] - {a} if slot[c] == slot[a])
                out[(a, b)] = (False, "collision with " + ",".join(hidden)) if hidden \
                    else (True, "clean")
    return out


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def analyse(run_dir: Path) -> dict:
    raw = json.loads((run_dir / "raw.json").read_text())
    order, ids = raw["node_order"], raw["nem_ids"]
    name_of = {str(v): k for k, v in ids.items()}
    pred = predict(order, raw["matrix"], raw["links"])
    sent = {name_of[n]: t["sent"] for n, t in raw["traffic"].items()}
    rx = {name_of[n]: {name_of[s]: c for s, c in t["rx"].items()}
          for n, t in raw["traffic"].items()}

    rows, agree, partial = [], 0, 0
    pdr = {}
    for (a, b), (ok, reason) in pred.items():
        p = rx[b].get(a, 0) / sent[a] if sent[a] else 0.0
        pdr[(a, b)] = p
        if (ok and p >= DELIVER_MIN) or (not ok and p <= BLOCK_MAX):
            agree += 1
        elif BLOCK_MAX < p < DELIVER_MIN:
            partial += 1
        if reason != "out of range" or p > 0:
            rows.append((a, b, ok, reason, p))

    # EMANE's own view: bytes accepted vs dropped for SINR, summed over radios.
    acc = sum(to_int(r.get("Num Bytes Rx")) for s in raw["emane"].values() for r in s["accept"])
    sinr = sum(to_int(r.get("SINR")) for s in raw["emane"].values() for r in s["drop"])
    tx_valid = sum(to_int(r.get("Valid")) for s in raw["emane"].values() for r in s["tx_slots"])
    tx_missed = sum(to_int(r.get("Missed")) for s in raw["emane"].values() for r in s["tx_slots"])
    rx_valid = sum(to_int(r.get("Valid")) for s in raw["emane"].values() for r in s["rx_slots"])
    rx_missed = sum(to_int(r.get("Missed")) for s in raw["emane"].values() for r in s["rx_slots"])
    accepted = [v["accept_full"] for v in raw["installed_schedule"].values()]

    return {
        "variant": raw["variant"], "raw": raw, "order": order, "pdr": pdr, "pred": pred,
        "rows": rows, "pairs": len(pred), "agree": agree, "partial": partial,
        "frame": len(raw["matrix"]), "links": len(raw["links"]),
        "emane_bytes_accepted": acc, "emane_bytes_sinr_drop": sinr,
        "tx_valid": tx_valid, "tx_missed": tx_missed,
        "rx_valid": rx_valid, "rx_missed": rx_missed,
        "schedules_accepted": sum(1 for a in accepted if a and a >= 1),
        "schedule_mismatch": raw["schedule_mismatch"],
    }


def short(name: str) -> str:
    return name.split("_")[-1]


def pdr_grid(r: dict) -> str:
    """ASCII receiver x transmitter PDR grid in percent ('.' = out of range)."""
    order = r["order"]
    head = "rx\\tx | " + " ".join(f"{short(n):>3}" for n in order)
    lines = [head, "-" * len(head)]
    for b in order:
        cells = []
        for a in order:
            if a == b:
                cells.append("  -")
            elif r["pred"][(a, b)][1] == "out of range" and r["pdr"][(a, b)] == 0:
                cells.append("  .")
            else:
                cells.append(f"{round(100 * r['pdr'][(a, b)]):>3}")
        lines.append(f"{short(b):>5} | " + " ".join(cells))
    return "\n".join(lines)


def report(r: dict) -> str:
    in_range = [x for x in r["rows"] if x[3] != "out of range"]
    clean = [x for x in in_range if x[2]]
    blocked = [x for x in in_range if not x[2]]
    mean = lambda xs: sum(x[4] for x in xs) / len(xs) if xs else float("nan")
    out = [
        f"# EMANE run: `{r['variant']}`", "",
        f"- Frame length: **{r['frame']} slots** of 1 ms",
        f"- Radios that accepted the full schedule: **{r['schedules_accepted']}/16**",
        f"- Installed TX slots differ from the matrix on: "
        f"**{r['schedule_mismatch'] or 'no radio'}**",
        f"- Traffic: {r['raw']['rate_pps']:.0f} broadcast datagrams/s per radio, "
        f"{r['raw']['payload_bytes']} B payload, {r['raw']['duration_s']:.0f} s",
        f"- Directed in-range links: {len(in_range)} "
        f"(schedule predicts {len(clean)} clean, {len(blocked)} blocked)",
        f"- Mean PDR on links predicted clean: **{100 * mean(clean):.1f} %**",
        f"- Mean PDR on links predicted blocked: **{100 * mean(blocked):.1f} %**",
        f"- Ordered pairs where EMANE agrees with the schedule: "
        f"**{r['agree']}/{r['pairs']}** (partial: {r['partial']})",
        f"- EMANE MAC bytes accepted: {r['emane_bytes_accepted']:,}; "
        f"dropped for low SINR (collisions): {r['emane_bytes_sinr_drop']:,}",
        f"- TX slots valid/missed: {r['tx_valid']:,}/{r['tx_missed']:,}; "
        f"RX slots valid/missed: {r['rx_valid']:,}/{r['rx_missed']:,}",
        "", "## PDR grid (%), rows = receiver, columns = transmitter", "",
        "```", pdr_grid(r), "```", "",
        "## Links the schedule predicts as blocked", "",
        "| TX | RX | Reason | Measured PDR |", "|---|---|---|---|",
    ]
    out += [f"| {a} | {b} | {why} | {100 * p:.1f} % |" for a, b, _, why, p in blocked] \
        or ["| - | - | none | - |"]
    return "\n".join(out) + "\n"


def summary(results: list[dict]) -> str:
    out = ["# EMANE validation summary", "",
           "| Variant | Slots | Clean links PDR | Blocked links PDR | Agreement | SINR drops (B) |",
           "|---|---|---|---|---|---|"]
    for r in results:
        inr = [x for x in r["rows"] if x[3] != "out of range"]
        c = [x[4] for x in inr if x[2]]
        b = [x[4] for x in inr if not x[2]]
        f = lambda xs: f"{100 * sum(xs) / len(xs):.1f} % ({len(xs)})" if xs else "n/a (0)"
        out.append(f"| {r['variant']} | {r['frame']} | {f(c)} | {f(b)} | "
                   f"{r['agree']}/{r['pairs']} | {r['emane_bytes_sinr_drop']:,} |")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    dirs = [Path(a) for a in argv] or sorted(p.parent for p in Path("results").glob("*/raw.json"))
    results = []
    for d in dirs:
        r = analyse(d)
        (d / "report.md").write_text(report(r), encoding="utf-8")
        print(report(r))
        results.append(r)
    if results:
        s = summary(results)
        (dirs[0].parent / "SUMMARY.md").write_text(s, encoding="utf-8")
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
