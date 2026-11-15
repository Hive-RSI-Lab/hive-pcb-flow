"""Mutant: io-3v3-into-5v-hc (usbbuck4).

NETLIST fault (class 15, I/O supply against the bus level): a 74HC245
bus buffer, U95, is added powered from VBUS (5 V), and the STM32F103's
PA9 (U1, VDD +3V3) drives its A1 input. A 74HC input needs VIH 3.15 V at
VCC 4.5 V (TI SN74HC245 recommended operating conditions,
io_tolerance.yaml), 3.5 V at 5 V, above the 3.3 V high the MCU gives, so
the buffer reads the line late or not at all (the fix is a 74HCT part).
ERC sees an output on an input. Must be caught by netlist_audit (kind
"io_domain_underdrive", ref U95). The quiet twin places a 74HCT245.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
from _sigfault import build, node, onto

PA9 = "unconnected-(U1-PA9-Pad30)"


def netlist(fault=True):
    text = build({"U95": "74HC245" if fault else "74HCT245"},
                 {"VBUS": [node("U95", "20", "VCC", "power_in")],
                  "GND": [node("U95", "10", "GND", "power_in")]}, {})
    text = onto(text, PA9, [node("U95", "2", "A1", "input")])
    return mutlib.replace_once(text, f'(name "{PA9}")', '(name "/BUF_A1")',
                               "PA9 net")


def surgery(text):
    return text, {"ref": "U95", "fault": "3.3 V PA9 into a 5 V 74HC245 A1",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("io-3v3-into-5v-hc", "usbbuck4", surgery))
