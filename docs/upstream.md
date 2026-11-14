# Where it came from

pcb-flow began as ihsan-sa/pcb-flow's Claude Code skill, `.claude/skills/hwde/` at
`5d7bbe52483ca0bc6bf28a56e4c1e0d24b5ca4b7`. That skill is frozen: this module
doesn't follow it, copy it over itself, or keep its layout. Change, slim or
rename anything here when it earns it.

What moved where:

- `SKILL.md` (the orchestrator playbook) is [playbook.md](playbook.md), with
  the skill's frontmatter gone.
- `scripts/`, `agents/` and `reference/` are at the module root. The scripts
  find the module root one level above `scripts/` (`env.repo_root()`, and
  each lib's `SKILL`/`REPO`, which are now the same folder).
- Upstream's `/hwde` command, the skill's README, the empty `templates/` and
  check_env's vendored-copy check (whether a copy was behind an ai-ee
  checkout) are gone.
- `reference/knowledge/sources/*.pdf` (36 MB of manufacturers' datasheets
  and application notes) was never brought over. NOTICE says they're
  redistributed under their owners' terms, not MIT.
  `scripts/lib/knowledgelib.py`'s `SOURCES_DIR` reads `HWDE_KNOWLEDGE_SOURCES`.
  `docs/knowledge-sources.txt` pins them at upstream's commit above (git blob
  sha, size, name, from the GitHub contents API for that folder), and
  `bin/sources.py` fetches them from upstream into HOME; `bin/pcb-flow` points
  `HWDE_KNOWLEDGE_SOURCES` there.
- `scripts/parts_search.py --kind/--category` (with
  `reference/part_kinds.yaml` and `category_search` in
  `scripts/lib/partslib.py`): finding a part by what it is, by browsing its
  JLC category, since the keyword search only matches part numbers. Tested
  offline by `tests/test_parts_kind.py`.
