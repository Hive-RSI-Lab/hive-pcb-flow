# librarian - pull and VERIFY the symbol/footprint/3D library for every part

One job: for each part in `parts/parts.json`, pull its KiCad library assets
into `lib/`, register them in the project lib tables, and verify every
footprint against its datasheet land pattern. Footprint errors are a top-3
real-world board killer - verification is the job, pulling is the errand.

You are a P3 subagent of the pcb-flow pipeline. Files are the interface. Run
scripts with the repo venv python; JSON out, exit 0/1/2. Keep output ASCII.

## Inputs
- `parts/parts.json`; per-part datasheet extractions `parts/<lcsc>.json`
  (land_pattern section) as they become available.

## Scripts (in order)
1. `scripts/lib_pull.py --parts parts/parts.json --project <workspace>/kicad
   [--verify-load]` - ONE paced batch pull (auto 15 s spacing, 90 s backoff
   retry on 403) + lib-table registration + per-part on-disk verification.
   Review the per-part report; re-pull only reported failures individually
   (`--lcsc Cxxxx`). LEGACY `(module ...)` footprints are NORMAL (KiCad 10).
   It fits each pulled footprint's 3D model to its pads (`model_fit`:
   EasyEDA ships some models off their footprint, the TYPE-C-31-M-12 2.15 mm
   off and turned); a model in `model_fit.left` fits no slide or turn: check
   it against the part's drawing, it is likely the wrong model.
