"""Mutant: mate-gender-mismatch (blinky2).

The hit (gcm-v50-4 and gp8b-v50-1 in the real-errata corpus, issue #1124):
a pin socket placed where the board must plug into a socket, so both sides
are female. Here the +5V input J1, a 1x02 pin header, becomes the 1x02 pin
socket of the same pitch and pads, and constraints.json states that J1
mates with a 2.54 mm pin socket (mating_pairs mate_standard
"pin-socket-2.54"), as a board stacking onto a socket header would. Must be
caught by check_mate_pins (kind "mate_gender_mismatch", ref J1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    text = mutlib.edit_footprint(
        text, "J1",
        '(footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"',
        '(footprint "Connector_PinSocket_2.54mm:PinSocket_1x02_P2.54mm_Vertical"',
        "J1 footprint")
    cons = mutlib.golden_json("blinky2", "constraints.json")
    cons["mating_pairs"] = [{"ref": "J1", "mate_standard": "pin-socket-2.54"}]
    return text, {"ref": "J1", "mate_standard": "pin-socket-2.54",
                  "sidecars": {"constraints.json": mutlib.dump_json(cons)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("mate-gender-mismatch", "blinky2", surgery))
