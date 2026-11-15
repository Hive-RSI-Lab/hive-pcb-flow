"""Shared surgery for the signal-level mutants (netlist_audit's class 14
and 15 rules, lib/signal_domains.py): parts and nets added to usbbuck4's
own netlist, the copper untouched. Each mutant's quiet twin is the same
surgery with the fault left out (tests/test_signal_domains.py runs both).
"""
import mutlib

NETLIST = mutlib.GOLDEN / "netlist" / "usbbuck4.net"


def node(ref, pin, fn, ptype="passive"):
    fn = f'(pinfunction "{fn}") ' if fn else ""
    return f'(node (ref "{ref}") (pin "{pin}") {fn}(pintype "{ptype}"))'


def onto(text, net, nodes):
    """nodes added to net `net` (the (nets ...) section only: a component's
    fields carry names too)."""
    comps, sep, nets = text.partition("\t(nets\n")
    head = f'(name "{net}")'
    if not sep or nets.count(head) != 1:
        raise mutlib.SurgeryError(f"net {net} not found once in {NETLIST.name}")
    return comps + sep + nets.replace(head, head + " " + " ".join(nodes), 1)


def build(comps, rails, new_nets):
    """usbbuck4.net with comps {ref: value} added, rails {net: [node, ...]}
    joined onto existing nets and new_nets {name: [node, ...]} created."""
    text = NETLIST.read_text(encoding="utf-8")
    rows = "".join(f'\t\t(comp (ref "{r}") (value "{v}"))\n'
                   for r, v in comps.items())
    text = mutlib.replace_once(text, "\t(components\n",
                               "\t(components\n" + rows, "components")
    for net, nodes in rails.items():
        text = onto(text, net, nodes)
    rows = "".join(f'\t\t(net (code "{950 + i}") (name "{name}") '
                   + " ".join(nodes) + ")\n"
                   for i, (name, nodes) in enumerate(new_nets.items()))
    return mutlib.replace_once(text, "\t(nets\n", "\t(nets\n" + rows, "nets")
