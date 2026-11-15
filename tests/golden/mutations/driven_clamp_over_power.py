"""Mutant: driven-clamp-over-power (blinky2).

SIDECAR fault + board edits (hive-hwde #1006). D1 moves from /LED_A onto
/LED, which U1 drives from pin 15, and is extracted as a 1.8 V zener with its
cathode on /LED and anode on GND. U1 gets an extraction naming its supply
pins (VDD on +3V3, VSS on GND) and pin 15 a push-pull output rated for 25 mA.
U1's 3.3 V rail reaches past the zener's 1.8 V, so the pin can force its
rated 25 mA through D1: 45 mW at 1.8 V, over the 10 mW Pd parts.json gives.
Before #1006 a clamp on a driven net was never judged: no resistor limits
what a driver forces through it. Must be caught by check_ratings (kind
"rating_over_current", on D1).
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
            f'\t\t\t(uuid "aaaa0010-dead-beef-1006-00000000000{n}")\n'
            '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n'
            '\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t\t')


MCU = {
    "mpn": "SEEDED-MCU-25MA", "lcsc": "C991006",
    "pinout": [*({"pin": p, "name": "VDD", "type": "power_in"}
                 for p in ("1", "9", "24", "36", "48")),
               *({"pin": p, "name": "VSS", "type": "ground"}
                 for p in ("8", "23", "35", "47")),
               {"pin": "15", "name": "PA5", "type": "output"}],
    "pin_ratings": [{"pins": ["PA5"], "kind": "current", "level": "abs_max",
                     "min": -0.025, "max": 0.025,
                     "source": "seeded: IIO output current"}]}
ZENER = {
    "mpn": "ZEN1V8-AK", "lcsc": "C991007",
    "pinout": [{"pin": "1", "name": "A", "type": "passive"},
               {"pin": "2", "name": "K", "type": "passive"}],
    "pin_ratings": [{"pins": ["K"], "kind": "voltage", "level": "recommended",
                     "max": 1.8, "ref": "A", "source": "seeded: zener range top"}]}
BOM = {"parts": [{"lcsc": "C991007", "mpn": "ZEN1V8-AK", "attributes": [
    {"name": "Pd - Power Dissipation", "value": "10mW"}]}]}


def surgery(text):
    what = "driven-clamp-over-power"
    text = mutlib.edit_footprint(text, "U1", '(property "Reference" "U1"',
                                 prop("C991006", 1) + '(property "Reference" "U1"', what)
    text = mutlib.edit_footprint(text, "D1", '(property "Reference" "D1"',
                                 prop("C991007", 2) + '(property "Reference" "D1"', what)
    text = mutlib.edit_footprint(text, "D1", '(net "/LED_A")', '(net "/LED")', what)
    return text, {"sidecars": {"parts/C991006.json": mutlib.dump_json(MCU),
                               "parts/C991007.json": mutlib.dump_json(ZENER),
                               "parts/parts.json": mutlib.dump_json(BOM)},
                  "ref": "D1", "net": "/LED"}


if __name__ == "__main__":
    sys.exit(mutlib.run("driven-clamp-over-power", "blinky2", surgery))
