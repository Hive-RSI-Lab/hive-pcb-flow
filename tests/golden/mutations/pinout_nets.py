"""Netlist surgery shared by the check_pinouts mutants (#1027): parts and
nets added to usbbuck4's own netlist, the copper left alone."""
import mutlib

NETLIST = mutlib.GOLDEN / "netlist" / "usbbuck4.net"


def node(ref, pin, fn="", kind="passive"):
    return (f'(node (ref "{ref}") (pin "{pin}") (pinfunction "{fn}") '
            f'(pintype "{kind}"))')


def onto(text, net, nodes):
    """nodes added to net `net` (the (nets ...) section only: a component's
    fields carry names too)."""
    comps, sep, nets = text.partition("\t(nets\n")
    head = f'(name "{net}")'
    if not sep or nets.count(head) != 1:
        raise mutlib.SurgeryError(f"net {net} not found once in {NETLIST.name}")
    return comps + sep + nets.replace(head, head + " " + " ".join(nodes), 1)


def build(comps, rails, new_nets):
    """comps: {ref: value}; rails: {existing net: [nodes]}; new_nets:
    {name: [nodes]}, coded from 900."""
    text = NETLIST.read_text(encoding="utf-8")
    text = mutlib.replace_once(
        text, "\t(components\n", "\t(components\n" + "".join(
            f'\t\t(comp (ref "{r}") (value "{v}"))\n' for r, v in comps.items()),
        "components")
    for net, nodes in rails.items():
        text = onto(text, net, nodes)
    body = "".join(f'\t\t(net (code "{900 + i}") (name "{n}") '
                   + " ".join(nodes) + ")\n"
                   for i, (n, nodes) in enumerate(new_nets.items()))
    return mutlib.replace_once(text, "\t(nets\n", "\t(nets\n" + body, "nets")
