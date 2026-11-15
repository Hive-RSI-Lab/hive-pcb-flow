"""Mutant: io-5v-hc-into-3v3 (usbbuck4).

NETLIST fault (class 15, I/O supply against the bus level): a 74HC595
shift register, U97, is added powered from VBUS (5 V), and its QA output
drives GPIO3 of an RP2040, U96, whose IOVDD is +3V3. The RP2040's I/O
absolute maximum is IOVDD + 0.5 V (datasheet 5.5.1, pin_abs_max.yaml),
so a 5 V high drives 3.8 V past it into the pin's clamp. ERC sees an
output on a bidirectional pin. Must be caught by netlist_audit (kind
"io_domain_overdrive", ref U96). The quiet twin powers U97 from +3V3.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
from _sigfault import build, node


def netlist(fault=True):
    vcc = node("U97", "16", "VCC", "power_in")
    rails = {"VBUS" if fault else "+3V3": [vcc],
             "GND": [node("U97", "8", "GND", "power_in"),
                     node("U96", "19", "GND", "power_in")]}
    rails.setdefault("+3V3", []).extend(
        [node("U96", "1", "IOVDD", "power_in"),
         node("U96", "10", "IOVDD", "power_in")])
    return build({"U97": "74HC595", "U96": "RP2040"}, rails,
                 {"/SR_QA": [node("U97", "15", "QA", "tri_state"),
                             node("U96", "5", "GPIO3", "bidirectional")]})


def surgery(text):
    return text, {"ref": "U96", "fault": "5 V 74HC595 QA into RP2040 GPIO3",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("io-5v-hc-into-3v3", "usbbuck4", surgery))
