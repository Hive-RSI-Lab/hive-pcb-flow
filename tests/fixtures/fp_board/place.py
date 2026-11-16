"""Rebuild placed.kicad_pcb with KiCad's own placement and flip (bundled
python only: ~/tools/hive-tools/kicad-10.0.6/bin/python3.11 place.py), so
fp_verify's board mode is tested against KiCad's transform, not its own.

lib.pretty: BGA25_TOP (a 5 x 5 ball grid numbered from the top), BGA25_BOT
(its mirror), SO8_TOP (pads as parts/C999008.json's land drawing) and
SO8_BOT (its mirror). Each placed top and bottom, some turned; the SO8s
carry LCSC C999008. Judged: the _TOP copies fit, the _BOT copies are
bottom_view_numbering, on either side and under any turn."""
import sys
from pathlib import Path

import pcbnew

HERE = Path(__file__).resolve().parent
PLAN = [("BGA25_TOP", "U1", 0, False), ("BGA25_TOP", "U2", 30, False),
        ("BGA25_TOP", "U3", 0, True), ("BGA25_TOP", "U4", 120, True),
        ("BGA25_BOT", "U5", 0, False), ("BGA25_BOT", "U6", 210, True),
        ("SO8_TOP", "U7", 0, False), ("SO8_TOP", "U8", 90, True),
        ("SO8_BOT", "U9", 45, False), ("SO8_BOT", "U10", 0, True)]


def main() -> int:
    b = pcbnew.BOARD()
    for i, (name, ref, rot, flip) in enumerate(PLAN):
        fp = pcbnew.FootprintLoad(str(HERE / "lib.pretty"), name)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20 + 15 * i),
                                       pcbnew.FromMM(20)))
        b.Add(fp)
        if flip:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(rot)
        if name.startswith("SO8"):
            fp.SetField("LCSC", "C999008")
    b.Save(str(HERE / "placed.kicad_pcb"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
