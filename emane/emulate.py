#!/usr/bin/env python3
"""Run one EMANE TDMA experiment inside the vmn-emane container.

What it builds
--------------
* One Linux network namespace per radio (``nem1`` ... ``nem16``). Each runs
  its own ``emane`` process with the TDMA radio model, exactly like a
  separate radio box. Splitting them into namespaces matters: if all
  virtual interfaces lived in one namespace the kernel would short-circuit
  traffic between them and EMANE would never see it.
* A bridge ``br0`` (10.99.0.100/24) joining all namespaces. EMANE uses it
  as the control backchannel: the over-the-air (OTA) multicast group that
  carries emulated transmissions, and the event multicast group that
  carries pathloss and TDMA schedule events. It is *not* the radio link;
  the radio link is ``emane0`` (10.100.0.<nem>/24) inside each namespace.
* ``emaneeventservice`` in the root namespace replays ``pathloss.eel`` so
  EMANE enforces the same 500 m unit-disk topology Part 1 scheduled for.
* ``emaneevent-tdmaschedule`` publishes the chosen schedule XML.

Then every radio broadcasts UDP for ``--duration`` seconds and counts what
it hears from each neighbour, and EMANE's own per-source accept/drop
tables are dumped. Everything is written to ``results/<variant>/``.

Usage (inside the container, project mounted at /work)::

    python3 emane/emulate.py --variant part1
    python3 emane/emulate.py --variant distance1
    python3 emane/emulate.py --variant allslot0
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import emane_bridge  # noqa: E402  (needs the sys.path entry above)

GEN_DIR = ROOT / "emane" / "generated"
PCR_FILE = ROOT / "emane" / "tdma_pcr.xml"
TRAFFIC = ROOT / "emane" / "traffic.py"
RUN_DIR = Path("/tmp/vmn-emu")

BRIDGE = "br0"
BRIDGE_IP = "10.99.0.100"
OTA_GROUP = "224.1.2.8:45702"
EVENT_GROUP = "224.1.2.8:45703"
CONTROL_PORT = 47000

# The radio model and PHY configuration. Kept next to the code that writes
# them so every value in the generated XML can be traced to a reason.
MAC_PARAMS = {
    # Pack several small datagrams into one slot, as a real TDMA MAC would.
    "queue.aggregationenable": "on",
    "queue.aggregationslotthreshold": "90.0",
    "queue.fragmentationenable": "on",
    "queue.depth": "255",
    "queue.strictdequeueenable": "off",
    "fragmentcheckthreshold": "2",
    "fragmenttimeoutthreshold": "5",
    "neighbormetricdeletetime": "60.0",
    "neighbormetricupdateinterval": "1.0",
    "pcrcurveuri": "emane-tdma-pcr.xml",
}
PHY_PARAMS = {
    "fixedantennagain": "0.0",
    "fixedantennagainenable": "on",
    # Listen on the schedule's transmit frequency. The EMANE PHY defaults
    # frequencyofinterest to 2347 MHz, so without this the receiver ignores
    # the 2.4 GHz transmissions as out of band and nothing is ever received.
    "frequencyofinterest": str(emane_bridge.FREQUENCY_HZ),
    # Same receiver bandwidth as the schedule <structure bandwidth>.
    "bandwidth": str(emane_bridge.BANDWIDTH_HZ),
    # Every overlapping signal counts as interference. This is what makes
    # a hidden-terminal transmission destroy a reception.
    "noisemode": "all",
    # Pathloss comes from pathloss.eel, not from positions.
    "propagationmodel": "precomputed",
    "systemnoisefigure": "4.0",
    "subid": "7",
    "fading.model": "none",
}


def sh(cmd: str, check: bool = True, capture: bool = False) -> str:
    """Run a shell command, echoing it, and return stdout when captured."""
    print(f"$ {cmd}", flush=True)
    res = subprocess.run(cmd, shell=True, check=check, text=True,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.STDOUT if capture else None)
    return res.stdout or ""


def params(d: dict[str, str]) -> str:
    return "\n".join(f'    <param name="{k}" value="{v}"/>' for k, v in d.items())


# ---------------------------------------------------------------------------
# Configuration files
# ---------------------------------------------------------------------------

def write_node_config(nem: int, node_dir: Path) -> None:
    """Write the four EMANE XML files one radio needs."""
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "emane-platform.xml").write_text(f"""<?xml version="1.0" ?>
<!DOCTYPE platform SYSTEM 'file:///usr/share/emane/dtd/platform.dtd'>
<platform>
  <param name="otamanagerchannelenable" value="on"/>
  <param name="otamanagerdevice" value="backchan0"/>
  <param name="otamanagergroup" value="{OTA_GROUP}"/>
  <param name="eventservicegroup" value="{EVENT_GROUP}"/>
  <param name="eventservicedevice" value="backchan0"/>
  <param name="controlportendpoint" value="0.0.0.0:{CONTROL_PORT}"/>
  <nem definition="emane-tdma-nem.xml" id="{nem}">
    <transport definition="emane-transvirtual.xml">
      <param name="device" value="emane0"/>
      <param name="address" value="10.100.0.{nem}"/>
      <param name="mask" value="255.255.255.0"/>
    </transport>
  </nem>
