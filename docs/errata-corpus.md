# The real-errata corpus

Public boards whose next revision fixed a defect the first one shipped with,
quoted from the designer's own errata (`reference/errata_corpus.yaml`), and
what pcb-flow's checks do with the first revision. Written by
`bin/pcb-flow errata_corpus table` after `fetch`, `run` and `sweep`.

Two sets. **Training**: most of its catches were written from the defect
they catch, so its count says how well the checks fit what they were built
from, not how they do on a new board. **Holdout**: boards no rule was written
from, swept with every check before their findings were read against the
errata; the *blind* column is that first judgement and never changes. It is
the number that says how the flow does on a board it has never seen, and
only for the round's first reading: each round is swept once.

## Count

- Training: 38 of 44 caught (38 caught, 2 missed, 4 not_static).
- Holdout round 1, blind: 1 of 10 caught (1 caught, 9 missed).
- Holdout round 1, with the rules written since: 3 of 10 caught (3 caught, 7 missed).
- Holdout round 2, blind: 1 of 26 caught (1 caught, 25 missed).
- Holdout round 2, with the rules written since: 4 of 26 caught (4 caught, 22 missed).

## A new holdout round

A holdout board stays blind only until its findings are read against its
errata; after that its defects are known, and a rule written later is no
longer tested by it. So each round of rules is scored on a fresh holdout
round, pinned and swept before those rules are:

1. Before the round's rules are scored, an agent who has not read the
   boards picks about 20 errata from boards nobody here has read, chosen as
   the earlier rounds were: a public KiCad repository with a licence
   (`fetch` upgrades a pre-KiCad-6 board with kicad-cli), where a later
   revision fixes a defect the designer's own errata quote, both revisions
   pinned to a commit, and no repository an earlier round or the training
   set holds.
2. Each goes under `holdout:` with `swept: {flow: SHA, date, round: N}`,
   SHA being main as it stands, and its defects with the designer's quote,
   before anyone reads the findings. `errata_corpus.py sweep ID...` runs
   every check on both revisions.
3. Each defect's `blind` is then judged from that sweep alone and never
   changes. Rules written after it may still catch it (`verdict`, with the
   rule in `caught_by`); a rule seeded from a holdout defect moves its board
   to `boards:`.
4. Report three numbers: training, the earlier rounds with the rules
   written since (known boards, not an independent test), and round N
   blind. Round N is known once read: the next round of rules gets round
   N+1.
5. Add the round's misses to `docs/miss-taxonomy.md`, one class per check,
   and re-rank it: the next rules go to the top classes.

## Training set

### digits: v1 -> v2

