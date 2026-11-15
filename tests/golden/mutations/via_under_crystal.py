"""Mutant: via-under-crystal (blinky2).

ASSEMBLY fault: a GND stitching via dropped at the centre of the Y1 crystal
(3.2 x 2.5 mm SMD can, body on the board), between its four pads and clear
of them, with its own tenting switched off on top (`(tenting (front no)
(back no))`). The open ring sits under a metal can that rests on the
board: flux and solder balls trap under it and the can can short to the
ring. KiCad's DRC passes it (the via clears every pad). Must be caught by
check_wicking (kind "via_under_body" at the via). Quiet twin: the same via
tented (tests/test_check_wicking.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

VIA = """
	(via
		(at 114 128)
		(size 0.6)
		(drill 0.3)
		(layers "F.Cu" "B.Cu")
		(tenting
			(front no)
			(back no)
		)
		(net "GND")
		(uuid "a492e000-0000-4000-8000-000000001088")
	)
"""


def surgery(text):
    text = mutlib.append_items(text, VIA, "open via under Y1")
    return text, {"via": [114, 128], "under": "Y1"}


if __name__ == "__main__":
    sys.exit(mutlib.run("via-under-crystal", "blinky2", surgery))
