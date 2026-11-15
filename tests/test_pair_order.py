"""netlist_audit rule 37, pair order across a connector (#1217).

Each finding gets a planted fault and a quiet twin that differs only in the
two lines the rule judges; the not_judged cases are held to say so rather
than guess, and the constraints key connector_pinouts gets its lint.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import constraints_lint  # noqa: E402
import netlist_audit as na  # noqa: E402
import pair_order as po  # noqa: E402

KINDS = ("pair_polarity_swap", "pair_split", "pair_lane_order")


def _netlist(nets: dict, comps=()) -> str:
    """Synthetic kicadsexpr netlist; a node is (ref, pin, pintype[, func])."""
    comp_s = "".join(f'(comp (ref "{r}") (value "{v}") (footprint "{f}"))'
                     for r, v, f in comps)
    net_s = ""
    for i, (name, nodes) in enumerate(nets.items(), 1):
        node_s = ""
        for node in nodes:
            r, p, t = node[:3]
            func = f' (pinfunction "{node[3]}")' if len(node) > 3 else ""
            node_s += f'(node (ref "{r}") (pin "{p}"){func} (pintype "{t}"))'
        net_s += f'(net (code "{i}") (name "{name}") {node_s})'
    return f'(export (version "E") (components {comp_s}) (nets {net_s}))'


def _audit(tmp_path, nets, comps, constraints=None, tag="a"):
    d = tmp_path / tag
    d.mkdir()
    (d / "board.net").write_text(_netlist(nets, comps), encoding="utf-8")
    (d / "constraints.json").write_text(json.dumps(constraints or {}),
                                        encoding="utf-8")
    payload, _ = na.run(["--constraints", str(d / "constraints.json"),
                         "--netlist", str(d / "board.net")])
    return payload


def _hits(payload):
    return sorted((v["kind"], v["severity"]) for v in payload["violations"]
                  if v["kind"] in KINDS)


def _nj(payload):
    return [n for n in payload.get("not_judged", [])
            if str(n).startswith("pair_order")]


def _gnd(*refs):
    return {"GND": [(r, "99", "power_in") for r in refs]}


# ------------------------------------------------------------ pin names

def test_role_reads_common_pin_names():
    assert po.role("USB_DP") == ("usb", "", "P")
    assert po.role("PA11/USB_DM") == ("usb", "", "N")
    assert po.role("D-") == ("usb", "", "N")
    assert po.role("TD+") == ("eth", "TX", "P")
    assert po.role("RXN") == ("eth", "RX", "N")
    assert po.role("MDI1-") == ("mdi", "1", "N")
    assert po.role("BI_DC+") == ("mdi", "2", "P")
    assert po.role("TXOUT2-") == ("lvds", "2", "N")
    assert po.role("RXCLKIN+") == ("lvds", "CLK", "P")
    assert po.role("TMDS_D1+") == ("lane", "D1", "P")
    assert po.role("TTIP") == ("e1", "TX", "P")
    assert po.role("GPIO4") is None
    assert po.role("DATA") is None


# ------------------------------------------- USB through series resistors

def _usb(swap=False, conn_fp="Conn:Conn_01x04", named=True, choke=False):
    """U1's USB_DP/DM through 22R R1/R2 to J1 D+/D-."""
    jdp, jdm = ("2", "3") if swap else ("3", "2")
    jf = (lambda f: (f,)) if named else (lambda f: ())
    nets = {
        "/UP_A": [("U1", "10", "bidirectional", "USB_DP"),
                  ("R1", "1", "passive")],
        "/UP_B": [("U1", "11", "bidirectional", "USB_DM"),
                  ("R2", "1", "passive")],
    }
    if choke:
        nets["/UP_C"] = [("R1", "2", "passive"), ("FL1", "1", "passive")]
        nets["/UP_D"] = [("R2", "2", "passive"), ("FL1", "2", "passive")]
        nets["/UP_E"] = [("FL1", "4", "passive"),
                         ("J1", jdp, "passive", *jf("D+"))]
        nets["/UP_F"] = [("FL1", "3", "passive"),
                         ("J1", jdm, "passive", *jf("D-"))]
    else:
        nets["/UP_E"] = [("R1", "2", "passive"),
                         ("J1", jdp, "passive", *jf("D+"))]
        nets["/UP_F"] = [("R2", "2", "passive"),
                         ("J1", jdm, "passive", *jf("D-"))]
    nets.update(_gnd("U1", "J1"))
    comps = [("U1", "STM32F072", "LQFP-48"), ("R1", "22", "R0402"),
             ("R2", "22", "R0402"), ("J1", "USB", conn_fp)]
    if choke:
        comps.append(("FL1", "CMC", "Choke_4pin"))
    return nets, comps


