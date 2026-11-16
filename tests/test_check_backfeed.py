"""check_backfeed (#394): a live domain driving current into a domain that
can be off, on synthetic netlists. Each planted fault has a quiet twin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import check_backfeed  # noqa: E402
import verify_all  # noqa: E402


def _nl(comps: dict, nets: dict) -> dict:
    """comps {ref: value}; nets {name: [(ref, pin, func, type)]}."""
    return {"components": {r: {"value": v} for r, v in comps.items()},
            "nets": {n: [{"ref": r, "pin": p, "pinfunction": f,
                          "pintype": t} for r, p, f, t in nodes]
                     for n, nodes in nets.items()}}


def _run(comps, nets, ioff=None):
    nl = _nl(comps, nets)
    import netlist_audit as na
    volts = na._rail_volts({}, nl["nets"])
    return check_backfeed.analyse(
        nl, volts, check_backfeed.load_ioff() if ioff is None else ioff)


def _add(nets, name, *nodes):
    nets.setdefault(name, []).extend(nodes)


def _base():
    """J1 USB power -> U1 LDO -> +3V3 (MCU U3); U2, a second LDO off VBUS,
    makes +3V3_SENS, enabled by the MCU's PA5."""
    comps = {"J1": "USB_C", "U1": "AP2112K-3.3", "U2": "AP2112K-3.3",
             "U3": "STM32G031K8", "C1": "1u"}
    nets = {}
    _add(nets, "/VBUS", ("J1", "1", "VBUS", "passive"),
         ("U1", "1", "VIN", "power_in"), ("U1", "3", "EN", "input"),
         ("U2", "1", "VIN", "power_in"))
    _add(nets, "GND", ("J1", "2", "GND", "passive"),
         ("U1", "2", "GND", "power_in"), ("U2", "2", "GND", "power_in"),
         ("U3", "2", "VSS", "power_in"), ("C1", "2", "", "passive"))
    _add(nets, "/+3V3", ("U1", "5", "VOUT", "power_out"),
         ("U3", "1", "VDD", "power_in"))
    _add(nets, "/+3V3_SENS", ("U2", "5", "VOUT", "power_out"),
         ("C1", "1", "", "passive"))
    _add(nets, "/SENS_EN", ("U3", "10", "PA5", "bidirectional"),
         ("U2", "3", "EN", "input"))
    return comps, nets


def _sensor(comps, nets, pull_rail="/+3V3", r="4.7k"):
    """U4, an I2C sensor on +3V3_SENS, its SDA pulled up by R1."""
    comps.update({"U4": "BME280", "R1": r})
    _add(nets, "/+3V3_SENS", ("U4", "1", "VDD", "power_in"))
    _add(nets, "GND", ("U4", "2", "GND", "power_in"))
    _add(nets, "/SDA", ("U3", "11", "PB7", "bidirectional"),
         ("U4", "3", "SDA", "bidirectional"), ("R1", "1", "", "passive"))
    _add(nets, pull_rail, ("R1", "2", "", "passive"))
    return comps, nets


def test_pull_up_on_live_rail_into_switched_sensor_fails():
    hits, facts = _run(*_sensor(*_base()))
    (h,) = hits
    assert h["severity"] == "error" and h["from"] == "/+3V3"
    assert h["to"] == "/+3V3_SENS" and h["sink"].startswith("U4 SDA")
    assert 500 < h["ua"] < 700          # (3.3 - 0.5) / 4.7k
    assert "U3 PA5" in h["witness"] and "J1" in h["witness"]


def test_pull_up_on_switched_rail_passes():
    """The quiet twin: the pull-up sits on the sensor's own rail, which can
    never be on while +3V3 is off."""
    hits, facts = _run(*_sensor(*_base(), pull_rail="/+3V3_SENS"))
    assert hits == [] and facts["small_backfeed"] == []
    assert facts["checked"] >= 1


