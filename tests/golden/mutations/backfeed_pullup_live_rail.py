"""Mutant: backfeed-pullup-live-rail (usbbuck4).

NETLIST fault (#394, power-domain back-feed): a second LDO, U93, makes
+3V3_SENS from VBUS, switched by header J98's SENS_EN pin; a BME280, U92,
runs from it. Its SDA is pulled up by R98 (4.7 k) to the always-on +3V3,
so with J98 holding the LDO off, +3V3 drives about 0.6 mA through R98
into U92's SDA clamp diode and up its dead VDD rail. Must be caught by
check_backfeed (kind "backfeed", ref R98). The quiet twin pulls SDA up to
+3V3_SENS, the sensor's own rail.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib
from _sigfault import build, node


def netlist(fault=True):
    rails = {"VBUS": [node("U93", "1", "VIN", "power_in")],
             "GND": [node("U93", "2", "GND", "power_in"),
                     node("U92", "2", "GND", "power_in"),
                     node("J98", "2", "GND", "passive")],
             "+3V3": [], }
    pull = [node("R98", "2", "", "passive")]
    if fault:
        rails["+3V3"] = pull
    new = {"+3V3_SENS": [node("U93", "5", "VOUT", "power_out"),
                         node("U92", "1", "VDD", "power_in")],
           "/SENS_EN": [node("U93", "3", "EN", "input"),
                        node("J98", "1", "SENS_EN", "passive")],
           "/SENS_SDA": [node("U92", "3", "SDA", "bidirectional"),
                         node("R98", "1", "", "passive")]}
    if not fault:
        new["+3V3_SENS"] += pull
    rails = {k: v for k, v in rails.items() if v}
    return build({"U93": "AP2112K-3.3", "U92": "BME280", "R98": "4.7k",
                  "J98": "Conn_01x02"}, rails, new)


def surgery(text):
    return text, {"ref": "R98", "fault": "SDA pulled up to the live +3V3 "
                  "into a BME280 on a switched rail",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("backfeed-pullup-live-rail", "usbbuck4", surgery))