def _usb_crossed(tmp_path, **kw):
    """J1's D+ named on pin 3 but wired to U1's DM through R2."""
    nets, comps = _usb(**kw)
    nets["/UP_E"] = [("R2", "2", "passive"), ("J1", "3", "passive", "D+")]
    nets["/UP_F"] = [("R1", "2", "passive"), ("J1", "2", "passive", "D-")]
    return nets, comps


def test_usb_pair_crossed_through_series_resistors_fires(tmp_path):
    nets, comps = _usb_crossed(tmp_path)
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [("pair_polarity_swap", "error")]
    v = next(x for x in p["violations"] if x["kind"] == "pair_polarity_swap")
    assert "J1" in v["refs"] and "U1" in v["refs"]
    assert p["connector_pairs_checked"] == 1


def test_usb_pair_wired_straight_is_quiet(tmp_path):
    nets, comps = _usb()
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [] and _nj(p) == []
    assert p["connector_pairs_checked"] == 1


def test_micro_b_standard_pinout_swap_fires(tmp_path):
    # J1's pins carry no names: micro-B's pin 2 D-, pin 3 D+ judges them
    nets, comps = _usb(swap=True, named=False,
                       conn_fp="Connector_USB:USB_Micro-B_Molex-105017")
    assert _hits(_audit(tmp_path, nets, comps)) == [
        ("pair_polarity_swap", "error")]


def test_micro_b_standard_pinout_straight_is_quiet(tmp_path):
    nets, comps = _usb(named=False,
                       conn_fp="Connector_USB:USB_Micro-B_Molex-105017")
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [] and p["connector_pairs_checked"] == 1


def _type_c(swap=False):
    if swap:
        dp, dm = ("A7", "B7"), ("A6", "B6")
    else:
        dp, dm = ("A6", "B6"), ("A7", "B7")
    nets = {
        "/UP_A": [("U1", "10", "bidirectional", "USB_DP")]
        + [("J1", p, "passive") for p in dp],
        "/UP_B": [("U1", "11", "bidirectional", "USB_DM")]
        + [("J1", p, "passive") for p in dm],
    }
    nets.update(_gnd("U1", "J1"))
    comps = [("U1", "STM32F072", "LQFP-48"),
             ("J1", "USB_C", "Connector_USB:USB_C_Receptacle_GCT_USB4085")]
    return nets, comps


def test_type_c_pair_crossed_fires(tmp_path):
    nets, comps = _type_c(swap=True)
    assert _hits(_audit(tmp_path, nets, comps)) == [
        ("pair_polarity_swap", "error")]


def test_type_c_pair_straight_is_quiet(tmp_path):
    nets, comps = _type_c()
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [] and p["connector_pairs_checked"] == 1


def test_unnamed_choke_in_the_way_is_not_judged(tmp_path):
    nets, comps = _usb(choke=True)
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == []
    assert any("FL1" in n for n in _nj(p))


def test_generic_header_with_a_chip_pair_is_not_judged(tmp_path):
    nets, comps = _usb(named=False)
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == []
    assert any("J1" in n and "connector_pinouts" in n for n in _nj(p))


def test_connector_pinouts_names_a_bare_header(tmp_path):
    # the same bare header, its pinout stated: crossed fires, straight is
    # quiet
    stated = {"connector_pinouts": [{"ref": "J1", "pins": {"3": "D+",
                                                           "2": "D-"},
                                     "source": "cable drawing"}]}
    nets, comps = _usb(named=False, swap=True)
    p = _audit(tmp_path, nets, comps, stated, tag="x")
    assert _hits(p) == [("pair_polarity_swap", "error")]
    assert "cable drawing" in next(
        v["msg"] for v in p["violations"]
        if v["kind"] == "pair_polarity_swap")
    nets, comps = _usb(named=False)
    p = _audit(tmp_path, nets, comps, stated, tag="y")
    assert _hits(p) == [] and _nj(p) == []


def test_connector_pinouts_overrides_the_symbol(tmp_path):
    # the symbol's names are wrong; the stated pinout says pin 2 is D+
    stated = {"connector_pinouts": [{"ref": "J1", "pins": {"2": "D+",
                                                           "3": "D-"}}]}
    nets, comps = _usb(swap=True)
    assert _hits(_audit(tmp_path, nets, comps, stated)) == []


# ----------------------------------------------------- Ethernet magjack

def _magjack(mode="straight"):
    """U1 PHY TXP/TXN/RXP/RXN to J1 magjack TD+/TD-/RD+/RD-."""
    tdp, tdn, rdp, rdn = "TD+", "TD-", "RD+", "RD-"
    if mode == "swap":
        tdp, tdn = "TD-", "TD+"
    elif mode == "split":
        tdn, rdp = "RD+", "TD-"
    elif mode == "cross":
        tdp, tdn, rdp, rdn = "RD+", "RD-", "TD+", "TD-"
    nets = {
        "/UP_A": [("U1", "1", "output", "TXP"), ("J1", "1", "passive", tdp)],
        "/UP_B": [("U1", "2", "output", "TXN"), ("J1", "2", "passive", tdn)],
        "/UP_C": [("U1", "3", "input", "RXP"), ("J1", "3", "passive", rdp)],
        "/UP_D": [("U1", "4", "input", "RXN"), ("J1", "6", "passive", rdn)],
    }
    nets.update(_gnd("U1", "J1"))
    comps = [("U1", "LAN8720A", "QFN-24"),
             ("J1", "HR911105A", "Connector_RJ:RJ45_Hanrun_HR911105A")]
    return nets, comps


