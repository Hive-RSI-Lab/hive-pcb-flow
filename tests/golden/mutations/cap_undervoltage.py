"""Mutant: cap-undervoltage (blinky2).

SIDECAR + one board property. C6 (10 uF on the 5 V input rail) gets an
`LCSC` property on the board (goldens carry none; check_ratings finds parts
through it) and this mutant's parts/ dir describes that LCSC part as a 10 uF 0603 cap with
`Voltage Rated` 4 V - below the 5 V rail it sits on. The code (C9999901) is
planted: no library extraction covers it, so the listing is the only rating
(a real code such as C15850 now has a datasheet row, 25 V, which outranks a
listing; k2271).
Must be caught by check_ratings (kind "rating_over_voltage").
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

LCSC = "C9999901"
PROP = (f'(property "LCSC" "{LCSC}"\n'
        '\t\t\t(at 0 0 0)\n'
        '\t\t\t(layer "F.Fab")\n'
        '\t\t\t(hide yes)\n'
        '\t\t\t(uuid "aaaa0010-dead-beef-0010-000000000001")\n'
        '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n'
        '\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t\t')
PARTS = {"parts": [{
    "ref_prefix_hint": "C", "mpn": "CL10A106MQ8NNNC", "lcsc": LCSC,
    "value": "10uF", "package": "0603",
    "attributes": [{"name": "Capacitance", "value": "10uF"},
                   {"name": "Voltage Rated", "value": "4V"}],
    "role": "C6 5V input bulk"}]}


def surgery(text):
    text = mutlib.edit_footprint(
        text, "C6", '(property "Reference" "C6"',
        PROP + '(property "Reference" "C6"', "cap-undervoltage")
    return text, {"sidecars": {"parts/parts.json": mutlib.dump_json(PARTS)},
                  "ref": "C6", "net": "+5V", "rated_v": 4}


if __name__ == "__main__":
    sys.exit(mutlib.run("cap-undervoltage", "blinky2", surgery))
