"""Mutant: usb-pair-crossed (usbbuck4).

NETLIST fault (#1217, pair order across a connector): a second USB port,
J98, a micro-B receptacle whose pins name D+ (pin 3) and D- (pin 2), is
added with a USB-serial bridge, U98, whose pins name USBDP and USBDM.
Each line runs through a 22R series resistor (R98, R97). The D+ line is
wired to USBDM and the D- line to USBDP, so the pair is crossed: USB 2.0
receivers have no polarity correction, the host reads J for K and the
device never enumerates. ERC sees nothing, and the net names are neutral
(/USB2_A, /USB2_B and their bridge-side halves) so rule 8's name check
says nothing. Must be caught by netlist_audit (kind "pair_polarity_swap",
ref J98). The quiet twin wires D+ to USBDP (tests/test_pair_order.py has
both ways round on a small netlist).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
from _sigfault import build, node


def netlist(fault=True):
    dp, dm = ("USBDM", "USBDP") if fault else ("USBDP", "USBDM")
    pin = {"USBDP": "15", "USBDM": "16"}
    return build(
        {"J98": "USB_B_Micro", "U98": "FT232RL", "R98": "22R",
         "R97": "22R"},
        {"VBUS": [node("J98", "1", "VBUS", "power_out"),
                  node("U98", "20", "VCC", "power_in")],
         "GND": [node("J98", "5", "GND", "passive"),
                 node("U98", "7", "GND", "power_in")]},
        {"/USB2_A": [node("J98", "3", "D+", "bidirectional"),
                     node("R98", "1", "")],
         "/USB2_A_B": [node("R98", "2", ""),
                       node("U98", pin[dp], dp, "bidirectional")],
         "/USB2_B": [node("J98", "2", "D-", "bidirectional"),
                     node("R97", "1", "")],
         "/USB2_B_B": [node("R97", "2", ""),
                       node("U98", pin[dm], dm, "bidirectional")]})


def surgery(text):
    return text, {"ref": "J98", "fault": "USB D+/D- crossed at the bridge",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("usb-pair-crossed", "usbbuck4", surgery))
