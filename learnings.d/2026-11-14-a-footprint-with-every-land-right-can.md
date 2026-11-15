## 2026-11-14 [fp_verify][datasheet_extract][footprint] A footprint with every land right can still put the pins on the wrong pads - fp_verify checks pad numbers against the record's top-view pin positions
Found building fp_verify's pin-position check (#1102) from two faults in a board review (2026-11-14).
An oscillator (SiT1602, QFN-4) and a microphone (T5848, LGA-8) had every pad
the right size and in the right place, so fp_verify passed both, but the
numbers were wrong: the oscillator's ran a quarter turn off, each pin on the
next pad, and the microphone's were read from the datasheet's bottom view.
Land sizes and pitches cannot show
this. A record with `land_pattern.pads` (each pin's top-view spot) can:
fp_verify finds the pad at each pin's spot and fails `pad_numbering` when its
number differs, naming the cause (numbering_rotated, bottom_view_numbering,
mirrored_footprint, numbers_differ). Both parts fail as they were and pass
as fixed (tests/test_fp_verify.py).

Without `land_pattern.pads` it can judge only the direction, from the
package family: top-view numbers run counter-clockwise round a perimeter
package, and a BGA's rows and columns run from A1 as seen from the top. That
is a warning (`pad_numbering_direction`, `bga_numbering_mirrored`), not an
error. Measured 2026-11-15, it flagged none of 1536 stock perimeter
footprints (17 give no direction), 1 of 303 stock BGAs (a WLCSP-6,
unconfirmed) and 3 of 456 pulled footprints: two Bosch LGA-8s, which really
number clockwise (`land_pattern.numbering: cw_top` says so), and an SO-6
not yet confirmed either way. It misses the oscillator altogether, since
numbers a quarter turn off still run counter-clockwise. Over the errata
corpus (docs/errata-corpus.md), fp_verify's own no-record check, run on
every footprint in both revisions' .kicad_pcb with back-copper parts read
un-mirrored, judged 708 footprints on 31 boards and flagged none.

A footprint fixed in the library still has to reach the routed board:
learnings.d/2026-11-14-a-footprint-renumbered-in-the-library-under.md
(part_repair, #1128).

Triage: now L1 | target L2 | owner scripts/fp_verify.py | status open | note pad_numbering fails only when the record has land_pattern.pads, which nothing requires yet; without them a quarter-turn renumbering passes and a bottom-view one only warns
