"""Mutant: pinout-jack-tip-ground (usbbuck4).

NETLIST fault (the hit: echoplayer r1-rev1's errata, "both jacks are
incorrectly wired and will not work with standard headphones"). usbbuck4's
own netlist gains a 3.5 mm stereo jack, J97, with pads T, R and S, and a
headphone driver, U97. The tip is on GND and left on the sleeve: a
headphone plugged in shorts its left channel and floats its ground. ERC
sees a connector on three nets. Must be caught by check_pinouts (kind
"pinout_mismatch", ref J97).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"U97": "TPA6132A2", "J97": "AudioJack3"},
        {"+3V3": [pn.node("U97", "1", "VDD", "power_in")],
         "GND": [pn.node("U97", "2", "GND", "power_in"), pn.node("J97", "T")]},
        {"/HP_LEFT": [pn.node("J97", "S"), pn.node("U97", "3", "OUTL", "output")],
         "/HP_RIGHT": [pn.node("J97", "R"), pn.node("U97", "4", "OUTR", "output")]})


def surgery(text):
    return text, {"ref": "J97", "swap": "tip on GND, left on the sleeve",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("pinout-jack-tip-ground", "usbbuck4", surgery))
