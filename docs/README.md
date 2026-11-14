# Designing a board with pcb-flow in the Hive

pcb-flow is the Hive's KiCad design module. It takes a board from a written
brief to checked KiCad files and a JLCPCB fab package, or another
vendor's ([vendors.md](vendors.md): PCBWay, OSH Park, Aisler, Eurocircuits,
Seeed Fusion; their rules, files, sourcing and prices). The way through is
[playbook.md](playbook.md); the agent roles are in [`agents/`](../agents/),
the scripts in [`scripts/`](../scripts/) and their tables, recipes and
knowledge in [`reference/`](../reference/). It began as ihsan-sa/pcb-flow's Claude
Code skill ([where it came from](upstream.md)), but it isn't a skill: nothing
here is loaded by `/hwde`, and nothing needs to stay as upstream has it. This
page covers how an agent runs it inside the Hive.

## Before you start

- Run `pcb-flow check` (`$HIVE_MODULES/hive-hwde/bin/pcb-flow check --quiet`). It must
  exit 0. On first use it builds the module's Python venv in your HOME from
  `requirements.lock`, using the Hive's uv; that takes about 15 s.
- KiCad 10.0.6, freerouting 2.2.4 and KiCadRoutingTools 0.19.0 (for
  `route_critical` and route_auto's KRT finish) are the Hive's, read-only
  in `/hive/tools` (put there through a warden). Java 25 is the system's.
  `bin/pcb-flow` points the skill at that folder
  (`HWDE_TOOLS_DIR`) and the skill's `env.py` finds them all, plus KiCad's
  own python with `pcbnew`, its ngspice, its 3D model loaders and its stock
  symbols (KiCad 10's `.kicad_symdir` libraries, flattened into
  `~/.cache/hwde/symbols` on first use). If `pcb-flow check` can't find one in a
  Hive that has no `/hive/tools` (a fresh install on a laptop), run
  `pcb-flow tools`: it downloads all three into `~/.cache/hwde/tools` (about
  0.6 GB, no root, each file checked against a pinned sha256; Linux x86_64),
  adds a Java 21 runtime if the system has none below 17, and tells you if
  xvfb is missing (that one needs `sudo apt install xvfb`). In a Hive that
  has `/hive/tools`, ask a warden; don't install KiCad yourself.
- In another project's test or score check (a fresh HOME, only `/work` and
  `/hive/tools` mounted, no `$HIVE_MODULES`), run pcb-flow as
  `/hive/tools/hwde/bin/pcb-flow`. That's a published copy: `pcb-flow publish <dir>`
  builds it at `<dir>/hwde` with its own Python 3.14 and the lock's packages
  installed, so it starts with no venv to build and no download, and writes
  `<dir>/hwde.version`. The copy runs the installed module's code
  (`/hive/modules/hive-hwde`, which follows main at every merge) with its own
  Python whenever it can see the module and both have the same
  `requirements.lock`, so code changes reach checks with no new publish; a
  warden re-publishes only when the lock changes. Agents see the module now;
  checks see it once the Hive mounts modules in checks (hive task
  261029-140256); until then a check runs the copy's own code. A board's dfm gate still fetches
  LCSC models from EasyEDA (rate-limited, HTTP 403) into a fresh HOME's
  cache, unless the board has an `easyeda/` folder beside its `parts.json`
  (`cpl_verify.default_cache`); commit that folder so a check needs no fetch.
- Boards go in the project you work in, under `boards/<name>/`
  (`HWDE_BOARDS_ROOT`, default `$PWD/boards`). Commit the workspace on your
  branch like any other change. Gate `--commit` isn't needed.

## Running it

- `pcb-flow <script> [args]` runs `scripts/<script>.py` with the venv python
  and the `HWDE_*` pins set. Where the playbook, a recipe or a role prompt
  says `scripts/<x>.py`, run `pcb-flow <x>`. With no X
  display it runs the script under its own Xvfb, with GLX off because H0's
  NVIDIA libraries crash Xvfb.
- `eval "$(pcb-flow env)"` puts the same pins in your shell. Its `HWDE_PY` and
  `HWDE_MODULE` (the module's root) are what a board's own generator script
  (`kicad/gen/*.py`) runs with:
  `PYTHONPATH=$HWDE_MODULE/scripts:$HWDE_MODULE/scripts/lib $HWDE_PY kicad/gen/root.py`.
  `HWDE_SKILL`, its old name, is still set to the same path for boards that
  use it.
- Part datasheets: `pcb-flow datasheet_extract --fetch boards/<name>` puts
  each parts.json line's PDF in `parts/datasheets/<lcsc>.pdf` (not
  committed; `index.json` beside them keeps URL and sha256). LCSC's
  wmsc.lcsc.com answers with a 140-byte script redirect, not the PDF;
  the fetch follows it, so don't curl datasheets yourself. Each entry gets a
  status from the PDF, written whenever `check_ratings` or the ledger reads the
  index and again only if the PDF, the MPN or the check changed (`--assess
  boards/<name>` forces it): a datasheet matches by naming the MPN or by an
  ordering table (`reference/ordering_codes.yaml`) that decodes it, else it is
  suspect. An entry with no PDF on disk stays unassessed until `--fetch`.
  A part with no `parts/<lcsc>.json` is rated from pcb-flow's own library,
  `reference/extractions/<lcsc>.json`, when it has an entry (rules in its
  README: every number cited to a datasheet page).
- Every board with a netlist gets `pcb-flow sim_board --workspace boards/<name>
  --out boards/<name>/reports/sim_board.json`: ngspice over the board's own
  passives and discretes (KiCad 10's bundled libngspice, found by this
  wrapper), with the hand calcs a reviewer would do and a `known_unknowns`
  list. A review answers each known unknown from a datasheet or states the
  margin it rests on; `pcb-flow eng_calc <calc> ...` gives the numbers a written
  margin cites.
- Every board with a power input also gets its power paths checked:
  `pcb-flow sim_inrush --workspace boards/<name> --out boards/<name>/reports/sim_inrush.json`
  (load switches against a USB PD source: inrush, a switch that conducts
  with firmware off, turn-on SOA as a peak junction temperature, hard
  reset), `pcb-flow check_power_path` (in the verify suite: the current each
  pad-track-via chain carries, against the budget, `power_paths` and the
  PD contract) and `pcb-flow mlcc_dcbias --workspace boards/<name>` (ceramics
  at their DC bias). Put the PD contract in constraints.json as
  `"pd_source": {"contract_v": .., "contract_a": ..}` and the real current
  of each high-current run in `power_paths`; `pcb-flow soa_check` gives a FET's
  Tj for a written margin. These are what hifi-receiver-2's first-spin
  power review did by hand (its docs/review-2/power.md).
