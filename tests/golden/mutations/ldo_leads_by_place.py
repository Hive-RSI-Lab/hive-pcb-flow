"""Mutant: ldo-leads-by-place (blinky2).

SIDECAR + one board property (#843). U2 (the SOT-223 LDO) gets an `LCSC`
property, and this mutant's parts/ dir holds a datasheet extraction whose
pin numbers and names agree with the board (1 GND, 2 VOUT, 3 VIN) but whose
land drawing (land_pattern.pads, top view) puts pin 1 where the footprint
has pad 3 and pin 3 where it has pad 1: the leads run the other way round.
By number every pin is in rating; by the copper, the GND lead sits on +5V.
Must be caught by check_ratings (kind "rating_reverse_polarity", ref U2).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

LCSC = "C9900843"
PROP = (f'(property "LCSC" "{LCSC}"\n'
        '\t\t\t(at 0 0 0)\n'
        '\t\t\t(layer "F.Fab")\n'
        '\t\t\t(hide yes)\n'
        '\t\t\t(uuid "aaaa0010-dead-beef-0843-000000000001")\n'
        '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n'
        '\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t\t')
EXTRACTION = {
    "mpn": "LDO-LEADS-REVERSED", "lcsc": LCSC,
    "pinout": [{"pin": "1", "name": "GND", "type": "ground"},
               {"pin": "2", "name": "VOUT", "type": "power_out"},
               {"pin": "3", "name": "VIN", "type": "power_in"}],
    "abs_max": [{"param": "Input voltage VIN", "value": 15, "unit": "V"}],
    "land_pattern": {"pad_count": 4, "source": "planted fault", "pads": [
        {"number": "1", "at_mm": [-3.15, 2.3]},
        {"number": "2", "at_mm": [-3.15, 0.0]},
        {"number": "3", "at_mm": [-3.15, -2.3]},
        {"number": "2", "at_mm": [3.15, 0.0]}]}}


def surgery(text):
    text = mutlib.edit_footprint(
        text, "U2", '(property "Reference" "U2"',
        PROP + '(property "Reference" "U2"', "ldo-leads-by-place")
    return text, {"sidecars": {f"parts/{LCSC}.json": mutlib.dump_json(EXTRACTION)},
                  "ref": "U2", "net": "+5V"}


if __name__ == "__main__":
    sys.exit(mutlib.run("ldo-leads-by-place", "blinky2", surgery))
