# rating_over_clamp

A part on a rail that a unidirectional TVS holds is rated under what that TVS lets the rail reach. A
warning (#500): the rail's nominal is within the part's rating, but a TVS does not conduct until its
breakdown voltage, and in a surge it holds the rail at its clamping voltage, both well above the
standoff it was picked by. A 58 V TVS (SMCJ58A) on a 56 V bus lets it reach 71.2 V with no surge
current and 93 V in one, so a 63 V capacitor or an 80 V gate-driver supply pin on that bus is unguarded.

- Emitted by: scripts/check_ratings.py (kind="rating_over_clamp", warning, one per part or pin)
  Gate: verify (P8, via verify_all.py).
- Fields: refs, net, pin (an extracted part's pin, else null), net_v (what the rail reaches, from
  the part's reference pin), limit_v (the part's rating), rating (where that came from), level
  (`breakdown`: the TVS's V(BR) max, no surge current; `clamp`: its VC at rated peak pulse current),
  clamp (the TVS refs, or `constraints`).
- The rail's clamp: the cathode-on-rail, anode-on-ground TVS with the lowest numbers, read from its
  extraction's V(BR)/VC rows, its listing's clamping voltage (never LCSC's "Voltage - Breakdown",
  which is the range's bottom), else its part number's family in reference/diode_vr.yaml. A
  constraints `voltages` entry's `clamp_v` (and `breakdown_v`) states it outright and wins. Facts
  `clamped_rails` lists each rail, its clamp, and every part judged on it.

## Is it real?
Check the TVS's datasheet row for its exact part number: V(BR) max at IT and VC at IPP. A clamp level
hit needs a surge that reaches the TVS's rated peak pulse current; if the rail's worst surge is
known to be smaller, VC at that current is lower (the datasheet's VC-vs-IPP curve), and stating it as
`clamp_v` in constraints is the honest fix. A breakdown level hit is real on any board: the TVS
lets the rail sit there with no current at all.

## Fix ladder (cheapest first)
1. A part rated higher in the same footprint (a 100 V capacitor, a 100 V-rated driver variant).
2. A TVS with a lower breakdown and clamp, still with its standoff above the rail's highest normal
   voltage (rating_over_voltage judges that side).
3. A worst surge known to be smaller than the TVS's rating: state `clamp_v` (and `breakdown_v`)
   on the rail in constraints, with the source in the entry's note.

## Do not
- Do not lower the rail's stated voltage: the TVS's numbers do not depend on it.
- Do not treat a breakdown level hit as surge-only: it holds with no current.

## Verify
```
bin/pcb-flow check_ratings --pcb <ws>/kicad/<board>.kicad_pcb --parts <ws>/parts --constraints <ws>/kicad/constraints.json
```
