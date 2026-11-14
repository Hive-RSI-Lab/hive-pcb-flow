# What the errata corpus misses, by class

Every defect in `reference/errata_corpus.yaml` that no check catches today,
grouped by the check that would see it, so the next rules go where they
catch the most. Read with [errata-corpus.md](errata-corpus.md).

**Counted (2026-11-14, main at a58d762).** 42 misses:
- training: 7 (3 missed, 4 not_static);
- holdout round 1: 10 (blind it was 11; netscore-1 is caught since #1085);
- holdout round 2: 25.

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
| 1 | Footprint vs the part's package | 4 | 3 | 12 | footprint + BOM (LCSC/MPN, value) | each part's package, pad count and pitch from its LCSC record (EasyEDA footprint) or MPN ordering code; case-size limits for passives (largest C or R a case holds) |
| 2 | Connector mechanics vs the mate | 6 | 2 | 12 | footprint, 3D | lead sizes per connector footprint, the mate's gender and row pitch (a stated mating side), mounting heights of raised THT parts |
| 3 | Off-board inputs: protection, pulls, levels | 6 | 2 | 12 | netlist | pin abs-max (negative too), input leakage, which nets leave the board, which rail is reliable |
| 4 | Board-level DFM | 3 | 3 | 9 | footprint, pcb | none beyond the board: vias inside SMD pads, vias under a part's body, mounting holes |
| 5 | Symbol pin map vs the part | 3 | 3 | 9 | netlist + symbol | per-part pin tables: KiCad's stock symbol, the LCSC record's symbol, module pinouts |
| 6 | Pin function on the wrong net | 3 | 3 | 9 | netlist | pin roles (a pin named GND on a supply net), a protector's cell-side pins, MCU debug pins (mcu_pins.yaml) |
| 7 | Datasheet application circuit | 3 | 2 | 6 | netlist | per-part required externals and reference networks from the typical-application figure; pins that must be connected |
| 8 | Wiring against design intent | 5 | 1 | 5 | netlist + a stated fact | which signal a pin should carry: UART crossover, a level shifter's DIR net, a jumper map, a named bus checked complete |
| 9 | Shared-bus drivers | 3 | 1 | 3 | netlist | driver type per part (push-pull vs open drain), I2C masters per bus, bus strobes |
| 10 | Silk pin labels (two-column legends) | 1 | 2 | 2 | footprint + silk | none; extends #1028 |
| 11 | Load against a source's limit | 1 | 1 | 1 | netlist + a stated load | the off-board load's capacitance |
| - | Not static | 4 | - | - | not static | firmware, radio power, MCU timer and interrupt pin choice |

## Members

1. **Footprint vs the part's package.** On one MPN or LCSC record and
   value, every part can be judged.
   - ice1usb-r01-1: W25Q80DVSNIG (SOIC-8, 150 mil) on the wide 208 mil SO8 footprint.
   - gcm-v50-3: J7 on the wrong footprint.
   - flexgrid-v3-1: 1N5819 and 1N4007 on SOD-123F, but the stocked parts are SOD-123W.
   - flexgrid-v3-2: C9, 100 uF, on 0603. No 100 uF part comes in 0603.

2. **Connector mechanics vs the mate** (afcfa's area, #1027 and #1052).
   - Gender: gcm-v50-4 and gp8b-v50-1.
   - Holes too small, where check_connector_fit says nothing: zxdiag-v10-2 and mdbt-micro-50q-1.
   - Row spacing against the mate: zxdiag-v10-1.
   - Raised-part height: minuet-7, in training.

3. **Off-board inputs.**
   - smart-lcc-2: no clamp on the UART port.
   - fc980c-2: an open-collector input with no pull-up.
   - pdlc-3: a floating EN, in training.
   - zxdiag-v11-2: a 1N4148 clamp lets -0.7 V reach an ADC rated -0.5 V.
   - zxdiag-v11-3: a divider referenced to an off-board rail.
   - identidem-1: no decoupling at a connector feeding a pulsed load.

4. **Board-level DFM.**
   - flexgrid-v4-2: 29 vias in SMD pads.
   - pico-if1-v10-3: a via under U4 that can't be reached.
   - cmm2-1: no mounting holes.

5. **Symbol pin map vs the part.**
   - netscore-big-1: a 0.8 in digit drawn with the 0.2 in digit's symbol.
   - gcm-v50-2: a logic IC symbol with its pins flipped.
   - pico-if1-v13-1: OLED module's SDA and SCK in the wrong order.

6. **Pin function on the wrong net.**
   - flexgrid-v4-1: KiCad's own USBLC6-2SC6 symbol with its GND pin on VBUS.
   - netscore-2: FS312MH GND and BAT- swapped.
   - fc980c-1: SWDIO on PA15, not PA13.

7. **Datasheet application circuit.**
   - healthmonitor-1: AD8232's IOUT to SW capacitor missing.
   - healthmonitor-2: the REFIN network.
   - pico-if1-v13-2: SD card-detect unconnected.

8. **Wiring against design intent.** smart-lcc-1, ice1usb-r01-2,
   pico-if1-v10-2, gp8b-v50-2 and zxdiag-v11-1.

9. **Shared-bus drivers.**
   - byte-hamr-1: push-pull onto wired-OR lines.
   - ice1usb-r10-1: a GPS module that is an I2C master shares the port.
   - gcm-v50-1: two drivers on one strobe.

10. **Silk pin labels.** lofence-1.

11. **Load against a source's limit.** pdlc-2, in training.

**Not static.** minuet-5, glasgow-8, bedlift-3 and ambient-1, in training.

## What it says

Most misses are classes the flow has no check for at all, not checks that
fall short.
- Five of the top six rows judge every part, every connector or every
  board.
- Rows 8, 9 and 11 need a fact from the designer, so they rank low however
  often they occur.

When a class lands, score it on training plus both holdout rounds as **not
independent**. The next blind round (see "A new holdout round" in
errata-corpus.md) comes once two or three classes have landed.
