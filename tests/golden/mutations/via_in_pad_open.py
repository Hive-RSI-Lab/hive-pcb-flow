"""Mutant: via-in-pad-open (blinky2).

ASSEMBLY fault: a GND via dropped in the middle of C5 pad 2 (0805), open
(not filled, not capped; its own tenting off). The pad's mask opening
exposes the barrel, so the joint's solder drains down the hole and the
part starves or tombstones. KiCad's DRC passes it (same net). Must be
caught by check_wicking (kind "wick_via_in_pad" at the pad). Quiet twin:
the same via filled and capped (IPC-4761 type VII;
tests/test_check_wicking.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

VIA = """
	(via
		(at 117.6 105.05)
		(size 0.6)
		(drill 0.3)
		(layers "F.Cu" "B.Cu")
		(tenting
			(front no)
			(back no)
		)
		(net "GND")
		(uuid "a492e000-0000-4000-8000-000000001089")
	)
"""


def surgery(text):
    text = mutlib.append_items(text, VIA, "open via in C5 pad 2")
    return text, {"via": [117.6, 105.05], "pad": "C5.2"}


if __name__ == "__main__":
    sys.exit(mutlib.run("via-in-pad-open", "blinky2", surgery))