def test_weak_pull_up_is_a_warning_then_a_fact():
    hits, _ = _run(*_sensor(*_base(), r="47k"))        # about 60 uA
    assert [h["severity"] for h in hits] == ["warning"]
    hits, facts = _run(*_sensor(*_base(), r="82k"))    # about 34 uA
    assert hits == []
    (f,) = facts["small_backfeed"]
    assert "R1 pull-up" in f and "34 uA" in f


def _buffer(comps, nets, part):
    """The MCU's clock output MCO straight into a part on +3V3_SENS."""
    comps["U5"] = part
    _add(nets, "/+3V3_SENS", ("U5", "5", "VCC", "power_in"))
    _add(nets, "GND", ("U5", "3", "GND", "power_in"))
    _add(nets, "/SIG", ("U3", "12", "MCO", "output"),
         ("U5", "2", "A", "input"))
    return comps, nets


def test_output_straight_into_unpowered_part_fails():
    hits, _ = _run(*_buffer(*_base(), "74HC1G34"))
    (h,) = hits
    assert h["severity"] == "error" and h["ua"] is None
    assert h["sink"].startswith("U5 A")


def test_ioff_part_passes():
    """The twin: an LVC1G34's datasheet gives Ioff, so its input blocks."""
    hits, _ = _run(*_buffer(*_base(), "SN74LVC1G34DBVR"))
    assert hits == []


def test_gpio_straight_in_is_a_warning():
    comps, nets = _base()
    comps["U5"] = "74HC1G34"
    _add(nets, "/+3V3_SENS", ("U5", "5", "VCC", "power_in"))
    _add(nets, "/SIG", ("U3", "12", "PA1", "bidirectional"),
         ("U5", "2", "A", "input"))
    (h,) = _run(comps, nets)[0]
    assert h["severity"] == "warning" and h["kind"] == "gpio"
    assert "firmware" in check_backfeed._msg(h)


def _linux(bat54: bool):
    """linux-computer's case: the CH340 on the USB jack J2's own LDO drives
    DTR through R2 into Q1's base; Q1's collector holds the SoC's reset,
    pulled up by R3 to +3V3 (J1's rail)."""
    comps = {"J1": "Barrel", "J2": "USB_B", "U1": "AP2112K-3.3",
             "U6": "AP2112K-3.3", "U7": "RK3308", "U5": "CH340C",
             "R2": "10k", "R3": "10k", "Q1": "MMBT3904"}
    nets = {}
    _add(nets, "/VIN", ("J1", "1", "", "passive"),
         ("U1", "1", "VIN", "power_in"), ("U1", "3", "EN", "input"))
    _add(nets, "/VBUS", ("J2", "1", "VBUS", "passive"),
         ("U6", "1", "VIN", "power_in"), ("U6", "3", "EN", "input"))
    _add(nets, "GND", ("J1", "2", "", "passive"),
         ("J2", "2", "GND", "passive"), ("Q1", "2", "E", "passive"))
    _add(nets, "/+3V3", ("U1", "5", "VOUT", "power_out"),
         ("U7", "1", "VCC_IO", "power_in"), ("R3", "2", "", "passive"))
    _add(nets, "/CH_3V3", ("U6", "5", "VOUT", "power_out"),
         ("U5", "16", "VCC", "power_in"))
    _add(nets, "/DTR", ("U5", "13", "DTR", "output"),
         ("R2", "1", "", "passive"))
    _add(nets, "/Q1_B", ("R2", "2", "", "passive"),
         ("Q1", "1", "B", "passive"))
    if bat54:
        comps["D1"] = "BAT54"
        # the cathode on the collector: Q1 still pulls reset low
        _add(nets, "/Q1_C", ("Q1", "3", "C", "passive"),
             ("D1", "1", "K", "passive"))
        _add(nets, "/RESET_N", ("D1", "2", "A", "passive"))
    else:
        _add(nets, "/RESET_N", ("Q1", "3", "C", "passive"))
    _add(nets, "/RESET_N", ("U7", "20", "NRESET", "input"),
         ("R3", "1", "", "passive"))
    return comps, nets


