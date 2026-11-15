"""Mutant: pulsed-load-no-bulk (usbbuck4).

NETLIST fault (#1090, seeded from Garmin's LIDAR-Lite v3 manual, which asks
for a 680 uF capacitor across the module's supply). usbbuck4's own netlist
gains a LIDAR-Lite connector, J93, fed from +3V3 through a ferrite, FB93,
on /3V3_LIDAR, with only a 100 nF capacitor, C93, beside it: the laser's
shot current comes down the cable with nothing to hold the rail. ERC sees
a connector on a rail. Must be caught by check_pinouts (kind
"pulsed_load_no_bulk", ref J93).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"J93": "LIDAR-Lite v3", "FB93": "600R", "C93": "100nF"},
        {"+3V3": [pn.node("FB93", "1")],
         "GND": [pn.node("J93", "6"), pn.node("C93", "2")]},
        {"/3V3_LIDAR": [pn.node("FB93", "2"), pn.node("J93", "1"),
                        pn.node("C93", "1")],
         "/LIDAR_SCL": [pn.node("J93", "4")],
         "/LIDAR_SDA": [pn.node("J93", "5")]})


def surgery(text):
    return text, {"ref": "J93", "fault": "LIDAR-Lite supply with no bulk cap",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("pulsed-load-no-bulk", "usbbuck4", surgery))
