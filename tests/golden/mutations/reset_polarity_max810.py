"""Mutant: reset-polarity-max810 (usbbuck4).

NETLIST fault (class 14, active level across a connection): a 3-pin
reset supervisor, U98, is added on +3V3 with its reset output on the
STM32F103's /NRST, but the part placed is the MAX810, whose RESET output
asserts high (the MAX809 beside it in the family asserts low). NRST
asserts low, so the supervisor holds the MCU in reset whenever the rail
is good and lets it run while the rail is low. ERC sees an output on an
input. Must be caught by netlist_audit (kind "polarity_mismatch",
ref U98). The quiet twin places the MAX809 (~{RESET}).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
from _sigfault import build, node


def netlist(fault=True):
    part, out = ("MAX810S", "RESET") if fault else ("MAX809S", "~{RESET}")
    return build({"U98": part},
                 {"+3V3": [node("U98", "3", "VCC", "power_in")],
                  "GND": [node("U98", "1", "GND", "power_in")],
                  "/NRST": [node("U98", "2", out, "output")]}, {})


def surgery(text):
    return text, {"ref": "U98", "fault": "MAX810 RESET (active high) on NRST",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("reset-polarity-max810", "usbbuck4", surgery))
