"""Mutant: pinout-jtag-swap (usbbuck4).

NETLIST fault (the hit: sizif rev.C's errata, "JTAG pinout differs from
Altera standard"). usbbuck4's own netlist gains an FPGA, U98, on +3V3 and
GND, and a 2x5 Altera JTAG header, J98, with its ground on pins 2 and 10 and
+3V3 on pin 4. Header pins 1 and 3 are named for TCK and TDO, but /JTAG_TCK
goes to the FPGA's TDO and /JTAG_TDO to its TCK: a USB-Blaster's clock
drives the FPGA's data out. ERC sees a header and an IC. Must be caught by
check_pinouts (kind "pinout_mismatch", ref J98).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"U98": "10M08SAE144", "J98": "Conn_02x05_Odd_Even"},
        {"+3V3": [pn.node("U98", "1", "VCCIO", "power_in"), pn.node("J98", "4")],
         "GND": [pn.node("U98", "2", "GND", "power_in"), pn.node("J98", "2"),
                 pn.node("J98", "10")]},
        {"/JTAG_TCK": [pn.node("J98", "1"), pn.node("U98", "18", "TDO", "output")],
         "/JTAG_TDO": [pn.node("J98", "3"), pn.node("U98", "16", "TCK", "input")],
         "/JTAG_TMS": [pn.node("J98", "5"), pn.node("U98", "15", "TMS", "input")],
         "/JTAG_TDI": [pn.node("J98", "9"), pn.node("U98", "17", "TDI", "input")]})


def surgery(text):
    return text, {"ref": "J98", "swap": "TCK and TDO crossed at U98",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("pinout-jtag-swap", "usbbuck4", surgery))
