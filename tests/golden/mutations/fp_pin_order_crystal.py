"""Mutant: fp-pin-order-crystal (blinky2).

LIBRARY fault: Y1's Crystal_SMD_3225-4Pin footprint has pads 1 and 2
numbered the wrong way round, so the land is the part's mirror image: the
crystal's pin 1 lands where its pin 2 should be and the case (ground) pads
move with them. The pad count, sizes and name all still match, so only a
check against the drawing sees it. Copper and nets are untouched. Must be
caught by check_fp_geometry (kind "fp_pin_order_vs_stock", ref Y1, at Y1's
origin).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    s, e = mutlib.footprint_block(text, "Y1")
    block = mutlib.replace_once(text[s:e], '(pad "1" ', '(pad "@1@" ', "Y1 pad 1")
    block = mutlib.replace_once(block, '(pad "2" ', '(pad "1" ', "Y1 pad 2")
    block = mutlib.replace_once(block, '(pad "@1@" ', '(pad "2" ', "Y1 pad 1 back")
    return text[:s] + block + text[e:], {"ref": "Y1", "swapped": ["1", "2"]}


if __name__ == "__main__":
    sys.exit(mutlib.run("fp-pin-order-crystal", "blinky2", surgery))
