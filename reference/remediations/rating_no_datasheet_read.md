# rating_no_datasheet_read

These parts' voltage ratings were compared with the board's rails and held, but the number came from
the distributor's listing in parts.json, a capacitor's part-number voltage code or Value field
("220u/16V"), or a generic package table. No datasheet was read, so their
ledger facts are `listed`, not `verified`, and `rated_from` says which source it was. A warning: a
listed fact doesn't block release (`lib/ledgerlib.py` says why), and attest names them in its result.

- Emitted by: scripts/check_ratings.py (kind="rating_no_datasheet_read", warning, one per board)
  Gate: verify (P8, via verify_all.py).
- Fields: refs [every part whose rating fact is listed].

## Is it real?
Yes, as a statement about the evidence. It doesn't say the number is wrong: a listing can be wrong,
and until a datasheet is read nothing here would notice.

## Fix ladder (cheapest first)
1. `bin/pcb-flow datasheet_extract --fetch <ws>` downloads and judges each PDF.
2. Extract the ones that matter most (datasheet-extractor agent, `--pdf FILE`). An extracted part's
   pins are then compared with its datasheet, and its fact reads `verified`.
3. A part you read yourself: `bin/pcb-flow ledger set --workspace <ws> --subject R1 --kind rating
   --status verified --source "datasheet, page"`.
4. A part with no number at all is a different finding (rating_unrated, rating_no_attributes):
   `parts_enrich.py --workspace <ws>` fills its attributes, which takes it from unknown to listed.

## Do not
- Do not set `verified` on the strength of the listing. `listed` is what that evidence is worth.

## Verify
```
bin/pcb-flow check_ratings --pcb <ws>/kicad/<board>.kicad_pcb --constraints <ws>/kicad/constraints.json
```
