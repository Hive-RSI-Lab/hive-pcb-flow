# The real-errata corpus

Public boards whose next revision fixed a defect the first one shipped with,
quoted from the designer's own errata (`reference/errata_corpus.yaml`), and
what pcb-flow's checks do with the first revision. Written by
`bin/pcb-flow errata_corpus table` after `fetch` and `run`.

## digits: v1 -> v2

[https://github.com/justinlindh/digits](https://github.com/justinlindh/digits) at `9807030c8411`, AGPL-3.0; errata `hardware/pcb/v1/ERRATA.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| digits-1: LM2596 switching node Net-(D1-K) routed at 0.25 mm (default class) for 3 A | PI (track width vs current) | caught on rev A, gone on rev B (`check_current` undersized_track) | With no constraints.json, check_current finds U1's OUT pin (an LM2596, reference/switch_nodes.yaml) and screens its net at the part's rated 3 A at dT=60C: 0.25 mm fails (needs 0.72 mm); v2's 0.75 mm passes. |
| digits-2: J1 2x20 Pi header on B.Cu rotated 180 deg, so odd/even columns swap against the Pi below | MECH (mating orientation) | caught on rev A, gone on rev B (`check_mate_pins` mate_mirrored_pinout) | J1 is the stock PinHeader_2x20 (numbered for F.Cu) on B.Cu, so KiCad's flip mirrors it; a mirror survives any rotation, so the 180 deg turn the errata blames is not what swaps the rows. check_mate_pins knows the Pi header by its supply pins (2 +5V, 6 GND) and compares the pattern's handedness, seen from above, with the Pi's face up below. v2 puts J1 on F.Cu for a Pi face down above, which the rule does not judge. |
| digits-3: MIC_GND is a local label, not the GND symbol; the mic return floats | SCH (net connectivity) | caught on rev A, gone on rev B (`netlist_audit` ground_island) |  |
| digits-4: J7 bell terminal on single-pin nets BELL_1/BELL_2, wired to nothing | SCH (net connectivity) | caught on rev A, gone on rev B (`netlist_audit` dangling_net) |  |

## oasis: v0.2 -> mainboard-v0.3

[https://github.com/justbuchanan/oasis](https://github.com/justbuchanan/oasis) at `3462f162f80e`, Apache-2.0; errata `pcb/fabricated-boards/v0.2/errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| oasis-1: Q1/Q3 SI2328DS on Q_NMOS_GDS + stock SOT-23-3, so gate and source are swapped | LIB (symbol pin map vs package) | caught on rev A, gone on rev B (`netlist_audit` sot23_pinout) | v0.3 keeps the GDS symbol and moves to lib:SOT-23-3-pinswapped-mosfet, so the rule is quiet there. |
| oasis-2: Rfbb1 BOM part an 0402 resistor (C844504) on an 0603 footprint | DFA (BOM part vs footprint package) | caught on rev A, gone on rev B (`check_bom_sync` bom_package_mismatch) | check_bom_sync --live reads each LCSC number off the board and JLC's package for it; on a pcb-flow fab package stock_check makes the same comparison against BOM.csv's footprint. v0.3 buys C844759, an 0603. |
| oasis-3: MISTER1 connector placed 180 deg off at JLC assembly | DFA (CPL rotation) | caught on rev A, gone on rev B (`dfm` cpl_rotation) | The board published the CPL it sent JLC (v0.2/positions.csv). dfm's shipped-CPL check fits each part's LCSC model to the board's pads: mister1 (a keyed JST-PH) is at 119 deg where the model needs 299, the 180 the designer had JLC fix. v0.3's CPL puts it on the bottom at a rotation that fits, and the finding is gone. The same run flags more, judged by hand: PS1 (LDD-700LS) and U4 (SHT30) on v0.2 and PS1 on v0.3 are real, 90 deg off (the LCSC model's pin rows run along x, KiCad's along y, and the Fabrication-Toolkit CPL adds no offset; JLC's own placement review most likely fixed them, as the boards work). J2's 1.48 mm offset on v0.3 is real too: the footprint origin sits on the shield pad, not the body. Q1/Q3's no_fit on v0.3 is false: the board's lib:SOT-23-3-pinswapped-mosfet renumbers the pads, so a fit by pad number fails where one by geometry holds (sent to cpl_verify's owner). |
| oasis-4: J1 power-input wire holes too big (SolderWire-2.5sqmm, 3.0 mm drill) | DFA (THT hole vs lead) | caught on rev A, gone on rev B (`check_connector_fit` tht_hole_loose) | J1 is a solder-wire footprint with no part to hold it to, so check_connector_fit sizes the wire from the copper feeding the pads: +18V's only feed is a pour's four 0.5 mm thermal spokes, about 5.4 A at a 20 degC rise (IPC-2221), a 0.75 mm^2 wire with a 1.25 mm conductor, which leaves 1.75 mm in the 3.0 mm hole. v0.3 drops the solder-wire pads for a header, and the finding is gone. |

## minuet: v3 -> v4

[https://github.com/brown-studios/minuet](https://github.com/brown-studios/minuet) at `1e43aa3df01b`, MIT; errata `hardware/minuet/v3/design-and-errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| minuet-1: no 1 uF capacitor FB_BK to GND_BK, which the MCF8316D datasheet asks for | PI (datasheet-required pin capacitor) | caught on rev A, gone on rev B (`netlist_audit` pin_part_missing) | reference/pin_requirements.yaml carries the MCF8316D's Table 10-1 (SLLSFX9A). v3's +3.3V rail has caps, all drawn on other sheets for other parts, so the finding is a warning that none was drawn for FB_BK; v4 draws C20 1u on /fan/ with the IC and the rule is quiet. |
| minuet-2: LMV331IDCK comparator's input common mode (Vcc-0.7 = 2.6 V) below the 3.1 V rain-sensor signal | ANA (input common-mode range) | caught on rev A, gone on rev B (`netlist_audit` input_common_mode) | netlist_audit solves each input's DC level from the resistors to known rails and compares it with reference/opamp_cm.yaml: U2's - input rests at 3.09 V on the 68K/1M divider and + at 3.3 V (J3 open, dry), both above 2.6 V. v4 drops the comparator, so the rule is quiet there. |
| minuet-3: board areas over 50 C at 4 A per motor phase | TH (copper area under a power IC) | caught on rev A, gone on rev B (`check_thermal` thermal_area) | With no constraints.json check_thermal screens the power ICs reference/thermal_parts.yaml knows (#884): v3's MCF8316D (U5) sheds heat into 2-layer copper that gives 58 C/W against the datasheet's 25.7, so its 135 C thermal warning comes at ~4.2 A peak per phase from 25 C ambient (6.4 A on the datasheet's board). v4's 4-layer pours give U4 42 C/W (1.65x, under the 2x bar) and the finding is gone. |
| minuet-4: ESP32-C3 GPIO6 (MTCK) pulled up by the ROM at reset turns the LED strip on before firmware runs | FW-HW (reset-state pull on a load enable) | caught on rev A, gone on rev B (`check_straps` reset_pull_offboard) | check_straps reads the C3's reset_pulls (MTMS/MTDI/MTCK pull-ups) from reset_states.yaml: v3's GPIO6 leaves the board on the EXPANSION header J4.5 with only the chip's pull-up, so the light accessory's strip sees it high from reset. It also warns on GPIO5 (J4.6), bare the same way. v4 (ESP32-C6) puts R13 100 k to 3V3 on GPIO6, a pull the accessory is designed for, and the finding is gone. On a board whose load is on the board itself, load_on_at_reset names the gate or enable instead. |
| minuet-5: an access-point WiFi connect resets the ESP32-C3 at full TX power | FW (radio power) | not_static |  |
| minuet-6: fan motor connector J8 has no A/B/C phase labels on silk | DFA (connector pin labels) | caught on rev A, gone on rev B (`check_silk` pin_labels_missing) | J8's three pins carry /fan/A, /fan/B and /fan/C; check_silk reads a connector whose nets name three motor phases (A/B/C or U/V/W, bare or after PH/OUT/MOTOR) and looks for silk within 5 mm of its pads that names each one. On J8 only its refdes and two capacitors' are there; v4 labels the phases and the finding is gone. Two-pin power inputs are not judged: which pin is + is a polarity mark, not a pin label. |
| minuet-7: IR receiver U1's 3D model raised 17 mm where the built standoff is 16 mm | MECH (3D model height) | missed | a stated mounting height (constraints or a mech block) for raised THT parts (#980) |

## glasgow: revD0 -> revD1

[https://github.com/GlasgowEmbedded/glasgow](https://github.com/GlasgowEmbedded/glasgow) at `70cb38c9b034`, 0BSD; errata `https://github.com/GlasgowEmbedded/glasgow/issues/1184`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| glasgow-1: U24/U25 nRESET left floating | SCH (datasheet pin state) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): TCA6416A/PCA6416A RESET must not be open (TI SCPS194F, NXP PCA6416A Table 3). revD0 leaves both on unconnected-* nets; revD1 ties them up. |
| glasgow-2: U3 TMP112 and U21 DAC43204 answer on the same I2C address | SCH (I2C address map) | caught on rev A, gone on rev B (`netlist_audit` i2c_address_collision) | reference/i2c_addresses.yaml (#1034): TMP112 ADD0 and DAC43204 A0 both on GND put both at 0x48 on /SCL (SBOS473L Table 7-4, SLASEX3A Table 7-16). revD1 moves ADD0 to V+ (0x49). |
| glasgow-3: U25 ADDR strap tied to the wrong power domain | SCH (strap supply domain) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml's ADDR row with rail_pin VDD_P (#1034): the PCA6416A reads ADDR against VCCP (VIH 0.7 x VCCP, TI SCPS194F), so a tie to +3V3 while VDD_P is +5V5 reads low and lands on U24's 0x20. revD1 ties it to VDD_P's rail. |
| glasgow-4: U4 FUSB302 SCL and SDA swapped on the schematic | SCH (bus pin map) | caught on rev A, gone on rev B (`netlist_audit` pin_net_swap) | netlist_audit's pin_net_swap reads U4's SCL pin on /SDA and its SDA pin on /SCL; revD1 swaps them back and the finding is gone. |
| glasgow-5: STM32G031 MCU_nRST held near 1.2 V by an unprogrammed FX2/FPGA | ANA (pull vs pin leakage) | caught on rev A, gone on rev B (`netlist_audit` pull_vs_leakage) | reference/pin_leakage.yaml (#1026): R26 47k and the STM32's own 25-55k pull-up against the unconfigured ECP5's 150 uA pull-down and the FX2's and ECP5's 10 uA leakage solves to 0 V, below the NRST VIL (an error). revD1 gates NRST through a 74LVC1G08 and the old net carries a 10k pull-down that holds the gate input at 0.15 V. |
| glasgow-6: FX2 U8B and U2 can both drive MCU_nRST | SCH (driver contention) | caught on rev A, gone on rev B (`netlist_audit` driver_contention) | reference/pin_leakage.yaml (#1026) marks the STM32G031 PF2-NRST as a reset input; revD0 puts FX2 PD3 and ECP5 PT13B, both bidirectional, on it (a warning). revD1 drives NRST from a 74LVC1G08 alone. |
| glasgow-7: R9 100k pull-up loses to the unconfigured FPGA's 150 uA pull-down, so nALERT sits asserted | ANA (pull vs pin leakage) | caught on rev A, gone on rev B (`netlist_audit` pull_vs_leakage) | reference/pin_leakage.yaml (#1026): R9 100k against the ECP5's 150 uA IPD (FPGA-DS-02012 Table 3.7) and 20 uA of leakage solves to 0 V (an error). revD1's 2.2k holds 2.9 V, above the FX2's 2 V VIH. |
| glasgow-8: INTB/DRDY lines on U6 pins that cannot take an interrupt | FW-HW (pin function) | not_static | every STM32G0 GPIO can take an EXTI line, so no pin table rules a pin out; which pins the firmware wants for interrupts is its own choice (#1032). |
| glasgow-9: ADC AICOM pin left off GND though single-ended mode is referenced to it | SCH (datasheet pin state) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): NAFE71388 AICOM belongs on ground, directly or through its series resistor (section 7.3.2.2.1). revD0 runs it through R34 to /ADC/REMOTE_COM; revD1 takes it to GND. A warning. |
| glasgow-10: BUSY/CSSPIN on RD instead of a FLAG pin | FW-HW (pin function) | caught on rev A, gone on rev B (`netlist_audit` mcu_input_only_drive) | reference/mcu_pins.yaml (#1032): the FX2's RDY0/SLRD is input-only (38-08032 Rev. *K Table 4-1), and revD0 joins flash U9's ~{CS} to it over 0R R40. revD1 moves it to a FLAG pin. A warning. |

## echoplayer: r1-rev1 -> r1-rev1.1

[https://github.com/amachronic/echoplayer](https://github.com/amachronic/echoplayer) at `50f31a141c4c`, CERN-OHL-S-2.0; errata `r1-rev1/docs/errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| echoplayer-1: USB3320C ID pin grounded on a device-only board, so the core starts as a host | SCH (datasheet pin state) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): USB3320 ID grounded makes an A-device (DS00001792F Table 2-1, 5.6.1), a warning as a host board may mean it. rev1.1 ties U8's ID to 3V3, as the datasheet allows. |
| echoplayer-2: HP/LO 3.5 mm jacks wired against the standard headphone pinout | LIB (standard connector pinout) | missed | off-board and standard connector pinouts, with the mate stated (#1027) |
| echoplayer-3: ESD diodes D1-D4 load the SDMMC lines past the bus's capacitance limit | SI (bus capacitive load) | missed | a per-bus capacitive load budget, ESD arrays included (#1031) |

## bedlift: v0.2 -> v1

[https://github.com/j9brown/bed-lift](https://github.com/j9brown/bed-lift) at `6b5053345233`, MIT; errata `hardware/v0.2/design-and-errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| bedlift-1: lift actuator connectors J8/J9 pins 2 and 3 swapped, reversing the hall sensors' supply | SCH (off-board connector pinout) | missed | off-board and standard connector pinouts, with the mate stated (#1027) |
| bedlift-2: DRV8434S nSCS tied to GND, but the part needs a falling edge to start each SPI frame | SCH (datasheet pin state) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): DRV8434S nSCS must be driven, as each frame needs it high between (SLOSE70 7.5.1.2). v0.2 ties U7-U10's nSCS to GND; v1 drives them from the MCU. |
| bedlift-3: DRV8434S STEP not on a hardware-timer pin, so stall detection sees jitter | FW-HW (pin function) | not_static | the DRV8434S also takes steps over SPI, and which timer the firmware gives STEP is its own choice; no pin table rules the wiring out (#1032). |
| bedlift-4: DRV8434S VREF wired straight to DVDD, 5 V where 3.3 V is recommended | SCH (datasheet pin range) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): DRV8434S VREF is limited to 3.3 V and DVDD is 5 V (SLOSE70 6.3, 8.2.2.2), so VREF on the part's own DVDD fires. v1 feeds it through a divider. |
| bedlift-5: LIFT1 IN2 on an MCU pin with no timer channel, so the motor cannot be driven by PWM | FW-HW (pin function) | caught on rev A, gone on rev B (`netlist_audit` mcu_pwm_timer) | reference/mcu_pins.yaml (#1032): with PMODE high both DRV8874s are in PWM mode (SLVSF66A Table 2), and v0.2's four inputs (PB0, PA15, PC6, PA8) fit no single timer of the STM32C031. v1's PA0-PA3 all sit on TIM1 of its STM32C051. A warning. |
| bedlift-6: actuator hall sensors powered at 3.3 V where they are meant for 5 V | SCH (off-board part supply) | missed | off-board and standard connector pinouts, with the mate stated (#1027) |

## pdlc: v1 -> v2

[https://github.com/brown-studios/pdlc](https://github.com/brown-studios/pdlc) at `423481719bae`, MIT; errata `hardware/v1/design-and-errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| pdlc-1: the driver does not wait for power-good before driving the panel | PI (power-good sequencing) | missed | a power-good pin that gates nothing; a switched load against the converter's limit (#1030) |
| pdlc-2: no current limit on the panel drive, so large panels drain the output capacitor | PI (load current limit) | missed | a power-good pin that gates nothing; a switched load against the converter's limit (#1030) |
| pdlc-3: the EN input is too sensitive when left floating | SCH (input pull strength) | missed | cable pickup on a floating off-board input: no datasheet leakage to size against, so #1026's rule leaves it (survey #1079) |

## megabuzz: revA -> revA1

[https://github.com/andykarpov/karabas-megabuzz](https://github.com/andykarpov/karabas-megabuzz) at `b3a026f15f44`, MIT; errata `ERRATA.en.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| megabuzz-1: OPL3 at 5 V drives the FPGA's 3.3 V inputs through dropping resistors only | SCH (I/O supply domain) | missed | driver supply against receiver bank voltage without parts.json (#1029) |

## sizif: rev.C -> rev.C1

[https://github.com/UzixLS/zx-sizif-512](https://github.com/UzixLS/zx-sizif-512) at `d9b1f4d71383`, MIT; errata `pcb/rev.C/ERRATA.txt`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| sizif-1: JTAG header pinout differs from the Altera standard | SCH (standard connector pinout) | missed | off-board and standard connector pinouts, with the mate stated (#1027) |
| sizif-2: J3 header's silkscreen is wrong | DFA (silk labels) | missed | check_silk reading a header's labels against its pins' nets (#1028) |

## ambient: rev_c -> rev_d

[https://github.com/lab11/signpost](https://github.com/lab11/signpost) at `f8f9efe01185`, Apache-2.0; errata `modules/ambient/rev_c/errata.txt`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| ambient-1: no pull-ups on SWDIO and SWDCLK | SCH (datasheet pin state) | not_static | At the pinned commit rev_c's own files already carry the fix: its ambient.sch, kicad_pcb, shipped .net and BOM all have R7 and R8, 100k from /SAM4L_SWDIO and /SAM4L_SWDCLK to +3V3, as rev_d has R1 and R2. The files were patched after the boards were made, so none holds the defect and no check can see it (#1044). The errata's other line, "Random trace on pin32?", is a question with no stated fix, so it is not judged. |

## Count

41 defects: 26 caught, 11 missed, 4 not_static.
