"""Mutant: bus-strobe-push-pull (usbbuck4).

NETLIST fault (class 9, seeded from Zilog's Z80 CPU User Manual UM0080:
the CPU floats WR only on BUSACK, and from TI's SN74HC08 datasheet:
balanced CMOS push-pull outputs). usbbuck4's own netlist gains a Z80, U93,
whose WR strobe is also driven by gate 1Y of a 74HC08, U94, with no 3-state
buffer or OE between them, so whenever the two differ one output sources
into the other. ERC sees an output on an output only when the pin types
say so. Must be caught by check_pinouts (kind "bus_driver_contention",
ref U94).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"U93": "Z84C0010PEG", "U94": "74HC08"},
        {"+3V3": [pn.node("U93", "11", "VCC", "power_in"),
                  pn.node("U94", "14", "VCC", "power_in")],
         "GND": [pn.node("U93", "29", "GND", "power_in"),
                 pn.node("U94", "7", "GND", "power_in")]},
        {"/WR": [pn.node("U93", "22", "~{WR}", "tri_state"),
                 pn.node("U94", "3", "1Y", "output")],
         "/DMA_WR": [pn.node("U94", "1", "1A", "input")],
         "/DMA_EN": [pn.node("U94", "2", "1B", "input")]})


def surgery(text):
    return text, {"ref": "U94", "fault": "push-pull gate on the Z80 WR strobe",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("bus-strobe-push-pull", "usbbuck4", surgery))
