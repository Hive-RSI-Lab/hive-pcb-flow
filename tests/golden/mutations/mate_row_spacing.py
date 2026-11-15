"""Mutant: mate-row-spacing (blinky2).

The hit (zxdiag v1.0 Z101 in the real-errata corpus, issue #1124): a 2-row
connector whose rows are not the distance apart its mate's are, so the
mating part cannot go in (Z101's rows 2.54 mm apart for an edge socket
whose rows are 5.08). Here the board grows 25 mm at the bottom for a
Raspberry Pi Pico carrier U9: a DIP-40 socket footprint, rows 15.24 mm
apart, where a Pico's castellated rows are 17.78 mm apart (one 0.1 in
pitch wider than a 40-pin DIP, Pico datasheet). constraints.json states the
mate (mating_pairs mate_standard "raspberry-pi-pico"). No pad has a net, so
nothing new is left unrouted. Must be caught by check_mate_pins (kind
"mate_row_spacing", ref U9).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

AT = (102.5, 145.0)
ROW = 15.24
FPID = "Package_DIP:DIP-40_W15.24mm_Socket"


def socket() -> str:
    """The DIP-40 socket: pins 1-20 along +x, 21-40 back along the far row."""
    pads = []
    for n in range(1, 41):
        x = 2.54 * (n - 1) if n <= 20 else 2.54 * (40 - n)
        y = 0 if n <= 20 else ROW
        pads.append(
            f'\t\t(pad "{n}" thru_hole {"rect" if n == 1 else "oval"}'
            f' (at {x:.2f} {y:.2f}) (size 1.6 1.6) (drill 0.8)'
            f' (layers "*.Cu" "*.Mask")'
            f' (uuid "00000000-0000-4000-8000-0000011240{n:02d}"))\n')
    return (f'\n\t(footprint "{FPID}"\n\t\t(layer "F.Cu")\n'
            f'\t\t(uuid "00000000-0000-4000-8000-000001124000")\n'
            f'\t\t(at {AT[0]} {AT[1]})\n'
            f'\t\t(property "Reference" "U9" (at 24.13 -2.33 0)'
            f' (layer "F.SilkS")'
            f' (uuid "00000000-0000-4000-8000-000001124041")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n'
            f'\t\t(property "Value" "RPi_Pico" (at 24.13 7.62 0)'
            f' (layer "F.Fab")'
            f' (uuid "00000000-0000-4000-8000-000001124042")\n'
            f'\t\t\t(effects (font (size 1 1) (thickness 0.15))))\n'
            + mutlib.footprint_fields("00000000-0000-4000-8000-00000112405",
                                      "F.Fab") +
            f'\t\t(attr through_hole)\n'
            f'\t\t(fp_rect (start -1.55 -1.55) (end 49.81 16.79)'
            f' (stroke (width 0.05) (type solid)) (fill no)'
            f' (layer "F.CrtYd")'
            f' (uuid "00000000-0000-4000-8000-000001124043"))\n'
            + "".join(pads) + "\t)")


def surgery(text):
    text = mutlib.replace_once(
        text, "(start 100 100)\n\t\t(end 150 140)",
        "(start 100 100)\n\t\t(end 153 165)", "grow the outline")
    i = text.rfind("\n\t(footprint")
    last = re.search(r'\(property "Reference" "([^"]+)"', text[i:]).group(1)
    e_last = mutlib.footprint_block(text, last)[1]
    text = text[:e_last] + socket() + text[e_last:]
    cons = mutlib.golden_json("blinky2", "constraints.json")
    cons["mating_pairs"] = [{"ref": "U9", "mate_standard": "raspberry-pi-pico"}]
    return text, {"ref": "U9", "at": list(AT), "row_pitch_mm": ROW,
                  "sidecars": {"constraints.json": mutlib.dump_json(cons)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("mate-row-spacing", "blinky2", surgery))
