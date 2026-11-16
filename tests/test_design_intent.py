"""netlist_audit rules 34-36, wiring against design intent (#1137), and
rule 8b, a CC or left/right pair crossed on purpose (#843).

Each rule gets a planted fault and a quiet twin that differs only in the
wiring the rule judges; the constraints key design_intent gets its lint.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import constraints_lint  # noqa: E402
import netlist_audit as na  # noqa: E402


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


def _fired(payload, kind):
    return sorted(v.get("net") or "" for v in payload["violations"]
                  if v["kind"] == kind)


def _mcu_fill(ref="U1", start=10, n=20):
    """Enough other pins that REF counts as a bus master (20 or more)."""
    return {f"/IO{i}": [(ref, str(i), "bidirectional", f"GPIO{i}"),
                        ("R9", str(i), "passive")]
            for i in range(start, start + n)}


# ------------------------------------------------- 34 bus_member_dropped

def _z80(iorq_to=()):
    nets = {
        "/ZXMREQ": [("U1", "1", "input"), ("J1", "1", "passive")],
        "/ZXRD": [("U1", "2", "input"), ("J1", "2", "passive")],
        "/ZXWR": [("U1", "3", "input"), ("J1", "3", "passive")],
        "/ZXIORQ": [("J1", "4", "passive"), *iorq_to],
        "GND": [("J1", "5", "passive"), ("U1", "50", "power_in")],
    }
    nets.update(_mcu_fill())
    comps = [("U1", "Pico", "RPi_Pico"), ("J1", "ZXBus", "Edge"),
             ("R9", "R", "R0603"), ("U2", "74HC32", "SOIC-14")]
    return nets, comps


def test_bus_member_reaching_only_the_connector_fires(tmp_path):
    nets, comps = _z80()
    assert _fired(_audit(tmp_path, nets, comps),
                  "bus_member_dropped") == ["/ZXIORQ"]


def test_bus_member_read_by_the_master_is_quiet(tmp_path):
    nets, comps = _z80(iorq_to=[("U1", "4", "input")])
    assert _fired(_audit(tmp_path, nets, comps), "bus_member_dropped") == []


def test_bus_member_read_by_another_chip_is_quiet(tmp_path):
    # pico-if1 v1.3: a gate takes MREQ for the ROM, the I/O Pico needs none
    nets, comps = _z80(iorq_to=[("U2", "1", "input")])
    assert _fired(_audit(tmp_path, nets, comps), "bus_member_dropped") == []


def test_bus_member_stated_unread_is_quiet(tmp_path):
    nets, comps = _z80()
    cons = {"design_intent": {"bus_unread": [
        {"net": "/ZXIORQ", "why": "a memory-only device"}]}}
    assert _fired(_audit(tmp_path, nets, comps, cons),
                  "bus_member_dropped") == []


# ------------------------------------------------------ 35 uart_direction

def _uart(net, value="RP2040", func="GPIO0"):
    nets = {net: [("U1", "2", "bidirectional", func),
                  ("J1", "1", "passive")],
            "GND": [("U1", "1", "power_in", "GND"), ("J1", "2", "passive")]}
    comps = [("U1", value, "QFN-56"), ("J1", "Conn", "PinHeader_1x02")]
    return nets, comps


def test_uart_tx_on_a_receive_line_fires(tmp_path):
    # RP2040 GPIO0 is UART0 TX (pico-sdk io_bank0.h, function 2)
    nets, comps = _uart("/RX_FROM_AUX")
    assert _fired(_audit(tmp_path, nets, comps),
                  "uart_direction") == ["/RX_FROM_AUX"]


def test_uart_tx_on_a_transmit_line_is_quiet(tmp_path):
    nets, comps = _uart("/TX_TO_AUX")
    assert _fired(_audit(tmp_path, nets, comps), "uart_direction") == []


def test_stated_module_maps_its_header_pins(tmp_path):
    # smart-lcc: the Nano RP2040 Connect drawn with a Nano 33 IoT's value;
    # its D1 is GPIO0, UART0 TX
    cons = {"design_intent": {"modules": [
        {"ref": "U1", "is": "Arduino Nano RP2040 Connect",
         "why": "BOM names the RP2040 Connect"}]}}
    nets, comps = _uart("/RX_FROM_AUX", value="ABX00027", func="D1")
    assert _fired(_audit(tmp_path, nets, comps, cons, tag="a"),
                  "uart_direction") == ["/RX_FROM_AUX"]
    nets, comps = _uart("/TX_TO_AUX", value="ABX00027", func="D1")
    assert _fired(_audit(tmp_path, nets, comps, cons, tag="b"),
                  "uart_direction") == []


# ----------------------------------------- 36 transceiver_drives_host_bus

def _xcvr(dir_net, dir_extra=()):
    nets = {"GND": [("U4", "19", "input", "~{OE}"), ("U4", "10", "power_in",
                                                     "GND"),
                    ("J1", "20", "passive")],
            "+5V": [("U4", "20", "power_in", "VCC"), ("J1", "21", "passive")],
            dir_net: [("U4", "1", "input", "A->B"), *dir_extra]}
    for i in range(8):
        nets[f"/ZXD{i}"] = [("U4", str(2 + i), "tri_state", f"A{i}"),
                            ("J1", str(1 + i), "passive")]
        nets[f"/PD{i}"] = [("U4", str(18 - i), "tri_state", f"B{i}"),
                           ("U1", str(1 + i), "bidirectional")]
    comps = [("U4", "74LS245", "SOIC-20"), ("J1", "ZXBus", "Edge"),
             ("U1", "Pico", "RPi_Pico"), ("U3", "74HC32", "SOIC-14")]
    return nets, comps


def test_always_on_245_with_dir_on_a_raw_strobe_fires(tmp_path):
    nets, comps = _xcvr("/ZXRD", [("J1", "18", "passive")])
    assert _fired(_audit(tmp_path, nets, comps),
                  "transceiver_drives_host_bus") == ["/ZXRD"]


def test_dir_from_this_boards_decoder_is_quiet(tmp_path):
    nets, comps = _xcvr("/ROM_READ", [("U3", "3", "output")])
    assert _fired(_audit(tmp_path, nets, comps),
                  "transceiver_drives_host_bus") == []


def test_dir_tied_to_a_rail_is_quiet(tmp_path):
    # byte-hamr, gp8b: a one-way input buffer, DIR on +3V3 or GND
    nets, comps = _xcvr("+5V", [("J1", "21", "passive")])
    nets["+5V"] = nets["+5V"] + [("U4", "20", "power_in", "VCC")]
    assert _fired(_audit(tmp_path, nets, comps),
                  "transceiver_drives_host_bus") == []


# ------------------------------------------------- 8b symmetric_swap (#843)

def _ch224(cc1_pin="CC2", cc2_pin="CC1"):
    """hifi-receiver's sink: CC1 from the receptacle lands on the CH224's
    CC2 pin and CC2 on CC1, on purpose (Type-C CC is symmetric)."""
    return {"/USB_CC1": [("J1", "A5", "bidirectional", "CC1"),
                         ("U3", "7", "bidirectional", cc1_pin)],
            "/USB_CC2": [("J1", "B5", "bidirectional", "CC2"),
                         ("U3", "6", "bidirectional", cc2_pin)],
            "/LIN": [("U9", "1", "bidirectional", "RXD"),
                     ("U3", "1", "input", "LIN")]}


def _tpa3118(left_pins=("OUTPR", "OUTNR")):
    """lite-main's amp: the R channel drives the L speaker."""
    return {"/SPK_L+": [("U7", "21", "output", left_pins[0]),
                        ("J5", "1", "passive")],
            "/SPK_L-": [("U7", "22", "output", left_pins[1]),
                        ("J5", "2", "passive")],
            "/AUDIO_R": [("U7", "3", "input", "INPR"),
                         ("U2", "9", "output", "VOUTR")]}


