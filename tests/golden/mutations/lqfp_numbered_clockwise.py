"""Mutant: lqfp-numbered-clockwise (blinky2).

LIBRARY fault (#1285): U1's LQFP-48 footprint is numbered clockwise from
pin 1, the land drawn from the package's bottom view: each pad carries the
number of the pad at its mirror place (left and right sides swap, pin 1
stays top left), so every pin but those on the mirror line lands on
another's pad. Pad count, sizes and name still match the stock footprint
and copper and nets are untouched. U1's record (parts/C9900003.json, the
planted LCSC number) says land_pattern.numbering ccw_top. Must be caught by
fp_verify's board mode (kind "pad_numbering", ref U1, at U1's origin).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

LCSC = "C9900003"
EXTRACTION = {
    "mpn": "LQFP48-CCW", "lcsc": LCSC,
    "pinout": [{"pin": str(n), "name": f"P{n}", "type": "passive"}
               for n in range(1, 49)],
    "land_pattern": {"pad_count": 48, "pin1": "1", "pitch_mm": 0.5,
                     "numbering": "ccw_top", "source": "planted fault"},
}
PAD = re.compile(r'\(pad "(\d+)" smd (\w+)\n\t+\(at ([-\d.]+) ([-\d.]+)')


def surgery(text):
    s, e = mutlib.footprint_block(text, "U1")
    block = text[s:e]
    pads = [(m.start(1), m.end(1), m.group(1), float(m.group(3)),
             float(m.group(4))) for m in PAD.finditer(block)]
    if len(pads) != 48:
        raise mutlib.SurgeryError(f"U1: {len(pads)} pads, want 48")
    at = {(round(x, 4), round(y, 4)): n for _, _, n, x, y in pads}
    new = {}
    for a, b, n, x, y in pads:
        mate = at.get((round(-x, 4), round(y, 4)))
        if mate is None:
            raise mutlib.SurgeryError(f"U1 pad {n}: no pad at its mirror place")
        new[(a, b)] = mate
    for (a, b), mate in sorted(new.items(), reverse=True):
        block = block[:a] + mate + block[b:]
    block = mutlib.replace_once(
        block, "(at 128 118)",
        f'(at 128 118)\n\t\t(property "LCSC" "{LCSC}"\n'
        '\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n\t\t\t(hide yes)\n'
        '\t\t\t(uuid "9b1e0c7a-5d2f-4c61-9a0e-f00000000003")\n\t\t)',
        "U1 LCSC")
    return text[:s] + block + text[e:], {
        "ref": "U1", "lcsc": LCSC, "renumbered": len(new),
        "sidecars": {f"parts/{LCSC}.json": mutlib.dump_json(EXTRACTION)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("lqfp-numbered-clockwise", "blinky2", surgery))
