## 2026-11-15 [check_fp_geometry][fp_verify][land] A new source turned on against a real board first shows the checker's own blind spots: sort the first flood by cause before trusting or muting it
Found wiring the board's part records into check_fp_geometry on haos-voice, 2026-11-15 (#1332).
check_fp_geometry judged a placed footprint against land_patterns.yaml,
the stock footprint of its name, the LCSC model and its name, never the
part records. So the 19 footprints drawn for haos-voice itself, the ones
most likely to be wrong, were judged against nothing. Run through
fp_verify against their records, the first board run gave 220 findings.
Few of them were wrong lands. Most were fp_verify's own blind spots:
the nearest-pad pitch and the sorted pad size, on which a maker's land
and KiCad's IPC land of one 0603 differ by tenths of a millimetre; a
SOT-23-5's rows of three and two, which _row_spacings did not count as a
row pair; a USB-A's 2.3 mm shield legs held to the record's one 0.92 mm
drill_mm; and a float boundary, where 0.55 - 0.50 is just over 0.05, so
a land exactly at the tolerance failed. Sorted by cause and fixed, 17
remained, every one checked against the maker's drawing. One was KiCad's
stock 0201, whose 0.18 mm gap is outside Murata's 0.2-0.25 mm.

A land gap outside the maker's is judged as a fraction of the maker's own
nearest bound: info up to 25 %, an error beyond it. On this board the lands
drawn for their own part were off by up to 18 %, and one drawn for another
part by 29 %. Where that bound is 0 or below, any gap under it is an error
and any above it a warning. A gap given as one number is held to +-0.1 mm
only to decide whether it is out.

Before you mute a flood from a new source, group it by cause. Each group
is either a wrong land or a blind spot in the checker. Fix the blind
spots in the checker, with a test, and every tolerance test gets a float
slack (EPS_MM).

Triage: now L2 | target L2 | owner scripts/check_fp_geometry.py | status open | note records are judged only where a footprint's MPN, Value or LCSC field finds one in --parts; the 17 board findings went to the board's owner, not fixed here
