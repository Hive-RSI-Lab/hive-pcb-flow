"""Mutant: board-no-mounting-holes (blinky2).

MECHANICAL fault: the blinky2 outline stretched from 50 x 40 to 90 x 40 mm
(room for a later expansion header) and still no mounting hole: no
MountingHole footprint, no lone M2-M5 hole, no Edge.Cuts window. Nothing
holds a 90 mm board in its case (CMM2-JLC Rev.A shipped this way). KiCad's
DRC passes it. Must be caught by check_mounting (kind "no_mounting_holes"
at the outline's centre). Quiet twins: the same board with M3 holes, or
with constraints mechanical.mounting declared (tests/test_check_mounting.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

OLD = "\t(gr_rect\n\t\t(start 100 100)\n\t\t(end 150 140)"
NEW = "\t(gr_rect\n\t\t(start 100 100)\n\t\t(end 190 140)"


def surgery(text):
    text = mutlib.replace_once(text, OLD, NEW, "outline 50 -> 90 mm wide")
    return text, {"outline_mm": [90, 40]}


if __name__ == "__main__":
    sys.exit(mutlib.run("board-no-mounting-holes", "blinky2", surgery))
