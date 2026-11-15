# pcb-flow playbook

How an agent runs a board through pcb-flow: the phases, gates, fix loop and
sign-offs. It began as ihsan-sa/pcb-flow's Claude Code skill (`SKILL.md`, see
[upstream.md](upstream.md)) and is the module's own now; change it when it's
wrong. In the Hive, read [README.md](README.md) first: it says how the
sign-offs, boards and tools work here. `scripts/x.py` below is `bin/pcb-flow x`.

You are an EE picking up a project in whatever state it is in. YOU decide what
to spawn, what to run and how to react to what the checks say; the scripts do
everything checkable. Optimize for a working board at JLCPCB.

**The checks are tools, not a stage machine.** Gates, budgets, phases and
recipes tell you what is true about the board and what usually works; none of
them refuses to let you go on. A script that used to stop you now warns and
writes the gap into `state.json` history (`phase_ungated`, `budget_over`,
`cross_spawn_over`, a board_init note), so a later reader sees what you chose.
When you go past one, record why (`state.py decision`). What a check reports
about the board (a DRC error, a sub-fab clearance, a stackup JLC does not sell)
is a fact - you fix it or waive it, you do not argue it away. And when a rule
here keeps getting in the way of a good board, the rule is what to change:
say so in the run's LEARNINGS and fix the script or this playbook.

**What is the person's** (bring them only this, as a short packet - digest,
files, the question with your recommendation - and carry on with everything
else while they answer): waiving an error-severity finding on a board that
will be built, a cost or feature trade the brief did not settle, and the order
itself (H5: quote, pay). H1-H4 are reports you write at those points, not
stops: present them, go on, and rework if an answer comes back no.

## Front door - route the task first

```
scripts/task_router.py --task "<the user's words>" [--workspace ~/dev/boards/<name>]
```

- **exit 0** - one verb matched. `recipe.steps` are bound to this workspace's
  real paths; `recipe.gates` and `recipe.human_hold` come from
  `reference/invalidation.yaml`, not from prose. READ `recipe.doc`
  (`reference/recipes/<verb>.md`) before executing - it carries what the
  commands cannot.
- **exit 1** - a decision is needed, and `status` says whose:
  `ambiguous` -> classify among `candidates` yourself, re-run with
  `--verb <name>`; `unknown` -> classify against `--list` or ASK the user;
  `needs_args` -> ask the questions in `needs` (ONE batch); `blocked` -> a
  precondition failed (usually a stale gate before ordering) - fix that first.
- **exit 2** - error; the payload says what.

The verbs: `review` `fix-finding` `move` `swap-part` `add-part` `remove-part`
`reroute-net` `make-footprint` `dfm-check` `order` `track` `bring-up`
`resume-phase` `promote` `learn` `research` `full-run`. The whole pipeline is the `full-run`
recipe - a task like any other, not a separate code path. (`learn` is the
owner-present teaching session; `research` fills a coverage gap and is also
what full-run launches automatically on every gap - its recipe carries the
caps and the allowlist rule.)

A recipe is the route that has worked, not a script you must follow: add a
step it lacks or drop one that does not apply, and say so. Its gate set is the
invalidation map's answer to "what did this edit invalidate" - skip one and the
release attestation will name it, so skip only what you will re-run later.

**Build modes**: a brief opening with a mode token (`learning <target>:`, or the
legacy `ultra bare bones design:`) names a TARGET LEARNING OUTCOME that derives
two dials - read `reference/build-modes.md`. The SCOPE TIER bounds what goes on
the board (and at `product` makes the ABSENCE of protection/filtering a
finding); the BINDING LEVEL says whether the stated geometry is an input to the
design or an OUTPUT of it. Record it at P0 with `state.py mode --token "<it>"`
(not a bare decision - `board_init` reads it and notes a fixed `--outline WxH`
where the size is usually earned), and carry scope + binding into the P0/P2/P6
and reviewer spawns. A mode relaxes geometry, cost and packaging only - never a
gate, the coverage check, research, or a safety question - and every relaxed
spec is a recorded decision shown at H1.

