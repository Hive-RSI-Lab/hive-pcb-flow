# rating_datasheet_suspect

The PDF on disk for this part isn't shown to be its datasheet, so the ledger facts read from it (the
extraction's `source_pdf` says which PDF that was) are unknown, never verified, and `ledger.py set`
refuses to rest on it. A PDF with nothing to judge it by is not here: a scan with no text layer, or
one whose pages 1-2 print no part number at all (an approval sheet that names only "0603 green").
It is `not_judged` and lands in rating_datasheet_unassessed, with its facts held back the same way.
The 7 suspect files #768 counted on fmcw-radar were all of that kind. A part with no extraction read
nothing from it: its rating came from the distributor or a package table and stays `listed` (#781).
check_ratings and the ledger judge each PDF on disk when they read index.json, so nobody has to ask
for the verdict (#768). The reason in the message says which case below it is.

- Emitted by: scripts/check_ratings.py (kind="rating_datasheet_suspect", warning, one per part)
  Gate: verify (P8, via verify_all.py).
- Fields: refs [the part's refdes]; `suspect_datasheets` in the report maps LCSC code to the reason.

## Is it real?
Usually the file is fine and the check can't tell, or the file is another part's. Open it before you
do anything: the check reads text, so it can't tell these apart.
- "is not in the text", on a series document that never names the series: passives are often sold
  from a document that prints a scheme, not the part number, and this one has no name to tie it to
  the part, while its first pages name some other part number. It may be another maker's part.
- "is not in the text", with no ordering table: the document prints electrical values and some
  part number, with nothing that decodes the MPN.
- "truncated", "does not parse" or "host placeholder": the download is broken. Never the right
  document.

## Fix ladder (cheapest first)
1. Truncated, unparsable or placeholder: `bin/pcb-flow datasheet_extract --fetch <ws> --lcsc <code>` again.
2. Series document that never names the series: get the maker's document that does. With a real PDF
   in hand that has an ordering table, add a scheme to reference/ordering_codes.yaml with a test, and
   the MPN decodes against it.
3. No ordering table: look for the maker's ordering or part-numbering page and use that PDF, or
   step 2 if it is a scheme many parts share.
4. You read it yourself and it is that part's: record the fact and why, in the ledger:
   `bin/pcb-flow ledger set --workspace <ws> --subject <ref> --kind rating --status verified
   --source <page> --force "why"`.
   The warning stays. It says the check could not tie the file to the part, which is still so.
5. It is another part's document: delete the PDF, and `--fetch` or find the right one.

## Do not
- Do not write `"status": "ok"` into index.json by hand: the next read judges the PDF again only if
  its stamp changed, so a hand edit would hide the doubt.
- Do not add a scheme to reference/ordering_codes.yaml to quiet one part without a real PDF showing
  it: the scheme then passes every document that matches it.
- Do not use `--force` without reading the page the fact comes from.

## Verify
```
bin/pcb-flow check_ratings --pcb <ws>/kicad/<board>.kicad_pcb --constraints <ws>/kicad/constraints.json
```
The part's warning is gone (or, after step 4, stays with the reason on record in the ledger).

## Sources
- GitHub issues #706 (status recorded), #755 (ordering tables), #768 (assessed on read; the 7 cases on
  fmcw-radar), #1434 (scans and sheets that print no part number are not_judged).