_COMPS = [("U3", "CH224K", ""), ("U7", "TPA3118D2", ""), ("J1", "USB_C", "")]


def test_symmetric_swap_cc_is_info(tmp_path):
    p = _audit(tmp_path, _ch224(), _COMPS)
    hits = [v for v in p["violations"] if v["kind"] == "symmetric_swap"]
    assert [(v["severity"], v["refs"], v["pair"], sorted(v["pins"]))
            for v in hits] == [("info", ["U3"], "cc", ["6", "7"])]
    assert p["status"] != "fail"
    # quiet twin: straight through, and a LIN pin is not left in
    q = _audit(tmp_path, _ch224("CC1", "CC2"), _COMPS, tag="b")
    assert _fired(q, "symmetric_swap") == []


def test_symmetric_swap_left_right_is_info(tmp_path):
    p = _audit(tmp_path, _tpa3118(), _COMPS)
    hits = [v for v in p["violations"] if v["kind"] == "symmetric_swap"]
    assert [(v["refs"], v["pair"], v["nets"]) for v in hits] == [
        (["U7"], "lr", ["/SPK_L+", "/SPK_L-"])]
    q = _audit(tmp_path, _tpa3118(("OUTPL", "OUTNL")), _COMPS, tag="b")
    assert _fired(q, "symmetric_swap") == []