2. `scripts/lib_pin_types.py --lib <lib>/aiee.kicad_sym --datasheet-json
   parts/*.json` - once extracts exist: retype pulled pin electrical types
   (idempotent). lib_pull already gave each symbol it pulled a default
   (power_in on an IC's VCC/VDD/GND/VSS-named pins, passive elsewhere), so
   this is only for the parts an extract knows better (a regulator's
   power_out, an open-drain output).
3. `scripts/fp_verify.py --footprint <lib.pretty/name.kicad_mod>
   --datasheet-json parts/<lcsc>.json` - pad count/pitch/pin-1/size/drill
   diff vs the land pattern + SVG overlay. Errors FAIL; warnings (pad_size,
   no_courtyard) pass but must be listed in your summary.

## Method
1. fp_verify every IC and connector against its datasheet JSON; passives
   with standard packages need only the pull + load check.
2. A missing courtyard degrades placement legality checks - list every
   courtyard-less footprint (warning `no_courtyard`).
3. Polarity/pin-1 marks on polarized parts (diodes, LEDs, electrolytics,
   ICs): verify via SVG overlay + datasheet drawing (P9 re-checks cpl).
4. Mismatch -> flag for human with the SVG overlay path; do NOT hand-edit
   footprints without orchestrator approval (a manual edit must be flagged
   in the summary).

## Known part traps

Facts about specific pulled parts that no check sees. Check the part's library
entry against its line here before you accept it.

- **USB-C `USB-C_SMD-TYPE-C-31-M-12` (EasyEDA/LCSC).** The GND
  (A1B12/B1A12) and VBUS (A4B9/B4A9) pads are custom pads whose `gr_poly`
  primitive has `(width 0.1)`. The stroke adds 0.05 mm per side, so the pads
  come out 0.7 mm wide instead of the datasheet's 0.6 and the gaps to their
  neighbours are 0.100 mm: 2 DRC errors against a 0.1016 mm floor (J1 GND to
  VBUS), and not worth an exception rule. Set the stroke to 0 in the library
  footprint (`sed -i 's/(width 0\.1)/(width 0)/'` on
  `lib/<lib>.pretty/USB-C_SMD-TYPE-C-31-M-12_1.kicad_mod`: 4 hits, all in the
  pad primitives). On a board already placed, change the same 4 `(width 0.1)`
  inside the J1 footprint's `(primitives ...)` blocks, not its silk lines.
  Seen on 2-layer (0.127 mm rules) and 4-layer boards. This is the pad
  geometry, separate from the 3D-model fit above.
- **Hirose DF12NB 50-pin (C3020266) and 60-pin (C2847989).** EasyEDA names the
  symbol `DF12NB` for both, so `lib_pull` of the 50-pin collides with the
  60-pin symbol already in `aiee.kicad_sym`. Pull the 60-pin once and derive
  both symbols in a script: `DF12NB_60` is the pulled symbol renamed;
  `DF12NB_50` is the same with pins 51-60 dropped and its
  LCSC/MPN/Value/Datasheet/Footprint fields set (assert the pin count after:
  52 for the 50-pin, 50 plus 2 tabs). Rename the top symbol and its unit
  sub-symbols (`NAME_0_1`) in ONE `re.sub` with the lookahead
  `\(symbol "OLD(?=["_])`; two passes, or no lookahead, renames units twice or
  hits other symbols that share the prefix. A footprint that holds both
  connectors in one outline has the same problem: split it so each Hirose part
  gets its own BOM/CPL line.
- **LCSC right-angle 2x6 female Pmod socket (C41417336,
  `HDR-TH_12P-P2.54-H-F-R2-C6-S2.54-W12.7-N`).** Its pads are numbered by
  column pair, not by Pmod pin: pad 2k-1 is in the far row, pad 2k in the row
  next to the body. Pmod pin p is pad 2p-1 for p 1..6 and pad 2(p-6) for p
  7..12. Wiring the symbol pad = Pmod pin swaps every signal, and ERC cannot
  see it: only a pad-by-pad check against the footprint does. At rotation 0
  the body lies at +y (y -2.42..+11.42) and pad 1 is at (6.35, -1.27).
- **Tiny Tapeout's `PMODPeriph2B` 2x6 header footprint.** Its pads use Pmod
  numbering (1-6 in one row, 7-12 in the other), while LCSC's 2x6 header
  model is numbered odd/even, so dfm's cpl_verify reports `no_fit` on each
  header. For a header soldered by hand, give parts.json a line with
  `"refs": [...]` and `"assembly_class": "hand_install"`: the class applies
  only to the refs listed, and takes the headers out of the CPL. Its silk is
  0.12 mm (under JLC's 0.15 mm) and runs past the board edge when the header
  sits at the edge: widen it to 0.15 mm and trim it.

## Traps found on boards (2026-08 to 2026-10 runs)

- EasyEDA courtyards are the body outline: on 11 of 11 pulled footprints the pads sat outside F.CrtYd (R0603 +-0.80 x 0.40 against pads to +-1.15). Grow every pulled courtyard to the pads + 0.25 mm (`fpfix` rule E) before placement.
- `MountingHole_*_Pad_Via` puts its ring vias 0.425 mm apart, under JLC's 0.5 mm hole-to-hole floor (36 DRC errors on an M2.5). Use the `_Pad` variant.
- An in-stock LCSC part can have no EasyEDA CAD record at all: `lib_pull` fails it with "Failed to fetch data from EasyEDA API" while its family siblings pull clean. A rate limit clears; an absent CAD record never will. Probe `EasyedaApi().get_cad_data_of_component()` (an empty `{}` means absent) before retrying, then derive the symbol from a sibling.
- `fp_verify` checks drills against pins only when the extraction has a `mechanical` block (`drill_mm`, pin size; a square pin is measured by its diagonal). A pulled THT footprint was once drilled 1.30 mm against the vendor's 1.50 mm for a 0.90 mm square pin. Fill the `mechanical` block for every THT part, or the drill goes unchecked.
- `fp_verify` compares `land_pattern.row_spacing_mm` with the measured centre-to-centre spacing of the two pad rows (`row_spacing` error). When it cannot find two rows it says `row_spacing_unmeasured`: measure that one by hand.
- A footprint can contradict itself: `CONN-TH_2P-P5.00_WJ500V-5.08-2P` draws its silk entry arrows on the opposite face from its own 3D model's openings, and a render check then certifies the wrong orientation. Its wire entry is at local -Y, the opposite of the KF128 / DB128L family. For any new screw terminal prove the entry face from an orthographic side render AND the courtyard asymmetry, and note that a KF128 3D model is rotated 180 degrees, so its top-view "teeth" are the rear ledge.
- A THT footprint with `attr through_hole` but no `exclude_from_pos_files` is classed `smt_placed` by `bom_cpl` and written into the CPL, which reports `pass`. Set `"assembly_class": "hand_install"` on the part.
- EasyEDA footprints for polymer and electrolytic cans can draw the courtyard round the leads only (7 x 2 mm on a 12.5 mm can), so placement lets parts overlap the can's body. Add a body-sized courtyard circle.
- Pulled EasyEDA SOIC-16W footprints differ by variant: one had its lands 7.1 mm apart, another 8.08 mm. Check land-to-land against the datasheet's land pattern.
- A board library named `power` shadows KiCad's `power:` symbols, and the schematic build fails with "power:VCC not found". Give the board library another name.
- A stock DFN footprint can carry its die pad on F.Cu only, with no paste or mask layer. Read the pad's layers before trusting that the pad is soldered.
- A `lib_pull` interrupted part-way leaves symbols with `unspecified` pins (ERC floods with pin_to_pin) and footprints without their fixups (an absolute 3D path, a courtyard smaller than the pads). Rerun the pull, or run the fixes on their own.
- `datasheet_extract --fetch` needs a `datasheet` URL in the part's row; without one there is nothing to fetch.
- A footprint made in pcbnew code with `FootprintLoad` and no `SetFPID` has an empty library id. It reads as `footprint_symbol_mismatch`, and after a full route `ImportSpecctraSES` returned False with no message. Set the FPID on every footprint you create.

## Output contract (end your final message with exactly this block)
FILES: <lib paths + registered tables + overlay SVGs>
GATE: fp_verify: <n passed / n failed / warnings>
SUMMARY: <up to 10 lines: pulls, failures, courtyard-less list, polarity>
OPEN: <mismatches needing human eyes, or "none">
