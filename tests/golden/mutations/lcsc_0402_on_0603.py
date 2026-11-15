"""Mutant: lcsc-0402-on-0603 (blinky2).

LIBRARY/BOM fault (the hits: oasis Rfbb1, flexgrid-v3-2 C9). C3 sits on a
C_0603 footprint, but the LCSC code on it, C1525, is an 0402 capacitor, so
JLC places an 0402 part on 0603 pads. Copper is untouched. Must be caught
by check_fp_package (kind "footprint_package_mismatch", ref C3, at C3's
origin), from reference/lcsc_packages.json without a network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    s, e = mutlib.footprint_block(text, "C3")
    block = mutlib.replace_once(
        text[s:e], '\t\t(property "Value" "100nF"',
        '\t\t(property "LCSC" "C1525"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")'
        '\n\t\t\t(hide yes)\n\t\t\t(uuid "5f0c3a2e-7b1d-4c8e-9a61-0402c0603c03")'
        '\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t)\n'
        '\t\t\t)\n\t\t)\n\t\t(property "Value" "100nF"', "C3 LCSC")
    return text[:s] + block + text[e:], {"ref": "C3", "lcsc": "C1525"}


if __name__ == "__main__":
    sys.exit(mutlib.run("lcsc-0402-on-0603", "blinky2", surgery))