**Other agents** call the same routing, state, gates and review as MCP tools through
`scripts/mcp_server.py` (stdio, read-only unless `write: true`; see the README).

## Operating rules (the ones that keep the run readable)

1. **Never open design files.** No .kicad_sch/.kicad_pcb/netlists/gerbers in
   your context - ever. You read: `state.json`, gate results, agent output
   contracts (FILES/GATE/SUMMARY/OPEN), and digests. Agents read the design.
2. **Files are the interface.** Every agent gets: its role prompt from
   `agents/`, the exact file paths it needs, and its termination condition.
   Agents return their output contract; you parse only that.
3. **Record everything in state.json** (via `scripts/state.py`) - phases,
   gate results, issues, decisions, human checkpoints, and every declared edit
   (`state.py edit --class <c>`, which is what stamps derived artifacts stale).
   A killed session must resume from state.json alone. `set-phase` warns
   (`gate_coverage`) when you advance past a gate with no recorded result,
   and history keeps it; release still needs it on record.
4. **Gates carry `--workspace`, and commit on pass**: `scripts/gate.py --gate
   <g> <input> --workspace <ws> --commit "pcb-flow <board>: <gate> pass"`.
   `--workspace` records the result (input hashes, attempt, freshness) - a
   report on disk that state.json never saw is not evidence. Commits happen
   only on pass, never pushes. `--commit` needs the input inside a
   `boards/<name>/` workspace under the boards root (`HWDE_BOARDS_ROOT`);
   for a board kept anywhere else the gate prints "requested commit did not
   occur" and exits 2 even when it passed. There, leave `--commit` off,
   read the gate's own verdict, and commit the board yourself. Rollback for in-flight fix loops is
   `state.py snapshot/restore`.
5. **Scripts run through `bin/pcb-flow`** (`pcb-flow <script> [args]`): the module's
   venv python, with the tool pins set as `HWDE_*` env vars. All scripts: JSON out, exit 0 pass / 1 violations /
   2 error (an exit-2 payload carries remediation - read it). ASCII only.
6. **Ask the person in batches**, only what is theirs (above) - never
   trickle questions, and never wait on an answer you can work around.

## Phase machine

```
P0 Intake - P1 Research - P2 Architecture -[H1]- P3 Parts+Library -
P4 Schematic -[G:erc + review][H2]- P5 Board Setup - P6 Placement
-[G:place][H3 optional]- P7 Routing -[G:drc_routed]- P8 Verification
-[G:verify + review][H4]- P9 DFM -[G:dfm]- board guide - P10 Ordering
-[H5: pay]
```

The phases are where a run usually is, in the order that usually works; H1-H4
are reports, H5 (pay) is the person's. `[G:...]` are the checks worth running
before moving on - run them when they can run, and before release they must
pass (the attestation checks that, the phase does not).

Machine gates (defined in `reference/gates.yaml`, run via
`scripts/gate.py --gate <name> <input> --workspace <ws>`):

| Gate | Phase | Input | Criteria |
|---|---|---|---|
| erc | P4 | .kicad_sch | errors AND warnings = 0 |
| place | P6 | .kicad_pcb | legality + edges + keepouts + decoupler dist |
| drc | P6 | .kicad_pcb | parity + errors = 0 (interim) |
| drc_routed | P7 | .kicad_pcb | parity + all track errors, err+warn = 0 |
| verify | P8 | .kicad_pcb | 8-check suite, error-severity = 0 |
| sim | P8 | kicad/sims | every bench inside its bounds sidecar |
| dfm | P9 | .kicad_pcb | exported-gerber DFM, error-severity = 0 |
| verify_release / dfm_release | P10 | .kicad_pcb | strict: every applicable check must RUN; durable waivers only, each matching a finding |