def test_npn_collector_back_feed_fails():
    hits, _ = _run(*_linux(bat54=False))
    (h,) = [h for h in hits if h["sink"].startswith("U7")]
    assert h["severity"] == "error" and h["via"] == ["R2", "Q1"]
    assert h["from"] == "/CH_3V3" and h["to"] == "/+3V3"
    assert 200 < h["ua"] < 230          # (3.3 - 0.6 - 0.5) / 10k
    assert "J2" in h["witness"]


def test_npn_with_bat54_in_collector_passes():
    """The board's own fix: a BAT54 in the collector blocks the B-C path."""
    hits, facts = _run(*_linux(bat54=True))
    assert hits == [] and facts["small_backfeed"] == []


def test_single_source_tree_has_nothing_to_judge():
    comps, nets = _sensor(*_base())
    nets["/SENS_EN"] = [("U2", "3", "EN", "input")]
    nets["/VBUS"].append(("U2", "3", "EN", "input"))
    del nets["/SENS_EN"]
    hits, facts = _run(comps, nets)
    assert hits == [] and "no two rails" in facts["nothing_to_judge"]


def test_unknown_enable_is_not_judged():
    """U2's enable sits on a test point only: +3V3_SENS is unknown, so the
    pull-up into U4 is not judged rather than guessed."""
    comps, nets = _sensor(*_base())
    comps["TP1"] = "TestPoint"
    nets["/SENS_EN"] = [("U2", "3", "EN", "input"),
                        ("TP1", "1", "", "passive")]
    hits, facts = _run(comps, nets)
    assert hits == []
    assert any("+3V3_SENS" in s and "enable" in s
               for s in facts["not_judged"]), facts["not_judged"]


def _bus(two_connectors: bool):
    """A card: J1 brings +12V, which a three-pin AMS1117 (no enable pin)
    drops to +3V3 for U4; a +5V pull-up R1 drives U4's input. +5V comes
    in on J1 too, or on a second connector J2."""
    comps = {"J1": "Conn_02", "U1": "AMS1117-3.3", "U4": "74HC1G34",
             "R1": "10k"}
    nets = {}
    _add(nets, "/+12V", ("J1", "1", "", "passive"),
         ("U1", "3", "VI", "power_in"))
    if two_connectors:
        comps["J2"] = "Conn_02"
        _add(nets, "/+5V", ("J2", "1", "", "passive"))
    else:
        _add(nets, "/+5V", ("J1", "3", "", "passive"))
    _add(nets, "/+5V", ("R1", "2", "", "passive"))
    _add(nets, "GND", ("J1", "2", "", "passive"),
         ("U1", "1", "GND", "power_in"), ("U4", "3", "GND", "power_in"))
    _add(nets, "/+3V3", ("U1", "2", "VO", "power_out"),
         ("U4", "5", "VCC", "power_in"))
    _add(nets, "/SIG", ("R1", "1", "", "passive"),
         ("U4", "2", "A", "input"))
    return comps, nets


def test_pull_up_from_a_second_connector_fails():
    hits, _ = _run(*_bus(two_connectors=True))
    (h,) = hits
    assert h["severity"] == "error" and h["to"] == "/+3V3"
    assert h["witness"] == "on: J2 power; off: J1 power"


def test_rails_on_one_connector_and_a_three_pin_ldo_pass():
    """The twin: +5V and +12V come in on one cable, and the AMS1117 with no
    enable pin is on whenever +12V is, so +3V3 is never off under +5V."""
    hits, facts = _run(*_bus(two_connectors=False))
    assert hits == [] and "no two rails" in facts["nothing_to_judge"]


