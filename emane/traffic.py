#!/usr/bin/env python3
"""Per-node UDP broadcast traffic for the EMANE TDMA experiment.

Runs inside one radio's network namespace. It does two things at once:

* sends ``--rate`` broadcast datagrams per second to ``--dst`` (the radio
  subnet broadcast address), each carrying ``(sender NEM id, sequence)``;
* receives every datagram on the same port and counts unique sequence
  numbers per sender.

Broadcast is used so that a single transmission is a test of every 1-hop
link at once, which is exactly what a TDMA slot is: one transmitter, every
neighbour listening. No routing protocol is involved, so a datagram is only
counted if EMANE delivered it directly over one hop.

At the end a JSON summary is written:
``{"nem": n, "sent": k, "rx": {"<src nem>": unique_count, ...}}``.
"""

from __future__ import annotations

import argparse
import json
import select
import socket
import struct
import time

HEADER = struct.Struct("!HI")  # sender NEM id, sequence number


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nem", type=int, required=True)
    ap.add_argument("--dst", default="10.100.0.255")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--rate", type=float, default=500.0, help="datagrams per second")
    ap.add_argument("--size", type=int, default=100, help="UDP payload bytes")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds of sending")
    ap.add_argument("--linger", type=float, default=2.0, help="seconds to keep receiving")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"emane0")

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    rx.bind(("", args.port))
    rx.setblocking(False)

    padding = b"\x00" * max(0, args.size - HEADER.size)
    interval = 1.0 / args.rate
    seen: dict[int, set[int]] = {}
    seq = 0
    start = time.monotonic()
    next_tx = start
    stop_tx = start + args.duration
    stop_rx = stop_tx + args.linger

    while True:
        now = time.monotonic()
        if now >= stop_rx:
            break
        if now < stop_tx and now >= next_tx:
            tx.sendto(HEADER.pack(args.nem, seq) + padding, (args.dst, args.port))
            seq += 1
            next_tx += interval
            continue
        timeout = max(0.0, min(next_tx if now < stop_tx else stop_rx, stop_rx) - now)
        ready, _, _ = select.select([rx], [], [], timeout)
        if not ready:
            continue
        while True:
            try:
                data = rx.recv(65535)
            except BlockingIOError:
                break
            if len(data) < HEADER.size:
                continue
            src, s = HEADER.unpack_from(data)
            if src != args.nem:  # local loopback of our own broadcast
                seen.setdefault(src, set()).add(s)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"nem": args.nem, "sent": seq,
                   "rx": {str(k): len(v) for k, v in sorted(seen.items())}}, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
