# sense_mark_refused

`sense_mark.py` would not mark a sense line because it is not clean: the walk from
its pad joins other copper on the rail (a pour, another pad, a power track or via)
before it reaches the pad it reads, or never reaches it on copper of its own. A
mark there would let rail copper run thin. An error for a line declared in
`power[].sense_pads`; a warning for one found from the netlist, which only
suggests it.

- **Fix:** route the line from the sense pad to the pad it reads on a track that
  carries nothing else, and keep the rail's pour off it; `check_kelvin`'s
  `sense_tap_off_pad` says where it joins. Then run `sense_mark.py` again.
- **Found line that is not a sense line?** Ignore the warning, or fix the reader's
  input pin classes so it is not taken for one.
