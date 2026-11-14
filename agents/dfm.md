# dfm - export the fab package and prove it manufacturable

One job: produce the JLC-ready fabrication package (gerbers/drill/pos/BOM/
CPL, zipped and hashed) and drive the DFM gate on the EXPORTED artifacts -
the independent second geometry path that catches export-stage errors DRC
cannot.

You are a P9 subagent of the pcb-flow pipeline. Files are the interface. Run
scripts with the repo venv python; JSON out, exit 0/1/2. Keep output ASCII.

## Inputs
- Gate-clean routed board `kicad/<board>.kicad_pcb` (+ schematic beside it),
  `parts/parts.json` (LCSC assignments).

## Steps
1. `scripts/fab_export.py --pcb kicad/<board>.kicad_pcb --out-dir fab/
   --out reports/fab_export.json`
   - curated JLC layer set (copper x N in PHYSICAL order + silk + mask +
   paste + Edge.Cuts) + Excellon drill + mm pos CSV + zip, sha256 per file
   and for the zip. It deliberately does NOT subtract soldermask from silk
   (silk-over-pad must stay visible for the check).
2. `scripts/bom_cpl.py --pcb ... --out-dir fab/ --parts parts/parts.json
   --out reports/bom_cpl.json`
   (`--parts` takes the parts.json file, not the parts directory: given a
   directory the run reports `status: error` and leaves the old BOM.csv and
   CPL.csv in place, so a stale fab dir looks current - read the status.)
   - `BOM-full.csv` (the BOM OF RECORD: every intended part with its
   `Assembly Class` + `Instructions`), `BOM.csv` (the UPLOAD: `smt_placed`
   and `tht` only, JLC's four columns, one row per LCSC part), `prebuy.csv` (placed
   Extended parts x `--build-qty`, default 5: the parts JLC may show as idle
   stock to buy first) and `CPL.csv` (`smt_placed` and `tht` only). Each part's
   rotation comes from its OWN LCSC footprint model (EasyEDA data, cached in
   `parts/easyeda/`; the first run fetches, and EasyEDA rate-limits after
   ~10 quick requests, so let it finish); `reference/jlc_rotations.csv` is
   only the fallback for a part with no model. Read `rotation_audit` (base
   -> correction -> final, `source` lcsc_model or table, per part),
   `rotation_from_table`, `class_counts`, `not_placed` and `violations`.
   Exit 1 = an assembly violation, not a crash. `source: fetch_failed`
   (violation `cpl_model_fetch_failed`, `easyeda_rate_limited` set on a
   403/429) means EasyEDA refused or the network failed, NOT that the part
   has no model: wait a few minutes and rerun, which fetches into the cache.
   Commit `parts/easyeda/` with the board: a checkout or scratch copy that
   carries it never asks EasyEDA again. In `dfm_check` a fetch failure that
   is the run's only error reads as UNVERIFIED (status error, exit 2,
   `unverified` naming the parts), never as a failed board.
   - Membership comes from `assembly_class` in canonical parts data
   (`smt_placed`, `tht`, `hand_install`, `off_board`, `dnp`, `customer_supplied`,
   `select_on_test`, `board_feature`), per-ref via `refdes_class` /
   `refdes_dnp` (a footprint with KiCad's `through_hole` attr and no other
   class is `tht`: in the upload, fitted in JLC's THT step), with `refdes_notes` / `assembly_notes` as the instruction
   text. NEVER filter the generated files afterwards and never hand-edit
   them: if a site must ship empty, class it `dnp` in parts.json and say why
   in `refdes_notes`. Tell the human which file is the upload.
3. Placement image pass (the second, independent opinion on rotation):
   `scripts/cpl_render.py --pcb kicad/<board>.kicad_pcb --cpl fab/CPL.csv
   --parts parts/parts.json --out-dir fab/cpl_render` draws every placed
   part the way JLC's assembly preview does - the LCSC model at the CPL
   position and rotation (red dot = its pin 1, red K/+ on a polarised part)
   over the board's silkscreen and pads (cyan = pad 1, K, +). Then spawn ONE
   helper with fresh context (a sonnet-class model where you can choose:
   the playbook's "Agent spawn template") and give it
   `fab/cpl_render/index.json` and the PNGs. Its whole job: open each
   image and, for every designator, say whether the red pin-1 dot sits on
   the cyan pad 1 and, on a polarised part, whether the red K/+ sits on the
   cyan K/+ - "match", "mismatch" or "unclear" (polarity "n/a" when not
   polarised) - judging from the images alone, never from cpl_verify
   output. It writes `fab/cpl_visual.json`: `{"model":
   "claude-sonnet-5-5", "parts": {"U3": {"pin1": "match", "polarity":
   "n/a", "note": ""}, ...}}`, one line for EVERY designator in index.json;
   a "NO LCSC MODEL" crop is "unclear"; on a "NO MODEL, A1 from footprint"
   crop, judge the orange dot on the cyan pin 1 ("match" there still isn't a
   pass, see `cpl_footprint_basis` below); a "FETCH FAILED" crop (script
   exits 1, index.json note) is "unclear" too, and the fix is to rerun
   cpl_render in a few minutes, not to treat it as no model. Do not edit
   its verdicts -
   dfm_check merges them in the next step.
   The merge matches verdicts by designator only, with no digest of the part
   behind them: after you swap, move, rotate or reshape a part, cpl_visual.json
   still holds the old verdict for that ref and the strict gate passes on it.
   Rerun cpl_render and the image pass (at least for the changed refs) after
   any such change.
4. Gate: `scripts/gate.py --gate dfm kicad/<board>.kicad_pcb` - runs
   dfm_check on a scratch export: copper (trace/clearance/edge), drill
   (size/spacing/annular), mask/silk, release completeness, and **CPL
   polarity vs the schematic** - the ONLY catcher for a polarized part
   rotated with its nets swapped (net-level parity is blind to it), and
   **CPL placement per part** (`scripts/cpl_verify.py` inside dfm_check,
   on the fab dir's CPL.csv): pin 1 of each part's LCSC model must land on
   the board's pad 1, and a diode/LED/polarised cap's K/+ on the board's.
   A crop titled "judge K/+, not pin 1" is a polar part whose model numbers its
   pins the other way from the board: judge the K or + labels, and mark `pin1`
   as `match` when they agree, since the red dot sits on the other pad.
   Bottom parts are fitted mirrored and read from below, and the images draw
   them that way (title "BOTTOM, from below"). That rule isn't JLC's own
   (`reference/jlc_bottom_cpl.md`), so `cpl_bottom_preview` stays as a warning:
   report it, and have those parts checked in JLC's order preview.
   A ball grid with no LCSC model is checked against its footprint's own A1
   corner (`reference/jlc_bga_cpl.md`). A turned row is `cpl_rotation`. A match
   is the warning `cpl_footprint_basis`, because JLC's zero is the part's tape
   orientation and it publishes no BGA rule: report it, and give the part an
   LCSC number or have A1 checked in JLC's preview against the part's tape spec.
   JLC places by the silkscreen when it disagrees with the CPL. On ball grids
   and U/IC parts, a pin-1 dot or chamfer off pin 1's corner is the error
   `cpl_silk_mark`: fix the footprint's mark, not the CPL. No mark at all is the
   warning `cpl_no_silk_mark`, because JLC emails to confirm: report it.
   `both_sides_needs_panel` warns that parts on both sides need Standard PCBA's
   rails, fiducials and 70 x 70 mm, on the board or on a panel.
   A wrong part (`cpl_rotation`), a part with no model or no fit
   (`cpl_no_model`), parts whose model fetch failed (one
   `cpl_fetch_failed` for the run, with the reason - rerun later, it is not
   a pin-1 result), and a part the script and
   `fab/cpl_visual.json` disagree on or the image pass left out
   (`cpl_visual_disagree`) are ERRORS; `placement.parts` in the report puts
   both verdicts side by side. With no cpl_visual.json the release gate
   (strict) refuses, because the image pass is required. Fix a wrong
   rotation by re-running bom_cpl (it derives from the model), never by
   editing CPL.csv. Errors
   fail; advisory classes (0.12 mm stock silk, tight mask dams, a placed part
   sourced off LCSC) are warnings - list them, do not silence them. A placed
   part with NO source at all, a `smt_placed` part with no placement, a
   populate quantity the classes contradict, and a shipped BOM/CPL that lists
   a part the classes exclude are ERRORS.
   IPC-D-356 gives a refdes 6 characters. On a panel, a copy suffix that
   takes a ref past 6 (U201_B1, SW201B1) is cut, the cut ref matches no pad,
   and `dfm_net_short` reports shorts that aren't in the copper. Keep
   every panel ref at 6 characters or fewer. Net names keep their last 14
   characters, so put a copy tag at the end of a net name, not the front.
   A `dfm_net_short` with `tie` set is a net tie the IPC-D-356 still names
   apart: re-export the package with fab_export, which gives a tie one
   name. `dfm_net_tie_open` is a tie whose bridge the package's copper
   lacks: re-export, and if `check_net_tie` fails, put the bridge back.
   At release, attest re-judges `reports/dfm_release.json` with the
   board's `reports/verify-waivers.json` only (`releaselib.waivers_for_input`).
   A dfm waiver kept in another file and passed with `--waivers` clears the
   gate run but not attest: put dfm waivers in verify-waivers.json.
5. On gate failure: report; the orchestrator dispatches fixers (do not fix
   routing/placement yourself).

## The semi-manual second opinion (no public API - human step)
Prepare the instruction for checkpoint 5's `human_steps`: upload
`fab/<board>_gerbers.zip` to jlcdfm.com (and the JLC order viewer) and
eyeball: rendering sane, no missing layers, CPL preview shows polarized
parts (LEDs/diodes/electrolytics) oriented correctly.

## Traps found on boards (2026-08 to 2026-10 runs)

- `gate.py --gate dfm` is non-strict. A leg that cannot run (`coverage.skipped_error: {"bom": "no parts.json"}`) is skipped and the gate still says `pass`. After P5 the board's own directory must hold `parts.json` beside the board, not only `constraints.json` and `decoupling.json`; read `coverage.skipped_error` and treat a skipped BOM/assembly leg as a failure.

## Output contract (end your final message with exactly this block)
FILES: fab/ contents + zip (with sha256s)
GATE: dfm: <pass/fail, errors/warnings>; bom_complete: <true/false,
  missing refs>
SUMMARY: <up to 10 lines: package contents, rotation corrections applied
  (model vs table), script/image placement disagreements,
  warnings worth human eyes>
OPEN: <human upload steps + anything unresolved, or "none">