def test_magjack_pair_reversed_warns(tmp_path):
    nets, comps = _magjack("swap")
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [("pair_polarity_swap", "warning")]
    assert "autopolarity" in next(v["msg"] for v in p["violations"]
                                  if v["kind"] == "pair_polarity_swap")


def test_magjack_straight_is_quiet(tmp_path):
    nets, comps = _magjack()
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [] and _nj(p) == []
    assert p["connector_pairs_checked"] == 2


def test_magjack_pair_split_across_tx_and_rx_fires(tmp_path):
    nets, comps = _magjack("split")
    assert ("pair_split", "error") in _hits(_audit(tmp_path, nets, comps))


def test_magjack_crossover_is_not_judged(tmp_path):
    nets, comps = _magjack("cross")
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == []
    assert any("crossover" in n for n in _nj(p))


# ------------------------------------------------------------- LVDS

def _lvds(order=("0", "1", "2", "CLK")):
    """U1 serializer TXOUT0..2/TXCLKOUT to J1 RXIN0..2/RXCLKIN."""
    def tx(k):
        return "TXCLKOUT" if k == "CLK" else f"TXOUT{k}"

    def rx(k):
        return "RXCLKIN" if k == "CLK" else f"RXIN{k}"
    nets = {}
    for i, (src, dst) in enumerate(zip(("0", "1", "2", "CLK"), order)):
        for j, pol in enumerate("+-"):
            nets[f"/UP_{i}{j}"] = [
                ("U1", str(10 + 2 * i + j), "output", tx(src) + pol),
                ("J1", str(1 + 2 * i + j), "passive", rx(dst) + pol)]
    nets.update(_gnd("U1", "J1"))
    comps = [("U1", "SN75LVDS83B", "TSSOP-56"),
             ("J1", "LCD", "Connector:Conn_01x20")]
    return nets, comps


def test_lvds_lanes_out_of_order_fire(tmp_path):
    nets, comps = _lvds(order=("1", "0", "2", "CLK"))
    assert _hits(_audit(tmp_path, nets, comps)) == [
        ("pair_lane_order", "error"), ("pair_lane_order", "error")]


def test_lvds_lanes_in_order_are_quiet(tmp_path):
    nets, comps = _lvds()
    p = _audit(tmp_path, nets, comps)
    assert _hits(p) == [] and _nj(p) == []
    assert p["connector_pairs_checked"] == 4


def test_lvds_lane_reversed_fires(tmp_path):
    nets, comps = _lvds()
    a, b = nets["/UP_10"][1], nets["/UP_11"][1]
    nets["/UP_10"][1], nets["/UP_11"][1] = ("J1", a[1], a[2], "RXIN1-"), \
        ("J1", b[1], b[2], "RXIN1+")
    assert _hits(_audit(tmp_path, nets, comps)) == [
        ("pair_polarity_swap", "error")]


# ---------------------------------------------- rule 8 already fired

def test_rule_8_pin_net_swap_is_not_repeated(tmp_path):
    # net names that say USB_DP/USB_DM let rule 8 name the cross; rule 37
    # stays quiet on the same lines
    nets, comps = _usb_crossed(tmp_path)
    ren = {"/UP_E": "/USB_DP", "/UP_F": "/USB_DM"}
    nets = {ren.get(k, k): v for k, v in nets.items()}
    p = _audit(tmp_path, nets, comps)
    if any(v["kind"] == "pin_net_swap" for v in p["violations"]):
        assert _hits(p) == []


# -------------------------------------------------------------- lint

def _lint(tmp_path, val):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"connector_pinouts": val}), encoding="utf-8")
    vs, _ = constraints_lint.lint_file(f)
    return [v["kind"] for v in vs if v["severity"] == "error"]


def test_lint_takes_a_stated_pinout(tmp_path):
    assert _lint(tmp_path, [{"ref": "J4", "pins": {"1": "TD+"},
                             "source": "datasheet"}]) == []


def test_lint_refuses_a_malformed_pinout(tmp_path):
    assert _lint(tmp_path, [{"ref": "J4", "pins": ["TD+"]}])
    assert _lint(tmp_path, [{"ref": "J4", "pins": {"1": ""}}])
    assert _lint(tmp_path, [{"ref": "J4", "pins": {"1": "D+"},
                             "standard": "usb"}])
