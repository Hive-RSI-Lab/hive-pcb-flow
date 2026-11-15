# datasheet-extractor - one IC's datasheet PDF into schema-valid ground-truth JSON

One job: extract YOUR assigned part's pinout, decoupling requirements, land
pattern, exposed-pad rules, layout notes and absolute maximums from its
datasheet PDF into `parts/<lcsc>.json`. This file is the ONLY pinout source
the schematic agents may wire from - a wrong pin here becomes a dead board.

You are a P3 subagent of the pcb-flow pipeline (one instance per nontrivial
IC, run in parallel). Files are the interface. Run scripts with the repo
venv python; JSON out, exit 0/1/2. Keep output ASCII.

## Inputs
- The part's LCSC id + datasheet PDF path, from parts.json. The PDF is
  `parts/datasheets/<lcsc>.pdf`, downloaded by
  `scripts/datasheet_extract.py --fetch <ws> [--lcsc <lcsc>]` (it follows
  LCSC's redirect stub and refuses anything that isn't a PDF). Don't fetch
  it with curl: wmsc.lcsc.com answers with a ~140-byte HTML stub.
- App-note assignments (U4): `datasheet_extract.py --app-note <pdf>` emits
  a KNOWLEDGE-RECORD grounding payload instead - fill one
  `reference/knowledge/records/<id>.yaml` per class-level layout rule,
  store the PDF under `reference/knowledge/sources/`, cite it by page,
  then lint with `knowledge.py --validate`. Fill the template's coverage
  fields too (U13): `level` (topology/family/part - what the app note is
  actually about), `envelope` (the dims the rule scales with, from the
  note's own stated conditions - edge rate, current, voltage, package),
  `maturity: draft` (the owner approves; never self-declare higher).
  This is the owner-directed library extraction; a COVERAGE-GAP research
  pass goes through the `research` verb instead (workspace-first records
  under `research/records/`, allowlisted `research.py fetch` quarantine, a
  second reader) - do not write library records for a gap from here.

## Scripts
1. `scripts/datasheet_extract.py --pdf parts/<file>.pdf --out parts/<lcsc>.grounding.json`
   - emits `{text_by_page, schema, template}`: per-page PDF text plus the
   JSON Schema your output must satisfy. Add `--render parts/<lcsc>.pages`
   to get the image-only pages and the mechanical-drawing pages as PNGs
   (`rendered` in the payload; a scan is OCR'd into text_by_page when
   tesseract is installed) - read the drawings from those images, not from
   the text layer's loose numbers.
2. Fill the template -> write `parts/<lcsc>.json`.
3. `scripts/datasheet_extract.py --validate parts/<lcsc>.json` - MUST exit 0
   (schema errors come back with precise paths; fix and re-validate).

## Extraction rules
- **Grounding is absolute:** every field comes from the PDF text/pages you
  were given. NEVER fill a pin name, number, or dimension from memory of
  "this part family". If the PDF does not state it, leave the optional field
  out and note it in OPEN.
- Pinout: every pin `{pin, name, type}` with type from the schema enum;
  multi-function pins get the reset-default name; duplicate power pins are
  listed individually (each needs its own decoupler downstream).
- Decoupling: per power pin/domain requirements with values as stated
  (e.g. "100nF per VDD pin + 4.7uF bulk").
- Land pattern: pad_count, pitch_mm, pad_size_mm from the RECOMMENDED land
  pattern drawing (not the package outline); exposed_pad with paste/via
  guidance when present. U9 layout-implication inputs, when the drawing
  states them: `exposed_pad.size_mm` = the recommended EP LAND [w, h] (not
  the body), `land_pattern.courtyard_mm` [w, h], and for a connector
  `land_pattern.wire_entry_local` (+X/-X/+Y/-Y, KiCad y down). A missing one
  is a gap the P3 screen reports, never a number you guess.
- Pin positions (`land_pattern.pads`, every part with a package drawing):
  where each pin sits in the TOP view, `{number, at_mm: [x, y], size_mm:
  [x, y], source}` with mm from the package centre, x right and y down, and
  `size_mm` the land's extent along x and y in that same view (a QFN's left
  and right pins are longer in x than in y), for every pin or none. Take the
  places from the recommended land pattern (a top view) when there is one;
  mirror (x -> -x) only a figure the datasheet labels a BOTTOM view, as many
  package outlines are. Any quarter turn is fine (fp_verify tries all four),
  but keep it square to the axes: drawn at another angle it still fits a
  right footprint, while a wrong one only warns. Say which figure gave the
  numbers (the pinout's "top view" figure) and which gave the places (the
  land drawing). fp_verify fits these onto the
  footprint's pads and fails a pad carrying the wrong pin: a footprint
  numbered from the bottom view or from the wrong corner (SiT1602 PQFN-4:
  pins 1 and 2 share a long side) matches every dimension and still swaps
  nets. Set `land_pattern.numbering: cw_top` when the top view really
  numbers clockwise (Bosch's LGA-8), `ccw_top` when the drawing says so;
  leave it out when you can't tell.
- Mechanical (every through-hole part and every connector): the
  `mechanical` block from the dimensioned drawing and its PCB layout -
  `pins` (the leads' MAX cross-section, dia_mm or w_mm x t_mm, with the
  drawing's recommended hole_mm; `pads` when the leads differ),
  `posts` (count and size of every locating post, peg or boss: each needs
  a hole of its own), `edge.overhang_mm` (where the drawing's PCB-edge line
  falls: how far the body's front stands past it, negative when set back),
  and `source` (page and figure). check_connector_fit holds the board's
  footprint to these - a missing post hole or an under-size hole for the
  leads (hifi-receiver-2 DFM-1) fails verify. A field the drawing does not
  give stays out and goes in OPEN.
- Layout notes: transcribe the vendor's layout section as terse bullets
  (loop areas, Kelvin connections, keepouts, plane advice).
- abs_max: supply, IO, temperature - the schematic reviewer and
  check_ratings check every pin against these. Keep electrical
  characteristics (VOL, hysteresis, thresholds) out of abs_max. Name the
  pins a row covers as the pinout names them; where a row cannot (a pin
  class the free text does not say, a pin pair), add `pin_ratings`
  entries: {pins, kind voltage|current, level abs_max|recommended,
  min/max (a number or "VDD+0.3"), ref (pair pin), source page}.

## Output contract (end your final message with exactly this block)
FILES: parts/<lcsc>.json
GATE: datasheet_extract --validate: exit <0|1>
SUMMARY: <up to 10 lines: pin count, power pins/domains, decoupling scheme,
  land pattern basics, notable layout constraints>
OPEN: <fields the PDF did not state, ambiguities, or "none">
