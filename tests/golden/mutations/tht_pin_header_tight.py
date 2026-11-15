"""Mutant: tht-pin-header-tight (blinky2).

The hit (zxdiag v1.0 Z101 and mdbt-micro-50q J1 in the real-errata corpus,
issue #1123): a project's own pin header footprint, named for the part but
not a stock library one, drilled 0.76-0.80 mm, so the 0.64 mm square post
(0.91 mm across its corners) does not go in. Here the +5V input J1, a stock
PinHeader_1x02, becomes a project-library "Pin_Header_1x02" with 0.75 mm
drills; pad positions and nets are untouched. Its 2.54 mm hole pitch makes
it a 2.54 mm header (reference/tht_leads.yaml pitch_mm). Must be caught by
check_connector_fit (kind "tht_pin_tight", ref J1, at J1 pin 1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    s, e = mutlib.footprint_block(text, "J1")
    block = mutlib.replace_once(
        text[s:e],
        '(footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"',
        '(footprint "blinky2:Pin_Header_1x02"', "J1 footprint")
    old = "(size 1.7 1.7)\n\t\t\t(drill 1)"
    if block.count(old) != 2:
        raise ValueError(f"J1 pad drill: expected 2 pads, found {block.count(old)}")
    block = block.replace(old, "(size 1.5 1.5)\n\t\t\t(drill 0.75)")
    return text[:s] + block + text[e:], {"ref": "J1", "drill_mm": 0.75}


if __name__ == "__main__":
    sys.exit(mutlib.run("tht-pin-header-tight", "blinky2", surgery))