[https://github.com/justinlindh/digits](https://github.com/justinlindh/digits) at `9807030c8411`, AGPL-3.0; errata `hardware/pcb/v1/ERRATA.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| digits-1: LM2596 switching node Net-(D1-K) routed at 0.25 mm (default class) for 3 A | PI (track width vs current) | caught on rev A, gone on rev B (`check_current` undersized_track) | With no constraints.json, check_current finds U1's OUT pin (an LM2596, reference/switch_nodes.yaml) and screens its net at the part's rated 3 A at dT=60C: 0.25 mm fails (needs 0.72 mm); v2's 0.75 mm passes. |
| digits-2: J1 2x20 Pi header on B.Cu rotated 180 deg, so odd/even columns swap against the Pi below | MECH (mating orientation) | caught on rev A, gone on rev B (`check_mate_pins` mate_mirrored_pinout) | J1 is the stock PinHeader_2x20 (numbered for F.Cu) on B.Cu, so KiCad's flip mirrors it; a mirror survives any rotation, so the 180 deg turn the errata blames is not what swaps the rows. check_mate_pins knows the Pi header by its supply pins (2 +5V, 6 GND) and compares the pattern's handedness, seen from above, with the Pi's face up below. v2 puts J1 on F.Cu for a Pi face down above, which the rule does not judge. |
| digits-3: MIC_GND is a local label, not the GND symbol; the mic return floats | SCH (net connectivity) | caught on rev A, gone on rev B (`netlist_audit` ground_island) |  |
| digits-4: J7 bell terminal on single-pin nets BELL_1/BELL_2, wired to nothing | SCH (net connectivity) | caught on rev A, gone on rev B (`netlist_audit` dangling_net) |  |

### oasis: v0.2 -> mainboard-v0.3

[https://github.com/justbuchanan/oasis](https://github.com/justbuchanan/oasis) at `3462f162f80e`, Apache-2.0; errata `pcb/fabricated-boards/v0.2/errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| oasis-1: Q1/Q3 SI2328DS on Q_NMOS_GDS + stock SOT-23-3, so gate and source are swapped | LIB (symbol pin map vs package) | caught on rev A, gone on rev B (`netlist_audit` sot23_pinout) | v0.3 keeps the GDS symbol and moves to lib:SOT-23-3-pinswapped-mosfet, so the rule is quiet there. |
| oasis-2: Rfbb1 BOM part an 0402 resistor (C844504) on an 0603 footprint | DFA (BOM part vs footprint package) | caught on rev A, gone on rev B (`check_bom_sync` bom_package_mismatch) | check_bom_sync --live reads each LCSC number off the board and JLC's package for it; on a pcb-flow fab package stock_check makes the same comparison against BOM.csv's footprint. v0.3 buys C844759, an 0603. |
| oasis-3: MISTER1 connector placed 180 deg off at JLC assembly | DFA (CPL rotation) | caught on rev A, gone on rev B (`dfm` cpl_rotation) | The board published the CPL it sent JLC (v0.2/positions.csv). dfm's shipped-CPL check fits each part's LCSC model to the board's pads: mister1 (a keyed JST-PH) is at 119 deg where the model needs 299, the 180 the designer had JLC fix. v0.3's CPL puts it on the bottom at a rotation that fits, and the finding is gone. The same run flags more, judged by hand: PS1 (LDD-700LS) and U4 (SHT30) on v0.2 and PS1 on v0.3 are real, 90 deg off (the LCSC model's pin rows run along x, KiCad's along y, and the Fabrication-Toolkit CPL adds no offset; JLC's own placement review most likely fixed them, as the boards work). J2's 1.48 mm offset on v0.3 is real too: the footprint origin sits on the shield pad, not the body. Q1/Q3's no_fit on v0.3 is false: the board's lib:SOT-23-3-pinswapped-mosfet renumbers the pads, so a fit by pad number fails where one by geometry holds (sent to cpl_verify's owner). |
| oasis-4: J1 power-input wire holes too big (SolderWire-2.5sqmm, 3.0 mm drill) | DFA (THT hole vs lead) | caught on rev A, gone on rev B (`check_connector_fit` tht_hole_loose) | J1 is a solder-wire footprint with no part to hold it to, so check_connector_fit sizes the wire from the copper feeding the pads: +18V's only feed is a pour's four 0.5 mm thermal spokes, about 5.4 A at a 20 degC rise (IPC-2221), a 0.75 mm^2 wire with a 1.25 mm conductor, which leaves 1.75 mm in the 3.0 mm hole. v0.3 drops the solder-wire pads for a header, and the finding is gone. |

### minuet: v3 -> v4

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

### glasgow: revD0 -> revD1

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

### echoplayer: r1-rev1 -> r1-rev1.1

[https://github.com/amachronic/echoplayer](https://github.com/amachronic/echoplayer) at `50f31a141c4c`, CERN-OHL-S-2.0; errata `r1-rev1/docs/errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| echoplayer-1: USB3320C ID pin grounded on a device-only board, so the core starts as a host | SCH (datasheet pin state) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): USB3320 ID grounded makes an A-device (DS00001792F Table 2-1, 5.6.1), a warning as a host board may mean it. rev1.1 ties U8's ID to 3V3, as the datasheet allows. |
| echoplayer-2: HP/LO 3.5 mm jacks wired against the standard headphone pinout | LIB (standard connector pinout) | caught on rev A, gone on rev B (`check_pinouts` pinout_mismatch) | check_pinouts reads J4's pads T/R1/R2/S from their names: rev1 has the tip on /audio/hp_com and left on R1 (J5, the line out, has its tip on GND and fires too); rev1.1 has both in the CTIA order. |
| echoplayer-3: ESD diodes D1-D4 load the SDMMC lines past the bus's capacitance limit | SI (bus capacitive load) | caught on rev A, gone on rev B (`netlist_audit` bus_cap_load) | reference/bus_load.yaml (#1031): a CESD3V3AP is 120 pF typ at 0 V (JSCJ rev C), four times the 30 pF an SD host side may add (NXP AN10911 Table 2). rev A is pinned at e14b09d, the board as built: the errata commit a0d0b02 also marked D1-D4 DNP in r1-rev1, and the rule leaves a DNP part out, as it isn't fitted. r1-rev1.1 has no clamps on the SDMMC lines. |

### bedlift: v0.2 -> v1

[https://github.com/j9brown/bed-lift](https://github.com/j9brown/bed-lift) at `6b5053345233`, MIT; errata `hardware/v0.2/design-and-errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| bedlift-1: lift actuator connectors J8/J9 pins 2 and 3 swapped, reversing the hall sensors' supply | SCH (off-board connector pinout) | caught on rev A, gone on rev B (`check_pinouts` pinout_mismatch, with constraints) | v0.2 has pin 2 on VHALL and pin 3 on GND; v1 has pin 2 on GND and pin 3 on +5V. Only the actuator's own pinout says which is right, so the catch states the mate (reference/connector_pinouts.yaml bedlift-lift-actuator) on J8 (v1's actuator connector is J10). |
| bedlift-2: DRV8434S nSCS tied to GND, but the part needs a falling edge to start each SPI frame | SCH (datasheet pin state) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): DRV8434S nSCS must be driven, as each frame needs it high between (SLOSE70 7.5.1.2). v0.2 ties U7-U10's nSCS to GND; v1 drives them from the MCU. |
| bedlift-3: DRV8434S STEP not on a hardware-timer pin, so stall detection sees jitter | FW-HW (pin function) | not_static | the DRV8434S also takes steps over SPI, and which timer the firmware gives STEP is its own choice; no pin table rules the wiring out (#1032). |
| bedlift-4: DRV8434S VREF wired straight to DVDD, 5 V where 3.3 V is recommended | SCH (datasheet pin range) | caught on rev A, gone on rev B (`netlist_audit` pin_state) | reference/pin_states.yaml (#1033): DRV8434S VREF is limited to 3.3 V and DVDD is 5 V (SLOSE70 6.3, 8.2.2.2), so VREF on the part's own DVDD fires. v1 feeds it through a divider. |
| bedlift-5: LIFT1 IN2 on an MCU pin with no timer channel, so the motor cannot be driven by PWM | FW-HW (pin function) | caught on rev A, gone on rev B (`netlist_audit` mcu_pwm_timer) | reference/mcu_pins.yaml (#1032): with PMODE high both DRV8874s are in PWM mode (SLVSF66A Table 2), and v0.2's four inputs (PB0, PA15, PC6, PA8) fit no single timer of the STM32C031. v1's PA0-PA3 all sit on TIM1 of its STM32C051. A warning. |
| bedlift-6: actuator hall sensors powered at 3.3 V where they are meant for 5 V | SCH (off-board part supply) | caught on rev A, gone on rev B (`check_pinouts` pinout_supply_volts, with constraints) | /VHALL is U4's output (a 74LVC1G17 on +3.3V), so check_pinouts puts it at 3.3 V against the 5 V the actuator row asks on its supply pin; v1 feeds pin 3 from +5V. |

### pdlc: v1 -> v2

[https://github.com/brown-studios/pdlc](https://github.com/brown-studios/pdlc) at `423481719bae`, MIT; errata `hardware/v1/design-and-errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| pdlc-1: the driver does not wait for power-good before driving the panel | PI (power-good sequencing) | caught on rev A, gone on rev B (`netlist_audit` power_good_unused) | #1030: v1's LM5158 PGOOD reaches only R1 2.2k and the D2 power LED, so nothing holds the half-bridge off until the boost is in regulation; v2 takes every PG (/HV_PG, /PDLC_PG_A, /PDLC_PG_B) into a 74LVC1G08 and STM32 inputs. |
| pdlc-2: no current limit on the panel drive, so large panels drain the output capacitor | PI (load current limit) | missed | a switched capacitive load against the boost's output charge and current limit: needs the load's capacitance, which no schematic carries (#1030, survey) |
| pdlc-3: the EN input is too sensitive when left floating | SCH (input pull strength) | caught on rev A, gone on rev B (`netlist_audit` offboard_input_weak_pull) | #1079: v1's EN terminal (J1.3) is held only by R4 100k to +12V, with no clamp, into Q1's base through R5 20k (C7 100n at the base did not save it): above TI SPRACK9's 10k for a line that may be disconnected, and no TVS as Microchip AN2587 asks on external I/O. v2's /~{CTRL_EN_IN} has 22k to +3.3V and the D4 PESD3V3L5UY array; it is the clamp that keeps v2 quiet. |

### megabuzz: revA -> revA1

[https://github.com/andykarpov/karabas-megabuzz](https://github.com/andykarpov/karabas-megabuzz) at `b3a026f15f44`, MIT; errata `ERRATA.en.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| megabuzz-1: OPL3 at 5 V drives the FPGA's 3.3 V inputs through dropping resistors only | SCH (I/O supply domain) | caught on rev A, gone on rev B (`netlist_audit` io_overvoltage) | reference/io_levels.yaml (#1029): the YMF262's VOH min VDD-1.0 V on its 5 V rail is 4 V at the Spartan-6 IO_ pins through R13-R16 27 ohm, over DS162's 3.95 V VIN max (YM_DATA, YM_DCLK, YM_SMP1, YM_SMP2); revA1 runs it from +3V3. The errata's /RST items are fixed in the revA files at this commit, and the revA1 items have no next revision here, so only this one is judged. |

### sizif: rev.C -> rev.C1

[https://github.com/UzixLS/zx-sizif-512](https://github.com/UzixLS/zx-sizif-512) at `d9b1f4d71383`, MIT; errata `pcb/rev.C/ERRATA.txt`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| sizif-1: JTAG header pinout differs from the Altera standard | SCH (standard connector pinout) | caught on rev A, gone on rev B (`check_pinouts` pinout_mismatch) | A KiCad 5 legacy schematic, read through lib/legacy_net. J11's net names follow the Altera pinout on both revisions; the fault is at the FPGA end (rev.C puts /TCK on U6's TDO, /TDI on its TCK, /TDO on its TDI). check_pinouts judges each header pin by the JTAG pin function it meets on the IC, so rev.C fires and rev.C1 is clean. |
| sizif-2: J3 header's silkscreen is wrong | DFA (silk labels) | caught on rev A, gone on rev B (`check_silk` pin_label_wrong) |  |

### ambient: rev_c -> rev_d

[https://github.com/lab11/signpost](https://github.com/lab11/signpost) at `f8f9efe01185`, Apache-2.0; errata `modules/ambient/rev_c/errata.txt`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| ambient-1: no pull-ups on SWDIO and SWDCLK | SCH (datasheet pin state) | not_static | At the pinned commit rev_c's own files already carry the fix: its ambient.sch, kicad_pcb, shipped .net and BOM all have R7 and R8, 100k from /SAM4L_SWDIO and /SAM4L_SWDCLK to +3V3, as rev_d has R1 and R2. The files were patched after the boards were made, so none holds the defect and no check can see it (#1044). The errata's other line, "Random trace on pin32?", is a question with no stated fix, so it is not judged. |

### one-rom-fire28: fire-28-a2 -> fire-28-a3

[https://github.com/piersfinlayson/one-rom](https://github.com/piersfinlayson/one-rom) at `1b189260d85a`, CERN-OHL-W-2.0; errata `hardware/pcb/verified/fire-28-a2/README.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| one-rom-1: SWD header silk labels CLK and DIO swapped | SILK (pin label vs net) | caught on rev A, gone on rev B (`check_silk` pin_label_wrong) | Swept blind as a holdout board (missed, with the flow at db43cb8), then moved here: check_silk's per-pin pin_label_wrong form (#1028) was written knowing this defect, so its catch is a training catch. The blind and swept fields are kept as the record. |

### byte-hamr: Rev 2 -> Rev 3

[https://github.com/robertrico/project_byte_hamr](https://github.com/robertrico/project_byte_hamr) at `f3e105fb7585`, MIT; errata `hardware/byte_hamr/rev2_errata.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| byte-hamr-1: U14 SN74LVC8T245 drives the Apple II slot's wired-OR lines (IRQ, NMI, DMA, INH, RES) push-pull, DIR and OE tied to GND | SCH (driver type on a shared line) | caught on rev A, gone on rev B (`check_pinouts` wired_or_push_pull) | Swept blind as a holdout board (missed, with the flow at db43cb8), then moved here: check_pinouts' wired_or_push_pull (#1087) was seeded from the I2C specification, the Apple II manual's open-collector slot lines and TI's 74LVC8T245 function table, but written knowing this defect, so its catch is a training catch. Rev 3 drives the slot lines through open-drain 74LVC07s and is clean. The blind and swept fields are kept as the record. |

### identidem-breakout: MRF-Pro-v7.5 breakout -> MRF-Pro-v8 breakout

[https://github.com/acornelissen/IDENTIDEM.design-MRF2](https://github.com/acornelissen/IDENTIDEM.design-MRF2) at `e495d6494dc6`, GPL-3.0; errata `Documentation/hardware-errata/lidar-stage1-decoupling.md`.

| defect | class | pcb-flow on rev A | follow-up |
|---|---|---|---|
| identidem-1: pulsed-laser LiDAR on J7 fed 3.3 V over a long path with no decoupling capacitor near it | PI (local decoupling) | caught on rev A, gone on rev B (`check_pinouts` pulsed_load_no_bulk) | Swept blind as a holdout board (missed, with the flow at db43cb8), then moved here: check_pinouts' pulsed_load_no_bulk (#1090) was seeded from makers' supply notes for pulsed loads (Garmin's LIDAR-Lite, SIMCom's SIM800), but written knowing this defect, so its catch is a training catch. J7's supply, /3.3V, has no capacitor of 10 uF or more; v8 feeds J7 from its own filtered rail with 47 uF on it and is clean. The blind and swept fields are kept as the record. |

## Holdout round 1

### netscore-main: main-board v3.0 -> main-board v3.1

[https://github.com/arcticlula/Netscore](https://github.com/arcticlula/Netscore) at `4426f309aab5`, MIT; errata `hardware/docs/versions.md`. Holdout round 1, swept blind with pcb-flow `db43cb8459ff` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| netscore-1: Q3 power-mux FET source and drain swapped, body diode VCC to +BATT | SCH (part orientation) | missed | caught on rev A, gone on rev B, since #1085 (`netlist_audit` power_path_body_diode) | Not an independent catch: the rule was written after this defect was read, though seeded from datasheets (TI SLVA139, Microchip AN1149), not from this board. The warning is on v3.0 and gone on v3.1. |
| netscore-2: BMS1 FS312MH GND (cell negative) and BAT- (load negative) swapped | SCH (pin function vs datasheet) | missed | missed | protector pins named GND and BAT- (FS312-style): #1085 reads only VDD/VSS/VM/DO/CO protectors |

### netscore-big: display-board-big v2.0 -> display-board-big v3.0

[https://github.com/arcticlula/Netscore](https://github.com/arcticlula/Netscore) at `4426f309aab5`, MIT; errata `hardware/docs/versions.md`. Holdout round 1, swept blind with pcb-flow `db43cb8459ff` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| netscore-big-1: 0.8 in score digits placed with the 0.2 in digit symbol, so common anode and two segments land wrong | LIB (symbol pin map vs part) | missed | caught on rev A, gone on rev B, since #PRNUM (`netlist_audit` symbol_value_mismatch) | Not an independent catch: the rule (#1086) was written after this defect was read. It reads the symbol's own size or part number against the Value, not this board. The warning names all four 0.8 in digits on v2.0 and is gone on v3.0. |

### cmm2-jlc: Rev.A -> Rev.B

[https://github.com/Manawyrm/CMM2-JLC](https://github.com/Manawyrm/CMM2-JLC) at `318b012b10ac`, GPL-3.0; errata `README.md`. Holdout round 1, swept blind with pcb-flow `db43cb8459ff` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| cmm2-1: no mounting holes | MECH (mounting) | missed | missed | a board with no mounting holes (#1088) |
| cmm2-2: SDRAM address line A12 not connected | SCH (net connectivity) | caught | caught on rev A, gone on rev B (`netlist_audit` dangling_net) | PG2 is the STM32H743's FMC_A12. Rev.A labels it on U1 pin 106 alone, and Rev.B joins it to U2 (SDRAM) A12. The finding is one of 33 dangling_net warnings on Rev.A (32 stay on Rev.B, PG3 among them), so the flow names the fault, but a reader has to pick it out of the list. |

### smart-lcc: Rev A -> Rev B

[https://github.com/magnusnordlander/smart-lcc](https://github.com/magnusnordlander/smart-lcc) at `8f4f0c26a3d1`, CERN-OHL-P-2.0; errata `pcb/errata.md`. Holdout round 1, swept blind with pcb-flow `db43cb8459ff` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| smart-lcc-1: auxiliary UART RX and TX crossed the wrong way | SCH (signal direction) | missed | missed | off-board and standard connector pinouts, with the mate stated (#1027) |
| smart-lcc-2: no zener/diode protection on the RX and TX ports | SCH (port protection) | missed | missed | check_esd counting a bare zener to ground as a port clamp, seeded from a training board |

### fc980c: rev1 -> rev2

[https://github.com/wlhlm/fc980c-controller](https://github.com/wlhlm/fc980c-controller) at `6e5bdc90e227`, CERN-OHL-S-2.0; errata `README.md`. Holdout round 1, swept blind with pcb-flow `db43cb8459ff` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| fc980c-1: SWD header's SWDIO routed to PA15 instead of the STM32's SWDIO pin PA13 | SCH (pin function vs datasheet) | missed | missed | SWD header rows, and the MCU's own SWDIO/SWCLK pins (#1070) |
| fc980c-2: KEY_STATE open-collector line has no 5V pull-up | SCH (missing pull-up) | missed | missed | an off-board control input left floating needs a stiff pull (#1079) |

### lofence: rev_a -> rev_b

[https://github.com/kiu/lofence](https://github.com/kiu/lofence) at `9a1d09bd57b8`, MIT; errata `rev_b/errata.txt`. Holdout round 1, swept blind with pcb-flow `db43cb8459ff` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| lofence-1: ISP header silk marks RES where 3V3 is | SILK (pin label vs net) | missed | missed | check_silk reading a header's labels against its pins' nets (#1028) |

## Holdout round 2

### ice1usb-r01: icE1usb r0.1 -> icE1usb r1.0

[https://github.com/osmocom/osmo-e1-hardware](https://github.com/osmocom/osmo-e1-hardware) at `85acea8b6d65`, CERN-OHL-S-2.0; errata `hardware/icE1usb/ecn.md`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| ice1usb-r01-1: SPI flash footprint is the wide SO8; the part is the narrow one | LIB (wrong footprint variant) | missed | missed | the footprint against the part's package: a SPI flash MPN in SOIC-8 150 mil on a 208 mil SO8 footprint |
| ice1usb-r01-2: NT/TE selection pin header wired wrongly | SCH (wrong header wiring) | missed | missed | a stated jumper map for configuration headers, so their wiring can be checked |

### ice1usb-r10: icE1usb r1.0 -> icE1usb r1.1

[https://github.com/osmocom/osmo-e1-hardware](https://github.com/osmocom/osmo-e1-hardware) at `85acea8b6d65`, CERN-OHL-S-2.0; errata `hardware/icE1usb/ecn.md`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| ice1usb-r10-1: GPS I2C shares the extension port's lines and the GPS module drives them as a master; r1.1 adds jumpers | SCH (bus conflict, no isolation) | missed | missed | I2C masters per bus: a module that drives I2C as a master (from its datasheet) sharing lines with an off-board port |

### zxdiag-v10: v1.0 -> v1.1

[https://github.com/derekfountain/zx-spectrum-diagnostics](https://github.com/derekfountain/zx-spectrum-diagnostics) at `a6d874e92e14`, GPL-3.0; errata `pcb/errata.txt`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| zxdiag-v10-1: Edge connector footprint has its rows about 2.5 mm apart instead of 5 mm | LIB (wrong footprint geometry) | missed | missed | an edge connector's row spacing against the mating connector's |
| zxdiag-v10-2: Edge connector header holes too small for the pins | PCB (drill size) | missed | missed | check_connector_fit gave no finding on the edge header's holes: find whether the lead size is unknown for this footprint |

### zxdiag-v11: v1.1 -> v1.2

[https://github.com/derekfountain/zx-spectrum-diagnostics](https://github.com/derekfountain/zx-spectrum-diagnostics) at `1d701d1ba594`, GPL-3.0; errata `pcb/errata.txt`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| zxdiag-v11-1: /IORQ not connected to the Pico | SCH (missing connection) | missed | missed | a named bus checked complete at the MCU: a Z80 control line the target needs left off the Pico |
| zxdiag-v11-2: 1N4148 clamps on the -5V input let -0.7 V reach the Pico ADC, whose minimum is -0.5 V | SCH (input beyond absolute maximum) | missed | missed | a clamp diode's forward drop against the clamped pin's negative abs-max (-0.5 V on the RP2040 ADC) |
| zxdiag-v11-3: Top of the -5V divider fed from the system-under-test +5V, not a reliable reference | SCH (reference from an unreliable rail) | missed | missed | a measurement divider whose reference comes from an off-board rail |

### pico-if1-v10: v1.0 -> v1.1

[https://github.com/derekfountain/zx-spectrum-pico-interface-one](https://github.com/derekfountain/zx-spectrum-pico-interface-one) at `7d1be0cb53fe`, GPL-2.0; errata `errata.txt`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| pico-if1-v10-1: Transistor Q1 footprint rotated 90 degrees | PCB (part rotation) | caught | caught on rev A, gone on rev B (`netlist_audit` sot23_pinout) | The erratum calls it a rotation; the flow names the same part's fault as pin order (an E-B-C symbol on a B-E-C SOT-23), and the finding is gone on rev B. Counted caught on the part and the fault. |
| pico-if1-v10-2: Data bus level shifter direction on /RD instead of ROM_READ_INV | SCH (wrong net) | missed | missed | a level shifter's DIR pin checked against a stated direction (read strobe) net |
| pico-if1-v10-3: GND via for U4 pin 19 (/OE) sits under the chip where it can't be reached | PCB (via placement) | missed | missed | a via under a part's body tying one of its own pins, on a hand-built board |

### pico-if1-v13: v1.3 -> v1.4

[https://github.com/derekfountain/zx-spectrum-pico-interface-one](https://github.com/derekfountain/zx-spectrum-pico-interface-one) at `7d1be0cb53fe`, GPL-2.0; errata `errata.txt`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| pico-if1-v13-1: OLED CLK and SDA transposed | SCH (swapped signals) | missed | caught on rev A, gone on rev B, since #PRNUM (`netlist_audit` mcu_i2c_role) | Not an independent catch: the rule was written after this defect was read, from the RP2040's I2C pin functions (pico-sdk io_bank0.h), not from this board. It sees the swap on the Pico side: GPIO4 (I2C0 SDA) on OLED_SCK and GPIO5 (I2C0 SCL) on OLED_SDA. It fires on v1.0 and v1.1 too (the same wiring, before the erratum was written) and is gone on v1.4. |
| pico-if1-v13-2: SD card reader card-detect pin not connected | SCH (missing connection) | missed | missed | a per-part list of pins that must be connected (SD socket card-detect) |

### gcm-v50: V5.0 -> V5.1

[https://github.com/JonathSpirit/GCM](https://github.com/JonathSpirit/GCM) at `d33fcd27bed2`, CERN-OHL-W-2.0; errata `CHANGELOG`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| gcm-v50-1: U12 on the BWRITE1 bus, in conflict with U40; V5.1 moves it to BWRITE2 | SCH (bus conflict) | missed | missed | bus contention: two drivers enabled by one bus strobe (U12 and U40 on BWRITE1) |
| gcm-v50-2: A symbol's pins flipped and out of order, causing a clock problem | LIB (symbol pinout) | missed | caught on rev A, gone on rev B, since #PRNUM (`netlist_audit` bus_order_flipped) | Not an independent catch: the rule was written after this defect was read. On V5.0 one unit of each SN74LV393A divider chain counts its QA..QD outputs onto the clock nets the other way round from its twin unit. The warning is gone on V5.1. |
| gcm-v50-3: J7 on the wrong footprint | LIB (wrong footprint) | missed | missed | the footprint against the part's MPN package |
| gcm-v50-4: J12 the wrong gender; V5.1 uses a male connector | PCB (connector gender) | missed | missed | connector gender against a stated mating side |

### gp8b-v50: V5.0 -> V5.1

[https://github.com/JonathSpirit/GP8B](https://github.com/JonathSpirit/GP8B) at `6335b5a59bfc`, CERN-OHL-W-2.0; errata `CHANGELOG`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| gp8b-v50-1: J1 not a male connector | PCB (connector gender) | missed | missed | connector gender against a stated mating side |
| gp8b-v50-2: ADDSCR_CLK not sending the right pulses to the motherboard | SCH (wrong net) | missed | missed | the erratum does not name the fault; read the v5.0 to v5.1 ADDSCR_CLK change to find a rule |

### mdbt-micro-50q: 50q-v1.1 -> 50q-v1.2

[https://github.com/siderakb/mdbt-micro](https://github.com/siderakb/mdbt-micro) at `7821d9046258`, MIT; errata `Changelog.md`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| mdbt-micro-50q-1: Pro Micro 2.54 mm header holes 0.7 mm, too small; 1 mm in v1.2 | DFM (hole size) | missed | missed | check_connector_fit gave no finding on 0.7 mm holes for 2.54 mm header pins: find why |

### flexgrid-v4: V4 as ordered -> V4-2

[https://github.com/Open-Muscle/OpenMuscle-FlexGrid](https://github.com/Open-Muscle/OpenMuscle-FlexGrid) at `d3529287a899`, CERN-OHL-S-2.0; errata `KiCad/OM-FlexGrid V4/README.md`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| flexgrid-v4-1: USBLC6 with pin 2 on VBUS and pin 5 on GND; the part has pin 2 GND, pin 5 VCC | SCH (pin function vs datasheet) | missed | missed | pin maps of common TVS arrays (USBLC6: pin 2 GND, pin 5 VBUS) in check_pinouts |
| flexgrid-v4-2: 29 vias inside SMD pads that wick solder in reflow | DFM (via in pad) | missed | missed | via-in-pad on SMD pads without fill or tent (dfm) |

### flexgrid-v3: V3 -> V4

[https://github.com/Open-Muscle/OpenMuscle-FlexGrid](https://github.com/Open-Muscle/OpenMuscle-FlexGrid) at `d3529287a899`, CERN-OHL-S-2.0; errata `KiCad/OM-FlexGrid V4/README.md`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| flexgrid-v3-1: 1N5819 and 1N4007 on D_SOD-123F; the fab stocks the SOD-123 W parts | LIB (wrong footprint variant) | missed | missed | the footprint against the stocked part's package (SOD-123F against SOD-123W) |
| flexgrid-v3-2: C9 (100 uF) on an 0603 footprint that cannot hold the part | LIB (footprint too small for part) | missed | caught on rev A, gone on rev B, since #1129 (`check_fp_package` lcsc_value_mismatch) | Not an independent catch: the rule was written after this defect was read. It sees C9 through its JLC number, C12530, a 2.2 uF 0402 part (also footprint_package_mismatch, 0402 on 0603), not through the case size a 100 uF part needs. Needs --live (JLC's record). On V3, not on V4. |

### healthmonitor: HM-Rev1 -> HM-Rev2

[https://github.com/konstantinosfragkoulis/HealthMonitor](https://github.com/konstantinosfragkoulis/HealthMonitor) at `2d22f3d8d10c`, MIT; errata `hardware/HM-Rev1/ERRATA.md`. Holdout round 2, swept blind with pcb-flow `4540a94bce11` on 2026-11-14.

| defect | class | blind sweep | pcb-flow on rev A | follow-up |
|---|---|---|---|---|
| healthmonitor-1: AD8232 missing its 4.7 uF capacitor between IOUT and SW | SCH (missing component) | missed | missed | a part's required external parts from its datasheet (AD8232's IOUT to SW capacitor) |
| healthmonitor-2: AD8232 resistor and capacitor between REFIN and GND wired wrongly | SCH (wrong connectivity) | missed | missed | a reference pin's network against the datasheet's typical circuit (AD8232 REFIN) |
