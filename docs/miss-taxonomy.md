# What the errata corpus misses, by class

Every defect in `reference/errata_corpus.yaml` that no check catches today,
grouped by the check that would see it, so the next rules go where they
catch the most. Read with [errata-corpus.md](errata-corpus.md).

**Counted (2026-11-14, main at 0e14f1e plus #1134).** 35 misses:
- training: 6 (2 missed, 4 not_static; flexgrid-v3-2 is caught since #1129);
- holdout round 1: 7 (netscore-1 is caught since #1085, netscore-big-1 since #1134);
- holdout round 2: 22 (gcm-v50-2 and pico-if1-v13-1 are caught since #1134).

The count before #1134, on the same main, was 38 (6, 8 and 24).

**Since then (#1139, rule 28, class 3).** smart-lcc, fc980c (holdout round
1) and zxdiag-v11 (round 2) moved to training, since rule 28 was seeded from
their defects; smart-lcc-2, fc980c-2, zxdiag-v11-2 and zxdiag-v11-3 are
caught. That leaves 31 misses: training 9 (5 missed, 4 not_static;
smart-lcc-1, fc980c-1 and zxdiag-v11-1 came with their boards), holdout
round 1: 3, holdout round 2: 19.

**Since then (#1142, class 4).** flexgrid-v4-2 (holdout round 2) is
caught by check_wicking's via-in-pad rule, now in the corpus runners
(#1088): 30 misses, training 9, holdout round 1: 3, holdout round 2: 18.

**Since then (#1155, rules 29-31, class 6).** netscore-main (holdout round
1) and flexgrid-v4 (round 2) moved to training, since rules 29 and 30 were
seeded from their defects; fc980c-1, netscore-2 and flexgrid-v4-1 are
caught. That leaves 27 misses: training 8 (4 missed, 4 not_static;
flexgrid-v4-2 came with its board, already caught), holdout round 1: 2,
holdout round 2: 17.

**Since then (holdout round 3, swept blind with main at ba7c056,
2026-11-15).** 12 new boards, 29 defects, 1 caught blind (scanlight-driver-1,
netlist_audit dangling_net). The other 28 are added below: 55 misses,
training 8, holdout round 1: 2, round 2: 17, round 3: 28. Round 3 adds four
classes (12-15) and re-ranks the table. No rule has been written from round
3 yet, so all of its 28 are still independent tests. With #1158, which
landed after the sweep, healthmonitor-1 and -2 (round 2) are caught: 53
misses, round 2 down to 15.

## How classes are cut and ranked

A class is **one check**: misses that a single rule with one data source
would see. That is why "pin map" is split into two rows below. The symbol's
own numbering against the part, and a correctly drawn pin wired to the
wrong kind of net, need different data and different code.

**Generality** says how much of a board the check judges:
- 3: every part, or every board;
- 2: every connector or off-board line;
- 1: only with a fact the designer states, or one narrow circuit.

**Rank** is count × generality. A tie goes to the check that judges more
parts on an ordinary board.

**Seen by** names the files the check reads:
- netlist;
- footprint (pads, holes);
- CPL;
- 3D;
- not static: a fact no design file holds.

## Ranked

| # | Class | Count | Gen. | Rank | Seen by | Data it needs |
|---|---|---|---|---|---|---|
| 1 | Footprint vs the part's package | 7 | 3 | 21 | footprint + BOM (LCSC/MPN, value) | each part's package, pad count and pitch from its LCSC record (EasyEDA footprint) or MPN ordering code; body widths of SSOP/SOIC/PowerPAK variants; case-size limits for passives (largest C or R a case holds) |
| 4 | Board-level DFM | 5 | 3 | 15 | footprint, pcb, 3D | none beyond the board: vias inside SMD pads, vias under a part's body, mounting holes; plus body/courtyard collisions with socketed parts and module antenna keep-outs |
| 2 | Connector mechanics vs the mate | 7 | 2 | 14 | footprint, 3D | lead sizes per connector footprint, the mate's gender and row pitch (a stated mating side), mounting heights of raised THT parts, power-connector pin order |
| 12 | Footprint geometry vs the maker's drawing | 4 | 3 | 12 | footprint | the maker's land pattern: hole sizes, pin-1 corner, pin order seen from the mating face |
| 14 | Active-low polarity across a connection | 3 | 3 | 9 | netlist | the active level of each pin (from its name: ~, /, N suffix; from the symbol) and of each net it is wired to |
| 8 | Wiring against design intent | 8 | 1 | 8 | netlist + a stated fact | which signal a pin should carry: UART crossover, a level shifter's DIR net, a jumper map, a named bus checked complete, ISA IRQ per port |
| 15 | I/O supply domain vs the bus level | 2 | 3 | 6 | netlist | each IC's I/O supply pins and the level each interface needs; which rail powers the parts on a bus |
| 13 | Boot straps on modules | 2 | 3 | 6 | netlist | reference/reset_states.yaml rows for modules (ESP-12/ESP8266); check_straps exists, the rows don't |
| 7 | Datasheet application circuit (part landed, #1158) | 3 | 2 | 6 | netlist | per-part required externals and reference networks from the typical-application figure; pins that must be connected (unused CMOS inputs, open-collector inputs' pulls) |
| 10 | Silk pin labels and polarity marks | 3 | 2 | 6 | footprint + silk | none; extends #1028 to pin-number legends and diode cathode marks |
| 9 | Shared-bus drivers | 5 | 1 | 5 | netlist | driver type per part (push-pull, open drain, tri-state), I2C masters per bus, bus strobes, a pull on a tri-state line |
| 5 | Symbol pin map vs the part (landed, #1134) | 1 | 3 | 3 | netlist + symbol | per-part pin tables: KiCad's stock symbol, the LCSC record's symbol, module pinouts |
| 6 | Pin function on the wrong net (landed, #1155) | 1 | 3 | 3 | netlist | pin names of the stock symbols, protector cell/load pin names, reference/mcu_pins.yaml debug pins; input-only pins of CPLDs |
| 11 | Load against a source's limit | 1 | 1 | 1 | netlist + a stated load | the off-board load's capacitance |
| 3 | Off-board inputs (landed, #1139) | 0 | 2 | 0 | netlist | reference/pin_abs_max.yaml (abs-max, negative too, cited; part_rows drafts it): netlist_audit rules 23 and 28; identidem-1 by check_pinouts pulsed_load_no_bulk (#1090) |
| - | Not static | 4 | - | - | not static | firmware, radio power, MCU timer and interrupt pin choice |

Ties go to the check that judges more parts: 15 (every IC) before 13
(MCUs and modules) before 7 (parts with a row) before 10 (connectors and
diodes); 5 before 6.

## Members

1. **Footprint vs the part's package.** On one MPN or LCSC record and
   value, every part can be judged.
   - ice1usb-r01-1: W25Q80DVSNIG (SOIC-8, 150 mil) on the wide 208 mil SO8 footprint.
   - gcm-v50-3: J7 on the wrong footprint.
   - flexgrid-v3-1: 1N5819 and 1N4007 on SOD-123F, but the stocked parts are SOD-123W.
   - flexgrid-v3-2: C9, 100 uF, on 0603. No 100 uF part comes in 0603.

   `check_fp_package` (#1129) is this class's check. Scored on training and
   both holdout rounds, so it is not independent:
   - It catches flexgrid-v3-2 on V3 and not on V4. It sees C9 through its
     JLC number, a 2.2 uF 0402 part.
   - It doesn't catch flexgrid-v3-1. The diodes name a part only in their
     Value, and JLC sells parts named 1N5819 or 1N4007 in DO-41, SMA and
     SOD-123 packages, so the check won't pick one.
   - ice1usb-r01-1 and gcm-v50-3 give no part number on rev A, so the check
     has nothing to judge.
   - Not built yet: a case-size limit per value (the largest C a case holds).

   Round 3 (blind, all missed):
   - k30-sbc-1: 74HC253s on a 4.9 mm SSOP-16 footprint; the parts are 6.2 mm wide.
   - mmm-1: MOSFETs bought for the footprint are far smaller than it (a PowerPAK size mix-up).
   - spitfire-4: crystal load capacitors C2 and C6 on 0201 with no value.
   check_fp_package judged k30-sbc and z280-sbc's 74HC parts (SOIC against
   TSSOP, from JLC's part named by Value), but not the 74HC253s or the defect
   parts, and those findings stay on rev B.

2. **Connector mechanics vs the mate** (afcfa's area, #1027 and #1052).
   - Gender: gcm-v50-4 and gp8b-v50-1. Caught with a stated mate since #1124 (not independent).
   - Holes too small, where check_connector_fit says nothing: zxdiag-v10-2 and mdbt-micro-50q-1. Caught since #1123 (not independent).
   - Row spacing against the mate: zxdiag-v10-1. Caught with a stated mate since #1124 (not independent).
   - Raised-part height: minuet-7, in training.
   - Power connector orientation: z280-sbc-1 (ground on top, no labels), round 3.

3. **Off-board inputs.** Rule 28 (offboard_input_*) judges every IC pin
   on a connector net, or one resistor from one, against its row in
   reference/pin_abs_max.yaml (abs-max, negative included, drafted cited
   by part_rows.py), the level a resistor solve gives with each off-board
   rail present or absent, and the clamps that hold it. Caught, all by
   training rules (the boards moved to training):
   - smart-lcc-2: no clamp on the UART port (offboard_input_unprotected).
   - fc980c-2: an open-collector input with no pull-up (offboard_input_floating).
   - pdlc-3: a floating EN (rule 23, offboard_input_weak_pull).
   - zxdiag-v11-2: a 1N4148 clamp lets -0.7 V reach an ADC rated -0.5 V (offboard_input_clamp).
   - zxdiag-v11-3: a divider referenced to an off-board rail (offboard_input_rail_absent).
   - identidem-1: no decoupling at a connector feeding a pulsed load
     (check_pinouts pulsed_load_no_bulk, #1090; a power check, not an
     input one).

4. **Board-level DFM.**
   - flexgrid-v4-2: 29 vias in SMD pads.
   - pico-if1-v10-3: a via under U4 that can't be reached.
   - cmm2-1: no mounting holes.
   - kharon386-1: the BIOS PLCC socket collides with the CF slot (round 3).
   - karabas-pro-2: no keep-out under the ESP-12 antenna (round 3).

5. **Symbol pin map vs the part.** All three are caught since #1134, scored
   on holdouts already read, so none is independent:
   - netscore-big-1: a 0.8 in digit drawn with the 0.2 in digit's symbol.
     `symbol_value_mismatch` reads the size in the symbol's name against the
     Value.
   - gcm-v50-2: a logic IC symbol with its pins flipped. `bus_order_flipped`
     sees one SN74LV393A unit count onto the clock nets the other way round
     from its twin.
   - pico-if1-v13-1: OLED module's SDA and SCK in the wrong order.
     `mcu_i2c_role` sees GPIO4 (I2C0 SDA) on OLED_SCK.

   The general check, `symbol_pinmap`, compares each symbol's pin numbers
   and names with KiCad's stock symbol of the same part. It judged 135 parts
   on 26 boards with no hit, so it has no real catch yet. The datasheet pin
   tables in part_rows and module pinouts are not read yet.

   Round 3 miss: rosco-m68k-1, MC68901 MFP address lines wired in reverse
   order. symbol_pinmap flagged a different part on that board (IC6, an
   Eagle-import 74174), not the MFP.

6. **Pin function on the wrong net.** All three are caught since #1155,
   by rules written knowing them, so none is independent:
   - flexgrid-v4-1: KiCad's own USBLC6-2SC6 symbol with its GND pin on
     VBUS. Rule 29 `pin_role_net` reads each pin's role from its name (GND,
     VCC/VBUS, VBAT) and flags a ground pin on a supply net or the reverse.
   - netscore-2: FS312MH GND and BAT- swapped. Rule 30
     `protector_cell_side` finds the cell's own terminal and checks the
     protector's cell-side pin is on it, not the load-side one.
   - fc980c-1: SWDIO on PA15, not PA13. Rule 31 `debug_pin_wrong` checks a
     net named SWDIO/SWCLK lands on the MCU's own debug pin from
     reference/mcu_pins.yaml (ST open pin data); `debug_pins_reused` warns
     when those pins, or a BOOT0 they share, carry other signals.

   Over 83 board revisions they judged 761 parts, 17 protectors and 8 MCUs,
   with 3 hits, all three of these; every fixed revision is quiet.

   Round 3 miss: z280-sbc-4, CTIN1 and CTIN2 on CPLD input-only pins where
   the CPLD must drive them. No row says which CPLD pins are input-only.

7. **Datasheet application circuit.** healthmonitor-1 and -2 are caught
   since #1158 (rows in `reference/pin_requirements.yaml` from the AD8232
   datasheet), scored on a holdout already read, so neither is independent:
   - healthmonitor-1: AD8232's IOUT to SW capacitor missing; SW is tied
     straight to IAOUT, which the row's `distinct` check reports.
   - healthmonitor-2: the REFIN network; only the upper 10 M is drawn, so
     the lower resistor is an error and the filter capacitor a warning.
   - pico-if1-v13-2: SD card-detect unconnected. Still missed: a socket's
     switch pin is a connector row, not a part's application circuit.

   The same rows cover bootstrap capacitors (TPS563201, TPS561201,
   AP64350), the TPS7A20 output capacitor and TLV62569's EN, which must not
   float. Not covered yet: compensation networks, feedback dividers and
   soft-start capacitors (their values come from equations, not a fixed
   part), AMS1117 (no text layer in the PDF), and AP63203, LM5017,
   TPS61023 and RT9080 (no datasheet on file).

   Round 3 (blind, before #1158):
   - karabas-pro-1: no 10k pull-ups on the floppy controller inputs.
   - k30-sbc-3: a spare NAND input (DTACK) left floating.

8. **Wiring against design intent.** smart-lcc-1, ice1usb-r01-2,
   pico-if1-v10-2, gp8b-v50-2 and zxdiag-v11-1.
   Round 3: kharon386-2 (U15 the wrong part; a TXS0104 shifter needed),
   kharon386-3 (COM3/COM4 IRQs on the wrong lines) and kharon386-4 (ESP8266
   CTS/RTS crossed).

9. **Shared-bus drivers.**
   - byte-hamr-1: push-pull onto wired-OR lines.
   - ice1usb-r10-1: a GPS module that is an I2C master shares the port.
   - gcm-v50-1: two drivers on one strobe.
   - spitfire-2: SPI MISO with no pull-up floats when nothing is selected (round 3).
   - mmm-2: an LED circuit designed for a tri-state output on a status pin that is not tri-state (round 3).

10. **Silk pin labels and polarity marks.** lofence-1; round 3:
    rosco-m68k-2 (expansion pin 2 labelled 64) and libcm-1 (D5's silk
    drawn backwards).

11. **Load against a source's limit.** pdlc-2, in training.

12. **Footprint geometry vs the maker's drawing** (new, round 3). The
    footprint matches the package name but not the part:
    - karabas-pro-3: CF card socket holes don't match the connector.
    - spitfire-1: the custom male DE-9 footprint (J13) is mirrored.
    - z280-sbc-2: the flash footprint is rotated, with pin 1 in the wrong corner.
    - relaymux-1: the reverse-mount LED's hole is too large.

13. **Boot straps on modules** (new, round 3). check_straps judges MCUs
    with a reset_states.yaml row; the ESP8266 module has none. On a board
    with a module and no row, the strap check reports mcu_unknown and stops.
    - thermometer-hw-1: R9 stops the module booting.
    - thermometer-hw-2: the 1-wire bus with its pull-up on D8 (GPIO15), a strap that must idle low.

14. **Active-low polarity across a connection** (new, round 3). One
    active-low signal goes into a pin that needs the opposite level, or the
    idle state of a decoder selects something:
    - k30-sbc-2: the ATA DSACK output is not inverted into an active-low NAND input.
    - z280-sbc-3: 74HC573 LE driven by /AS directly, where it needs the inverse.
    - spitfire-3: decoder Y7, the resting all-ones state, selects J9.

15. **I/O supply domain vs the bus level** (new, round 3):
    - switch-v5-1: SJA1105Q ports 3 and 4 have their I/O supply on 3.3 V, where the link needs 1.8 V.
    - thermometer-hw-3: 1-wire sensors powered from 5 V on a 3.3 V bus.

**Not static.** minuet-5, glasgow-8, bedlift-3 and ambient-1, in training.

## What it says

Most misses are classes the flow has no check for at all, not checks that
fall short.
- Round 3 was swept blind before any of its rules: 1 of 29, against 1 of 4
  and 1 of 21 in the first two blind rounds. The classes landed since round
  2 (5 and 6) each missed one defect of their own kind in round 3.
- Footprint against the part (rows 1 and 12, 11 misses) is now the largest
  group, and both rows judge every part.
- Rows 8, 9 and 11 need a fact from the designer, so they rank low however
  often they occur.

When a class lands, score it on training plus all three holdout rounds as
**not independent**. The next blind round (see "A new holdout round" in
errata-corpus.md) comes once two or three classes have landed.
