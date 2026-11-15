"""Mutant: rail-bead-short (usbbuck4).

RAIL BUDGET fault (#1118): a ferrite bead in series with a rail that has no
margin for it. usbbuck4's buck U2 (AP63203WU) feeds +3V3 straight from its
pad 1; here a bead FB98 sits between them: U2 pad 1 drives a new net
/BUCK_OUT, FB98 joins it to +3V3, and +3V3's via moves onto FB98's far pad.
The sidecar constraints give the budget's terms: U2's output can sit at
3.234 V (3.3 V - 2%), FB98's max DCR is 0.7 ohm, and U1 (the STM32, its USB
needs VDD >= 3.0 V) draws the rail's 0.4 A. The bead drops 280 mV: U1 gets
about 2.952 V, 48 mV short. Each net's copper is fine on its own, and ERC
sees a bead on a rail. Must be caught by check_rail_budget (kind
"rail_budget_short", ref U1); the golden with the same constraints is quiet
(232 mV of margin).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def _u(k):
    return f"79118000-0000-4000-8000-{k:012d}"


PAD1_OLD = ('(pad "1" smd roundrect\n\t\t\t(at -1.1375 -0.95)\n'
            '\t\t\t(size 1.325 0.6)\n\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")\n'
            '\t\t\t(roundrect_rratio 0.25)\n\t\t\t(net "+3V3")')
PAD1_NEW = PAD1_OLD.replace('(net "+3V3")', '(net "/BUCK_OUT")')

SEG_OLD = ('\t(segment\n\t\t(start 108.862 104.55)\n\t\t(end 108 104.55)\n'
           '\t\t(width 0.25)\n\t\t(layer "F.Cu")\n\t\t(net "+3V3")\n')
SEG_NEW = SEG_OLD.replace('(net "+3V3")', '(net "/BUCK_OUT")')
VIA_OLD = '\t(via\n\t\t(at 108 104.55)\n'
VIA_NEW = '\t(via\n\t\t(at 108 102.4)\n'


def _pad(num, y, net, k):
    return (f'\t\t(pad "{num}" smd rect\n\t\t\t(at 0 {y})\n'
            '\t\t\t(size 0.5 0.5)\n\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")\n'
            f'\t\t\t(net "{net}")\n\t\t\t(uuid "{_u(k)}")\n\t\t)\n')


BEAD = ('\t(footprint "t:BEAD_0402"\n\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{_u(1)}")\n\t\t(at 108 103.2)\n'
        '\t\t(property "Reference" "FB98"\n\t\t\t(at 0 0 0)\n'
        f'\t\t\t(layer "F.Fab")\n\t\t\t(uuid "{_u(2)}")\n\t\t)\n'
        '\t\t(property "Value" "600R@100MHz"\n\t\t\t(at 0 0 0)\n'
        f'\t\t\t(layer "F.Fab")\n\t\t\t(uuid "{_u(3)}")\n\t\t)\n'
        + mutlib.footprint_fields(_u(9)[:35])
        + _pad("1", 0.5, "/BUCK_OUT", 4) + _pad("2", -0.5, "+3V3", 5)
        + '\t)\n')

# /BUCK_OUT from U2 pad 1's old trace end up to FB98 pad 1
RISER = mutlib.segment_sexpr((108, 104.55), (108, 103.7), 0.25, "F.Cu",
                             "/BUCK_OUT", _u(6))

BUDGET = {"sources": {"U2": {"pins": "1", "vout_v": 3.3,
                             "vout_min_v": 3.234,
                             "source": "AP63203 output 3.3 V -2% (test)"}},
          "series": {"FB98": {"dcr_max_ohm": 0.7}},
          "loads": {"U1": {"vmin_v": 3.0, "current_a": 0.4}}}


def constraints(budget=BUDGET):
    cons = mutlib.golden_json("usbbuck4", "constraints.json")
    if "rail_budget" in cons:
        raise mutlib.SurgeryError("golden constraints already hold rail_budget")
    cons["rail_budget"] = budget
    return cons


def surgery(text):
    text = mutlib.edit_footprint(text, "U2", PAD1_OLD, PAD1_NEW, "U2 pad 1")
    text = mutlib.replace_once(text, SEG_OLD, SEG_NEW, "U2 pad 1 trace")
    text = mutlib.replace_once(text, VIA_OLD, VIA_NEW, "+3V3 via")
    text = mutlib.append_items(text, BEAD + RISER, "FB98")
    return text, {"ref": "U1", "series": "FB98",
                  "sidecars": {"constraints.json":
                               mutlib.dump_json(constraints())}}


if __name__ == "__main__":
    sys.exit(mutlib.run("rail-bead-short", "usbbuck4", surgery))
