# move - relocate a footprint (or its label)

Two variants, and the difference is ceremony: moving a FOOTPRINT is `move_fp`
(hold 1, five gates); moving a refdes/value LABEL is `silk_edit` (hold 0, three
gates). The router picks the silk variant when the task names refdes / silk /
label / text.

## Footprint moves

`place_edit.py` is the only writer. Ops are ABSOLUTE and idempotent:

    {"version":1,"ops":[{"op":"move","ref":"C12","x":141.2,"y":98.5}]}
    {"op":"rotate","ref":"U1","deg":90} | {"op":"flip","ref":"J2","side":"back"}
    {"op":"lock","ref":"J1","locked":true}
    {"op":"swap_fp","ref":"Y301","footprint":"lib/aiee.pretty/X.kicad_mod","fpid":"aiee:X"}
    {"op":"renumber","ref":"R6","pads":[{"at":[26.25,50.3],"number":"2"}, ...]}

`swap_fp` re-pads a placed part with a fixed library footprint (the nets follow
the pad numbers); `renumber` gives the pads at board positions new numbers.
`flip`, when it changes the side, mirrors the part top to bottom about its
position, not left to right:
each pad's local y and the part's angle are negated (-90 becomes 90), local x
is kept. Follow it with `rotate` to set the angle you want.

It validates, edits a scratch copy, re-parses to verify every op landed
(1e-3 mm / 0.05 deg), and only then swaps the file in. Any failure leaves the
board byte-identical. KiCad regenerates UUIDs on save, so compare parsed
positions, never file hashes.

## The two things place_edit does not do for you

1. **Old copper stays put.** `--allow-routed` is required on a routed board and
   it means exactly what it says: the tracks that used to reach those pads are
   still where they were. On a routed board give the same ops to
   `part_repair.py` instead: it rips the copper the move spoils in a window
   round each part, makes the move, routes every net back to as few islands as
   it had, and keeps the result only if DRC got no worse and no violation is
   new near the move (exit 1 and nothing written otherwise). A new via on a
   net whose removed vias had teardrops (ripped, pushed, or pruned before or
   after the refill) takes the nearest one's settings (route_edit's add_via
   `teardrops`, carried in its route file). The joins lay a net at the widest
   width ripped from it, so a track they end on such a via can be too wide
   for its drop: it narrows from the via to just past where the drop ends,
   to a width the drop takes, the net's floor allows and the track does not
   graze the via at (Necks in the report). After the refill the run counts,
   per layer, the tracks into each via that a drop answers. It fails when a
   new via has fewer than the via it took them from had, as far as the
   tracks into it now allow (lost when answered `a1 < min(a0, n1)`), and
   the same for a via it kept in the repair area with teardrops on, against
   its own count before; the report names each track left without a drop
   and why (too wide for the via, grazing its copper, or none made). A
   track with no drop where none was lost (short) is only a note. A track
   that crosses a via's copper off the centre is not asked for a drop:
   KiCad makes only a sliver there. A via the run removes is not checked,
   nor are pads; one with teardrops on that passed them to no new via is a
   note. DRC has no check for a missing drop.
   With bare place_edit, re-route the affected nets (`reroute-net`) before
   believing `drc_routed`. Unconnected items in DRC are the honest signal.
2. **Neighbour silk does not move.** The footprint's own silk travels with it;
   the label you slid out of its way three edits ago does not. `check_silk`
   (verify, P8) catches the overlap, DRC does not.

## Zones

Any copper-adjacent edit stales the pours. The only headless filler at this pin
is `kicad-cli pcb drc --refill-zones --save-board` (kc.py `drc --refill --save-board`);
`drc_routed` refuses stale fills outright rather than grading phantom clearance
errors.

## Where to move it TO

The router does not choose coordinates - that is placement judgment. Cheapest
sources, in order: the violation's own coordinates (a decoupler that is 21 mm
from its pin wants to be at the pin), `place_metrics.py` for legality after the
move, and `placement` (the agent) when the move cascades into its neighbours.

## Do not

- Do not move a locked footprint without saying so - a lock is usually a
  mechanical constraint someone paid for.
- Do not batch a move with a re-route in one snapshot. Snapshot before the move;
  the re-route is its own step with its own gate. part_repair is the one
  exception: it gates the pair itself, and its op files (rip, place, route)
  still replay one at a time.
