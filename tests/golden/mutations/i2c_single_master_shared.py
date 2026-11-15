"""Mutant: i2c-single-master-shared (usbbuck4).

NETLIST fault (class 9, seeded from TI's TUSB8041 datasheet SLLSEE4E: with
SMBUSz high or unconnected the hub is a single-master I2C controller for a
dedicated EEPROM). usbbuck4's own netlist gains a TUSB8041 hub, U93, with
SMBUSz unconnected, its 24LC64 EEPROM U94, and an MCU, U95, whose I2C1
pins sit on the same SDA and SCL, so two masters share a bus with no
arbitration. ERC sees nothing. Must be caught by check_pinouts (kind
"i2c_single_master", ref U93).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
import pinout_nets as pn


def netlist():
    return pn.build(
        {"U93": "TUSB8041RGCR", "U94": "24LC64", "U95": "STM32F072CBT6"},
        {"+3V3": [pn.node("U94", "8", "VCC", "power_in"),
                  pn.node("U95", "1", "VBAT", "power_in")],
         "GND": [pn.node("U94", "4", "VSS", "power_in")]},
        {"/HUB_SDA": [pn.node("U93", "38", "SDA/SMBDAT", "bidirectional"),
                      pn.node("U94", "5", "SDA", "bidirectional"),
                      pn.node("U95", "43", "PB7/I2C1_SDA", "bidirectional")],
         "/HUB_SCL": [pn.node("U93", "39", "SCL/SMBCLK", "bidirectional"),
                      pn.node("U94", "6", "SCL", "input"),
                      pn.node("U95", "42", "PB6/I2C1_SCL", "bidirectional")]})


def surgery(text):
    return text, {"ref": "U93", "fault": "single-master hub I2C shared with an MCU",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("i2c-single-master-shared", "usbbuck4", surgery))