</platform>
""")
    (node_dir / "emane-tdma-nem.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE nem SYSTEM "file:///usr/share/emane/dtd/nem.dtd">
<nem>
  <transport definition="emane-transvirtual.xml"/>
  <mac definition="emane-tdma-radiomodel.xml"/>
  <phy>
{params(PHY_PARAMS)}
  </phy>
</nem>
""")
    (node_dir / "emane-tdma-radiomodel.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mac SYSTEM "file:///usr/share/emane/dtd/mac.dtd">
<mac library="tdmaeventschedulerradiomodel">
{params(MAC_PARAMS)}
</mac>
""")
    (node_dir / "emane-transvirtual.xml").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE transport SYSTEM "file:///usr/share/emane/dtd/transport.dtd">
<transport library="transvirtual"/>
""")
    shutil.copy(PCR_FILE, node_dir / "emane-tdma-pcr.xml")


def write_host_config(host_dir: Path) -> None:
    """Write the event service files that replay pathloss.eel."""
    host_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(GEN_DIR / "pathloss.eel", host_dir / "pathloss.eel")
    (host_dir / "eelgenerator.xml").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE eventgenerator SYSTEM "file:///usr/share/emane/dtd/eventgenerator.dtd">
<eventgenerator library="eelgenerator">
  <param name="inputfile" value="pathloss.eel"/>
  <paramlist name="loader">
    <item value="pathloss:eelloaderpathloss:delta"/>
  </paramlist>
</eventgenerator>
""")
    (host_dir / "eventservice.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE eventservice SYSTEM "file:///usr/share/emane/dtd/eventservice.dtd">
<eventservice>
  <param name="eventservicegroup" value="{EVENT_GROUP}"/>
  <param name="eventservicedevice" value="{BRIDGE}"/>
  <generator definition="eelgenerator.xml"/>
</eventservice>
""")


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

def teardown(nems: list[int]) -> None:
    sh("pkill -x emane; pkill -f emaneeventservice; pkill -f traffic.py", check=False)
    time.sleep(1)
    for n in nems:
        sh(f"ip netns del nem{n} 2>/dev/null", check=False)
    sh(f"ip link del {BRIDGE} 2>/dev/null", check=False)


def build_network(nems: list[int]) -> None:
    sh(f"ip link add {BRIDGE} type bridge")
    # Snooping would need IGMP queriers to forward multicast reliably.
    sh(f"echo 0 > /sys/class/net/{BRIDGE}/bridge/multicast_snooping")
    sh(f"ip addr add {BRIDGE_IP}/24 dev {BRIDGE}")
    sh(f"ip link set {BRIDGE} up")
    sh(f"ip route add 224.0.0.0/4 dev {BRIDGE}")
    for n in nems:
        ns = f"nem{n}"
        sh(f"ip netns add {ns}")
        sh(f"ip link add veth{n} type veth peer name backchan0 netns {ns}")
        sh(f"ip link set veth{n} master {BRIDGE} up")
        sh(f"ip netns exec {ns} ip link set lo up")
        sh(f"ip netns exec {ns} ip addr add 10.99.0.{n}/24 dev backchan0")
        sh(f"ip netns exec {ns} ip link set backchan0 up")
        sh(f"ip netns exec {ns} ip route add 224.0.0.0/4 dev backchan0")


