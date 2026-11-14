"""Mutant: pinout-cable-supply-volts (usbbuck4).

NETLIST fault (the hit: bed-lift v0.2's errata, hall sensors "intended to be
driven at 5 V" fed from 3.3 V). usbbuck4's own netlist gains a 6-pin
actuator harness connector, J96, wired in the bed-lift actuator's pinout
(pin 2 GND, pin 3 the hall supply), with pin 3 on +3V3; constraints.json
states J96's mate (pinouts: bedlift-lift-actuator), whose hall supply is
5 V. ERC sees a header on a rail. Must be caught by check_pinouts (kind
"pinout_supply_volts", ref J96).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"J96": "Conn_01x06"},
        {"+3V3": [pn.node("J96", "3")], "GND": [pn.node("J96", "2")]},
        {"/HALL2": [pn.node("J96", "1")], "/MOT_A": [pn.node("J96", "4")],
         "/MOT_B": [pn.node("J96", "5")], "/HALL1": [pn.node("J96", "6")]})


def surgery(text):
    cons = mutlib.golden_json("usbbuck4", "constraints.json")
    if "pinouts" in cons:
        raise mutlib.SurgeryError("golden constraints already state pinouts")
    cons["pinouts"] = [{"ref": "J96", "standard": "bedlift-lift-actuator"}]
    return text, {"ref": "J96", "supply": "+3V3 where the mate needs 5 V",
                  "sidecars": {"usbbuck4.net": netlist(),
                               "constraints.json": mutlib.dump_json(cons)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("pinout-cable-supply-volts", "usbbuck4", surgery))
