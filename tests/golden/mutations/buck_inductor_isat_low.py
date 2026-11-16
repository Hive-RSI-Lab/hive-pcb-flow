"""Mutant: buck-inductor-isat-low (usbbuck4).

SIDECAR fault (#355). U2 (AP63203, 1.1 MHz) bucks VBUS to +3V3 at 0.4 A
through L1, 4.7 uH on /SW. The ripple is (5 - 3.3) * 3.3 / (5 * 1.1e6 *
4.7e-6) = 0.217 A, so L1 peaks at 0.4 + 0.109 = 0.509 A (TI SLVA477C eq. 4;
Diodes AP63203 Eq. 8). Here the buck block states L1's saturation current as
0.45 A, a small chip inductor's rating: it saturates every cycle at the
stated load. Nothing else on the board changes. Must be caught by
check_converter (kind "converter_isat_low", net /SW, ref L1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    cons = mutlib.golden_json("usbbuck4", "constraints.json")
    if "voltages" in cons or "blocks" in cons:
        raise mutlib.SurgeryError("golden constraints already declare "
                                  "voltages or blocks")
    cons["voltages"] = [{"net": "VBUS", "voltage": 5},
                        {"net": "+3V3", "voltage": 3.3}]
    cons["blocks"] = [{"topology": "buck", "block": "U2",
                       "operating_point": {"vin_v": 5, "vout_v": 3.3,
                                           "vin_max_v": 5.5, "inductor": "L1",
                                           "inductor_isat_a": 0.45}}]
    return text, {"net": "/SW", "inductor": "L1", "isat_a": 0.45,
                  "sidecars": {"constraints.json": mutlib.dump_json(cons)}}


if __name__ == "__main__":
    sys.exit(mutlib.run("buck-inductor-isat-low", "usbbuck4", surgery))