def wait_for_device(ns: str, dev: str, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if subprocess.run(f"ip netns exec {ns} ip link show {dev}", shell=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"{dev} never appeared in {ns}")


# ---------------------------------------------------------------------------
# emanesh parsing
# ---------------------------------------------------------------------------

def emanesh_table(nem: int, table: str) -> list[dict[str, str]]:
    """Return rows of one MAC statistic table as dicts keyed by column label."""
    out = sh(f"emanesh 10.99.0.{nem} get table nems mac {table}", check=False, capture=True)
    rows: list[dict[str, str]] = []
    labels: list[str] | None = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if labels is None:
            labels = cells
        else:
            rows.append(dict(zip(labels, cells)))
    return rows


def emanesh_stat(nem: int, name: str) -> int | None:
    out = sh(f"emanesh 10.99.0.{nem} get stat nems mac {name}", check=False, capture=True)
    m = re.search(rf"{re.escape(name)}\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", choices=emane_bridge.VARIANTS, default="part1")
    ap.add_argument("--schedule-json", type=Path, default=ROOT / "schedule.json")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--rate", type=float, default=500.0)
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    part1 = emane_bridge.load_schedule_json(args.schedule_json)
    order = part1.node_order
    ids = emane_bridge.nem_ids(order)
    nems = [ids[n] for n in order]
    sched_file = GEN_DIR / f"schedule_{args.variant}.xml"
    _, matrix = emane_bridge.parse_schedule_xml(sched_file.read_text(), order)
    out_dir = args.out or ROOT / "results" / args.variant
    out_dir.mkdir(parents=True, exist_ok=True)

    teardown(nems)
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    try:
        build_network(nems)
        for n in nems:
            write_node_config(n, RUN_DIR / f"node-{n}")
        write_host_config(RUN_DIR / "host")

        for n in nems:
            d = RUN_DIR / f"node-{n}"
            sh(f"cd {d} && ip netns exec nem{n} emane emane-platform.xml -r -d -l 3 "
               f"-f {d}/emane.log --pidfile {d}/emane.pid --uuidfile {d}/emane.uuid")
        for n in nems:
            wait_for_device(f"nem{n}", "emane0")

        host = RUN_DIR / "host"
        sh(f"cd {host} && emaneeventservice -d eventservice.xml -l 3 "
           f"-f {host}/eventservice.log --pidfile {host}/eventservice.pid")
        time.sleep(2)  # let the pathloss events land before the schedule

        shutil.copy(sched_file, host / "schedule.xml")
        sh(f"emaneevent-tdmaschedule {host / 'schedule.xml'} -i {BRIDGE}")
        time.sleep(3)  # takes effect at the next multiframe boundary

        # --- 1. Did every radio accept the schedule, and is it the right one?
        installed: dict[str, dict] = {}
        for n in nems:
            tx_slots = sorted(int(r["Slot"]) for r in
                              emanesh_table(n, "scheduler.ScheduleInfoTable")
                              if r.get("Type") == "TX")
            installed[str(n)] = {
                "accept_full": emanesh_stat(n, "scheduler.scheduleAcceptFull"),
                "tx_slots": tx_slots,
            }
        expected_slots = {str(ids[name]): [s for s, row in enumerate(matrix) if row[j]]
                          for j, name in enumerate(order)}
        mismatch = {n: (installed[n]["tx_slots"], expected_slots[n])
                    for n in installed if installed[n]["tx_slots"] != expected_slots[n]}
        print(f"schedule installed on {len(nems)} radios, mismatches: {mismatch or 'none'}")

        # Clear counters so only the traffic window is measured.
        for n in nems:
            sh(f"emanesh 10.99.0.{n} clear table nems mac BroadcastByteAcceptTable0 "
               f"BroadcastByteDropTable0 TxSlotStatusTable RxSlotStatusTable",
               check=False, capture=True)

        # --- 2. Traffic.
        procs = []
        for n in nems:
            cmd = (f"ip netns exec nem{n} python3 {TRAFFIC} --nem {n} --rate {args.rate} "
                   f"--size {args.size} --duration {args.duration} "
                   f"--out {RUN_DIR / f'node-{n}' / 'traffic.json'}")
            procs.append(subprocess.Popen(cmd, shell=True))
        for p in procs:
            p.wait()

        # --- 3. Collect.
        traffic = {}
        emane_stats = {}
        for n in nems:
            traffic[str(n)] = json.loads((RUN_DIR / f"node-{n}" / "traffic.json").read_text())
            emane_stats[str(n)] = {
                "accept": emanesh_table(n, "BroadcastByteAcceptTable0"),
                "drop": emanesh_table(n, "BroadcastByteDropTable0"),
                "tx_slots": emanesh_table(n, "TxSlotStatusTable"),
                "rx_slots": emanesh_table(n, "RxSlotStatusTable"),
            }
        result = {
            "variant": args.variant,
            "duration_s": args.duration,
            "rate_pps": args.rate,
            "payload_bytes": args.size,
            "node_order": order,
            "nem_ids": ids,
            "matrix": matrix,
            "links": [list(l) for l in part1.links],
            "installed_schedule": installed,
            "schedule_mismatch": mismatch,
            "traffic": traffic,
            "emane": emane_stats,
        }
        (out_dir / "raw.json").write_text(json.dumps(result, indent=1))
        for n in nems:
            shutil.copy(RUN_DIR / f"node-{n}" / "emane.log", out_dir / f"emane-nem{n}.log")
        print(f"wrote {out_dir / 'raw.json'}")
    finally:
        teardown(nems)
    return 0


if __name__ == "__main__":
    sys.exit(main())
