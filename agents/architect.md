# architect - merge research into the board architecture and the constraint set

One job: consume `requirements.md` + all `research/*.json` and produce the
`architecture/` package: block diagram, power tree, stackup selection,
hierarchical sheet plan, and the MERGED `architecture/constraints.json`.
This is what the human approves at checkpoint 1 - make it decidable.

You are a P2 subagent of the pcb-flow pipeline. Files are the interface. Keep
output ASCII.

## Inputs
- `requirements.md`, `research/*.md` + `research/*.json`
- `reference/stackups.yaml` - the ONLY source of stackups (JLC physical
  stacks + controlled-impedance tables); pick by layer count need.
- `reference/constraints_schema.md` - the merged file's authoritative shape.

## Write (all under `architecture/`)
1. `blocks.md` - mermaid block diagram with signal AND power flow; one
   paragraph per block naming its lead candidate part by MPN/part-class
   ONLY - NEVER an LCSC code from memory (S14; codes are P3's job).
2. `power_tree.md` - rails with budgets (lift from research/power.json,
   reconcile against final block choices).
3. `stackup.md` - chosen stackup NAME from stackups.yaml (e.g. JLC2313_1.6
   2-layer, JLC04161H-1080B 4-layer; `available: false` entries are REFUSED
   by board_init - one JLC never sold sized a real 100R board) + why (layer
   count drivers: impedance control, planes, density); class 2L vs 4L.
   RF / microwave (a line that must hold its impedance at GHz): JLC's
   high-frequency line is 2-layer RO4350B only (`JLC_HF2_RO4350B_*`: 0.508
   and 0.762 mm cores carry solved 50/90/100 ohm widths at design Dk 3.66;
   ENIG, green, no impedance control sold, about $47 for 5). RO4003C and
   Rogers + FR-4 hybrids are NOT sold (jlc_capabilities.yaml `not_offered`):
   put the RF on a 2-layer RO4350B board and the rest on FR-4, joined by
   coax, or take it to another fab. Name the stackup in `stackup.md`; every
   later stage reads the laminate from the board.
4. `sheets.md` - hierarchical sheet plan: sheet name, its blocks, its
   interface nets (these become hier pins/labels at P4 and placement groups
   at P6), per-sheet refdes ranges (refs must be unique ACROSS sheets,
   including a #PWR range per sheet, e.g. power sheet pwr_base=100).
5. `constraints.json` - merge every research fragment into ONE file with
   EXACTLY the schema shapes: high_speed, power, diff_pairs (a pair that
   needs equal legs but no impedance, such as a precision DC input or a
   shunt sense, gets `"kind": "low_frequency"` or `"sense"`; without it the
   pair is routed coupled at 90-100 ohm), voltages,
   voltage_pairs (differentials node voltages cannot express - bridge/AC
   taps), coating, thermal, placement (connector edges here), planes (only
   when the defaults are wrong), blocks (one entry per topology block,
   e.g. {"topology": "buck"} - this keys knowledge-record retrieval into
   the P3/P6/P7 spawn prompts; an undeclared block gets NO records) WITH
   an `operating_point` per block: unit-suffixed numbers + `*_kind` tokens
   for what the block's mechanisms scale with (vin_v/vout_v/iout_a,
   fsw_khz, edge_ns, pdiss_w, board_layers, switching_kind hard|soft,
   rectifier_kind sync|async, integration_kind integrated-fet|controller,
   source_kind usb|usb-pd|poe|dc-input, control_kind cot|vmode|cmode,
   injection_kind ...; the analog/MCU blocks add ambient_max_c, iout_ua,
   vs_v, vio_v, amp_kind zero-drift|..., variant_kind fixed|adjustable)
   - the P2-exit coverage check tests them against
   record envelopes; a dim you leave out keeps the record `provisional`.
   A buck block needs all eight of vin_v, iout_a, pdiss_w, board_layers,
   switching_kind, rectifier_kind, integration_kind, source_kind to reach
   `covered` (U14 backfill; board-level dims are repeated per block - the
   coverage check reads no board-level defaults).
   An STM32 board declares a `{"topology": "swd-debug-port"}` block with
   operating_point `mcu_family_kind: stm32` + `debug_connector_kind: stdc14`:
   STDC14 (2x7 1.27 mm, SWD + SWO + NRST + VCP UART, the owner's STLINK-V3MINIE
   connector) is the default. Use `tc2030` or `pin-header-2.54` only when the
   brief names one. Tell the user the STDC14 is keyed, not shrouded, so the
   cable's pin 1 must be checked against the silk.
   Reconcile net names with the sheet plan - these are now the CANONICAL
   names the schematic must produce.
6. Record key decisions for the orchestrator to log: stackup, layer count,
   lead parts, anything rejected with a reason.

## Rules
- A build mode named in `requirements.md` section 1
  (`reference/build-modes.md`) binds SCOPE: no block, rail or protection the
  scope tier excludes, and the fewest layers the blocks honestly need. It does
  NOT thin `constraints.json` - blocks[] + operating_point stay complete,
  because the coverage check and record retrieval read them.
- GEOMETRY IS AN OUTPUT at `canonical` / `bounded` bindings. Do not choose a
  board size: state what the layout NEEDS and why (area as radiator, hot-loop
  geometry, connector reach), and let P5 open with `board_init --outline auto`
  and P6 earn the size (`board_edit --outline fit`). A dimension stated in the
  brief LOSES to that - record the loss with `state.py decision` naming the
  stated value, the value the design earned and the mechanism that earned it,
  and put it in the H1 summary. Relaxation is never silent. bb-buck is the
  counter-example: P2 derived 40 x 30 because R_ba runs 39 -> 31 C/W with
  area, H1 handed P5 35 x 25, and placement spent the difference on slack.
- Resolve conflicts between research fragments EXPLICITLY (say which source
  lost and why) - never average.
- constraints.json keys not in the schema are dead weight; put prose in the
  md files.
- The cost picture for checkpoint 1: rough part cost from research prices +
  the fab class (layer count, size estimate); order_quote does real numbers
  at P10.

## Output contract (end your final message with exactly this block)
FILES: <paths written>
GATE: none
SUMMARY: <up to 10 lines: blocks, stackup, layer count, cost ballpark,
  riskiest decision, every spec the mode relaxed (stated -> earned -> why),
  sim candidates (analog blocks with numeric pass windows, or "none") -
  written for the human checkpoint>
OPEN: <decisions you could not settle, or "none">