Tools that are not gates, run from P4 on (every exported netlist) and
weighed like any other report:
- `scripts/sim_solve.py`: the open field and circuit solvers beyond
  KiCad's ngspice (Xyce, openEMS, Palace, FastHenry, FasterCap, pcb-flow's own
  thermal/electro-thermal plane solver), each held to a closed-form case by
  `sim_solve validate`; `sim_solve line` checks a microstrip's Z0 full-wave
  against impedance.py; `sim_solve board` gives the S-parameters of a
  board region (all layers, vias, lumped parts) as a Touchstone file.
  docs/solvers.md.
- `scripts/sparam.py check F.sNp` / `fit F.sNp --out F.cir [--verify]`:
  IEEE P370 passivity/reciprocity/causality of an EM or VNA S-parameter
  file with the points that fail, and a passive vector-fitted SPICE subckt
  of it for a sim_run bench (docs/solvers.md).
- `scripts/parts_enrich.py --workspace <ws>`: fills parts.json
  `attributes` (Power, Voltage Rated, Vgs, RDS(on), Vf, common mode) from
  each row's LCSC listing, keeping any value the row already has. Run it
  once the BOM has LCSC codes and before sim_board / check_ratings: without
  it every resistor is judged at its package default, and a diode, fuse,
  regulator, cap or known power IC with an empty row fails verify as `rating_no_attributes`:
  verify reads the row and never fetches, so the same board gets the same
  answer offline and on any machine.
- `scripts/rating_fill.py --parts <ws>/parts [--mpn X] [--write]`: fills
  a two-pin passive record's `pin_ratings` (voltage across its ends, power,
  current) and `operating_temp_c` from its own datasheet PDF, each row
  citing the page and table it came from. Readers: Yageo RC chip
  resistors (sqrt(P x R) capped at the max working voltage), Murata GRM
  and Samsung CL MLCCs (checked against the part number's voltage code),
  Murata BLM beads (current and temperature only: a bead's datasheet gives
  no voltage, so it stays unrated), JST PH headers (the one multi-pin
  record filled: voltage between neighbouring contacts, current on every
  contact, mounting pads left out). A figure that disagrees with the part
  number or the record's abs_max is reported, not written; rows written by
  hand are kept, and a rerun changes nothing. Without `--write` it only
  reports. Then rerun check_ratings: the filled parts leave rating_unrated.
- `scripts/sim_board.py --workspace <ws>`: ngspice on the board's own
  netlist, no testbench to write. Rails are sources, ICs open, every R, C,
  L, diode, LED, zener and recognised BJT/MOSFET in place (a dual, e.g.
  MMDT3904 or 2N7002DW, as its two units Q1A/Q1B), under four drive
  scenarios (default, every IC I/O low, high, running) plus a rail-ramp
  transient. Reports resistor overpower, cap overvoltage/DC bias, the Vout
  each FB divider sets against the rail it feeds, LED and GPIO currents,
  pins above their IC's supply (or above their own rating in
  reference/pin_ratings.yaml: CSA inputs, BMS AFE pins), MOSFET Vgs (too much, and too little to
  turn on: rerun at each rail's `min` in constraints), enable/reset RC timing, I2C
  pull-up limits, RC corners, crystal loads - and `known_unknowns`, every
  fact it could not decide (a Vref not verified, a Vgs rating not known).
  Each known unknown gets a datasheet answer or a stated margin in the
  review; pin what you learn with `--vref`/`--rail` or in
  `reference/regulator_vref.yaml`. An IC whose I/O runs from its own
  regulator rather than its highest supply (a USB-PD sink's VDDD) gets a
  row in `reference/io_supply.yaml` or `io_supply` in constraints, or the
  io scenarios drive its pins at VBUS.
- `scripts/sim_inrush.py --workspace <ws>`: the board's load switches
  against a USB PD source (`lib/pdsource.py`: slewed steps behind a cable,
  a source that cannot sink, a hard reset). Same netlist model as
  sim_board, but the entry is the PD source and each switch's output is
  free: attach, the 5 V -> contract step at 0.5/3/30 V/ms with firmware
  off, firmware turning the switch on, and a hard reset with firmware
  still on, plus the FET's Vth corners. Reports the source current against
  the contract (an OCP trip and hard-reset loop), a switch that conducts
  with firmware off, Vgs transients, and each switch's peak junction
  temperature through its turn-on (`lib/soa.py`, the part's Zth from
  `reference/fet_power.yaml`). Declare the contract in constraints.json
  (`"pd_source": {"contract_v": 28, "contract_a": 5}`) or pass
  `--contract-v/--contract-a`; `--load NET=ohms` adds a regulator's input.
- `scripts/check_power_path.py --pcb <pcb> --constraints <c>` (also in the
  verify suite): the current each pad -> track -> via -> pour chain carries,
  as a max-flow over the net's copper, with the min cut (the tracks and via
  barrels that limit it) named. Judged against `power_paths`
  (`[{"net", "from", "to", "current_a"}]`), each net's budget, and a USB PD
  input's contract (`pd_source`, or 3 A derived when a PD sink IC is on the
  board). Write budgets at the real current - a 0.5 A budget on a 5 A VBUS
  passes every piece check_current judges. A cut at a layer change is
  fixed with `scripts/zone_patch.py --pcb <pcb> --net <net> --region
  x0,y0,x1,y1 --vias N`: a solid PATCH_<net> zone over the pad with N vias
  down to the plane.
