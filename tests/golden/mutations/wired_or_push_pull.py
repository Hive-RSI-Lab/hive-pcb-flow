"""Mutant: wired-or-push-pull (usbbuck4).

NETLIST fault (#1087, seeded from the I2C-bus specification UM10204 3.1.1:
every output on SDA and SCL is open-drain, and from TI's SN74LVC8T245
function table: OE low, DIR low drives the A port). usbbuck4's own netlist
gains a sensor, U93, whose SDA and SCL reach the A port of a level
shifter, U94, an SN74LVC8T245 with OE and DIR on ground, so its push-pull
A outputs always drive the bus; the B port goes to the MCU side. ERC sees
an IC on an IC. Must be caught by check_pinouts (kind
"wired_or_push_pull", ref U94).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"U93": "BME280", "U94": "SN74LVC8T245"},
        {"+3V3": [pn.node("U93", "8", "VDD", "power_in"),
                  pn.node("U94", "1", "V_{CCA}", "power_in"),
                  pn.node("U94", "23", "V_{CCB}", "power_in")],
         "GND": [pn.node("U93", "1", "GND", "power_in"),
                 pn.node("U94", "11", "GND", "power_in"),
                 pn.node("U94", "2", "DIR", "input"),
                 pn.node("U94", "22", "~{OE}", "input")]},
        {"/SNS_SDA": [pn.node("U93", "3", "SDA", "bidirectional"),
                      pn.node("U94", "3", "A1", "bidirectional")],
         "/SNS_SCL": [pn.node("U93", "4", "SCL", "input"),
                      pn.node("U94", "4", "A2", "bidirectional")],
         "/MCU_SDA": [pn.node("U94", "21", "B1", "bidirectional")],
         "/MCU_SCL": [pn.node("U94", "20", "B2", "bidirectional")]})


def surgery(text):
    return text, {"ref": "U94", "fault": "push-pull A port on SDA and SCL",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("wired-or-push-pull", "usbbuck4", surgery))
