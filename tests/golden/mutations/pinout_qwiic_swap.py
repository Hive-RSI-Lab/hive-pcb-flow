"""Mutant: pinout-qwiic-swap (usbbuck4).

NETLIST fault (#1070, seeded from the SparkFun Qwiic standard: GND, 3.3V,
SDA, SCL on pins 1 to 4). usbbuck4's own netlist gains a sensor, U95, on
+3V3 and GND, and a Qwiic port, J95, with ground on pin 1 and +3V3 on pin
2. Header pins 3 and 4 are named for SDA and SCL, but /QW_SDA goes to the
sensor's SCL and /QW_SCL to its SDA: every Qwiic cable crosses the bus.
ERC sees a connector and an IC. Must be caught by check_pinouts (kind
"pinout_mismatch", ref J95).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"U95": "BME280", "J95": "Qwiic"},
        {"+3V3": [pn.node("U95", "8", "VDD", "power_in"), pn.node("J95", "2")],
         "GND": [pn.node("U95", "1", "GND", "power_in"), pn.node("J95", "1")]},
        {"/QW_SDA": [pn.node("J95", "3"), pn.node("U95", "4", "SCL", "input")],
         "/QW_SCL": [pn.node("J95", "4"), pn.node("U95", "3", "SDA", "bidirectional")]})


def surgery(text):
    return text, {"ref": "J95", "swap": "SDA and SCL crossed at U95",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("pinout-qwiic-swap", "usbbuck4", surgery))
