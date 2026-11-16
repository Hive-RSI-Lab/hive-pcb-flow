# rating_datasheet_unassessed

A part's datasheet can't be judged here: its index.json entry has no PDF on disk (they aren't
committed), there is a PDF but no MPN in index.json or parts.json to check it against, or the PDF
has nothing to check it by (index.json status `not_judged`): no text layer (a scan or image-only
file), or pages 1-2 that print no part number at all, this one or another, and no ordering table
that decodes it (an approval sheet naming only "0603-0.6 green"). So nobody knows it's
that part's datasheet. A warning, not a pass. With no PDF or no MPN the facts stay as they were:
refusing them would block every fetched board for lack of a file nobody kept. Facts read from a
`not_judged` PDF are held back as from a suspect one (unknown, never verified): the file is there,
and nothing in it could be checked. Other PDFs on disk don't land here: check_ratings and the ledger
judge them when they read the index (#768).

- Emitted by: scripts/check_ratings.py (kind="rating_datasheet_unassessed", warning, one per board)
  Gate: verify (P8, via verify_all.py).
- Fields: refs [every part affected]; `unassessed_datasheets` in the report lists the LCSC codes.

## Fix ladder (cheapest first)
1. No PDF: `bin/pcb-flow datasheet_extract --fetch <ws>` downloads and assesses.
2. No MPN: give the parts.json line its MPN; the next read judges the PDF.
3. No text layer (`not_judged`; 4 of 43 on fmcw-radar): find the same document with text (the
   maker's site, not the distributor's copy) and put it at parts/datasheets/<lcsc>.pdf; the next
   read judges it. If there is none, read the pages with
   `bin/pcb-flow datasheet_extract --pdf <file> --render <dir>`, and if it is that part's, record the
   fact and why with `bin/pcb-flow ledger set ... --force "why"`; the warning stays.
4. MPN not printed (`not_judged`; KT-0603 approval sheets and a TXC 7X brochure, 3 of 43 on
   fmcw-radar): get the maker's document that prints the part number or its ordering table. If
   there is none, read it, and if it is that part's, record it as in step 3. It can be another
   series' sheet: LCSC serves Fenghua's 2 kV 1206B102K202NT (C9196) with the general-series sheet,
   whose table stops at 50 V and which prints only a code scheme. The check can't tell that from
   the right sheet (#1434), so compare the rating you need against the sheet's own table.
5. Suspect after that: rating_datasheet_suspect says why.

## Do not
- Do not write `"status": "ok"` into index.json by hand.

## Verify
```
bin/pcb-flow check_ratings --pcb <ws>/kicad/<board>.kicad_pcb --constraints <ws>/kicad/constraints.json
```