- `scripts/soa_check.py --fet <mpn> --hotswap --c 940u --v 28 --t-ramp 10m`
  (or `--csv` of a simulated t,vds,id; `--at 10m` for the SOA power):
  a MOSFET's peak Tj through one pulse, for a written margin.
- `scripts/mlcc_dcbias.py --workspace <ws> [--min-c NET=22u]` or `--c 22u
  --package 0805 --rated 25 --vdc 5.2`: every ceramic's effective
  capacitance at its DC (`lib/mlcc.py`: the part's curve from
  `reference/mlcc_dcbias.yaml` by MPN, else a generic class-II curve,
  typical and low) and each rail's total, against a regulator's Cout floor.
  sim_board's DC-bias warning carries the same numbers.
- `scripts/eng_calc.py <divider|fb|rc|led|i2c|crystal|ldo|holdup|rpower>`:
  the same formulas (`lib/engcalc.py`) for a margin a review writes down,
  so the number cites a command anyone can re-run.
- RF boards' link budget goes through the same command:
  `eng_calc cascade --stage name:gain_db:nf_db[:oip3_dbm] ... [--bw --snr]`
  (Friis NF, gain, Te, IIP3, each stage's share of the noise, and the
  sensitivity), `rxsens` (kTB + NF), `detector` (a square-law diode
  detector's NEP, MDS and TSS from datasheet responsivity and Rv, or the
  ideal limit from Is), `pll` (a charge-pump passive 2nd-order loop
  filter: design from fc + phase margin, or the fc, phase margin, -3 dB
  bandwidth and peaking of the parts on the board) and `line` (free-space
  and guided wavelength, quarter wave, lambda/10, a length's phase and
  delay; microstrip eps_eff from the field solver with Getsinger
  dispersion at f), `lmatch` (the L-sections, Pozar 5.1).
