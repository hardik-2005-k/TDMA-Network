# TDMA Schedule Optimizer with EMANE Integration

A centralized Python optimizer that plans collision-free TDMA schedules for a
set of radios, plus an EMANE integration that enforces that schedule in a
packet-level emulation.

Labels used below:

- **[run]**: the command or result was run on this machine during development
  or final verification.
- **[from docs, not run]**: written from documentation or reasoning and not run
  as written.

- **Part 1** (`tdma_scheduler.py`): distance-2 graph colouring that turns node
  coordinates into a Slot x Node schedule, reusing slots between far-apart
  nodes. **[run]**
- **Part 2** (`emane_bridge.py`, `emane/`): converts the schedule into an EMANE
  TDMA schedule event and a pathloss topology, and runs the emulation. The
  bridge **[run]**. The emulation results are from a saved earlier run and were
  not re-run in the final verification.

See `DOCUMENTATION.md` for the design reasoning and final values.

## Requirements

- Tested on Python 3.14. Other versions were not tried. **[run]**
- `networkx` (Part 1) and `lxml` (schema validation in the bridge). **[run]**:
  both were importable in the tested environment.
- For the live EMANE run: Linux, or Docker. The saved results came from Docker
  Desktop on Windows with the provided Dockerfile, which builds EMANE v1.5.3.
  **[run]**

Setup commands **[from docs, not run]** (the existing `.venv` was used, so
these exact lines were not executed):

```
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux:    source .venv/bin/activate
pip install networkx lxml pytest
```

## Part 1: generate a schedule

Demo on the bundled 16-node topology **[run]**:

```
python tdma_scheduler.py --demo
```

Expected output **[run]**: 16 nodes, a 9-slot frame, "Proven optimal: yes" and
"Schedule verified conflict-free".

Coordinates from a file, saving the result for the bridge. **[from docs, not
run]** as written. `schedule.json` in the repo was produced this way:

```
python tdma_scheduler.py --file sample_nodes.json --json-out schedule.json
```

Coordinates as a JSON string. **[from docs, not run]** as written. The test
`test_json_argument` runs the same code path with the demo nodes:

```
python tdma_scheduler.py '{"Node_01":[0,0],"Node_02":[300,0]}'
```

## Part 2: generate EMANE inputs

Convert the Part 1 schedule into EMANE schedule XML and `pathloss.eel`
**[run]**:

```
python emane_bridge.py --schedule schedule.json --coords sample_nodes.json
```

Outputs land in `emane/generated/` **[run]**:

- `schedule_part1.xml`: the real schedule.
- `schedule_distance1.xml`: negative control, 1-hop separation only.
- `schedule_allslot0.xml`: negative control, everyone in slot 0.
- `pathloss.eel`: the 500 m unit-disk pathloss topology.

Each schedule XML validates against `emane/schema/tdmaschedule.xsd`. **[run]**

## Tests

```
python -m pytest
```

**[run]**: 21 passed (16 for Part 1, 5 for the bridge).
`python -m unittest -v test_tdma_scheduler` also ran, 16 tests OK.

## Live EMANE run (Linux or Docker)

EMANE needs Linux and root for network namespaces and `/dev/net/tun`.

### Build the image

```
docker build -t vmn-emane emane/docker
```

**[run]**: built EMANE v1.5.3 from source following EMANE's own CI recipe. The
first build takes a while.

### Run an experiment

Mount the project and run with `--privileged` (required for namespaces and the
tun device). **[run]** with `--duration 20`. The default duration is 30 seconds
and was not run:

```
docker run --privileged -v "$PWD":/work -w /work vmn-emane \
    python3 emane/emulate.py --variant part1 --duration 20
```

Repeat for the negative controls **[run]** with the same options:

```
docker run --privileged -v "$PWD":/work -w /work vmn-emane \
    python3 emane/emulate.py --variant distance1 --duration 20
docker run --privileged -v "$PWD":/work -w /work vmn-emane \
    python3 emane/emulate.py --variant allslot0 --duration 20
```

Each run writes `results/<variant>/raw.json` with per-radio sent and received
counts, the installed schedule and EMANE's accept and drop tables.

### Compare runs

```
python emane/analyze.py results/part1 results/distance1 results/allslot0
```

**[run]**: prints, per ordered pair, whether EMANE's measured delivery agrees
with what the schedule predicts, and a summary table.

### Live run results

All three variants ran on EMANE v1.5.3 for 20 s each at 500 packets per second
per radio. **[run]**, from the saved output in `results/`:

| Variant | Clean-link delivery | Collision or half-duplex pairs | Schedule agreement |
|---|---|---|---|
| part1 | 99.5 percent (84 pairs) | none exist | 240 of 240 pairs |
| distance1 | 98.4 percent (20 pairs) | 0.1 percent (64 pairs) | 240 of 240 pairs |
| allslot0 | no deliverable pairs | 0.0 percent (84 pairs) | 240 of 240 pairs |

The SINR drop counter, which is the main evidence of collisions, read 0 bytes
for part1, 39,182,660 bytes for distance1 and 0 for allslot0. **[run]** "Zero
measured collisions" for part1 means only that counter reading in the saved
results.

Known open point: the schedule XML sets 20 dBm but the PHY log showed 0 dBm, and
the applied power was not resolved. The delivery results hold in both cases.
See `DOCUMENTATION.md` Section 3.4.

Full numbers and per-radio detail are in `DOCUMENTATION.md` Section 5 and
`results/<variant>/raw.json`.

### Build notes **[run]**

The EMANE image must be built with `build-essential` present, because
`make deb` runs `dpkg-checkbuilddeps`, which requires it by name. If you build
with a logging pipe, use `set -o pipefail` or write the log directly
(`docker build ... > build.log 2>&1`), otherwise a `tee` pipeline hides a
failed build behind a zero exit code. See `DOCUMENTATION.md` Section 6.

## Project layout

```
tdma_scheduler.py        Part 1 optimizer (CLI)
test_tdma_scheduler.py   Part 1 tests
sample_nodes.json        16-node sample topology
schedule.json            saved Part 1 output
emane_bridge.py          Part 1 -> EMANE schedule XML and pathloss
test_emane_bridge.py     bridge tests
emane/
  schema/tdmaschedule.xsd   EMANE schedule schema
  tdma_pcr.xml              packet completion rate curves
  generated/                schedule XML and pathloss.eel
  docker/Dockerfile         EMANE v1.5.3 build
  emulate.py                runs one experiment
  traffic.py                per-node UDP traffic
  analyze.py                compares measured vs predicted
results/                 saved raw output of the three runs
DOCUMENTATION.md         design process and final values
presentation.pptx        slide deck for the panel
requirements.txt         Python dependencies (pinned to the tested versions)
```
