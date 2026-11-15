# sense_line_unmarked

`check_kelvin` found a sense line from the netlist (#1077): a shunt's Kelvin pad
wired to an amplifier's or ADC's input pin, on a power net. The line is a thin
track, as a sense line should be, but no `aiee_sensew_*` rule area covers it. The
`aiee_sense_width_<net>` rule from `rules_gen` lets a power net's track run at the
fab's minimum only inside that mark, so DRC holds this line to the rail's width
and fails it (or someone widens it to pass, which ties up the shunt's pad).

- **Fix:** run `scripts/sense_mark.py --pcb board.kicad_pcb` to mark every clean
  sense line (declared in `power[].sense_pads` or found from the netlist), then
  re-run `rules_gen` so the rule covers the net. `route_critical` marks the lines
  it lays itself; this is for lines routed by hand.
- **The line joins rail copper before the pin?** `sense_mark` refuses it
  (`sense_mark_refused`): route it on a track of its own from the shunt pad first.
- **Not a sense line?** If the pin is not a measurement input, the part row's
  input pin names are wrong: fix the pin classes, don't widen the track.
