"""Mutant: sense-line-unmarked (blinky2).

ROUTING fault (#1077: a sense line routed by hand carries no aiee_sensew mark,
so rules_gen's sense-width rule never reaches it). A 5 mOhm shunt R931 on a
3 A rail KA is read by U931's IN+/IN- over two 0.2 mm lines run side by side
(a clean pair). KA's rail rule wants far wider copper, and nothing marks the
line as a sense line, so DRC holds it to the rail width. Must be caught by
check_kelvin (kind "sense_line_unmarked", refs R931 and U931).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sense_pair as sp
import mutlib


def surgery(text):
    its = sp.items("79300000", *sp.TOGETHER)
    cons = mutlib.golden_json("blinky2", "constraints.json")
    cons["power"].append({"net": "KA", "current_a": 3.0, "pdn": False})
    return sp.insert(text, its), {
        "refs": ["R931", "U931"],
        "sidecars": {"constraints.json": mutlib.dump_json(cons)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("sense-line-unmarked", "blinky2", surgery))
