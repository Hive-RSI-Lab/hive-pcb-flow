"""Mutant: ldo-cout-derated-below-min (usbbuck4).

NETLIST fault (#1067): an LDO's output capacitor meets the datasheet's
minimum on its nominal value only. usbbuck4's own netlist gains U97, a
TPS7A2018 (1.8 V) fed from +3V3, its OUT on a new +1V8 rail whose one
capacitor C97 is 0.47 uF in 0201. TI SBVS338H asks for "Effective output
capacitance of 0.47 uF minimum ... for stability" (reference/
pin_requirements.yaml TPS7A20 COUT, `effective: true`); at 1.8 V the 0201
keeps about 0.39 uF typical, 0.35 uF low, so the LDO runs below its
stability floor. pin_part_missing sees 0.47 uF and passes it; ERC and DRC
see a capacitor where one is needed. The board's copper is untouched (the
check reads the netlist beside it). Must be caught by mlcc_dcbias (kind
"rail_c_below_min", ref U97).

quiet=1 (the twin, tests/test_power_paths.py): the same LDO with a 1 uF
0402, about 0.91 uF typical and 0.86 uF low at 1.8 V: no finding.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib

NETLIST = mutlib.GOLDEN / "netlist" / "usbbuck4.net"


def _comps(quiet: bool) -> str:
    cap = ('"1uF 6.3V X5R"', "C_0402_1005Metric") if quiet else \
        ('"0.47uF 6.3V X5R"', "C_0201_0603Metric")
    return ('\t\t(comp (ref "U97") (value "TPS7A2018PDQNR") '
            '(footprint "Package_SON:X2SON-4-1EP_1x1mm_P0.65mm_EP0.6x0.6mm"))\n'
            f'\t\t(comp (ref "C97") (value {cap[0]}) '
            f'(footprint "Capacitor_SMD:{cap[1]}"))\n')


def _node(ref, pin, fn):
    return (f'(node (ref "{ref}") (pin "{pin}") (pinfunction "{fn}") '
            '(pintype "passive"))')


def _onto(text, net, nodes):
    """nodes added to net `net` (the (nets ...) section only)."""
    comps, sep, nets = text.partition("\t(nets\n")
    head = f'(name "{net}")'
    if not sep or nets.count(head) != 1:
        raise mutlib.SurgeryError(f"net {net} not found once in {NETLIST.name}")
    return comps + sep + nets.replace(head, head + " " + " ".join(nodes), 1)


def netlist(quiet: bool = False) -> str:
    text = NETLIST.read_text(encoding="utf-8")
    text = mutlib.replace_once(text, "\t(components\n",
                               "\t(components\n" + _comps(quiet),
                               "components")
    text = _onto(text, "+3V3", [_node("U97", "4", "IN"),
                                _node("U97", "3", "EN")])
    text = _onto(text, "GND", [_node("U97", "2", "GND"),
                               _node("U97", "5", "GND"),
                               _node("C97", "2", "")])
    net = ('\t\t(net (code "913") (name "+1V8") '
           + _node("U97", "1", "OUT") + " " + _node("C97", "1", "") + ")\n")
    return mutlib.replace_once(text, "\t(nets\n", "\t(nets\n" + net, "nets")


def surgery(text):
    return text, {"ref": "U97", "load": "C97 0.47 uF 0201 at 1.8 V",
                  "sidecars": {"usbbuck4.net": netlist()}}


if __name__ == "__main__":
    sys.exit(mutlib.run("ldo-cout-derated-below-min", "usbbuck4", surgery))
