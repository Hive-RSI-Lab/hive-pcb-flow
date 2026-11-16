"""Mutant: sense-tap-back-pour-via (blinky2).

MEASUREMENT fault (#1356: a pour joined a Kelvin sense line at its via and no
check flagged it). A rail KP runs from L911 to its output capacitor C911 on a 1 mm
F.Cu track; R911's sense line, declared in constraints.json power[].sense_pads to
read C911.1 (an SMD pad, F.Cu only), drops to B.Cu and comes back up on a via
0.15 mm off C911.1's edge, then a short F.Cu stub into the pad. A KP pour on B.Cu
reaches that via, so the line reads the pour, not C911.1. The via sits within
check_kelvin's SENSE_SLACK_MM of the pad, which used to count it as the tap on
every layer. All nets connect and all widths are legal. Must be caught by
check_kelvin (kind "sense_tap_off_pad", refs C911 and R911). Quiet twins:
tests/test_kelvin.py (the pour kept off the via; a through-hole tap).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def _u(n, k):
    return f"79135600-0000-4000-8000-{n:06d}{k:06d}"


def _fp(ref, value, x, y, n, pads):
    rows = "".join(
        f'\t\t(pad "{num}" smd rect\n\t\t\t(at {dx} 0)\n\t\t\t(size {s} {s})\n'
        f'\t\t\t(layers "F.Cu")\n\t\t\t(net "{net}")\n'
        f'\t\t\t(uuid "{_u(n, 100 + int(num))}")\n\t\t)\n'
        for num, dx, s, net in pads)
    return (f'\t(footprint "t:SENSE"\n\t\t(layer "F.Cu")\n'
            f'\t\t(uuid "{_u(n, 1)}")\n\t\t(at {x} {y})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n'
            f'\t\t\t(uuid "{_u(n, 2)}")\n\t\t)\n'
            f'\t\t(property "Value" "{value}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n'
            f'\t\t\t(uuid "{_u(n, 3)}")\n\t\t)\n'
            + mutlib.footprint_fields(_u(n, 9)[:35])
            + f'{rows}\t)\n')


def _seg(x0, y0, x1, y1, w, n, layer="F.Cu"):
    return mutlib.segment_sexpr((x0, y0), (x1, y1), w, layer, "KP", _u(9, n))


def _zone(x0, y0, x1, y1):
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return (f'\t(zone\n\t\t(net "KP")\n\t\t(layer "B.Cu")\n'
            f'\t\t(uuid "{_u(8, 1)}")\n\t\t(name "KPBACK")\n'
            f'\t\t(hatch edge 0.5)\n'
            f'\t\t(connect_pads yes\n\t\t\t(clearance 0.3)\n\t\t)\n'
            f'\t\t(min_thickness 0.25)\n'
            f'\t\t(fill yes\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n'
            f'\t\t\t(island_removal_mode 0)\n\t\t)\n'
            f'\t\t(polygon\n\t\t\t(pts\n\t\t\t\t{pts}\n\t\t\t)\n\t\t)\n\t)\n')


# C911.1 spans y 124..126; the via back up to F.Cu sits at y 126.45 (its ring
# 0.15 mm off the pad); the pour on B.Cu stops at y 126.3, under the via's top
# and clear of the B.Cu run below it
ITEMS = (
    _fp("L911", "4.7uH", 120, 125, 1, [("1", 0, 2, "KP")])
    + _fp("C911", "22u", 132, 125, 2, [("1", 0, 2, "KP")])
    + _fp("R911", "100k", 132, 131, 3, [("1", 0, 1, "KP"), ("2", 2, 1, "KFB")])
    + _seg(120, 125, 132, 125, 1, 1)
    + _seg(132, 131, 132, 129, 0.2, 2)
    + mutlib.via_sexpr((132, 129), "KP", _u(7, 1))
    + _seg(132, 129, 132, 126.45, 0.2, 3, layer="B.Cu")
    + mutlib.via_sexpr((132, 126.45), "KP", _u(7, 2))
    + _seg(132, 126.45, 132, 125.5, 0.2, 4)
)


def surgery(text):
    cons = mutlib.golden_json("blinky2", "constraints.json")
    cons["power"].append({"net": "KP", "current_a": 0.1, "pdn": False,
                          "sense_pads": {"R911.1": "C911.1"}})
    zat = text.index("\t(zone\n")
    text = text[:zat] + _zone(129, 121, 135, 126.3) + text[zat:]
    at = text.index("\t(segment\n")
    return text[:at] + ITEMS + text[at:], {
        "refs": ["C911", "R911"],
        "sidecars": {"constraints.json": mutlib.dump_json(cons)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("sense-tap-back-pour-via", "blinky2", surgery))
