# Holdout round 3 (sealed, scorers only)

Rule builders: stop here. This file names the round's defects, and
a rule written after reading it is no longer tested by the round.
Written by `bin/pcb-flow errata_corpus table` from
`holdout/round-3.yaml`.

### scanlight-driver: bsl_driver v1_a -> bsl_driver v1_b

[https://github.com/jackw01/scanlight](https://github.com/jackw01/scanlight) at `889b7e06159a`, CERN-OHL-W-2.0; errata `pcb/bsl_driver_v1_a/errata.md`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| scanlight-driver-1: J101 pin 9 is on net TH where it should be TH+ | SCH (wrong net) | caught | caught (not run here) (`netlist_audit` dangling_net) | Caught blind with the flow at ba7c056: netlist_audit sees /TH with one pin (J101.9) on v1_a; v1_b has no such net. |

### thermometer-hw: v1.0 -> v2.0

[https://github.com/sullin/thermometer-hw](https://github.com/sullin/thermometer-hw) at `609fc2459e17`, GPL-3.0; errata `v1.0/ERRATA.txt`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| thermometer-hw-1: R9 must be left off, it stops the module booting | SCH (datasheet pin state) | missed | missed | a module's boot straps against what the board hangs on them (R9 on an ESP8266 strap pin; reset_states.yaml has no ESP-12 row) |
| thermometer-hw-2: 1-wire bus on D8, a boot strap that cannot take a pull-up; move it to D0 | SCH (pin function vs datasheet) | missed | missed | a bus pull-up on a boot strap that must idle low (1-wire on ESP8266 GPIO15/D8) |
| thermometer-hw-3: 1-wire sensors powered from 5 V while the bus runs at 3.3 V | SCH (off-board part supply) | missed | missed | a sensor's supply against the logic level of the bus it drives (5 V DS18B20s on a 3.3 V bus) |

### kharon386-main: main rev.A -> main rev.A1

[https://github.com/UzixLS/pc-kharon-386](https://github.com/UzixLS/pc-kharon-386) at `83f08c6beea1`, CC-BY-SA-4.0; errata `pcb_main/rev.A/ERRATA.txt`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| kharon386-1: BIOS PLCC socket collides with the CF card slot | MECH (part collision) | missed | missed | courtyard overlap between a socketed part's full body and a card slot (PLCC socket against CF slot) |
| kharon386-2: U15 is the wrong part; it should be a TXS0104PW | LIB (wrong part) | missed | missed | a level-shifter part number against the direction and voltage of the lines it sits on (U15 should be TXS0104) |
| kharon386-3: COM3 and COM4 IRQs routed to the wrong lines | SCH (wiring against design intent) | missed | missed | ISA IRQ lines against the port each UART is assigned (COM3/COM4) |
| kharon386-4: ESP8266 CTS and RTS connected the wrong way round | SCH (swapped signals) | missed | missed | UART flow-control pairs crossed between DTE and DCE (ESP8266 CTS/RTS) |

### karabas-pro: revA -> revB

[https://github.com/andykarpov/karabas-pro](https://github.com/andykarpov/karabas-pro) at `bcbf68d5f493`, MIT; errata `ERRATA-REVA.md`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| karabas-pro-1: no 10k pull-ups on the FDC inputs RDATA, WPRT, TR00, INTRQ, DRQ | SCH (missing pull-up) | missed | missed | the pulls a floppy controller's open-collector inputs need from its datasheet (RDATA, WPRT, TR00, INTRQ, DRQ) |
| karabas-pro-2: ESP-12 module has no keep-out under its antenna | PCB (antenna keep-out) | missed | missed | copper and parts inside a module's antenna keep-out (ESP-12) |
| karabas-pro-3: CF card footprint holes do not match the connector | LIB (footprint vs part) | missed | missed | a connector footprint's holes against its maker's drawing (CF card socket) |

### rosco-m68k: r0 (KiCad import) -> r1

[https://github.com/rosco-m68k/rosco_m68k](https://github.com/rosco-m68k/rosco_m68k) at `ec229f9e64b4`, CERN-OHL-1.2; errata `design/r0/r0-errata.md`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| rosco-m68k-1: MC68901 MFP address lines wired in reverse order | SCH (bus pin map) | missed | missed | bus-indexed nets on a peripheral against its address pins (MC68901 register-select lines in reverse order) |
| rosco-m68k-2: top silkscreen labels expansion pin 2 as pin 64 | SILK (pin label vs net) | missed | missed | silk pin-number labels against the pad they sit next to (expansion pin 2 labelled 64) |

### libcm-motherboard: Motherboard RevB -> Motherboard RevC

[https://github.com/doppelhub/Honda_Insight_LiBCM](https://github.com/doppelhub/Honda_Insight_LiBCM) at `16e12d8ae7f1`, CC-BY-SA-4.0; errata `Electronics/PCB (KiCAD)/Motherboard/RevB/CARs/CAR028 - Errata.txt`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| libcm-1: D5 diode silkscreen drawn backwards | SILK (polarity mark) | missed | missed | a diode's silk polarity mark against its cathode pad (D5) |

### switch-v5: 5.0.0 -> 5.1.0

[https://github.com/ben5049/switch-v5-hardware](https://github.com/ben5049/switch-v5-hardware) at `05c302550e90`, AGPL-3.0; errata `docs/errata.md`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| switch-v5-1: SJA1105Q switch 1 ports 3 and 4 I/O supply on 3.3 V where the link needs 1.8 V | SCH (I/O supply domain) | missed | missed | a switch port's I/O supply against the interface standard it is strapped for (SJA1105Q ports 3/4 need 1.8 V) |

### spitfire: Rev 1 -> Rev 2

[https://github.com/rob-smallshire/spitfire](https://github.com/rob-smallshire/spitfire) at `5ba22d2acb77`, GPL-3.0; errata `docs/spitfire-rev-1-errata.md`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| spitfire-1: custom male DE-9 footprint for J13 is mirrored | LIB (footprint vs part) | missed | missed | a male D-sub footprint's pin order against the mating face (J13 mirrored) |
| spitfire-2: SPI MISO has no pull-up, so it floats with no device selected | SCH (missing pull-up) | missed | missed | a tri-state shared line with no pull (SPI MISO with no device selected) |
| spitfire-3: decoder Y7, the resting all-ones state, selects expansion header J9 instead of nothing | SCH (wiring against design intent) | missed | missed | a decoder's idle output against what it selects (74HC138 Y7, the all-ones state, enables J9) |
| spitfire-4: crystal load capacitors C2 and C6 on 0201 footprints with no value | LIB (footprint vs part) | missed | missed | a crystal's load capacitors with no value and an implausible case size (C2/C6 0201) |

### k30-sbc: Rev 1 -> Rev 2

[https://github.com/transistorfet/computie](https://github.com/transistorfet/computie) at `2dd38015045b`, GPL-3.0; errata `hardware/k30-SBC/revisions/k30-SBC-rev1-errata.txt`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| k30-sbc-1: 74HC253s on a 4.9 mm SSOP-16 footprint; the parts are 6.2 mm wide | LIB (footprint vs part) | missed | missed | an SSOP footprint's body width against the part's (74HC253 4.9 mm against 6.2 mm) |
| k30-sbc-2: ATA DSACK output not inverted into an active-low NAND input | SCH (signal polarity) | missed | missed | an active-low output into a gate input without the inversion the logic needs (ATA DSACK) |
| k30-sbc-3: spare DTACK input on the NAND gate left floating | SCH (floating input) | missed | missed | an unused logic gate input left floating (NAND spare DTACK input) |

### z280-sbc: Rev 1 -> Rev 1 fixed

[https://github.com/transistorfet/z280](https://github.com/transistorfet/z280) at `483d8537dfc0`, GPL-3.0; errata `hardware/Z280-SBC/revisions/Z280-SBC-rev1-errata.txt`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| z280-sbc-1: power connectors upside down (ground on top) and unlabelled | PCB (connector vs mate) | missed | missed | power connector pin order and markings against the convention (ground on top, no labels) |
| z280-sbc-2: flash chip footprint rotated, pin 1 in the wrong corner | LIB (footprint vs part) | missed | missed | a footprint's pin 1 against the part's pin-1 corner (flash rotated) |
| z280-sbc-3: 74HC573 LE driven by /AS directly where it needs the inverse | SCH (signal polarity) | missed | missed | a latch enable's polarity against the strobe that drives it (74HC573 LE on /AS) |
| z280-sbc-4: CTIN1 and CTIN2 on CPLD input-only pins where the CPLD must drive them | SCH (pin function vs datasheet) | missed | missed | CPLD input-only pins carrying nets the CPLD must drive (CTIN1/CTIN2) |

### relaymux: first layout -> smaller LED holes

[https://github.com/PetteriAimonen/relaymux](https://github.com/PetteriAimonen/relaymux) at `77163be0b735`, BSD-2-Clause; errata `pcb/errata.txt`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| relaymux-1: reverse-mount LED footprint hole too large | LIB (footprint vs part) | missed | missed | a reverse-mount LED's hole size against the LED body |

### mmm-v1-alpha: V1 Alpha -> V1 Alpha fixed

[https://github.com/techy-robot/Mighty-Micro-Motors](https://github.com/techy-robot/Mighty-Micro-Motors) at `f2471eab10ce`, CERN-OHL-S-2.0; errata `Design Errata.md`. Holdout round 3, swept blind with pcb-flow `ba7c0560553c` on 2026-11-15.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| mmm-1: MOSFETs bought for the footprint are far smaller than it (PowerPAK size mix-up) | LIB (footprint vs part) | missed | missed | a MOSFET's package size against its footprint (PowerPAK size mix-up) |
| mmm-2: LED circuit designed for a tri-state output, but the driver's status pin is not tri-state | SCH (pin function vs datasheet) | missed | missed | an LED driven from a status pin whose output type does not suit it (the LED circuit assumes a tri-state output; the status pin is not) |
