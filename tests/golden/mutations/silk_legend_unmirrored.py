"""Mutant: silk-legend-unmirrored (blinky2).

SILK fault (class 10, a two-column pin legend that reads wrong against the
pads; extends #1028). The Raspberry Pi 40-pin header J9 of
mech-05-pi-header-mirrored, in its right back-side pattern, gains a
two-column legend on B.SilkS by pins 1-6, turned 90 degrees to run
across the two rows: "5V 3V3 / 5V SDA / GND SCL", the word order a
top-side legend there would read in, never mirrored. Read from below,
where the bottom silk is read, the odd row (3V3, pin 1) is on the left,
so every word sits over the other column. Only the column order is
judged, not the line order along the rows. Must be caught by check_silk (kind "pin_label_wrong", form
"mirrored", ref J9).

`surgery(text, mirrored=True)` lays the words out the right way, odd
row first ("3V3 5V / SDA 5V / SCL GND"); the unit test uses it as the
quiet twin.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mech_05_pi_header_mirrored as pi
import mutlib

UUID = "aaaa0010-dead-beef-0010-000000000001"
AT = (100.95, 136.2, -90)
LINES = [("3V3", "5V"), ("SDA", "5V"), ("SCL", "GND")]   # odd row first
LEGEND_AT = (98.4, 132.6, 90)


def legend(at, mirrored: bool) -> str:
    """A B.SilkS text: odd row first when mirrored (right), else even."""
    text = "\\n".join(" ".join(t if mirrored else reversed(t)) for t in LINES)
    return (f"\t(gr_text \"{text}\"\n\t\t(at {at[0]} {at[1]} {at[2]})\n"
            f"\t\t(layer \"B.SilkS\")\n\t\t(uuid \"{UUID}\")\n"
            f"\t\t(effects (font (size 1 1) (thickness 0.15))"
            f" (justify mirror))\n\t)\n")


def surgery(text, mirrored: bool = False):
    i = text.rfind("\n\t(footprint")
    last = re.search(r'\(property "Reference" "([^"]+)"', text[i:]).group(1)
    e_last = mutlib.footprint_block(text, last)[1]
    text = text[:e_last] + pi.header(mirrored=False, at=AT) + text[e_last:]
    text = mutlib.append_items(text, legend(LEGEND_AT, mirrored), "pin legend")
    return text, {"ref": "J9", "layer": "B.SilkS", "mirrored": mirrored,
                  "legend": [" ".join(t if mirrored else reversed(t))
                             for t in LINES]}



if __name__ == "__main__":
    sys.exit(mutlib.run("silk-legend-unmirrored", "blinky2", surgery))