def test_symmetric_swap_stated_in_design_intent(tmp_path):
    cons = {"design_intent": {"swaps": [
        {"ref": "U3", "why": "CH224 datasheet: CC is symmetric for a sink"},
        {"ref": "U7", "pair": "lr", "why": "README: R drives the L speaker"}]}}
    nets = {**_ch224(), **_tpa3118()}
    p = _audit(tmp_path, nets, _COMPS, cons)
    assert _fired(p, "symmetric_swap") == []
    assert sorted(x.split(":")[0] for x in p["swaps_as_stated"]) \
        == ["U3 cc", "U7 lr"]
    # a statement for the other pair doesn't cover this one
    cons = {"design_intent": {"swaps": [
        {"ref": "U7", "pair": "cc", "why": "x"}]}}
    p = _audit(tmp_path, nets, _COMPS, cons, tag="b")
    assert sorted(v["refs"][0] for v in p["violations"]
                  if v["kind"] == "symmetric_swap") == ["U3", "U7"]


def test_sda_on_scl_is_still_an_error_with_swaps_stated(tmp_path):
    nets = {"/SDA": [("U3", "2", "bidirectional", "SCL")],
            "/SCL": [("U3", "3", "bidirectional", "SDA")]}
    cons = {"design_intent": {"swaps": [{"ref": "U3", "why": "x"}]}}
    p = _audit(tmp_path, nets, _COMPS, cons)
    assert {v["severity"] for v in p["violations"]
            if v["kind"] == "pin_net_swap"} == {"error"}


# -------------------------------------------------------- constraints lint

def _lint(tmp_path, doc):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    vs, _ = constraints_lint.lint_file(p)
    return [v["kind"] for v in vs if v["severity"] == "error"]


def test_design_intent_shape(tmp_path):
    good = {"design_intent": {
        "modules": [{"ref": "U1", "is": "Arduino Nano RP2040 Connect",
                     "why": "BOM line 3"}],
        "bus_unread": [{"net": "/ZXIORQ", "why": "memory-only"}],
        "swaps": [{"ref": "U3", "why": "CC is symmetric"},
                  {"ref": "U7", "pair": "lr", "why": "README"}]}}
    assert _lint(tmp_path, good) == []
    for bad in ({"design_intent": {"modules": [{"ref": "U1", "why": "x"}]}},
                {"design_intent": {"bus_unread": [{"net": "/A"}]}},
                {"design_intent": {"bus_unread": [{"net": "/A",
                                                   "why": " "}]}},
                {"design_intent": {"jumpers": []}},
                {"design_intent": {"swaps": [{"ref": "U3"}]}},
                {"design_intent": {"swaps": [{"ref": "U3", "pair": "dp",
                                              "why": "x"}]}},
                {"design_intent": ["U1"]}):
        assert _lint(tmp_path, bad) == ["bad_type"], bad
