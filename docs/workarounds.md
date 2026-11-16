# Workarounds

Every place where pcb-flow gets a board through its gates only because something
worked around a defect in the module's scripts, by a shim in `bin/pcb-flow` or by an edit
made by hand in a board's files. The test (`bin/pcb-flow test`, and
`bin/pcb-flow check-all` after its boards) holds it to the truth. The score
(`bin/pcb-flow score`, [score.md](score.md)) doesn't read it: it runs the gates
with every shim off and charges what they find.

- Each row's id has a probe, `tests/workarounds/<id>.py`, that exits 0 while
  the cause still stands and 10 once it is fixed. The test runs every probe. A
  probe whose cause stands with no row here fails the test, so removing a row
  without fixing the cause fails it. A row whose probe says it's fixed fails
  the test too: take the workaround out (the shim, or redo the board's step
  without the hand edit) and remove the row.
- A shim in `bin/pcb-flow` runs only while its row is here (`wa <id>`), so
  delisting it also turns it off for the gates.

`steps` are the gate steps whose input the workaround made or changed, out of
the five `check-all` runs per board: `erc`, `drc_routed`, `verify`, `fab`
(gerbers, drill, BOM and CPL) and `dfm`. `-` means none of them (the
workaround serves a stage before the gates, or renders). `boards` is `*` for
every board, or a comma-separated list: boards under `boards/`, or the
product boards built in other projects that [boards.md](boards.md) lists
(their `where` paths are in that project's board). When a fix lands somewhere other
than where its probe looks, the fix updates the probe to look where the
cause now is.

| id | kind | boards | steps | where | cause | brief |
|---|---|---|---|---|---|---|
| power-neckdown | hand | lite-main, core | drc_routed | the boards' `kicad/constraints.json` (`power`: plane-fed rails and amp outputs at the current of their taps) | `rules_gen` gives a power net one width on every track: a 5 A rail's 2.38 mm minimum also binds the stub into a 0.22 mm DFN pin, which no track meets; the currents were declared down by hand to fit the pins | 261028-181236 |
| via-hole-floor | shim | lite-main, core | drc_routed, dfm | core `kicad/gen/pro_vias.py` (0.7/0.3 mm vias in every class); lite-main re-ran the route and nudged the last vias with route_edit by hand | Freerouting spaces vias by copper clearance only, and `rules_gen`'s classes (0.6/0.3 mm at 0.127 mm) put drill edges 0.43 mm apart, under JLC's 0.5 mm hole-to-hole | 261028-181236 |
| krt-finish-width | shim | core | drc_routed | core `kicad/gen/post_route.py` (deletes the finish pass's under-width power tracks) | `route_auto._krt_finish` hands KRT the open nets with no track width (KRT has `--track-width`, `--power-nets-widths`), so power nets come back at KRT's default, under their width rule | 261028-181236 |
