# Round 3 taxonomy members (sealed, scorers only)

Rule builders: stop here. These are round 3's defects, moved out of
`docs/miss-taxonomy.md` so the round stays a blind test; a rule written
after reading them is no longer tested by it. Builders' briefs quote only
the taxonomy's Ranked table. See "A new holdout round" in
`docs/errata-corpus.md`. Unsealing the round moves these lines back.

## Since then (round 3 sweep)

The one blind catch: scanlight-driver-1, netlist_audit dangling_net.

## 1. Footprint vs the part's package

   Round 3 (blind, all missed):
   - k30-sbc-1: 74HC253s on a 4.9 mm SSOP-16 footprint; the parts are 6.2 mm wide.
   - mmm-1: MOSFETs bought for the footprint are far smaller than it (a PowerPAK size mix-up).
   - spitfire-4: crystal load capacitors C2 and C6 on 0201 with no value.
   check_fp_package judged k30-sbc and z280-sbc's 74HC parts (SOIC against
   TSSOP, from JLC's part named by Value), but not the 74HC253s or the defect
   parts, and those findings stay on rev B.

## 2. Connector mechanics vs the mate

   - Power connector orientation: z280-sbc-1 (ground on top, no labels), round 3.

## 4. Board-level DFM

   - kharon386-1: the BIOS PLCC socket collides with the CF slot (round 3).
   - karabas-pro-2: no keep-out under the ESP-12 antenna (round 3).

## 5. Symbol pin map vs the part

   Round 3 miss: rosco-m68k-1, MC68901 MFP address lines wired in reverse
   order. symbol_pinmap flagged a different part on that board (IC6, an
   Eagle-import 74174), not the MFP.

## 6. Pin function on the wrong net

   Round 3 miss: z280-sbc-4, CTIN1 and CTIN2 on CPLD input-only pins where
   the CPLD must drive them. No row says which CPLD pins are input-only.

## 7. Datasheet application circuit

   Round 3 (blind, before #1158):
   - karabas-pro-1: no 10k pull-ups on the floppy controller inputs.
   - k30-sbc-3: a spare NAND input (DTACK) left floating.

## 8. Wiring against design intent

   Round 3: kharon386-2 (U15 the wrong part; a TXS0104 shifter needed),
   kharon386-3 (COM3/COM4 IRQs on the wrong lines) and kharon386-4 (ESP8266
   CTS/RTS crossed).

## 9. Shared-bus drivers

   - spitfire-2: SPI MISO with no pull-up floats when nothing is selected (round 3).
   - mmm-2: an LED circuit designed for a tri-state output on a status pin that is not tri-state (round 3).

## 10. Silk pin labels and polarity marks

10. **Silk pin labels and polarity marks.** lofence-1; round 3:
    rosco-m68k-2 (expansion pin 2 labelled 64) and libcm-1 (D5's silk
    drawn backwards).

## 12. Footprint geometry vs the maker's drawing

12. **Footprint geometry vs the maker's drawing** (new, round 3). The
    footprint matches the package name but not the part:
    - karabas-pro-3: CF card socket holes don't match the connector.
    - spitfire-1: the custom male DE-9 footprint (J13) is mirrored.
    - z280-sbc-2: the flash footprint is rotated, with pin 1 in the wrong corner.
    - relaymux-1: the reverse-mount LED's hole is too large.

## 13. Boot straps on modules

13. **Boot straps on modules** (new, round 3). check_straps judges MCUs
    with a reset_states.yaml row; the ESP8266 module has none. On a board
    with a module and no row, the strap check reports mcu_unknown and stops.
    - thermometer-hw-1: R9 stops the module booting.
    - thermometer-hw-2: the 1-wire bus with its pull-up on D8 (GPIO15), a strap that must idle low.

## 14. Active-low polarity across a connection

14. **Active-low polarity across a connection** (new, round 3). One
    active-low signal goes into a pin that needs the opposite level, or the
    idle state of a decoder selects something:
    - k30-sbc-2: the ATA DSACK output is not inverted into an active-low NAND input.
    - z280-sbc-3: 74HC573 LE driven by /AS directly, where it needs the inverse.
    - spitfire-3: decoder Y7, the resting all-ones state, selects J9.

## 15. I/O supply domain vs the bus level

15. **I/O supply domain vs the bus level** (new, round 3):
    - switch-v5-1: SJA1105Q ports 3 and 4 have their I/O supply on 3.3 V, where the link needs 1.8 V.
    - thermometer-hw-3: 1-wire sensors powered from 5 V on a 3.3 V bus.