- A board with analog or audio stages gets its hand numbers from
  `pcb-flow analog_calc <calc>`: stage noise (`noise`), line levels and
  bridging (`level`, `bridge`, `damping`), coupling corners (`hpf`),
  slew (`fpbw`), op-amp phase margin into a cap load and the Riso that
  fixes it (`riso`), class-D LC filters and output power (`lc`,
  `classd`), and a SAR ADC's charge-bucket settling (`sar`).
  `pcb-flow analog_verify` re-proves each one against ngspice.
  netlist_audit errors on an op-amp/comparator/in-amp input with no DC
  path for its bias current (`opamp_input_no_dc_path`), and warns on one
  held only by a connector and on a reverse-biased electrolytic
  (`polarized_cap_reverse`). Which rules of thumb held up, which are
  folklore, and the ngspice traps (`x^3` drops the sign; THD needs a
  max timestep) are in
  [reference/ingest/analog-audio.md](../reference/ingest/analog-audio.md).
- netlist_audit's datasheet-table rules (pin states, MCU pins, I2C
  addresses, pin leakage, I/O levels) only know the parts in their
  `reference/*.yaml` tables. Add a part with `bin/hwde part_rows PART
  --pdf DATASHEET.pdf`: it drafts rows from the PDF, each with a `cite`
  (page, table and a quote), and lists the pages an address table or AF
  matrix is on as leads. A reader fixes the draft, and `bin/hwde part_rows
  --verify reference/*.yaml --pdf ...` checks every quote is on its cited
  page and table and every cited number is in its quote before the part
  gets `verified: true`. Until then a finding that needs the row is
  reported as not judged, not as a fault.
- A board that runs firmware gets `pcb-flow check_fw --board boards/<name>`,
  run at every schematic change, not first at bring-up. It runs hive-fw's
  `fw constraints` (with `fw pins` inside it) for each firmware target whose
  board is this one in fw's `targets.json` (or `--target`), reading the
  firmware beside the board in your checkout. It reports fw's own problems
  with fw's code (PIN-NET a firmware pin on the wrong net, STRAP-0/2/12 a
  strap pulled the wrong way at reset, BOARD-* the module's flash or
  PSRAM) and the ones only the board side can see: the ESP32's supply
  budgeted under the radio's peak current in `constraints.json`, an
  input-only GPIO 34-39 nothing drives or pulls, and an I2C line with no
  pull-up. Its `targets[].needs` are what
  a netlist can't settle (antenna keep-out, address straps); weigh each.
  Exit 0 pass, 1 problems, 2 when an ESP32 board's fw (one with
  `constraints`) isn't found: it looks at `--fw`, `$HWDE_FW`, then the fw
  module's `bin/fw` (named hive-fw or fw) in `$HIVE_MODULES` or beside this
  module. A board with no ESP32 and no `--target`, or one that no target
  names, passes with `nothing_to_judge` and needs no fw. A check sandbox
  sees no fw, so on an ESP32 board it is an agent's check, not a per-PR one.
- Start from `pcb-flow task_router --task "<the ask>" --workspace boards/<name>`
  and follow its recipe (`reference/recipes/<verb>.md`). The whole pipeline
  is `full-run`.
