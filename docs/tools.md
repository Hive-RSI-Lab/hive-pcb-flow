# Tools and credits

Every external tool pcb-flow runs or can run, what it is for, its licence, and how a fresh
Hive gets it. pcb-flow's own code is MIT; the tools below are other people's work and keep
their own licences. Nothing below is copied into this repository except the one patched
Freerouting file listed in [NOTICE](../NOTICE). The rest are fetched or found at run time.

`bin/pcb-flow check` reports which of them a machine has and says how to get the rest.

## Board tools (the pipeline needs these)

| tool | version | licence | what for | how it gets there |
|---|---|---|---|---|
| [KiCad](https://www.kicad.org/) (`kicad-cli`, `pcbnew`, its stock symbol and footprint libraries, 3D loaders) | 10.0.6 | GPL-3.0-or-later (libraries CC-BY-SA-4.0 with an exception) | ERC, DRC, plots, Gerbers, the SWIG board edits, the symbols a schematic places | Hive: the shared tools folder. Fresh Hive: `bin/pcb-flow tools` unpacks the official AppImage (sha256 pinned, no root, no FUSE). Other OS: install KiCad and set `HWDE_KICAD_CLI` |
| [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools) by Andy Haas (KRT) | 0.19.0 pinned in `bin/tools.py`; newer ones live side by side in the Hive (`projects/tool-versions`) | MIT | `route_escape`, `route_critical`, `route_auto`'s finishing pass: Rust grid router, BGA fanout, differential pairs. Called through `scripts/lib/krt_launch.py`, which carries our local patches | Hive: shared tools folder. Fresh Hive: `bin/pcb-flow tools` (zip and `.so` from the GitHub release, sha256 pinned). `HWDE_KRT_DIR` points at another plugins dir |
| [Freerouting](https://github.com/freerouting/freerouting) | 2.2.4 (one class patched here) | GPL-3.0 | bulk autorouting in `route_auto` | Hive: shared tools folder. Fresh Hive: `bin/pcb-flow tools` (jar, sha256 pinned). `HWDE_FREEROUTING_JAR` |
| Java runtime ([Eclipse Temurin](https://adoptium.net/)) | 17 or newer on PATH, else Temurin 21 | GPL-2.0 with Classpath Exception | runs Freerouting | `bin/pcb-flow tools` fetches it when no Java is found. `HWDE_JAVA` |
| [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) | 1.0.1 (`requirements.lock`) | AGPL-3.0 | symbols, footprints and 3D models from LCSC/EasyEDA | the Python venv |
| Xvfb, xauth | the OS's | X11 licences | a private X display for KiCad's SWIG paths | needs root once: `sudo apt install xvfb`. `bin/pcb-flow tools --check` says when it is missing. `bin/xvfb-private` runs it |
| git | any | GPL-2.0 | boards, pins, the errata corpus | the OS |

## Simulators and solvers (the sim and verify stages use these; each is optional, the stage says so when one is missing)

| tool | version (2026.09 tree) | licence | what for |
|---|---|---|---|
| [ngspice](https://ngspice.sourceforge.io/) | 47; KiCad 10 also bundles `libngspice` | BSD-3-Clause | `sim_run`, `sim_board`, `sim_solve` |
| [Xyce](https://xyce.sandia.gov/) (Sandia) | 7.10 | GPL-3.0 | a second SPICE engine to cross-check ngspice |
| [openEMS](https://www.openems.de/) + CSXCAD | v0.37.0-rc2 | GPL-3.0 | full-wave 3D FDTD |
| [Palace](https://github.com/awslabs/palace) (AWS) | v0.18.1 | Apache-2.0 | full-wave 3D FEM: eigenmode, driven S-parameters, electro- and magnetostatics |
| [gmsh](https://gmsh.info/) | 4.12.1 | GPL-2.0-or-later | the mesher Palace reads |
| [FastHenry2](https://www.fastfieldsolvers.com/fasthenry2.htm) | 3.0.1 | see upstream | quasi-static inductance and resistance |
| [FasterCap](https://www.fastfieldsolvers.com/fastercap.htm) | 6.0.7 | see upstream | quasi-static capacitance |
| [OpenVAF](https://github.com/pascalkuthe/OpenVAF) (reloaded) | 20260616 | see upstream | Verilog-A to OSDI, compact models for ngspice |
| Ansys HFSS | | commercial | not driven: it needs a licence, which is money and the person's call. Palace and openEMS stand in ([solvers.md](solvers.md)) |

These all come from one place: the unpacked [IIC-OSIC-TOOLS](https://github.com/iic-jku/IIC-OSIC-TOOLS)
image (by Harald Pretl's group at JKU Linz and contributors, mostly Apache-2.0 for the
image itself; each tool inside keeps its own licence). `scripts/lib/solvers.py` finds the tree
at `HWDE_EDA_TOOLCHAIN` (or `EDA_TOOLCHAIN`), else the newest `/opt/eda-toolchain/iic-osic-tools-*` or
`~/.cc/toolchains/iic-osic-tools-*`, and runs each binary through the image's own loader.
**Fresh Hive:** unpack the `hpretl/iic-osic-tools` image (pinned by digest, no docker daemon and
no root) into one of those directories; the [chip-flow](https://github.com/Hive-RSI-Lab/hive-chip-flow)
module does the same for its own tests (`.github/actions/toolchain`), so installing chip-flow
shows how. `bin/pcb-flow check` reports this tree as `solvers`, with this instruction when it is
absent. pcb-flow's own field solvers (`lib/impedance.py`, `lib/thermalfv.py`, ...) need nothing
beyond the Python venv.

Further tools pcb-flow's scripts call when present, each optional: `pdftotext`, `pdftoppm`
(poppler-utils, GPL-2.0, for datasheet PDFs), `tesseract` (Apache-2.0, OCR of scanned
datasheets), `lualatex` or `pdflatex` (TeX Live, for the PDF reports), `node` (MIT, a few
checks), and the JLCPCB DFM rules, which are tables in `reference/`, not a program. Ordering
at JLCPCB or anywhere else is never done by a tool here.

## Python packages

Pinned in [`requirements.lock`](../requirements.lock), built into a venv with `uv` on first use.
The ones the pipeline is built on: kicad-python (MIT), kiutils (GPL-3.0), kicad-sch-api (MIT),
skidl (MIT), InSpice (GPL-3.0), shapely (BSD-3-Clause), numpy and scipy (BSD-3-Clause),
matplotlib (matplotlib licence, BSD-style), gerbonara (Apache-2.0), pypdf (BSD-3-Clause),
sexpdata (BSD-2-Clause), PyYAML (MIT), jsonschema (MIT), pytest (MIT), easyeda2kicad
(AGPL-3.0). `pip show NAME` gives the exact licence of any other line in the lock.

## Where pcb-flow came from

pcb-flow began as [ihsan-sa/pcb-flow](https://github.com/ihsan-sa/pcb-flow)'s Claude Code
skill ([docs/upstream.md](upstream.md)), MIT.