- Fixes from building boards here (issue #5, hwde-led-test), each tested in
  `tests/test_sourcer_records.py`:
  - `scripts/bom_cpl.py` reads a footprint's LCSC number from `LCSC Part`
    (what lib_pull's easyeda2kicad writes) as well as `LCSC`, and gives a
    refdes-less parts.json line the board refs with its LCSC number.
  - `scripts/check_ratings.py` reads LCSC's resistor rating names
    ("Voltage-Supply(Max)", "Working Voltage") and says which parts.json
    lines carry no attributes.
  - `scripts/check_pdn.py` counts a cap the board puts straight across a rail
    and ground when decoupling.json records none for that rail (since
    widened: such caps join the recorded ones, below).
  - `agents/part-sourcer.md` copies the parts_search row whole (attributes
    included) and writes no refdes.
- Knowledge the Hive added (offer upstream once approved):
  `reference/knowledge/checklists/led-indicator.yaml`, its covering record
  `reference/knowledge/records/led-indicator-series-resistor-sizing.yaml`, and
  `reference/knowledge/sources/APT1608SGC.pdf.not-redistributed.md`, the
  sidecar (url + sha256) for the Kingbright datasheet they cite, which may
  not be copied and so isn't in `docs/knowledge-sources.txt`.
- Fixes the Hive made while building boards, worth offering upstream:
  `scripts/check_courtyard.py` (+ `checks.d/check_courtyard.yaml`), a verify
  check that fails a courtyard that misses its own pads; `scripts/lib/fpfix.py`
  rule E, which `lib_pull` runs on every pull, and its `--pcb` mode for placed
  copies; `scripts/board_edit.py`, whose `--outline fit` counts silk and whose
  edits refuse to clip it; `scripts/check_silk.py`, whose silk items carry
  their footprint's reference. From the first product board
  (hifi-receiver-2 lite-main): `fpfix` rule F numbers an exposed pad's
  unnumbered thermal vias and drops their paste, and `fpfix.body_flags`
  reads each part's 3D model for a peg with no hole, a body outside the
  courtyard or a model off its footprint (`lib_pull` lists them in
  `autofix.flags`); `lib_pull --budget-s` stops before a 10-minute call
  runs out, and a re-run skips parts already whole in the library;
  `schlib.Sheet` registers the project's own symbol libraries from its
  `sym-lib-table`; the ERC gate reports `pin_types` and warns when every pin
  is passive; `board_init --origin` keeps a re-run's outline where the board
  had it; the schematic role prompts give `kc.py`'s positional input. From routing lite-main (hifi-receiver-2), a
  dense 2-layer power board: `stitch_vias` and `planes_gen` read KiCad's
  hole_to_hole as drill edge to drill edge; `route_auto` carries the hole
  rules into the DSN (`routelib.dsn_hole_floor`, `ses_restore_vias`,
  `dsn_npth_keepouts`) and keeps the best of `--tries` shuffled-order routes
  (`routelib.dsn_shuffle`); `rules_gen` sizes plane_fed taps, writes
  neck-down rules; `planes_gen` merges touching same-net pours; `scripts/keepout_gen.py`
  (new, with route_swig's `set_rule_areas`) draws copper keep-outs;
  `route_critical` rips only the nets whose copper broke DRC; `lib/geom.py`
  lists NPTH holes (`npth`).
- Two verify checks from hifi-receiver-2's first-spin review (issue #130):
  `scripts/check_straps.py` (+ `reference/reset_states.yaml`), an MCU's
  strapping pins and IO expander pins at power-on and on an MCU-only reset,
  and `scripts/check_antenna.py` (+ `reference/rf_modules.yaml`), copper
  under a module's PCB antenna and its place at the board edge, each with its
  `checks.d` fragment.
- Power-path checks from hifi-receiver-2's first-spin power review, which
  did each by hand: `scripts/check_power_path.py` (+
  `checks.d/check_power_path.yaml`, mutant `power-path-under-contract`), the
  current each pad-track-via chain carries as a max-flow over the copper;
  `scripts/sim_inrush.py` and `scripts/lib/pdsource.py`, a board's load
  switches against a USB PD source; `scripts/soa_check.py` and
  `scripts/lib/soa.py` with `reference/fet_power.yaml`, a FET's peak
  junction temperature through a pulse; `scripts/mlcc_dcbias.py` and
  `scripts/lib/mlcc.py` with `reference/mlcc_dcbias.yaml`, ceramics at DC
  bias (sim_board's DC-bias warning reports the derated value).
- Checks a reviewer of hifi-receiver-2's first spin did by hand (10-29):
  `scripts/check_connector_fit.py` holds each part's leads, locating posts
  and front edge to its mechanical drawing (the extraction's new
  `mechanical` block; `fp_verify` applies the lead and post rules to a
  library footprint), and reports every mating connector's overhang;
  `scripts/check_ep_paste.py` measures each exposed pad's paste coverage and
  fails paste printed over its vias. `datasheet_extract.py --render` renders
  scanned and drawing pages for the extractor (OCR'd with tesseract when
  installed).
- A live stock-and-match check on every BOM row (the person, 10-28: "didn't
  the hive pull the JLC and LCSC?"): `scripts/stock_check.py` and
  `scripts/lib/stocklib.py` read JLC's and LCSC's own record for each code
  (what JLC can still sell, whether it sells the code at all and what its BOM
  review would swap in, whether the model is the BOM's part), and `bom_cpl.py`
  fails a row JLC would not place as written (`dfm_check`'s scratch run and
  `--offline` skip it and say so).
- A fact ledger per board ("know what we dont know", the person 10-28):
  `scripts/lib/ledgerlib.py` and `scripts/ledger.py` keep `ledger.json`,
  every fact the design relies on as verified (source, date), failed or
  unknown; `stock_check`, `bom_cpl`, `check_ratings` (rating, and a new pinout
  comparison against the extraction) and `cpl_verify` (footprint) write it,
  and `lib/releaselib.py` refuses an attestation, and drops order-ready,
  while any fact is not verified now.
- The Hive's rules are judgment, not refusals (the person, 10-28): `state.py`
  `set-phase` warns instead of refusing an unrecorded gate, `budget
  --consume` and `cross-spawn` count past zero (`over: true`), `research.py
  open --slot` opens past the per-run cap, `board_init` notes a fixed outline
  under a geometry-as-output mode instead of refusing it, and the release
  attestation no longer needs an approved H4 (a rejected one still vetoes).
  `playbook.md`, `full-run.md`, `fix-finding.md`, `build-modes.md` and
  `tasks.yaml` say so: H1-H4 are reports, and only error waivers, cost/feature
  trades and the order go to the person.
- A coverage checklist counts on an independent ruling, not only on the
  person's approval (the person, 10-30: "the hive must be fully
  autonomous"). `scripts/checklist_verify.py` and `lib/checklistverify.py`
  build a blinded packet of the checklist and planted mutants of it, and
  grade a fresh-context `agents/checklist-verifier.md` ruling against the
  key. `knowledgelib.checklist_meets_floor` counts a `verified` checklist
  whose `verification` block is intact (by someone other than the author,
  every planted fault caught, a basis per class, digest current) at floors
  up to `approved`. `researchlib` takes a verified research checklist too.
- A shared tools folder (`HWDE_TOOLS_DIR`, the Hive's `/hive/tools`) is
  searched the way upstream searches its own `tools/`, so the scripts find
  the Hive's tools themselves and `bin/pcb-flow` needs no wrappers:
  - `scripts/lib/env.py`: `kicad_installs` takes KiCad AppImage AppDirs at
    `<tools>/kicad-*/` (`bin/kicad-cli` runs as is); `find_kicad_python`
    takes the AppDir's `bin/python3.<n>`; `find_ngspice_dll` its
    `lib/libngspice.so`; `find_krt` also looks under
    `<tools>/kicad-routing-tools/KiCadRoutingTools-*/plugins` and picks the
    newest by version, not by string.
  - KiCad 10 ships its stock symbols as one-file-per-symbol `.kicad_symdir`
    folders, which kicad-sch-api 0.5.6 can't read. `env.py` flattens a
    library into `~/.cache/hwde/symbols/<key>/` the first time it's needed
    (`stock_symbol_lib`, again when the symdir is newer):
    `export_kicad_symbol_dir` does `power` and `Device` and points
    `KICAD_SYMBOL_DIR` there; `register_stock_libs` adds any other.
    `schlib.py` calls it before each placement and `--pins`, and
    `schem_refdes.py` before its round trip (whose save drops symbols the
    cache can't resolve).
  - `scripts/kc.py`: a comment only. The AppDir's 3D loaders match
    `render_env`'s `usr/lib/*/kicad/plugins/3d` through the AppImage's own
    link once kicad-cli is the AppDir's, not a wrapper.
  - `scripts/check_env.py` checks the stock `power`/`Device` symbols
    (fail), the 3D loaders (warn) and KiCadRoutingTools (warn).
  - `scripts/route_critical.py`: the missing-KRT message names the shared
    layout.
- `agents/board-guide.md` finds pdf-material-builder through
  `HWDE_PDF_MATERIAL_BUILDER` before `~/.claude/skills/`, and says how to
  build the guide on a machine without it.
- `scripts/report_gen.py` (`STAGE_REPORTS`) and `scripts/guide_facts.py`
  read a stage's report under the names runs have used as well as the
  recipes' one, and `agents/dfm.md`, `recipes/dfm-check.md` and
  `recipes/full-run.md` give each report's name (dfm.md's command lines also
  had `--out` for `--out-dir` and `--parts-json` for `--parts`).

  `tests/test_env_lookups.py` covers these lookups (`$HWDE_PY -m pytest
  tests`). They're meant to go upstream.
- Part datasheets: `scripts/lib/researchlib.py` `http_transport` follows a short HTML
  redirect stub (`stub_redirect`) like an HTTP redirect, because
  wmsc.lcsc.com now answers with one. `scripts/datasheet_extract.py`
  gains `--fetch` to download a workspace's part datasheets through it.
  `agents/datasheet-extractor.md` and `reference/recipes/research.md` say so.
  Offer these upstream.
- KRT's writer (KiCadRoutingTools 0.19.0) moved a net tie's copper bridge to
  silkscreen with every other net-less copper graphic (#1008).
  `scripts/lib/krt_launch.py` keeps net-tie footprints out of that move, and
  `scripts/route_critical.py` `run_krt` now starts every KRT run through it
  (`tests/test_check_net_tie.py`). `scripts/check_net_tie.py` (+ its
  `checks.d` fragment and the `net-tie-open` mutant) fails a net tie whose
  pads its copper doesn't join. Offer the writer fix to KRT too.
- `scripts/check_thermal.py` holds an entry's `min_vias` on every part,
  rated and datasheet-theta ones too, not only where the copper estimate
  already wanted vias, and on 4+ layers credits the inner planes only to a
  part with vias into them (#1047, `tests/test_thermal_via_floor.py`).
- `scripts/check_straps.py` and `scripts/check_reset_drive.py` know the TI
  AM62x (and an Octavo OSD62x SiP that carries one): a
  `reference/reset_states.yaml` row whose BOOTMODE straps, unpulled during
  reset, need a defined level each (`want: defined`), VIL/VIH from the
  bank's rail, and a net tie's copper joining the nets it bridges
  (`tests/test_antenna_straps.py`).
- Every script that imports a `scripts/` module, at the top or lazily inside
  a function, puts `scripts/` on `sys.path` itself, and every one that
  imports a `scripts/lib` module puts `lib/` there, so each runs under
  `python -I`. `check_ratings` put only `lib/` there and died on its lazy
  `import datasheet_extract`; `check_creepage` and `check_pdn` died at their
  first import. `tests/test_scripts_isolated_imports.py` reads every
  module's imports, the lazy ones too.
- `scripts/check_body_clash.py` and `scripts/check_isolation.py` take
  `--parts`, as the gate passes it to every check. In body_clash a part
  whose drawing puts its front past the PCB edge (its extraction's
  `mechanical.edge.overhang_mm`, as `check_connector_fit` reads it) may
  stand out that far. In isolation `--parts` only names each crossing part's
  MPN; a rating still comes only from `isolation.parts`.
- `scripts/cpl_verify.py` reads KiCad's own placement file
  (`Ref,Val,Package,PosX,PosY,Rot,Side`) as well as a CPL; it raised
  KeyError 'Mid X' on one. A placement file's Rot is taken with no fab
  correction, and the report's `cpl_note` says what a wrong_rotation then
  means (`tests/test_cpl_verify.py`).
- `scripts/check_pdn.py` counts the caps the board puts straight across a
  rail and ground together with the ones decoupling.json records, not only
  when it records none. The file records a cap against an IC pin, so a
  rail's bulk cap at its regulator output or connector was never in it, and
  any rail with one recorded 100 nF read as "no bulk reservoir". A footprint
  KiCad marks `dnp` (a contingency) ships empty and is not counted; `geom`
  now carries each footprint's `attr` flags for that
  (`tests/test_false_pass.py`, `tests/test_sourcer_records.py`). The `core`
  pin's ledger had judged two such findings real (+5VA, +3V3E); the board
  holds 10 uF caps across both rails, so they are judged false now, a new
  score baseline.
- `scripts/check_fw.py` exits 2 only where fw is needed and missing: on a
  board with an ESP32 (a part whose value, footprint or symbol names one).
  A board with none, or one that no target in `targets.json` names, passes
  with `nothing_to_judge`. It finds fw as `hive-fw` or `fw`, in
  `$HIVE_MODULES` or the modules folder it is installed in
  (`tests/test_check_fw.py`).
- `scripts/first_spin.py` exits 0 with `nothing_to_judge`, not 2, while a
  workspace has no `reports/first-spin/` review yet. The schematic and
  board reviewers' `reports/review-*.json`, if there, are still classed and
  judged, with `not_judged` saying the first-spin review is missing. An
  explicit `--dir` that is missing or empty, or a missing workspace, still
  exits 2 (`tests/test_first_spin.py`).
- `scripts/check_irdrop.py` solves a net of more than 50k unknowns with CG
  and a smoothed-aggregation multigrid preconditioner on the grid's 2x2 cell
  blocks, in place of the sparse LU. The LU's fill grew past linear: a
  795k-cell net took 4.5 GB and exited 2 with SUPERLU_MALLOC under a 2.5 GB
  address-space limit. Smaller systems keep the LU, and a CG that stalls
  falls back to it. Each net's `grid.solver` says which ran. The grid keeps
  node ids for its in-copper cells only (a dense int64 id per frame cell was
  300 MB a layer on a thin net), the rasterizer tests a block of rows at a
  time, and current density is summed per node; `check_kelvin` uses the same
  numbering. On a board with that net the whole check went from a 4.5 GB
  peak to 0.75 GB with the same report, and runs in a 1.5 GB cgroup
  (`tests/test_layout_sim.py`).
- `scripts/gate.py`'s dfm stage checked every board at JLC's minimums. It
  now takes the board's fab: the vendor `vendor choose` recorded in
  `state.json`, else the fab of the `reference/stackups.yaml` build the
  board's (stackup) block matches, else JLC. The payload's `vendor` and
  `vendor_from` say which (`tests/test_vendors.py`).
- `scripts/check_bom_sync.py` reads the fab files a board ships under other
  names. Its fab leg read only `BOM.csv`, `CPL.csv` and `BOM-full.csv` in
  the fab dir, so a board whose package is a KiCad pos export and a
  distributor BOM in a folder of `fab/` had the leg skipped as if it had no
  fab files. Each role now takes its own name first, else the one
  `*bom*.csv`, `*cpl*.csv` or `*-pos.csv` (order_submit's patterns, in any
  case) in the fab dir or a folder directly in it; a `-top-`/`-bottom-pos`
  pair is read as one CPL. Two files that fit a role leave it unread and the
  report names them; `--bom` and `--cpl` name the files outright. The
  designators come from a Designator, Designators, Ref, Reference or RefDes
  column, split on commas, spaces or semicolons, and a kicad-cli range
  (`C1-C4`) is each ref in it. Another maker's file is judged on unknown and
  missing refs only: a KiCad export lists DNP parts unless told not to, so
  the DNP refs it lists go to `not_judged`, while bom_cpl's own `BOM.csv`
  and `CPL.csv` are still held to `bom_dnp_populated`. The report's
  `fab_files` says which file each role read
  (`tests/test_check_bom_sync.py`).
- `scripts/fab_export.py` gives the nets of each net tie
  (`net_tie_pad_groups`) one IPC-D-356 name where the shipped copper joins
  them (`lib/ipcd356.py` `merge_net_ties`), after the jumper islands are
  named apart. kicad-cli named them apart, and a fab's tester read the tie
  as a short. A tie whose copper is cut keeps its names, and `dfm_check`
  reports it as `dfm_net_tie_open`, which the tester can't see. A tie still
  named apart is a `dfm_net_short` with `tie` set and says how to fix it
  (`tests/test_netlist_compare.py`).

`requirements.lock`, `LICENSE` and `NOTICE` came from upstream's repo root,
and so did `docs/failure-modes.yaml` (`bench --scorecard-corpus` maps
findings to failure modes through it). `tests/` began as upstream's `tests/`,
without `check.sh` and the tests of what stayed upstream: evals
(`test_e2e_electrical`, `test_eval_sandbox`, `test_eval_writeset`), the
showcase (`test_showcase_gallery`), `LEARNINGS.md` and its triage
(`test_learnings`, `test_remediations`), the docker and CI pins
(`test_kicad_pin`), `docs/check-scorecard.jsonl` (`test_check_scorecard`)
and the vendored-copy check (`test_skill_copy_current`). `tests/conftest.py`
adds `KNOWN_FAILING`; `tests/test_workarounds.py`, `tests/workarounds/` and
`tests/test_parts_kind.py`, `tests/test_env_lookups.py` and `pytest.ini` (upstream's pytest settings from its `pyproject.toml`) are the
module's own. The plans, docker setup and design notes stayed upstream.

Everything else that's specific to the Hive is in `bin/` and `docs/`:

- `bin/pcb-flow` wraps the Hive's KiCad AppImage (kicad-cli, KiCad's python with
  pcbnew, ngspice, the 3D model loaders the render preflight looks for).
  It also builds the venv and runs scripts under Xvfb with GLX off.
- `bin/flatten-symdirs.py` exists because KiCad 10 ships its stock symbols
  as one-file-per-symbol `.kicad_symdir` folders, and kicad-sch-api 0.5.6
  can't read those. It writes flat `power` and `Device` libraries into HOME,
  and `bin/pcb-flow` points `KICAD_SYMBOL_DIR` at them.
- `bin/house-style-fallback/housestyle.sty` defines the few house-style
  commands report_gen writes, on stock LaTeX, for machines without the
  pdf-material-builder skill.

