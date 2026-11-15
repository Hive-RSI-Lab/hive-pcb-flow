"""Mutant: load-switch-pfet-reversed (usbbuck4).

NETLIST fault (#1085, power-path orientation): a high-side load switch,
Q98, a P-channel MOSFET, is added between VBUS and a new switched rail
/LOAD_5V that feeds a load (U97). Its gate is pulled up to VBUS by R98
and pulled down by an NPN, Q97. The FET is placed with its drain on VBUS
and its source on the load, so its body diode (drain to source) feeds
the load from VBUS whatever the gate does: the switch can never turn the
load off. ERC sees nothing. Must be caught by netlist_audit (kind
"power_switch_reversed", ref Q98). The quiet twin puts the source on
VBUS (tests/test_power_path.py has both ways round on a small netlist).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
from _sigfault import build, node


def netlist(fault=True):
    on_vbus, on_load = ("D", "S") if fault else ("S", "D")
    pin = {"G": "1", "S": "2", "D": "3"}
    return build(
        {"Q98": "AO3401A PMOS", "Q97": "MMBT3904", "R98": "10k",
         "R97": "4k7", "U97": "load"},
        {"VBUS": [node("Q98", pin[on_vbus], on_vbus), node("R98", "1", "")],
         "GND": [node("Q97", "2", "E"), node("U97", "2", "GND", "power_in")]},
        {"/LOAD_5V": [node("Q98", pin[on_load], on_load),
                      node("U97", "1", "VIN", "power_in")],
         "/SW_G": [node("Q98", "1", "G"), node("R98", "2", ""),
                   node("Q97", "3", "C")],
         "/SW_EN": [node("Q97", "1", "B"), node("R97", "1", "")],
         "/SW_EN_IN": [node("R97", "2", "")]})


def surgery(text):
    return text, {"ref": "Q98", "fault": "load-switch P-FET drain on VBUS",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("load-switch-pfet-reversed", "usbbuck4", surgery))