def _buck(buck: bool):
    """A gate driver U5 on +18V (J1) takes the MCU's PA1 on +3V3. +3V3
    comes from the LMR54406 buck U6 off +18V, whose SW pin the library
    types power_in, through L1; or, with no buck, from the header J3."""
    comps = {"J1": "Conn_01x02", "J3": "Conn_01x06", "U3": "STM32G031K8",
             "U5": "IX4427N"}
    nets = {}
    _add(nets, "/+18V", ("J1", "2", "", "passive"),
         ("U5", "6", "VCC", "power_in"))
    _add(nets, "GND", ("J1", "1", "", "passive"),
         ("J3", "1", "", "passive"), ("U3", "2", "VSS", "power_in"),
         ("U5", "3", "GND", "power_in"))
    _add(nets, "/+3V3", ("J3", "2", "", "passive"),
         ("U3", "1", "VDD", "power_in"))
    _add(nets, "/FAN", ("U3", "10", "PA1", "bidirectional"),
         ("U5", "2", "INA", "input"))
    if buck:
        comps.update({"U6": "LMR54406DBVR", "L1": "10u"})
        _add(nets, "/+18V", ("U6", "5", "VIN", "power_in"),
             ("U6", "4", "EN", "unspecified"))
        _add(nets, "GND", ("U6", "2", "GND", "power_out"))
        _add(nets, "Net-(U6-SW)", ("U6", "6", "SW", "power_in"),
             ("L1", "1", "", "passive"))
        _add(nets, "Net-(U6-CB)", ("U6", "1", "CB", "power_in"))
        _add(nets, "/+3V3", ("L1", "2", "", "passive"))
    return comps, nets


def test_gpio_from_a_separate_header_rail_into_a_gate_driver_warns():
    (h,) = _run(*_buck(buck=False))[0]
    assert h["severity"] == "warning" and h["to"] == "/+18V"
    assert h["witness"] == "on: J3 power; off: J1 power"


def test_buck_with_a_power_typed_sw_pin_is_read_as_the_source():
    """The twin: U6 makes +3V3 from +18V, so +3V3 is never on alone; its SW
    and CB nets are not rails."""
    hits, facts = _run(*_buck(buck=True))
    assert hits == [] and "no two rails" in facts["nothing_to_judge"]
    assert not any("U6-SW" in d or "U6-CB" in d for d in facts["domains"])


def test_a_path_ending_on_a_rail_names_the_rail_once():
    h = {"source": "R101 pull-up", "from": "+5V", "to": "3V3_PICO1",
         "sink": "D102 into 3V3_PICO1", "via": ["D102"], "ua": 2933.3,
         "witness": "on: J203 power", "kind": "pull", "into_rail": True}
    assert check_backfeed._msg(h).startswith(
        "R101 pull-up on +5V feeds 3V3_PICO1 through D102: about 2933 uA")


def _pico(vsys: bool):
    """zxdiag's case: R101 from +5V (J203) and the clamp D102 into the
    Pico U101's 3V3, which also feeds the OLED header J101. With its VSYS
    pin on PICO_PWR the Pico makes that 3V3 itself; without one, J101 is
    the only feed."""
    comps = {"J203": "Conn_02", "J101": "Conn_01x04", "U101": "Pico",
             "R101": "1K5", "D102": "1N4148", "D1": "1N5819"}
    nets = {}
    _add(nets, "+5V", ("J203", "1", "", "passive"),
         ("R101", "1", "", "passive"), ("D1", "2", "A", "passive"))
    _add(nets, "PICO_PWR", ("D1", "1", "K", "passive"))
    if vsys:
        _add(nets, "PICO_PWR", ("U101", "39", "VSYS", "power_in"))
    _add(nets, "GND", ("J203", "2", "", "passive"),
         ("J101", "4", "", "passive"), ("U101", "3", "GND", "power_in"))
    _add(nets, "3V3_PICO1", ("U101", "36", "3V3", "power_in"),
         ("J101", "3", "", "passive"), ("D102", "1", "K", "passive"))
    _add(nets, "MIN5V_DIV", ("R101", "2", "", "passive"),
         ("D102", "2", "A", "passive"))
    return comps, nets


