"""Mutant: sense-pair-apart (blinky2).

MEASUREMENT fault (#1077: a hand-routed shunt pair with no sense_pads entry and
no aiee_sensew mark, so no sense rule saw it; bldc's SPC/SNC lines part round
a 433 mm^2 loop). A 5 mOhm shunt R931 is read by U931's IN+/IN- over two
0.2 mm lines that leave its pads, part 20 mm across and meet again only at
U931: about 280 mm^2 between them, where a coupled pair would close under
60 mm^2. All nets connect and all widths are legal. Must be caught by
check_kelvin (kind "sense_pair_apart", refs R931 and U931).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sense_pair as sp
import mutlib


def surgery(text):
    its = sp.items("79200000", *sp.APART)
    return sp.insert(text, its), {"refs": ["R931", "U931"]}


if __name__ == "__main__":
    sys.exit(mutlib.run("sense-pair-apart", "blinky2", surgery))