- The playbook's roles run as helper agents: `hive agent spawn --role worker
  --brief @<file>`, or your harness's own subagent tool for a short
  read-and-report role ([playbook.md](playbook.md), "Agent spawn template").
  Give each one its role prompt (`agents/<role>.md`), the workspace paths, and
  the `pcb-flow` command line in place of the venv python. Record each spawn with
  `pcb-flow state spawn`. Reviewers always get fresh context.

## Documents: the design doc and the board guide

`pcb-flow report_gen --workspace boards/<name>` builds the board's design doc
(`--kind highlight` and `--kind full` the shorter and longer ones), and the
`board-guide` agent writes the owner's guide. Both are set in the
pdf-material-builder skill's house style. `bin/pcb-flow` finds that skill in
`~/.claude/skills/`, then `/opt/pdf-material-builder` (where this machine has
it), then `$HIVE_PROJECTS/pdf-material-builder`, and `pcb-flow env` prints it as
`HWDE_PDF_MATERIAL_BUILDER`. Where none is installed, `HWDE_HOUSE_STYLE` points
at the module's plainer stand-in (`bin/house-style-fallback/`), so report_gen
still builds the PDF rather than stopping at `--tex-only`, and the guide is
built with lualatex directly and skips the skill's style check.

report_gen reads each stage's report from the name the recipes give it
(`reports/verify_all.json`, `reports/fab_export.json`, `reports/bom_cpl.json`,
`reports/gate-dfm.json`). It also takes the names earlier runs used:
verify_all's own `reports/checks/summary.json`, `verify-summary.json`,
`fab-export.json`, `bom-cpl.json` and `dfm-gate.json`.

## Checkpoints are reports, not stops

pcb-flow's checks are tools you run, not a stage machine: `set-phase` warns
instead of refusing, the fix-loop and cross-stage budgets count past zero
(`over: true`), `board_init` notes a fixed outline instead of refusing it, and
release (`attest.py`) needs the gates passed, not the person's sign-off. Going
past a default is your judgment; record why with `pcb-flow state decision`.

H1 (architecture), H2 (schematic), H3 and H4 (verified board) are reports you
write and keep going. Bring the person only what is theirs: waiving an
error-severity finding on a board that will be built, a cost or feature trade
the brief didn't settle, and the order itself (H5, money). That goes as a short
packet to the project's lead: the files to read on your branch, a digest of 10
lines or fewer, and the exact question with your recommended answer; the lead
puts it to the person. Don't use a warden request for it, and don't wait on
it: carry on with your recommendation, say so in the packet, and rework if the
answer is no. Record answers with `pcb-flow state human` (a rejected H4 blocks
release until reworked).

## Know what the board doesn't know

Every board keeps `ledger.json`: each fact its design relies on (per BOM code:
JLC stock, the part JLC will really place, whether JLC still sells it; per
part: pin ratings, pinout, footprint), verified with its source and date,
failed, or unknown. A rating compared with a distributor's or a package table's
number, no datasheet read, is listed, and that doesn't stop an order. The checks fill it in as they run, and one that can't
tell writes unknown. `bin/pcb-flow ledger report --workspace <ws>` lists the gaps.
`bin/pcb-flow ledger set ... --source "..."` records a fact you checked yourself;
verified is refused over a suspect or placeholder datasheet unless
`--force "why"` says why.
A board isn't ready to order (attest refuses, order-ready drops) while any
fact is failed or unknown. Stock is good for 72 hours, so run
`bin/pcb-flow stock_check --workspace <ws>` right before the order packet; the
person's flow is "I order it, it arrives, I plug it in", with no surprise at
JLC's BOM review.

## Checklists count without the person's sign-off

A coverage checklist (`reference/knowledge/checklists/<id>.yaml`, or one a
research task drafts under `<ws>/research/checklists/`) says which knowledge
classes a block needs before pcb-flow claims it knows enough to design one.
Upstream, a checklist counted only once the person approved it. Here a
checklist also counts once an independent agent has ruled it:

    bin/pcb-flow checklist_verify packet --checklist <id> --packet <dir> --key <file>
    # spawn agents/checklist-verifier.md (fresh context, not the checklist's
    # author) on <dir> only; it writes a verdicts JSON
    bin/pcb-flow checklist_verify rule --checklist <id> --packet <dir> --key <file> \
        --verdicts <its JSON> --write
    bin/pcb-flow checklist_verify status        # which checklists count, and why not

The packet hides the checklist among at least three planted mutants of it: a
required class dropped or swapped, an unrelated class added, a number in a
note changed. The ruling counts only when the verifier names every planted
fault for the reason it was planted and calls the real checklist sound. Each
required class must also rest on an active record that holds the class and
applies to the checklist's own blocks, or on a cited source file that exists.
`rule` checks that itself rather than taking the verifier's word. A verifier
that misses one planted fault proves nothing, so its whole ruling is refused.
`--write` sets the checklist to `verified` with a `verification` block pinned
to a digest of what it decides (applies, requires, sources). Coverage then
counts it as it counts an approved one, at any floor up to `approved`. An
edit to what it decides breaks the digest, and the checklist reads draft
again until it is ruled again. The person's `approval` still works and still
ranks above this.

Planted faults don't catch a verifier that diffs the items and votes on the
majority. The basis check does, because it is mechanical.

## Works first time, watches itself, brought up by the Hive

The person, 10-28: they give the spec, order the board, plug it in, run one
script and leave it; the Hive brings it up, updates its firmware, tests it
and tells them when it is ready. "This should be the flow for all board
designs." Take it as far as each board needs, and record how far:

- Design the board to see itself (`reference/self-observation.md`): rail
  dividers into spare ADC pins, status pins to GPIO, output loopback taps, a
  USB console. `bin/pcb-flow check_observability --workspace <ws>` lists each
  rail as self / test_point / blind and each PG/FAULT/INT as watched or not.
- Before the order, a first-spin review (`reference/first-spin.md`): one
  `domain-reviewer` per domain, every margin a number, contingencies on the
  board, every blocker and major answered. `bin/pcb-flow first_spin --workspace
  <ws>` says what is still open and merges the tools the reviewers wanted;
  file those to hive-pcb-flow.
- Doubt the board (`reference/doubt.md`, `agents/doubter.md`): `bin/pcb-flow
  doubt ask|add|fact|run|dispose|status|promote --workspace <ws>`. The
  agent forms its own questions about how this board could fail, writes a
  check for each under `<ws>/doubts/checks/`, and closes every unknown with
  a cited fact; general checks are promoted into `scripts/doubts/`, which
  every board is asked.
- Boards in hand: the `bring-up` verb and `reference/remote-bringup.md`.
  `bin/pcb-flow state bringup --workspace <ws> --results bringup/results.json`
  closes it with the measurements (a project bench's session.json converts);
  a bare `bringup_passed` is refused, and `bring-up-passed` cites the file.

hifi-receiver-2 is the first instance (its `docs/review-2/`, `docs/remote.md`
and `bench/`).

## Every board gets its own docs

A board's `README.md`, at the root of its workspace, says what the board is,
the design decisions and why they were made, how to rebuild and re-check it,
and how to test a built one. It has to make sense to someone who has never
heard of pcb-flow. Write it when the run starts and keep it current at every
stage: a decision recorded in `state.json` belongs in the README the same day.
A run isn't finished until its README matches the board.
[`boards/hwde-led-test/README.md`](../boards/hwde-led-test/README.md) shows
the shape.

## Be critical of the tools

The module improves the way the rest of the Hive does: by changing it, not by
working around it. When a script, check, prompt or recipe gets in the way or
gets something wrong (a false finding, a missing case, an agent role that left
out a field, a path that only works upstream), note it with the file it lives
in. Fix it in this module if it's small and yours to make. Otherwise file it
as its own change: `hive agent brief --project hive-pcb-flow --title ...
--done ...`. Board-local workarounds are the failure mode. List what you hit
in your account.

A fix to a script goes in with a test under `tests/` (run them with
`eval "$(bin/pcb-flow env)"; $HWDE_PY -m pytest -q tests`), and a line in
[upstream.md](upstream.md) so an update from upstream keeps it.

A workaround you can't avoid gets a row in [workarounds.md](workarounds.md)
naming the gate steps it touched, and a probe in `tests/workarounds/` that
exits 0 while its cause stands. The test (`bin/pcb-flow test`) fails while a row
and its probe disagree. A shim in `bin/pcb-flow` checks its row (`wa <id>`), so
it runs only while it's listed. When you fix a cause, its probe starts exiting
10: take the workaround out, delete the row, and keep the probe as a guard.

The score (`bin/pcb-flow score`, [score.md](score.md)) doesn't read that table:
it re-runs the gates on the boards pinned in `pinned/`, with every shim off,
and counts what they find plus each board-local stand-in script. A fix in
pcb-flow that clears a finding on those boards raises it at once. When a product
board you build in another project drops a stand-in script or changes in
another way worth measuring, re-pin it: `bin/pcb-flow pin <board> --from <clone>
--commit <sha>`, classify any new script in `pinned/<board>.json`, run
`bin/pcb-flow pin --digests`, and name the commit in the commit message.

Research is held to the same standard: a paper is a lead to check, not a
fact. `pcb-flow papers` ([research/README.md](research/README.md)) follows the
topics in `reference/research/watch.yaml`. It keeps every paper it has seen
in `reference/research/papers.yaml`, reads only legitimate open copies (the
rest go on the person's list, [research/paywalled.md](research/paywalled.md)),
and gives each claim pcb-flow's own verdict, with the test that reached it. Only
claims pcb-flow reproduced become [guidelines](research/guidelines.md). When
`pcb-flow papers due` lists topics, scan them; when a design leans on a rule of
thumb nobody has checked, add a topic.

Record each run's wall time per stage (the `phase` events in
`state.json` history) and the gate attempts and fix loops per gate. That way
a later board shows whether the module got faster or cleaner.

## Stackup copper

`check_stackup` (verify suite, #728) reads the board's stackup block as written.
It fails a copper layer outside 0.009 to 0.21 mm (0.25 to 6 oz) and F.Cu and B.Cu
that differ by over 0.05 oz, the tolerance `order_submit --api-create` refuses at,
since an order's copper weight is one value for both outer layers. A board with
no stackup block is nothing to judge, not a pass. What a fab offers stays with
`dfm_check`.

It also compares the three thicknesses a board carries (#745): the stackup's
layer sum, `(general (thickness))` and `fab/quote.json`'s `spec.thickness_mm`.
Any pair over 10 % apart (of the larger) is an error. 10 % is over twice the
spread of 47 of 48 boards (4.4 %: mask and silk make the sum 1.674 against 1.6).
`order_submit --api-create` takes the stack sum as the board's thickness when it
and the general thickness are over that apart, since the layers are what is built.

`--api-create` holds the quote's `thickness` to the same rule: within 10 % of the
larger of quote and board, not 0.005 mm. A fab quotes the laminate's nominal
(1.52 for RO4350B 60 mil, 1.6 for FR-4, about +-10 %), so a corrected
rf-kit-ro4350b (stack 1.6, quote 1.52) orders, and a quote of 1.0 against 1.6
is refused (#753). Mask and silk layers must be 0.005 to 0.05 mm
(`mask_thickness_range`, `silk_thickness_range`); the 48 boards' mask is 0.01.

## Straps, reset states and the antenna

Two verify checks do what the first-spin review of hifi-receiver-2's boards
did by hand (its `docs/review-2/digital.md`, DIGITAL-1/2 and 18/19), on
every board:

- `check_straps` resolves each strapping pin of an MCU (ESP32, -S3, -C3)
  from the board's netlist at power-on and on an MCU-only reset: external
  pulls, the internal pulls of attached parts, series resistors, an
  oscillator's output while its enable reads high or floating, and an IO
  expander whose outputs hold through the MCU's reset. It fails a strap that
  can read the wrong level, a mid-band level or a clock, and an expander pin
  whose pull fights an external one. What each part does at reset is in
  `reference/reset_states.yaml`; a pin it doesn't know is a warning naming
  the pin. Add the part (with its datasheet) rather than ignoring it.
- `check_reset_drive` builds beside it with the same resolver: every FET
  gate, BJT base and part enable (EN, ON, SHDN, SDZ...) an MCU pin reaches,
  directly or through one resistor, must sit at its off level at power-on
  with the pin undriven. A floating FET gate or a mid-band enable fails, a
  floating enable or base warns (the part may pull it), and a load on a pin
  the chip itself drives during power-up (`glitch` in reset_states.yaml:
  the ESP32-S3's GPIO1-17 low for ~60 us, its USB_D+ high for up to
  ~170 ms, U0TXD's boot log) fails whatever pull it has. Pins the chip
  pulls at reset stay check_straps' `load_on_at_reset`.
- `check_antenna` finds a module's PCB antenna end from its footprint (the
  body beyond the pad field), and fails copper of any kind under it on any
  layer. It warns when the end is more than 1 mm inside the board edge, or
  copper starts less than 15 mm beside it. The modules are in
  `reference/rf_modules.yaml`; a board can name its own in constraints.json
  (`"antennas": {"U7": "pcb"}`).

On hifi-receiver-2's main at 66891e8, `check_straps` fails core's GPIO0
(the 50 MHz oscillator's OE at 1.65 V from the TCA9555, and held through an
ESP32 reset) and six of its expander pins, and `check_antenna` fails core's
U7 (GND or +3V3 fill under the antenna on all four layers). lite-main
passes both, with warnings: 2.2 mm cleared beside the antenna, and ES8388
MCLK on GPIO0 with no known reset state.

## Signal integrity

`check_si` (verify suite) finds the nets that need signal-integrity care by
interface rather than by declaration: oscillator and MCU clocks, RMII, I2S,
SPI, USB, Ethernet MDI and crystal nets, by name and by what drives them (an
oscillator part on the net or behind its series resistor makes a 1 ns edge,
an MCU pin 2 ns). For each it solves the board's own stackup (the field
solver in `lib/impedance.py`) for Z0 and delay, and holds the net's longest
pad-to-pad path against its critical length, t_rise / 6 (Johnson). Past it
the net is a transmission line, and:

- a clock with no series resistor is `si_unterminated_clock`, and one whose
  resistor plus a ~20 ohm driver misses Z0 is `si_series_mismatch`;
- two long parts of the net more than 0.10 apart in reflection coefficient
  are `si_impedance_step`; a declared `z0_ohm`, or a pair's standard Zdiff
  (USB 90, 100BASE-TX 100), missed is `si_impedance_target`;
- the net goes through `check_return_path`'s corridor and return-via tests
  with its own rise time (findings keep that check's kinds).

Every fast net is also an aggressor: each near-parallel neighbour on its layer
is solved as a coupled pair (`impedance.coupling`: Bogatin's kb and kf from
the even and odd modes), and NEXT and FEXT over the coupled run are held to
5% of the swing for a digital victim and 1% for an analog one or a crystal
(`si_crosstalk`). RMII's halves are held to 10% of their 20 ns interval in
flight-time spread (`si_skew`). The report lists every SI net's length,
delay, Z0, critical length, termination and worst neighbours, pass or fail.
`constraints.json` `si` declares, corrects or ignores nets
(`reference/constraints_schema.md`).

The three worst crosstalk estimates are also simulated (`pcb-flow sim_si --pcb B
--xtalk --aggressor A --victim V` runs one by hand): both nets' routed paths as
T lines and coupled sections with a 2x2 L, C from the same field solver, the
driver's R and any series R behind a ramp, the receiver's R and C at the far end
(`si_crosstalk_sim`; `crosstalk_sim` holds estimate vs simulated). Without
ngspice the estimates stand and `not_run` says so.

On the pinned boards it finds core's I2S_BCK (98 mm from the ESP32 to three
converters with no series resistor), three core B.Cu runs over In2's +3V3
plane where tracks on In2 cut it right under them, and lite-main's I2S_DOUT
0.13 mm from BCLK for 47 mm on a 2-layer board. The planted fault
`si-unterminated-clock` stretches usbbuck4's MCO past its critical length.

## Length-matched groups (DDR)

`check_length_groups` (verify suite) holds groups of nets to a reference
length, the way DDR layout rules are written: a byte lane's data and mask
within +/-0.635 mm of its DQS, address/command within a window of the clock.
Each net's length is its copper path between its two farthest terminal pads
(a stub or test-point branch doesn't count) plus the via barrel wherever the
path changes layer, from the board's stackup: the barrel below the exit
layer is a stub and isn't counted. `constraints.json` `length_groups`
declares the groups: `nets` (globs), `ref_net`/`ref_nets` (else the group's
longest member), `tol_mm` or `window_mm`, and `max_mm`, `min_mm`,
`not_longer_than` bounds (`reference/constraints_schema.md`). Each mismatch
(`length_group_mismatch`, `length_group_bound`) says how much the net needs;
an unrouted or open member is a warning, and a board with DDR-named nets but
no groups warns that nothing was matched. route_cleanup's snap and
route_improve leave every net a group names as routed.

`route_tune` (P7, after the snap) lengthens the short ones: trombone bumps on
the straight runs of each single-ended member's measured path, legs three
track widths apart, the tallest that clears the board's clearance and edge
rule, until the net sits in the middle of its range. It measures with the
check's own function, so what it tunes to is what verify judges, and works
on any copper (Freerouting's, KRT's, hand-routed). KRT's own matcher
(`route.py --length-match-group`) tunes only nets it routes in the same run,
and only to the group's longest. A net that is too long and a run with no
room are reported, not forced; DRC that grows rolls the board back.

Diff pairs (#476): a pair in a length group gets coupled bumps on both legs
(the inner bump nests inside the outer one at the pair pitch, so both gain
the same and the skew stays), and any pair over its skew gets bumps on the
short leg next to the bend that built the skew, facing away from the
partner and low enough that the leg stays coupled. `check_diffpair` judges
the running skew too (`diffpair_phase`): P's trunk is sampled every 0.1 mm
against the point of N's trunk abreast of it, and a stretch over
`max_phase_skew_mm` (default `max_skew_mm`) lasting `phase_window_mm`
(else `rise_ps`/10 as distance, else 5 mm) is flagged even when the end
skew is 0 (+1 mm at one bend, -1 mm at the next).

`check_ddr_timing` (verify suite, #487) turns the same paths into time and
puts them in a budget, as a memory-interface timing report does. Each
track's delay comes from the field solver's eps_eff for its layer and width
(check_si's line model); a pair's reference is the mean of its legs. The
constraints `ddr` block names the controller and DRAM, the rate and which
length groups are byte lanes (`dq`, members vs their DQS), address/command
(`addr`, vs CK) or strobes (`dqs`, vs the CK group). The DRAM's base
tDS/tDH/tIS/tIH come from `reference/ddr_timing.yaml` (Micron 4Gb DDR3L Rev. P,
with the slew-derating tables, interpolated bilinearly) and are referred to
VREF, which reproduces every "VREF @ 1 V/ns" row in Micron's Table 58 but
one, a misprint. The controller terms are what NXP still publishes for the
i.MX 6ULL (pad tSKD 100 ps, 400 MHz max; NXP dropped the MMDC output window in
datasheet Rev. 1.1) plus its write calibration (the lane is re-centred) and
write leveling (DQS can only be delayed, so DQS must not reach the DRAM after
CK). Each group reports setup and hold margin in ps with every term, the
read eye left at the controller, tDQSS, and the leveling margin. Findings:
`ddr_setup_violation`, `ddr_hold_violation`, `ddr_dqss_violation`,
`ddr_leveling_out_of_range` (a warning while the 6ULL's leveling range is
unverified), `ddr_rate_over`, `ddr_part_unknown`, `ddr_slew_clamped`,
`ddr_unrouted`. At DDR3L-800 the budget is wide (about 270 ps of setup on a
matched lane), so a single DQ 15 mm long still passes timing and only
check_length_groups' HDG window flags it; at DDR3L-1600 without calibration
the same fault is a `ddr_setup_violation`.
## Repeated channels: lay out one, stamp the rest

A board with N identical channels (one sheet instanced N times: an 8-input
DAQ, per-cell balancers, per-port ESD, the L and R amps) gets one layout.
`constraints.json` `channels` names the source channel and its targets (by
sheet instance, or ref lists) and the transform: a grid pitch, or explicit
offsets with a 0/90/180/270 turn. `place_replicate --stage place` (P6, after
the source channel is laid out) puts every target's parts where the
source's land under the transform and locks them; `--stage route` (P7, once
the source is routed) copies its tracks and vias with each net mapped
through the pad table (the source's MID becomes the target's MID, GND stays
GND). Copy that would touch another net is dropped and listed; the nets
between channels are route_auto's. Parts pair by symbol uuid, which is the
same in every instance of a sheet file, so R101 pairs with R201 because it
is the same symbol; two copies of a sheet file pair by ref number mod 100.
A part only one channel has is reported and keeps normal placement.
place_metrics and route_quality report `channel_symmetry`: the share of each
target's parts and copper sitting exactly where the source's land (1.0 = an
exact stamp). KiCad 10's own Repeat Layout needs the GUI, so pcb-flow does the
transform itself.

## Op-amp outputs into a cable or a big capacitor

`check_cap_load` follows every op-amp output on the board's netlist through
0 ohm links, beads and coupling caps (100 nF and up are shorts at MHz) to
its load: caps to ground or a rail, and a cable on each connector reached
(constraints.json `"cable_pf": {"J3": 1000}`, else 1000 pF, 10 m of audio
cable). It works out the noise gain from the feedback network and runs
`analogcalc.cap_load_pm` with the part's GBW and open-loop Ro from
`reference/opamp_drive.yaml`. Under 45 deg of phase margin is an error,
under 60 a warning, each with the build-out resistor that reaches 60 deg:
the calculation picks the value, not the 47-100 ohm rule of thumb. A series
resistor already there is checked as the build-out with the load beyond
it. An op-amp the table doesn't know is a warning when it drives a load
with no build-out. Ro is the datasheet's open-loop output impedance at the
crossover (its Zo plot), not the closed-loop figure in the spec table; the
rows typed from memory say so in the finding until someone reads the plot.

On hifi-receiver-2's core, the OPA1678 drives nothing capacitive and the
INA186 drives its 10 nF filter through 1 kohm: no findings.

## ESD at the connectors, and what the ingest runs found

`bin/pcb-flow check_esd --pcb <board> [--constraints c.json]` (run by hand for
now; it joins the verify suite once its findings on the pinned boards are
judged) checks that every line leaving the board through a connector reaches
a TVS / ESD clamp first, with no via or branch before it, the clamp near the
pin and a ground via at its ground pad. It also prints an L·dI/dt estimate
of the overshoot for each line, and for each connector-to-clamp stretch what
runs beside it in a corridor (same or adjacent layer): the volts the strike
couples in (M·dI/dt, Grover's partial mutual, by images over a ground plane)
against the victim pins' abs max (`--parts`, `esd.victim_abs_max_v`). The
corridor's width is a judgement derived from those volts (`esd.corridor_mm`,
`esd.corridor_v`). `constraints.json` `esd.internal`, `esd.exempt_nets`,
`esd.clamps` and `esd.level_kv` tune the rest. The rules behind
it, and which EMC, ESD and thermal rules of thumb are verified, disputed or
folklore, are in [`reference/ingest/emc-thermal.md`](../reference/ingest/emc-thermal.md),
with the scripts that reproduce them.

## 3D models on their pads

`check_model_fit` (verify suite) reads every footprint's 3D model, VRML or
STEP, with no CAD kernel (`scripts/lib/modelgeom.py`). It places the model
the way KiCad's 3D viewer does, by the board's own `(model (offset) (scale)
(rotate))`, front or back, and lays the model's contact features against
the footprint's pads. Leads are flat geometry at the seating plane that
isn't the body's underside. Pins, shell legs and pegs are geometry below the
board, scored at each hole's centre. The fit is a pad-coverage score,
worked out at once for every slide within ±5 mm on a 0.05 mm grid by
cross-correlating rasters, at the model's own turn and at +90/180/270°.
A slide or turn that covers clearly more pads, and meets no fewer, is a
`model_offset` or `model_rotation` error. The finding gives the
`(offset)`/`(rotate)` to write in the footprint library. `model_scale`
(not 1 1 1), `model_floats` (lowest point above the board) and
`model_in_board` (geometry inside the board's material, within the outline,
where the footprint has no hole) are errors too. `model_unmatched` (no
placement meets half the pads: a wrong model) is a warning. A missing or
hidden model is skipped.

`check_body_clash` (verify suite, #608) reads the same models as solids in board
coordinates, on a 0.1 mm lattice, and fails when two parts share material
(`body_clash`), pin tails against the far side's parts included, or a body stands
in a mounting hole's screw column (`body_over_hole`). It warns on a body past its
own courtyard, tall bodies under 0.3 mm apart, a non-connector overhanging the
edge, and a connector with no readable model. Parts with no model are listed in
`not_judged`: a board with few models is hardly judged, so read that list. The
visual half is `agents/mech-reviewer.md` with `render.py --views mech`.

`lib_pull` runs the same fit on every footprint it pulls
(`check_model_fit.fit_footprint`) and writes the fitted `(offset)`,
`(rotate)` and `(scale)` into the library's `(model ...)`, so a model
EasyEDA misplaced is fixed before any board takes the footprint (the
pull's `model_fit` report; `--no-model-fit` skips it). A model no
placement fits is left as it was and listed in `model_fit.left`.

The hit: EasyEDA's TYPE-C-31-M-12 model, as `lib_pull` fetches it for
lite-main J1, sits 2.2 mm off its footprint. Its shell legs and pegs miss
every hole, so renders, the STEP and an enclosure fitted to it put the
connector in the wrong place. The check reports `model_offset` with a fit of
`(offset (xyz 0 -2.2 0)) (rotate (xyz 0 0 180))`. `fpfix.body_flags`
still flags a pulled library footprint at pull time (THT pins only). This
check runs on the board, every time.

## Placement type against the pads

A footprint's `(attr smd)` or `(attr through_hole)` is how KiCad's position
export, `kicad-cli pcb export stats` and bom_cpl's assembly class say the
part is fitted. EasyEDA's XR0850 8x8 mm power inductor ships
`(attr through_hole)` on two SMD pads, so pinned lite-main's
`bom-cpl.json` sent L1-L4, four reflowed inductors, to JLC's THT step.
`scripts/lib/fpattr.py` reads the type off the pads: SMD pads only make it
`smd`, plated pins only make it `through_hole`, and both (USB-C shell legs,
pin-in-paste, THT parts with SMD tabs) leave its own type standing. A plated
hole inside an SMD pad is a thermal via, not a pin, so U8's exposed-pad
HTSSOP-32 stays `smd`. Parts marked `board_only`, `exclude_from_bom` or
`virtual` aren't judged.

- `fpfix` rule G writes the type the pads give into the library, so
  `lib_pull` fixes it on every pull. `fpfix.py --pcb` does the same for
  footprints already placed on a board. A footprint with both kinds of pad
  and no type at all is residue: only the part's drawing settles it.
- `bom_cpl` classes a part by its pads where the attr contradicts them,
  and lists each one under `attr_mismatch` in its report (and on stderr).
- `check_fp_attr` (verify suite) fails each placed footprint whose attr its
  pads contradict (`fp_attr_mismatch`), or that has both kinds of pad and no
  type (`fp_attr_unspecified`, which the position export drops). Planted
  fault `attr-tht-on-smd`: blinky2's SOT-223 U2 marked through_hole.

## Net ties and their bridges

A net-tie footprint (KiCad's `net_tie_pad_groups`: a Bridged solder jumper,
a NetTie, a part whose own copper ties two of its pads) joins each group's
pads with copper it draws itself. KiCad's connectivity doesn't read footprint
graphics and each pad keeps its own net, so DRC passes the board the same
with that copper or without it.

- KiCadRoutingTools 0.19.0's writer moves every copper graphic with no net
  to silkscreen when it saves a board, so a stray logo can't short nets. A
  footprint's graphics never carry a net in the file, so it moved net-tie
  bridges too (#1008). `scripts/lib/krt_launch.py` keeps the graphics of a
  footprint with `net_tie_pad_groups` where they are, and every KRT run that
  writes a board goes through it (`route_critical`, `route_escape`, and
  `route_auto`'s finish pass). Other copper with no net still moves, a
  footprint's logo included. KRT's own KiCad plugin isn't covered.
- `check_net_tie` (verify suite) fails each group whose pads the footprint's
  own copper (its pads and its copper graphics, layer by layer) leaves in
  more than one piece (`net_tie_open`). The finding names the footprint, the
  pads and the layer the bridge belongs on. `moved_to` names the silkscreen
  layer of a drawing of the footprint's that spans the pieces: the bridge,
  moved off copper. A group with fewer than two of its pads on the board
  isn't judged.
- A fab's E-test knows a net by its IPC-D-356 name, and kicad-cli names the
  nets of a tie apart, so the tester reads the tie's copper as a short and
  fails a good board. `fab_export` gives the nets of each tie one name where
  the shipped copper joins them (`ipc_tie_renames` in its manifest): of the
  tie's names, the one with the most records stays. A tie whose copper is
  cut keeps its names, and a tie whose nets sit in a short with other nets
  isn't renamed. `dfm_check`'s netlist compare reports a tie still named
  apart as `dfm_net_short` with `tie` set: re-export the package, or, where
  one of the names also stands for a net outside the tie (kicad-cli keeps
  the last 14 characters), rename the nets first. A tie's one name isn't a
  `dfm_netlist_name_clash`. A tie whose nets the copper leaves in pieces is
  `dfm_net_tie_open`: each net is whole, so the tester can't see it.
  Re-export a stale package, and if `check_net_tie` fails too, put the
  bridge back.

The hit: two SolderJumper-3 Bridged12 boot straps on a 6-layer board lost
their bridges to F.SilkS at the escape step, and a later silk clean-up
deleted them. Both boot pins were left floating with every gate green.
Update the footprint from its library to put the bridge back. A jumper
meant to be open takes an Open variant, which has no `net_tie_pad_groups`.
Planted fault `net-tie-open`: blinky2 gains a jumper strapping /BOOT0 to GND
with its bridge on F.SilkS.

## A USB device's PHY

`check_usb_phy` (verify suite) reads the board's netlist from each connector
pin named D+/D-/DP/DM/DP1... to the first IC, through 0 R and ferrites. A
stacked dual USB-A's D+1/D-1 and D+2/D-2 are two ports, read one by one:
each D+ is paired with the D- that reaches the same PHY on the pin named as
its partner (a hub's USBDP_DN2 with USBDM_DN2, a SoC's USB0_DP with
USB0_DM). A link from IC to IC (a SoC to a hub's upstream port) isn't read.
A resistor of 100 ohm or less on the way is a series element, and a larger
one is a pull. A 4-pad common-mode choke (L, FL, CMC, T or FB with four pins) is
walked through by its `chokes` row's windings (Murata DLW21HN): D+ on a
winding's one end carries on from its other end. A choke with no row is a
warning `usb_choke_unknown` (add its windings), and a choke wired so D+
reaches D- through one winding, as one turned on its footprint, is an error
`usb_choke_shorts_pair`. Faults `usb-choke-pullup-weak` and
`usb-choke-turned` (issue #647). It holds the IC to what its own vendor's document says, row by
row in `reference/usb_phys.yaml` (WCH CH340, the STM32 lines of AN4879
Rev 5, NXP LPC per AN11392, RP2040, ESP32-S3, Microchip's USB2512B/13B/14B
hubs per DS00001692C, TI AM62x per SPRADO3B and the Octavo OSD62x-PM SiP
built on it):

- series resistors only where the PHY wants them: an error for a series R
  on ST's, WCH's, Microchip's hub or TI's AM62x PHYs, which embed USB 2.0's
  28-44 ohm driver impedance, and an error for none on an LPC (33 ohm) or
  RP2040 (27 ohm). The "22-27 ohm always" rule is folklore.
- the D+ pull-up where the PHY has none: 1.5 k +/-5% to 3.0-3.6 V (USB 2.0
  pull-up ECN, 7.1.5.1). A Blue Pill's 10 k fails, and so does a pull-up to
  VBUS. An extra pull-up beside a PHY's own is a warning, and a pull-up on
  D- is an error.
- on USB-C (a connector with CC pins), both D+ pins (and both D-) joined,
  and on a sink each CC pin's total Rd 5.1 k +/-20%, internal Rd included
  (Type-C R2.5 Table 4-28). That catches a bare receptacle with none, and a
  CH224K/A/Q or an Infineon CYPD3177 (Rd inside) with an external 5.1 k
  beside it. Outside +/-10% is a warning: the sink attaches but can't read
  the source's current. A CC with an Rp is a source port and isn't judged.

An IC the file doesn't know is a warning naming it. Add its row with the
datasheet's section rather than copying another vendor's habit. lite-main
and core (CH340B, CH224A/Q) pass clean, and so does a voice SBC (an
OSD6254-1G-IPM, a USB2514B hub on a stacked dual USB-A, a CYPD3177) that
had four warnings before those rows.

## A transformer's phasing

`check_transformer` (verify suite) reads each transformer in the board's
netlist against its maker's drawing, row by row in
`reference/transformers.yaml` (each winding's dotted pin, other end and
turns, the pins the drawing bridges, and the topology the part is built
for). hw-lab mains-psu passed erc, verify and attest with its flyback wired
as a forward: bulk on the dotted primary pin, the drain on the other, the
rectifier's anode on the dotted secondary pin. Its output and aux diodes
then conduct in the on-time, and VDD heads for 103 V against the
controller's 38 V.

- phasing: the switch end is the primary pin on a transistor's drain or
  collector (or an IC's DRAIN/SW/LX pin), and each other winding's
  rectifier is the diode on its switching end (the end with fewer parts,
  through series resistors of 100 ohm or less). A flyback wants the switch
  and the anode on like-dotted pins; a forward, unlike. A mismatch is
  `xfmr_phase`. constraints.json `transformers` may declare the topology.
- bridges: a bridge the drawing puts on the board joins two windings in
  series and nothing else. Pins on two nets are `xfmr_bridge_missing`, and a
  bridge node tied to other parts (mains-psu's 10 and 11 on its return) is
  `xfmr_bridge_loaded`.
- windings: both ends on one net is `xfmr_winding_shorted`; one end wired
  and the other open is a warning, `xfmr_winding_open`.
- turns: windings joined on a net count as one, from its outer ends.
  constraints.json `transformers: {T1: {turns: {"8-13": 7}}}` fails
  (`xfmr_turns`) when 8 and 13 aren't that winding's two ends or its turns
  differ, naming what the wiring uses (mains-psu had 8-11, 5 turns).

A T-part with no row, a primary with no switch found, or a winding
rectified by a FET goes in not_judged. Add a part's row from its drawing,
with the revision. Planted fault `xfmr-forward`: a 750314727 added to
usbbuck4 wired as mains-psu had it.

## Finishing a dense board's routing

`route_auto` ends with `route_finish`, so a dense board needs no copper
scripts of its own. After the Freerouting ladder (and KRT, when it is
found), `route_finish` rips what DRC names in a short or clearance error,
then routes what is still unconnected. It is a grid maze router that works
at the board's own rules: it joins pads, stubs and cut-off pour pieces (with
a via into the plane), and frees a walled-in pad by ripping the fewest
signal nets in the way. It never rips pour nets or nets with a width rule.
Run it alone on any routed board (`pcb-flow route_finish --pcb ... --dry-run`
first). Its `failed` connections have no path at the rules, which makes
them a placement or topology finding, unless the reason names `--max-nodes`:
then the search ran out of nodes, so raise that, or set `--search-weight`
(2 to 3 reaches far ends across pours with far fewer nodes, not always by the
shortest route). `no path` is within the search window, which `--margin` sets
and the board's outline limits, so a large margin costs search time, not a
grid over empty space.

When a single `route_finish` run can't finish in the time you have, or stalls
on the same failures, use `pcb-flow route_close --pcb ... --budget 3600` with the
same routing options, and no loop of your own. It runs `route_finish` in chunks
and keeps each good one, and every open connection gets a try each round. It
resumes from its state on disk if run again, undoes a chunk that changes
protected copper (`constraints.json` diff pairs by default), and stops when a
round closes nothing. Before it stops it routes the failing connections and
the open ones near them together (`route_edit`'s `route_group`), and keeps
that only if fewer stay open (`facts.grouped`). Its `facts` give the count left, the failing connections
and nets, and what to try next, so there is no chunk log to read. Each chunk
goes through `--runner` (`heavy --quick` where `heavy` exists, else run
directly).

When what is left is a few connections you can name, route them by hand
with the same maze router (`scripts/lib/mazeroute.py`) through `route_edit`.
The edit is applied atomically and DRC-checked like any other op.
- `route` takes a net and its two ends. An end is `REF.PAD`, `REF`,
  `x,y@Layer`, or several of these joined by `|`.
- `route_group` routes several jobs together (PathFinder). Use it for a
  channel that two nets both need, which greedy one-at-a-time order can't
  fill.
- `reroute` takes a net and a region. It rips the other signal tracks
  crossing the region, routes the net, then routes each ripped net back.
  It is refused if any ripped net ends with more copper islands than it
  started with.

`zone_patch --from END --to END --half-width W` draws a pour strip along
the path the router finds. Use it for a corridor of plane between two
pads, or one that dodges a via fence.

On hifi-receiver-2's core (278 parts, 4 layers), with every signal track
ripped, one Freerouting rung left 54 unconnected and 16 DRC errors.
`route_auto` with `route_finish` then took it to a handful unconnected with
no DRC errors. What remained was a 0.4 mm-pitch QFN pocket walled in by a
VBUS_IN track and a 77 mm long haul.

Before the planes go in, fan out the power and ground pins that need a via:
`pcb-flow route_escape --pcb ... --pads U5.3,U5.7,L1.1` puts a via next to
each named pad of any part (a QFN or SOT pin, an inductor's pad) where it
clears every other net's copper and hole at the board's rules, with a stub on
the pad's own layer. It looks outward first, never under the part, and skips
a pad that already has a via of its net nearby. A pad with no spot is joined
to its own copper close by, or is reported as a `pad_fanout_incomplete`
warning. `--via-in-pad` puts that pad's via at its centre instead, when the
pad holds the whole via, and sets it filled and capped so `fab_export`'s
`VIA-FILL.md` asks the fab for it; it stops on a board whose layer count the
fab doesn't fill vias on. `--plan` only reports, `--out-ops` writes the
`route_edit` ops.

## Boards from other EDA tools, and back

`pcb-flow eda_io import <file> --out <dir>` brings in a board from Altium
(.PcbDoc), Allegro/OrCAD PCB Editor (a Fabmaster extract .txt), Eagle and
Fusion's EAGLE export (.brd), EasyEDA Std (.json) and PADS (.asc) through
KiCad's own importer. KiCad says "Successfully saved" even when it skips a
layer, and its JSON report keeps `warnings: []`. So eda_io reads the source
file itself (`scripts/lib/edasrc.py`; the Altium reader is a small
compound-file reader, `lib/cfb.py`) and compares it with the import: copper
objects per layer, parts by reference, nets by name, vias, the outline. It
also refills the zones, runs DRC at the imported rules, and writes
`losses.md`: **Lost** (copper, parts or the outline, exit 1), **Check**
(renames, unrouted, netless pads, layers with drawings that were dropped)
and **Note**, then what the path never carries. Then `pcb-flow intake --source
<dir>` makes it a workspace. It lands at P6/P8 with no schematic:
kicad-cli has no schematic importer, so the schematic comes in through
Eeschema's GUI importer or is redrawn.

On KiCad's own QA boards with KiCad 10.0.6:

- Altium 4 and 6 layers, Eagle, Allegro 4 layers, EasyEDA Std 4 layers and
  PADS all import. The 14 to 16 Altium "Internal Plane N" and Allegro
  subclasses KiCad skips are empty in the source, so they're a note.
  Allegro's 16 drawing layers (fab notes, dimensions, DXF imports) are gone
  and named.
- Eagle: KiCad writes the parts' name texts on a layer called `UNDEFINED`,
  then refuses to load its own board. eda_io moves them to Cmts.User and
  says so. It renames a part with no number (`PWR` -> `PWR0`).
- EasyEDA Pro (.epro) crashes kicad-cli (`bad_function_call`). Export
  Altium from EasyEDA Pro instead. Allegro's binary .brd and OrCAD Capture
  have no importer at all. eda_io says so and exits 2.

`pcb-flow eda_io export <board>.kicad_pcb --to altium,allegro,orcad,pads,eagle,fusion,easyeda,neutral`
writes what each tool reads: the KiCad project (Altium's Import Wizard,
EasyEDA), IPC-2581 and ODB++ (Allegro/OrCAD, PADS, CAM), Allegro Telesis,
OrCAD and PADS netlists, GenCAD, IPC-D-356 and STEP (`--step`). It reads the
IPC-2581 and ODB++ back and checks every part, net and copper layer is in
them. The netlists are checked against the board's nets. Per target,
`losses.md` says how to bring it in and what doesn't carry. Rules never
carry. Allegro/PADS need their own footprints by name. Eagle and Fusion
Electronics have no editable path from anything KiCad writes, so only
IPC-2581 and STEP go. None of those other tools runs here, so the steps on
their side are their makers' documented imports, not tested. `pcb-flow eda_io
formats` prints the table.

## What it never does

It never orders or pays. P10 (quote, order, payment) is the person's: stop
after the `dfm` gate with the package in `fab/` (gerber zip, BOM.csv,
CPL.csv, prebuy.csv) and say so. JLCPCB's API keys never reach a sandbox, and
nothing in the Hive should ask for them.

## What doesn't work here yet

- `route_escape`, `route_critical` and KRT's finishing pass need KiCadRoutingTools 0.19.0.
  It's now in `/hive/tools/kicad-routing-tools/KiCadRoutingTools-0.19.0`,
  but nothing points the skill at it yet (brief 261028-141924). A board with
  diff pairs, high-current power or RF needs it.
- DigiKey/Mouser price lookups need API keys and stay off.
- The knowledge records cite manufacturer PDFs that the module doesn't carry
  (see NOTICE). `docs/knowledge-sources.txt` pins them at upstream's commit;
  `bin/pcb-flow` fetches them into `$HOME/.cache/hwde/knowledge-sources` the first
  time `knowledge`, `research`, `check-all` or `pcb-flow sources` runs (37 MB,
  each checked against its git blob sha) and points `HWDE_KNOWLEDGE_SOURCES`
  there. A record `research promote` adds with a new PDF puts that PDF in the
  same cache, so it isn't in the module or upstream's list.
- Knowledge records the Hive adds start at `verified`, which means a
  fresh-context second reader has checked them against the cited pages.
  Coverage still counts only `approved` records, which is the person's
  sign-off, so their slots read `provisional`, not `gap`.

## The first board

[`boards/hwde-led-test/`](../boards/hwde-led-test/) is the first board pcb-flow
made in the Hive: a 2-layer, 13.4 x 10.5 mm board with a 5 V header, a bulk
cap and a green LED with its resistor. It went through every stage up to P9,
and ERC, place, drc_routed, verify and dfm all pass. Read its `log/P*-digest.md`
for what each stage did and `LEARNINGS.md` for what it taught.

## Ingestion notes

What each EE ingestion run found, with every rule marked verified, disputed
or folklore, and its ranked gap list: [dfm-assembly](../reference/ingest/dfm-assembly.md),
[analog-audio](../reference/ingest/analog-audio.md).
`pcb-flow check_paste --pcb <board> [--stencil-mm 0.10]` (stencil area ratio,
exposed-pad paste) came from it; run it in the DFM/assembly review. It is
not in the verify suite yet (task 261031-013753-d3238414d5).

- [reference/ingest/tools-ecosystem.md](../reference/ingest/tools-ecosystem.md):
  open-source EDA and EE tools against pcb-flow. The Hive already has openEMS,
  FasterCap, FastHenry, Palace and ngspice-47 in its IIC-OSIC-TOOLS tree;
  pcb-flow's impedance solver checked against two independent methods; KLC and
  kicad-happy run on lite-main; a ranked gap list.
