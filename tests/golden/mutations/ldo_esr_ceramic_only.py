"""Mutant: ldo-esr-ceramic-only (blinky2).

PART fault (#356). blinky2's U2 regulates +5V to +3V3 with only multilayer
ceramics on its output (C1 to C5, 0603 and 0805 chips, 10 uF the largest).
Here U2 becomes an LM1117-3.3 in the same SOT-223. TI's LM1117 datasheet
(SNOS412Q p. 15) asks for an output capacitor whose ESR is 0.3 to 22 ohm
(10 uF minimum if tantalum); a ceramic of more than 1 uF is a few
milliohms, which TI's AN-1482 (SNVA167A p. 5) calls unsuitable for an LDO
that relies on the output capacitor's ESR for its compensation zero. The
regulator can oscillate. Nothing else on the board changes. Must be caught
by check_ldo_esr (kind "ldo_esr_low", error, net +3V3, ref U2).

The quiet twin (tests/test_check_ldo_esr.py): the same board with C5
declared a 10 uF tantalum in constraints `ldo_caps`: no finding.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    text = mutlib.edit_footprint(text, "U2", '(property "Value" "AMS1117-3.3"',
                                 '(property "Value" "LM1117-3.3"', "U2 value")
    return text, {"ref": "U2", "net": "+3V3", "part": "LM1117-3.3"}


if __name__ == "__main__":
    sys.exit(mutlib.run("ldo-esr-ceramic-only", "blinky2", surgery))
