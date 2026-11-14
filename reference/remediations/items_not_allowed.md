# items_not_allowed

A copper item sits where a `disallow` DRC rule forbids it. In this pipeline the only generator of
`disallow` rules is `rules_gen.py`'s `aiee_diff_outer_only_<pair>` family (T6 P5-4, the V12 guard):
any TRACK segment of an impedance-solved diff pair on an inner layer that is NOT stripline, i.e.
lacks a declared pour (constraints `planes`) on the copper above or below. A stripline layer gets
solved widths instead (`aiee_diff_w_<pair>_<layer>`, a track_width violation when the run is off
its width; #323), so this rule names a layer with nothing to size the pair against. The rule is
`aiee_diff_outer_only_<pair>` (every inner layer) or `aiee_diff_outer_only_<pair>_<layer>` (just
the non-stripline ones, when the board has a stripline layer). Single-ended impedance nets
(constraints `high_speed` with `impedance_ohm`) get the same guard per impedance,
`aiee_z<Z>_outer_only` / `aiee_z<Z>_outer_only_<layer>` (#1108). route_auto writes both families
into the DSN as each net's class `use_layer`, so Freerouting should not draw these; a hit means a
hand edit, KRT, or a condition the DSN reader skipped.

- Emitted by: kicad-cli DRC (`<board>.kicad_dru` auto-loads, no flag), normalized by `kc.py`
  `normalize_violation`. Msg: `Items not allowed (rule 'aiee_diff_outer_only_<pair>')`.
  Gate: `drc` (P6, errors only), `drc_routed` (P7).
- Fixer domain: router   Scripts you may use: route_edit.py, route_critical.py, kc.py, render.py
- Fields on the violation: `pos` [x,y] mm, `layer` (an inner Cu layer), `net` (one of the pair),
  `items[0].uuid` (the offending segment - act on this).

## Is it real?

- Almost always: the router (or a hand edit) dropped an impedance-controlled net onto In1/In2.
  Layer TRANSITIONS are legal - vias are not tracks and never fire this rule - so what is flagged
  is specifically inner-layer track length, exactly the geometry the solver never solved.
- A rule named anything OTHER than `aiee_diff_outer_only_*` / `aiee_z*_outer_only*` means someone hand-authored a disallow
  rule; read the DRU comment beside it before touching anything.
- Not real only if the net was WRONGLY paired (rules_gen JSON `diff_pairs` lists the detected
  pairs; a suffix-matched false pair means fix constraints `diff_pairs`, then regenerate).

## Fix ladder (cheapest first)

1. Reroute the flagged segments onto F.Cu/B.Cu or a stripline layer (rules_gen JSON
   `stripline_layers`; the pair's `layers` facts give its width/gap there): for a short stub, one route_edit ops file
   (`remove` the inner segment uuids, `add_track` the outer replacement at the pair's solved
   width from the rules_gen JSON `diff_pairs` facts). For a full pair, route_critical.py
   re-routes from constraints `diff_pairs`.
2. If the layer should be stripline, declare its neighbours' pours in constraints `planes` and
   re-run rules_gen. If the escape genuinely needs a non-stripline inner layer: that is a
   CONSCIOUS waiver.
   Hand-solve the stripline geometry against the fab's impedance calculator (JLC's, for the
   exact entry in `reference/stackups.yaml` - its epsilon_r values are ASSUMED, see the header),
   record width/gap + source in the workspace log, apply with route_edit.py, and only then
   delete that one `aiee_*_outer_only*` rule from the `.kicad_dru`, with a comment
   naming the log entry. Never delete the rule first.

## Do not

- Do not delete or blanket-waive the rule to "make DRC pass" - the violation is the only thing
  standing between an unsolved inner-layer geometry and a shipped board.
- Do not hand-solve a layer rules_gen can solve: declare the pours and regenerate.
- Do not hand-edit the DRU beyond the single conscious rule deletion in ladder step 2; then
  `rules_gen.py --check-dru` must still pass (the aiee_* floors survive your edit).

## Verify

- Re-run the drc gate (`gate.py drc`): zero `items_not_allowed`, and no NEW clearance /
  diff_pair_gap violations at the rerouted segments.
- `rules_gen.py --check-dru <board>.kicad_dru --layers N --stackup NAME` exits 0.
- check_diffpair (P8) still passes: skew / uncoupled budgets survived the reroute.

## Sources

- PROGRESS.md S8 entry item 4 (V12: outer microstrip only); hive-pcb-flow #323 (stripline layers).
- `reference/stackups.yaml` header (assumed epsilon_r; confirm against the fab calculator).
- rules_gen.py `diff_pair_rules` (T6 P5-4 comment block: rationale + waiver path), `impedance_rules` (#1108).
