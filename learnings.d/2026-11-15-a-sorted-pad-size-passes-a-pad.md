## 2026-11-15 [fp_verify][footprint][land] A sorted pad size passes a pad turned a quarter, and centre-to-centre spacing never sees the gap under a chip
Found checking the land for haos-voice's 0201 capacitors (Murata GRM033R61A104KE15D), 2026-11-15 (#1331).
fp_verify compared pad_size_mm with each pad's (w, h) after sorting both, so
a pad turned 90 degrees passed. On a two-terminal chip that turn moves the
gap under the body, and row spacing, measured centre to centre, never saw
it: a 0201 land with 0.3 x 0.5 pads at +-0.3 mm and the same pads turned (a
0.1 mm gap) gave the same report. The record had no field for the gap, and
held the maker's ranges as midpoints with fp_verify's 0.1 mm tolerance round
them, wider than Murata's range on both sides. KiCad's stock
C_0201_0603Metric (0.46 x 0.40 pads, a 0.18 mm gap) is outside Murata's land
(gap 0.2-0.25 mm) on every dimension, and fp_verify only warned, by pad size.

The fix judges size along the drawing's axes: land_pattern.pads[].size_mm in
the drawing's frame, turned as the pads fit it, and for a two-terminal land
gap_mm, pad_length_mm and pad_width_mm along and across the line through the
pads, each one number or the maker's [min, max]. A turned pad and a gap out
of range are errors (pad_turned, land_gap). When a check compares shapes,
keep the axes: sorting a pair throws away which way it points.

Triage: now L2 | target L2 | owner scripts/fp_verify.py | status open | note judged only where the record gives the gap, length and width or per-pad size_mm (coverage says not_stated otherwise); no gate asks the extractor for them
