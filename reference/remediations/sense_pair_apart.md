# sense_pair_apart

`check_kelvin` walked a shunt's two sense lines into an amplifier's or ADC's input
pair (#1077) and found the loop between them larger than a coupled pair would
make: more than `PAIR_FACTOR` (3) input-pin pitches apart over the run, plus a
45 degree fan-in at each end. The loop picks up the switching field round the
shunt and a magnetic pickup reads as current; a common-mode-only pickup needs
the two lines side by side.

- **Fix:** route the two lines together as a pair from the shunt's Kelvin pads to
  the amplifier, on the same layer, a track width or two apart, with no part or
  pour between them. Put any filter resistors and capacitor at the amplifier end,
  symmetric on both lines.
- **Long run unavoidable?** Keep the pair together for all of it; length alone
  adds little loop, spacing does.
- `loop_mm2` is the area between the two lines; `allowed_mm2` the pair's bound.
