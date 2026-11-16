# Extraction library

pcb-flow's own datasheet extractions, one file per LCSC part: `<LCSC>.json`, in the
same schema as a board's `parts/<lcsc>.json` (`datasheet_extract.py`,
`DATASHEET_SCHEMA`).

`check_ratings` uses the board's own `parts/<lcsc>.json` when it has one.
Otherwise it falls back to the entry here, so a common part is judged on
every board that places it instead of being reported `rating_unrated`. The
board's own extraction always wins, because it may describe a later revision.

## Rules for an entry

Fact, not fiction: every number comes from the datasheet the part's maker
publishes for that exact LCSC code. A distributor listing, another vendor's
datasheet for the same MPN, or memory does not count.

- `lcsc` is the file's name. `pinout[].pin` is the pad number the KiCad
  footprint uses, and `name` is the datasheet's pin name.
- Every pin that is connected and is not ground or nc needs at least one
  `pin_ratings` row of `kind: "voltage"`. The exception is a pin used only as
  the `ref` of another row (an emitter, a source, an anode).
- Every row's `source` cites the document, its revision, `p.N` and the table,
  and quotes the line: `"JSCJ S8050 SOT-23 datasheet Rev 2.0 p.1 Maximum
  Ratings: 'VCEO Collector-Emitter Voltage 25 V'"`.
- `source_pdf` gives the maker, title, revision, page count, the page of the
  pin map, the URL, and `sha256 <hex>` of the copy that was read.
- Bounds relative to another pin use `ref`: a BJT's VCEO is `max` on C with
  `ref: "E"`, and a P-FET's VDS is `min` on D with `ref: "S"`. A
  two-terminal part (a capacitor) is rated on pin `1` with `ref: "2"` and
  `min: -V, max: V`; check_ratings reads the row backwards for pin 2.
- An output pin powered from one of several supplies names it in
  `supply` (a driver's SDO fed from `VSDO`, not `VM`): the net it drives is
  then bounded by that supply's rail, not by the part's highest one.
- If a pin has no rating in the PDF, leave it unrated: it is better reported
  as unrated than given a number nobody read. Connectors and switches use
  the contact rating printed for every contact.

PDFs are not committed (copyright). The URL and sha256 let anyone fetch the
same copy and re-check it.
`tests/test_check_ratings.py::test_library_entry_is_valid_and_cites_its_datasheet`
checks every entry for schema, LCSC, URL, sha256 and page citations.

## Batch readings (not in this folder)

`scripts/abs_max_batch.py` reads the absolute-maximum ratings of a board
set's ICs in bulk (#1339), one model reading per datasheet that a fixed
check then trims to what the PDF text shows. Its files go in the corpus
cache (`<cache>/abs-max/<set>/ext/`), never here, and check_ratings reads
them only when it is given `--extractions`: this library and a board's own
`parts/` both win over them. They follow the rules above except that a pin
the reader was not sure of is left unrated, and a supply row that names no
pin rates the part's single power input. A part whose listing names no
datasheet, or one the fetcher refuses, is read from the maker's datasheet
when `reference/datasheet_urls.yaml` names its MPN (rules for an entry are
at the top of that file); failing that, when the listing names none,
`scripts/lib/dsresolve.py` finds one by maker (`reference/datasheet_makers.yaml`:
the maker's URL pattern, else LCSC's copy of the maker's PDF), takes it
only when its first pages name the part, and names a miss (maker not
known, refused, not found) rather than guess. An exposed pad goes in
`exposed_pad`, not the pinout, so a board's pad one past the last pin is
that pad.

`scripts/land_batch.py` reads the pad numbering of the same ICs from their
datasheets' pin drawings (#1288), with the same listings and PDFs and the
same one-reading-then-fixed-check shape. Its records go in
`<cache>/land/<set>/ext/`, never here, and fp_verify reads them only through
`--parts` (errata_corpus.py's fp_verify runner passes that folder when it
exists). A record states the numbering direction (`land_pattern.numbering:
ccw_top`), and the pad places too only when the dimension page's text prints
the pitch and spans; a part whose drawing, view or pinout the check can't
confirm is left out, so fp_verify leaves it not judged.