- Matching networks: `pcb-flow rf_synth match --z R+jX@f | --s1p F --f0 ..
  --bw ..` gives every L-section and the pi/T networks for a loaded Q,
  snapped to E-series parts with 0402 parasitics, the return loss across
  the band before and after snapping, and an ngspice tolerance Monte Carlo
  with yield (`lib/rfmatch.py`, `lib/tolerance.py`). On the board,
  `check_rf_match` (P8) judges the populated network against constraints
  `rf_match` (the antenna's zin or s1p and the band): RL >= 10 dB.

Sidecars (`constraints.json`, `decoupling.json`, `parts.json`, schematic)
resolve from the board's own directory - keep them beside the board from P5
onward.

Phase is workflow position, NEVER a release certificate: ordering consumes
only the release attestation (`scripts/attest.py build|verify|disposition`,
recorded at `fab/attestation.json`) and `state.py resume` reports the derived
`release_disposition` beside the phase. The `order` recipe carries the rules.

**Know what the board does not know.** Each workspace keeps `ledger.json`
(`scripts/lib/ledgerlib.py`): every fact the design relies on - per BOM code
its stock, part match and lifecycle at JLC; per part its pin ratings, pinout
and footprint - as verified (source and date), failed, or unknown, and a rating
whose number came from the distributor or a package table, with no datasheet
read, as listed (`rated_from` says which). The checks
write it as they run (`stock_check.py` and `bom_cpl.py`'s live run,
`check_ratings.py` in verify, `cpl_verify.py` in dfm); a check that cannot
tell records unknown, never a pass. `ledger.py report --workspace <ws>` lists
the gaps; `ledger.py set ... --source` records one you checked yourself (`--force "why"`
over a suspect datasheet). Facts about a part that left the board
(swapped or removed refdes or LCSC code) show as `orphaned` in the report and
in `attest build`, never count as evidence; `ledger.py prune --workspace <ws>`
lists them, `--apply` deletes them.
Release refuses while any fact is failed or unknown. A listed fact passes and
`attest` names it, with a `rating_no_datasheet_read` warning saying how to read
the datasheet. Where a part has both, a listing that differs from the datasheet
by over 10 % gets a `rating_listing_disagrees` warning that says which number the
check used; `listing_survey.py <boards>` measures the rate by part class. Stock older than 72 h reads unknown, so re-run `stock_check.py --workspace <ws>` before the order.

## Works first time, watches itself, brought up unattended

The person, 10-28: they give the spec; the Hive designs a product that
"will def work with appropriate contingencies"; they order it, plug it in,
run one script, leave it, and the Hive brings it up, revs the firmware,
tests it and pings them when it is ready. "This should be the flow for all
board designs." It is the default for every board that will be built,
scaled by you to the board - not a gate; record how far you took it
(`state.py decision`).

- **Design for it** (P2-P4): `reference/self-observation.md` - what the
  board can measure about itself for cents (rail dividers into spare ADC
  pins, status pins to GPIO, output loopback taps, a console), and the
  test API firmware offers. `scripts/check_observability.py --workspace
  <ws>` lists each rail as self / test_point / blind and each PG/FAULT/INT
  as watched or not; weigh it at P4 and P8.
- **Doubt it** (from P4 on, again after every change): `reference/doubt.md`
  - `scripts/doubt.py ask` puts the library's questions to the board, the
  agent (`agents/doubter.md`) adds its own, each with a check script, and
  `run` answers them all; `status` lists the fails and unknowns still open.
- **Review it before money** (P8-P9): `reference/first-spin.md` - one
  `domain-reviewer` per domain with fresh eyes, every margin a number,
  contingencies on the board where a margin can't be closed on paper, every
  blocker/major answered. `scripts/first_spin.py --workspace <ws>` lists
  what is still open and merges the tools wanted (file them to hive-pcb-flow).
  The same review, cold, on any board you hold (built, imported, after an
  ECO) when you judge it worth it: every finding names its fault class in
  `docs/failure-modes.yaml`, and `--briefs` drafts a hive-pcb-flow brief for
  each class no pcb-flow check caught, so each review leaves a check behind.
- **Bring it up** (boards in hand): the `bring-up` verb and
  `reference/remote-bringup.md`. `state.py bringup --results <file>`
  closes it with the measurements (`scripts/lib/bringuplib.py` schema);
  `bring-up-passed` and `proven` cite them.

## Run start / resume

**New workspace** (`full-run`, or `review` of an outside project): the recipe's
first steps are `check_env.py` (exit 0 required; on failure present its
remediation strings and stop) and `state.py init --workspace ~/dev/boards/<name>`
(the boards repo, `HWDE_BOARDS_ROOT`; pcb-flow holds no boards of its own), which
creates the workspace + standard subdirs there so gate commits work. Copy user
inputs into `brief/`. A numbered board's dir and KiCad project are `<PN>_<name>`;
`<board>` in a KiCad/fab file name means that (`{project}`); see `lib/boardreg.py`.

**Existing workspace** (a resume, or any verb with `--workspace`):
run `check_env.py` first, as at run start (exit 0 required). `state.py resume` is the only source of truth for where the run is. Re-run the
gates it reports `gates_stale` or `gates_freshness_unknown`; never redo a gate
that is passed AND fresh. Log the seam (`state.py log --event resumed`) and
re-enter at the next unmet gate or checkpoint. Open issues in status `fixing`
already have work orders on disk - re-dispatch those.

## The fix loop (uniform for every gate)

On gate fail (exit 1, result JSON has `failing` with coordinates):

1. `state.py budget --path fix_loops.<gate> --consume` - a counter, not a
   wall: `over: true` means this attempt is past the default (3). Judge
   whether the loop is converging (fewer violations, a new cause each round);
   if it is, go on and record why; if not, change approach (below).
2. `state.py snapshot --label pre-fix-<gate>-a<attempt> --files
   <board rel path>` (plus the schematic for erc-phase fixes).
3. `scripts/fix_dispatch.py --input <gate result> --board <board>
   --state <ws>/state.json --out <dispatch summary>` - clusters the
   failures and writes one work order per cluster
   (`log/workorders/wo-<id>.json`), each carrying its
   `reference/remediations/<kind>.md` paths, and registers each as an open
   issue. At P4 the fix target is the .kicad_sch - pass IT as --board (the
   flag accepts either; a not-yet-existing .kicad_pcb is rejected).
4. Spawn one `fixer` per order - orders inside one `parallel_groups` entry
   run concurrently; groups run in sequence (their regions overlap).
   BUT: the board file is single-writer - parallel fixers are safe only
   because place_edit/route_edit apply atomically; still serialize fixers
   that share a board unless their ops are disjoint by region. When in
   doubt, run them sequentially - correctness beats wall-clock.
   Mark issues `fixing` -> `fixed`/`escalated` (`state.py issue`).
5. Declare what the fix changed: `state.py edit --class <c>` (the domain ->
   class table is in `reference/recipes/fix-finding.md`).
6. Re-run the gate with `--workspace` - EVERY attempt records itself, fail
   and pass (the history is the audit trail, the input hashes come from it).
7. A fixer that regressed (new violations appeared): restore its snapshot,
   mark the issue `escalated`, continue with the rest.
8. Gate passes -> `--commit`, close the loop, proceed.

Special cases:
- `cleanup_regression` (route_cleanup exit 1): restore the snapshot and
  continue WITHOUT cleanup - optional by design (see Known limits).
- A fixer reporting `requires_pipeline_rewind` (schematic/library change
  needed after P5): stop the loop, present the tradeoff. Since T8 this is
  usually `add-part`/`swap-part`/`remove-part`, which preserve placement and
  routing - a true rewind to P4/P5 is the last resort, not the first.
- Silk findings: scripted fixes (silk_place.py, then silk_fix.py for what
  it could not place; place_edit add_text/move_text/set_text/silk_clear);
  pin-locked labels ONLY; footprint-INTERNAL silk stays librarian.

**Change approach** (the loop is not converging, or the fix is out of reach):
render the board (`scripts/render.py`), write a digest (what failed, what was
tried, the remaining violations with coordinates) and pick the next move
yourself - a different fixer tier, an upstream change (placement, part,
architecture), a cross-stage spawn. Record it (`state.py decision`). Only a
waiver of an error on a board that will be built, or a cost/feature trade, goes
to the person, as a packet (see "What is the person's").

## Design document (living, per-run)

At every checkpoint report (H1-H5) and once more at P10 close, run inline
`scripts/report_gen.py --workspace <ws>` -> `reports/design_doc/<board>-design-doc.pdf`
(phase-aware: sections not yet due render as pending stubs). NON-BLOCKING: on
exit 1/2 log `state.py log --event report_gen_degraded` and continue - the
report never gates the run. Filing the final doc (`--file`) and the P10
highlight and full documents: `reference/design-docs.md`.

## Board guide (owner-facing, per board)

After the `dfm` gate passes and the fab package exists (gerber zip, BOM.csv,
CPL.csv), and before the H5 order handoff, spawn `board-guide`. It gathers
its own facts (`guide_facts.py --render`, then the quote, schematic export and
`gen_cost.py` run cost that its `todo` names) and writes
`fab/<board>-guide.pdf` with the pdf-material-builder skill: what the board
does, the schematic, how to use it, the BOM, the cost, how to order it at
JLCPCB and what it cost to design. It never gates anything, but a full run is
NOT done until the guide exists: a build failure is reported at H5 with the
.tex path, never skipped silently. It sits beside the design doc.

## Human checkpoint presentation format

Digest + artifact, never raw logs. Message shape:
- What phase or task completed and what the artifact is (one line).
- The digest (<= 10 lines, numbers first: gate counts, cost, key choices).
- The files to look at (render PNGs, schematic PDF, the design-doc PDF,
  quote table) as paths.
- The specific question(s), each with a recommended answer - or "none: going
  on" when nothing in it is the person's.
Then carry on. Record an answer when one comes: `state.py human --checkpoint
<n> --status approved|rejected [--note ...]`; a rejection loops the phase with
the person's notes as new constraints (and a rejected H4 vetoes release).

A recipe's `human_hold` is the ceremony dial for edits outside a full run:
0 proceed silently, 1 record a decision line, 2 summarize at the next
checkpoint, 3 the person sees it before an order.

## Agent spawn template

A role runs as a helper agent. In the Hive that's `hive agent spawn --role
worker --brief @<file>`, with the file holding what's below. Its account
comes back to you as a message, and its files land on its own branch for
you to take, so give it the workspace paths to write. If your harness has
its own in-turn helper (a subagent tool that shares your checkout), use that
instead for short read-and-report roles: it's faster and its files are
already yours. Either way, every spawn's brief contains exactly:
1. The role prompt file content (`agents/<role>.md`).
2. The workspace-relative paths it needs (inputs + where outputs go).
3. Its assignment specifics (which block/sheet/interface/work order).
   For P3/P6/P7 spawns: run `scripts/knowledge.py --select --workspace
   <ws>` and paste its `prompt_block` here - deterministic knowledge
   retrieval keyed by the P2 block list + P3 packages (empty = omit); the
   workspace's VERIFIED research records ride along, drafts never do.
   For a researcher spawn: paste the task's brief (`research.py open` /
   `research.py brief`); design and fixer agents get no web tools, ever.
4. Termination: "return the output contract; do not start other phases'
   work."
5. Log it: `state.py spawn --role .. --model .. --phase ..`.
Reviewers (`schematic-reviewer`, `verify-reviewer`) get FRESH context -
never reuse a generator/router conversation for its own review.

Spawn tiers (T6-measured; escalate one tier when a role must overrule its
inputs, never silently downgrade):
| fable/max | router (novel board; proven-chain re-run: sonnet/medium), learner (owner-present teaching) |
| fable/high | architect, placement, schematic-reviewer, verify-reviewer, domain-reviewer (first spin, one per domain), doubter (a board's own questions, a check for each), requirements-analyst, researcher (coverage-gap research; the ONLY role with web tools, allowlisted, acquisitions through research.py fetch) |
| fable/medium | schematic-block (thin root-stitch: sonnet/high) |
| opus/high | research-interface-spec, research-power-architect, sim-analyst, fixer (copper/route), research-second-reader (fresh context; refutes or verifies a task's records), checklist-verifier (fresh context; rules a blinded checklist packet with planted faults, `checklist_verify.py`) |
| sonnet/high | research-component-scout, research-reference-design, part-sourcer, datasheet-extractor, coverage-mapper (schema-forced record->slot edges at P2/P3 exit), board-guide (owner PDF at P9 close) |
| sonnet/medium | librarian, fixer (silk/sch/parts/fab), placement (backward-edge re-spawn) |
| inline-default | board-setup, ordering, dfm (spawn = exception path) |

The tiers name Claude models because that's what upstream measured. A helper
runs on whatever model the Hive routes it to, or your harness lets you pick,
so read a tier as how much reasoning the role needs: ask for that model when
you can choose, and record what it actually ran on with `state.py spawn
--model`.

Model availability: if a tier's model is unavailable to the session, substitute
the nearest available model one effort step UP (fable/max -> opus/max,
fable/high -> opus/xhigh, fable/medium -> opus/high) and record the
substitution in the spawn ledger. Never silently drop to a weaker tier.

## Known limits (be honest about these)

- No full-wave solver: impedance is impedance.py's 2D quasi-static field
  solve (no dispersion, no loss), SI checks geometric. Multi-GHz serdes is
  out of scope; say so if a brief asks.
- RF laminate at JLC: 2-layer RO4350B only (stackups `JLC_HF2_RO4350B_*`,
  jlc_capabilities.yaml `materials`). RO4003C and Rogers + FR-4 hybrids are
  not sold there and are refused by name; JLC sells no impedance control on
  the HF line, so its widths are pcb-flow's own solve at design Dk 3.66.
- JLCDFM and payment are human steps by design (no public APIs).
- kipy/IPC is the KiCad-11 migration target; this pin drives SWIG bundled
  python via the edit scripts - never bypass them.
- route_cleanup's V13 dangling-pass defect was root-cause-fixed at T6. Its
  one live run since (bb-amp, 2L pour) was 0 ops with DRC unchanged, so the
  fix is still unexercised on real dangling geometry: run it dry-run first,
  inspect, then live - no longer a blanket skip on 2L pour boards.
- Residual check blind spots are trigger-indexed in
  reference/remediations/<check_id>.md (viasless pour-channel disclosure
  stays a router duty; drill classes closed at T6 by drill-aware floors).
- The outline is editable after P5 (`board_edit.py --outline WxH|fit|keep`,
  verb `resize-board`): place against a provisional outline, then `fit` to
  the placement. It never clips - anything the new edge would push off the
  board or bring inside the fab edge clearance refuses, listed.
- Fab floors (.kicad_pro rules + ERROR severities) come from
  lib/fabfloors.py; netclasses split per required width (T1).
- board_update re-nets a surviving part's pads (mode rewire_pad) only where
  no track or via touches them - an unrouted board, or pads not yet routed.
  A rewire or net rename on ROUTED pads it refuses (dry-run exit 1,
  `plan.unsupported[].routed_pads`): that is a schematic + re-route job, not
  surgery. Its region scan for added parts is front-side only. A part whose
  footprint was wrong (pad numbers, pad shapes) is re-padded in place with
  `part_repair.py` (`swap_fp` from the fixed library, or `renumber`): it rips
  the copper round the part, re-routes the nets and keeps it only if no net
  gains an island, DRC is no worse and no violation is new near the part.
- order_quote figures are estimated:true; the JLC cart is the only real quote.
- JLCPCB Open API: PCB ordering only - there is NO assembly/PCBA API
  (BOM/CPL ordering stays the JLC web flow), and 4-layer boards are the
  web path (`--api-create` guards on layer count). copperWeight as a
  string is accepted (live, 2026-07-29). The API carries no tracking
  number and no delivered state - status 5 Shipped is the end of its
  ladder, so boards-in-hand comes from the owner. JLC Balance payment
  mechanics are still unverified (fail safe, before money).
- Sim legs: SPICE covers analog fragments only (digital pins = datasheet
  stimulus models; buck switching NOT simmed - no vendor models by
  policy); sim_board's ICs are open circuits and its I/O drive is an ideal
  source behind 25 ohm, its discretes generic models (LED Vf from
  parts.json or a colour table, BJT BF=100, MOSFET level 1 at the
  part's threshold); check_irdrop injection is worst-case unless source_ref/sinks
  declared; check_pdn_z's segmented planes (#498) leave out fringing and vias between
  the planes, and a ragged pour may not converge (it warns); its VRM and
  package/die models are class defaults (flagged `assumed`) unless the
  constraints power entry gives `vrm` / `package` or reference/pkg_pdn.yaml
  has a verified row.
