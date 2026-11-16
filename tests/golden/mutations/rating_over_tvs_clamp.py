"""Mutant: rating-over-tvs-clamp (blinky2).

SIDECAR + board edits (hive-hwde #500). D1 becomes a unidirectional TVS
across +3V3 (K moved GND -> +3V3, A moved /LED_A -> GND) whose extraction
prints a 3.6 to 4.0 V breakdown and a 7.3 V clamping voltage. C5 on +3V3
gets an LCSC property and a 6.3 V listing: above the 3.3 V rail and the
TVS's 4.0 V breakdown top, but under the 7.3 V a surge drives the rail to.
Must be caught by check_ratings (kind "rating_over_clamp", on C5).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def prop(lcsc, n):
    return (f'(property "LCSC" "{lcsc}"\n'
            '\t\t\t(at 0 0 0)\n'
            '\t\t\t(layer "F.Fab")\n'
            '\t\t\t(hide yes)\n'
            f'\t\t\t(uuid "aaaa0010-dead-beef-0500-00000000000{n}")\n'
            '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n'
            '\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t\t')


TVS = {
    "mpn": "SMF3.3A", "lcsc": "C990866",
    "pinout": [{"pin": "1", "name": "K", "type": "passive"},
               {"pin": "2", "name": "A", "type": "passive"}],
    "abs_max": [
        {"param": "VRWM reverse standoff voltage", "value": 3.3, "unit": "V"},
        {"param": "VBR breakdown voltage at IT = 1 mA, min to max",
         "value": "3.6 to 4.0", "unit": "V"},
        {"param": "VC maximum clamping voltage at IPP", "value": 7.3,
         "unit": "V"}]}
PARTS = {"parts": [{
    "ref_prefix_hint": "C", "mpn": "C-TEST-6V3", "lcsc": "C990867",
    "value": "10uF", "package": "0805",
    "attributes": [{"name": "Voltage Rated", "value": "6.3V"}],
    "role": "C5 +3V3 bulk"}]}


def surgery(text):
    what = "rating-over-tvs-clamp"
    text = mutlib.edit_footprint(text, "D1", '(property "Reference" "D1"',
                                 prop("C990866", 1) + '(property "Reference" "D1"', what)
    text = mutlib.edit_footprint(text, "D1", '(net "GND")', '(net "+3V3")', what)
    text = mutlib.edit_footprint(text, "D1", '(net "/LED_A")', '(net "GND")', what)
    text = mutlib.edit_footprint(text, "C5", '(property "Reference" "C5"',
                                 prop("C990867", 2) + '(property "Reference" "C5"', what)
    return text, {"sidecars": {"parts/C990866.json": mutlib.dump_json(TVS),
                               "parts/parts.json": mutlib.dump_json(PARTS)},
                  "ref": "C5", "net": "+3V3"}


if __name__ == "__main__":
    sys.exit(mutlib.run("rating-over-tvs-clamp", "blinky2", surgery))