def test_clamp_into_a_header_fed_rail_fails():
    hits, _ = _run(*_pico(vsys=False))
    (h,) = hits
    assert h["severity"] == "error" and h["to"] == "3V3_PICO1"
    assert h["via"] == ["D102"]


def test_a_modules_own_3v3_is_not_judged():
    """The twin: U101 has VSYS on PICO_PWR, so 3V3_PICO1 may be its own
    regulator's output; which way J101 runs is not known, so no guess."""
    hits, facts = _run(*_pico(vsys=True))
    assert hits == []
    assert any("3V3_PICO1" in s and "U101" in s and "VSYS" in s
               for s in facts["not_judged"]), facts["not_judged"]


def test_open_drain_pin_is_not_a_source():
    """The MCU's SDA (an I2C name) is not a push-pull source on its own."""
    comps, nets = _base()
    comps["U4"] = "BME280"
    _add(nets, "/+3V3_SENS", ("U4", "1", "VDD", "power_in"))
    _add(nets, "/I2C_SDA", ("U3", "11", "SDA", "bidirectional"),
         ("U4", "3", "SDA", "bidirectional"))
    assert _run(comps, nets)[0] == []


def _write(tmp_path, comps, nets) -> Path:
    c = " ".join(f'(comp (ref "{r}") (value "{v}"))' for r, v in comps.items())
    n = []
    for i, (name, nodes) in enumerate(nets.items()):
        nd = " ".join(f'(node (ref "{r}") (pin "{p}") (pinfunction "{f}") '
                      f'(pintype "{t}"))' for r, p, f, t in nodes)
        n.append(f'(net (code "{i + 1}") (name "{name}") {nd})')
    (tmp_path / "b.net").write_text(
        f'(export (version "E") (components {c}) (nets {" ".join(n)}))',
        encoding="utf-8")
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20260206) (generator "test"))\n',
                   encoding="utf-8")
    return pcb


def test_cli_report_and_exit_codes(tmp_path):
    pcb = _write(tmp_path, *_sensor(*_base()))
    out = tmp_path / "r.json"
    assert check_backfeed.main(["--pcb", str(pcb), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    (v,) = rep["violations"]
    assert v["kind"] == "backfeed" and v["refs"] == ["R1", "U4"]
    assert v["from_rail"] == "/+3V3" and v["current_ua"] > 500
    pcb = _write(tmp_path, *_sensor(*_base(), pull_rail="/+3V3_SENS"))
    assert check_backfeed.main(["--pcb", str(pcb), "--out", str(out)]) == 0


def test_no_netlist_is_nothing_to_judge(tmp_path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20260206) (generator "t"))\n')
    payload, _ = check_backfeed.run(["--pcb", str(pcb)])
    assert payload["status"] == "pass" and payload["nothing_to_judge"]


def test_golden_mutant_fires_and_its_twin_is_quiet(tmp_path):
    """manifest.d/check_backfeed.yaml's mutant on usbbuck4, and the board
    as it is."""
    import importlib
    sys.path.insert(0, str(REPO / "tests" / "golden" / "mutations"))
    mod = importlib.import_module("backfeed_pullup_live_rail")
    import netlist_audit as na
    for fault in (True, False):
        p = tmp_path / f"m-{fault}.net"
        p.write_text(mod.netlist(fault), encoding="utf-8")
        nl = na.parse_netlist(p)
        hits, _ = check_backfeed.analyse(
            nl, na._rail_volts({}, nl["nets"]), check_backfeed.load_ioff())
        assert [(h["severity"], "R98" in h["refs"]) for h in hits] == (
            [("error", True)] if fault else []), fault
    base = REPO / "tests" / "golden" / "netlist" / "usbbuck4.net"
    nl = na.parse_netlist(base)
    hits, facts = check_backfeed.analyse(
        nl, na._rail_volts({}, nl["nets"]), check_backfeed.load_ioff())
    assert hits == [] and facts["not_judged"] == []


def test_registered_in_verify_all():
    assert "check_backfeed" in {c["name"] for c in verify_all.load_checks()}
