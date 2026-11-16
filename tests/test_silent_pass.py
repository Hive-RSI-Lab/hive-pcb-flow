"""A check that left something unjudged must say so (#601, the rest of the #591
survey). Each case plants what the check can't judge and asserts it is named,
that it is not called a fault, and that the judged twin still judges."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import check_cap_load  # noqa: E402
import check_creepage  # noqa: E402
import check_current  # noqa: E402
import check_diffpair  # noqa: E402
import check_mate_pins  # noqa: E402
import check_ratings  # noqa: E402
import check_return_path  # noqa: E402
import check_testpoints  # noqa: E402
import check_thermal  # noqa: E402
import check_vias  # noqa: E402
import check_wicking  # noqa: E402
import dfm_check  # noqa: E402
import geom  # noqa: E402
import netlist_audit  # noqa: E402
from test_check_cap_load import PCB as CAP_PCB  # noqa: E402
from test_check_cap_load import _netlist as cap_netlist  # noqa: E402
from test_check_ratings import MCU, _run as ratings_run, _ws  # noqa: E402
from test_check_si import board as si_board, fp as si_fp, seg  # noqa: E402
from test_check_wicking import _board as wick_board, _setup  # noqa: E402
from test_mate_pins import HDR4, _board as mate_board, _fp as mate_fp  # noqa: E402


def _text(payload) -> str:
    return "\n".join(payload.get("not_judged") or [])


def _pcb(tmp_path, body="", setup="(setup)", stackup=False) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  {setup}
  (gr_rect (start 0 0) (end 40 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return p


def _cons(tmp_path, obj) -> Path:
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps(obj), encoding="utf-8")
    return c


# ---------------------------------------------------------------- creepage

def _via(x, net):
    return (f'  (via (at {x} 10) (size 0.6) (drill 0.3) '
            f'(layers "F.Cu" "B.Cu") (net "{net}"))\n')


# 0.3 mm of air between the two, far under IPC-2221's 48 V row
HV_BODY = _via(10, "+48V") + _via(10.9, "/SIG")


def _creepage(tmp_path, cons, body=HV_BODY):
    return check_creepage.run(["--pcb", str(_pcb(tmp_path, body)),
                               "--constraints",
                               str(_cons(tmp_path, cons))])[0]


def test_unlisted_48v_net_is_said_not_passed(tmp_path):
    """The bug: +48V unlisted reads 0 V, no pair is swept, status pass."""
    p = _creepage(tmp_path, {"voltages": [{"net": "/SIG", "voltage": 3.3}]})
    assert p["status"] == "pass"            # the name isn't evidence of a gap
    assert "+48V: its name reads 48 V" in _text(p)
    assert "nothing_to_judge" in p


def test_listing_the_48v_net_judges_the_same_gap(tmp_path):
    p = _creepage(tmp_path, {"voltages": [{"net": "+48V", "voltage": 48}]})
    assert p["status"] == "violations"
    assert p["violations"][0]["net"] == "+48V"
    assert "nothing_to_judge" not in p
    assert "+48V" not in _text(p)


@pytest.mark.parametrize("net,said", [
    ("VMOT", True), ("HV_BUS", True), ("VIN", True), ("+VDC", True),
    ("VIN_SENSE", False),        # a signal about the rail
    ("/SIG", False), ("GND", False), ("+3V3", False), ("+12V", False),
])
def test_unlisted_supply_with_no_stated_voltage(tmp_path, net, said):
    p = _creepage(tmp_path, {"voltages": [{"net": "/HV", "voltage": 60}]},
                  _via(10, "/HV") + _via(25, net))
    assert (f"{net}: a supply with no stated voltage" in _text(p)) is said


def test_power_entry_with_no_voltage_is_a_supply(tmp_path):
    p = _creepage(tmp_path, {"voltages": [{"net": "/HV", "voltage": 60}],
                             "power": [{"net": "/BUS", "current_a": 2}]},
                  _via(10, "/HV") + _via(25, "/BUS"))
    assert "/BUS: a supply" in _text(p)


def test_listed_hv_net_not_on_the_board_is_said(tmp_path):
    p = _creepage(tmp_path, {"voltages": [{"net": "/HV_RENAMED", "voltage": 60},
                                          {"net": "/LV", "voltage": 5}],
                             "voltage_pairs": [{"a": "/GONE", "b": "/SIG",
                                                "voltage": 100}]})
    assert "/HV_RENAMED: listed above 30 V but not on the board" in _text(p)
    assert "/GONE: listed above" in _text(p)
    assert "/LV:" not in _text(p)      # a low net missing costs no HV judgement


def test_no_voltages_is_nothing_to_judge(tmp_path):
    p = _creepage(tmp_path, {})
    assert "no voltages or voltage_pairs" in p["nothing_to_judge"]


def test_only_low_voltages_is_nothing_to_judge_with_its_own_reason(tmp_path):
    p = _creepage(tmp_path, {"voltages": [{"net": "/SIG", "voltage": 12}]},
                  _via(10, "/SIG") + _via(11, "GND"))
    assert "more than 30 V apart" in p["nothing_to_judge"]


# ----------------------------------------------------------------- ratings

def test_supply_pin_on_a_rail_of_unknown_voltage_is_named(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "VDD_MAIN", "2": "GND",
                                      "3": "+3V3", "4": "/ALERT"})],
             {"C1": MCU})
    p = ratings_run(ws)
    assert "VDD_MAIN: no known voltage, so 1 supply pin(s)" in _text(p)
    assert "U1.1" in _text(p)
    assert not [v for v in p["violations"] if v.get("net") == "VDD_MAIN"
                and v["severity"] == "error"]


def test_supply_pin_on_a_known_rail_is_judged_not_named(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND"})], {"C1": MCU})
    p = ratings_run(ws)
    assert "not_judged" not in p
    assert p["pins_checked"] == 1
    assert "nothing_to_judge" not in p


def test_signal_pin_on_an_unknown_net_is_only_counted(tmp_path):
    # VDD on a rail of unknown voltage (counted too): U1 can't bound /SIG
    # by its rail (#869)
    ws = _ws(tmp_path, [("U1", "C1", {"1": "VDD_X", "3": "/SIG"})], {"C1": MCU})
    p = ratings_run(ws)
    assert p["pins_on_unknown_nets"] == 2 and "/SIG" not in _text(p)


def test_a_signal_pin_driven_from_a_known_rail_makes_its_own_bound(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "3": "/SIG"})], {"C1": MCU})
    p = ratings_run(ws)
    assert p["pins_on_unknown_nets"] == 0
    assert any(c.startswith("U1.3 ") and "makes the bound on /SIG" in c
               for c in p["pins_covered_by_rule"])


def test_pad_missing_from_the_pinout_is_unrated(tmp_path):
    """A wrong-package extraction: pad 9 on a rail is a pin nobody compared."""
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "9": "+5V"})], {"C1": MCU})
    p = ratings_run(ws)
    un = [v for v in p["violations"] if v["kind"] == "rating_unrated"]
    assert len(un) == 1 and un[0]["severity"] == "warning"
    assert "9: on +5V, not in the extraction's pinout" in un[0]["msg"]


def test_ground_pad_missing_from_the_pinout_is_quiet(tmp_path):
    """The usual exposed pad: nothing to compare on ground."""
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "EP": "GND"})], {"C1": MCU})
    assert not ratings_run(ws)["violations"]


def test_extraction_with_no_pinout_is_unrated(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3"})],
             {"C1": {"mpn": "BARE", "pinout": []}})
    p = ratings_run(ws)
    assert [v["kind"] for v in p["violations"]] == ["rating_unrated"]
    assert "no pinout" in p["violations"][0]["msg"]


def test_ratings_with_nothing_compared_is_nothing_to_judge(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "VDD_MAIN"})], {"C1": MCU})
    p = ratings_run(ws)
    assert p["nothing_to_judge"] == (
        "no net on the board has a known voltage; no known voltage on its "
        "nets (1 of 1 parts not compared)")
    assert p["parts_not_compared"] == {"no known voltage on its nets": ["U1"]}


def test_ratings_nothing_judged_names_each_reason(tmp_path):
    """A board where no rating met a known voltage says why, part by part, in
    the reasons the coverage breakdown uses (#1330): biggest group first."""
    ws = _ws(tmp_path, [
        ("U1", "", {"1": "+5V", "2": "GND"}),
        ("U2", "", {"1": "+5V", "2": "GND"}),
        ("U3", "C9", {"1": "+5V", "2": "GND"}),
        ("U4", "C8", {"1": "+5V", "2": "GND"}),
        ("C1", "C7", {"1": "+5V", "2": "GND"}),
        ("U5", "C1", {"1": "VDD_X", "2": "GND"})],
        {"C1": MCU, "C8": {"mpn": "BARE", "pinout": []}})
    p = ratings_run(ws)
    assert p["parts_not_compared"] == {
        "no part number": ["U1", "U2"],
        "no datasheet read for its part number": ["U3"],
        "datasheet read but no pin rating it can use": ["U4"],
        "passive with no voltage field on its parts row": ["C1"],
        "no known voltage on its nets": ["U5"]}
    nj = p["nothing_to_judge"]
    assert nj == (
        "no part number (2 of 6 parts not compared) [also datasheet read but "
        "no pin rating it can use: 1; no datasheet read for its part number: "
        "1; no known voltage on its nets: 1; passive with no voltage field on "
        "its parts row: 1]")
    from errata_corpus import blocker_label
    assert blocker_label(nj) == "no part number"
    assert not re.search(r"\b[UC]\d", nj)


def test_ratings_judging_something_gives_no_reason(tmp_path):
    ws = _ws(tmp_path, [("U1", "C1", {"1": "+3V3", "2": "GND"}),
                        ("U2", "", {"1": "+3V3", "2": "GND"})], {"C1": MCU})
    p = ratings_run(ws)
    assert p.get("nothing_to_judge") is None
    assert p.get("parts_not_compared") is None


# ------------------------------------------- empty constraints: nothing judged

@pytest.mark.parametrize("mod,reason", [
    (check_current, "no power entries"),
    (check_thermal, "no thermal or ripple"),
    (check_return_path, "no high_speed"),
])
def test_empty_constraints_block_is_nothing_to_judge(tmp_path, mod, reason):
    p = mod.run(["--pcb", str(_pcb(tmp_path)), "--constraints",
                 str(_cons(tmp_path, {}))])[0]
    assert p["status"] == "pass"
    assert reason in p["nothing_to_judge"]


def test_current_with_an_entry_judges(tmp_path):
    body = (f'  (segment (start 1 1) (end 9 1) (width 0.1) (layer "F.Cu") '
            f'(net "+5V"))\n')
    p = check_current.run([
        "--pcb", str(_pcb(tmp_path, body)), "--constraints",
        str(_cons(tmp_path, {"power": [{"net": "+5V", "current_a": 5}]}))])[0]
    assert "nothing_to_judge" not in p
    assert p["status"] == "violations"


def test_switcher_no_table_knows_is_named(tmp_path):
    body = ('  (footprint "t:U" (layer "F.Cu") (at 5 5)\n'
            '    (property "Reference" "U7" (at 0 0 0))\n'
            '    (property "Value" "NOTINTABLE9" (at 0 0 0))\n'
            '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
            '(net "/SWN") (pinfunction "SW"))\n  )\n')
    p = check_current.run(["--pcb", str(_pcb(tmp_path, body)), "--constraints",
                           str(_cons(tmp_path, {}))])[0]
    assert "U7" in _text(p) and "/SWN" in _text(p)
    assert "switch_nodes.yaml" in _text(p)


# -------------------------------------------------------------- mate pins

def _mate(tmp_path, body, cons=None):
    argv = ["--pcb", str(mate_board(tmp_path / "m.kicad_pcb", body))]
    if cons is not None:
        argv += ["--constraints", str(_cons(tmp_path, cons))]
    return check_mate_pins.run(argv)[0]


def test_no_pairs_and_no_identical_connectors_is_nothing_to_judge(tmp_path):
    p = _mate(tmp_path, mate_fp("J1", HDR4, 5, 5, ["+5V", "A", "B", "GND"]))
    assert "no standard header on B.Cu" in p["nothing_to_judge"]


def test_a_back_side_standard_header_that_was_judged_is_not_nothing(tmp_path):
    """#893: the B.Cu header rule ran and passed, so this is a pass."""
    from test_mate_pins import _pi_board
    pcb = _pi_board(tmp_path, False)
    p, _ = check_mate_pins.run(["--pcb", str(pcb)])
    assert not p["violations"]
    assert "nothing_to_judge" not in p
    assert p["standard_headers"] == ["J1"]


def test_a_standard_header_off_its_pattern_is_named_not_passed(tmp_path):
    """Pi nets on a 1x40 row: the mirror can't be read, so it is not judged."""
    import mech_05_pi_header_mirrored as m
    nets = [m.NETS.get(str(i + 1)) for i in range(40)]
    p = _mate(tmp_path, mate_fp("J1", "Vendor:Row_1x40", 5, 5, nets,
                                layer="B.Cu"))
    assert not p["violations"]
    assert "J1 (Row_1x40) on B.Cu matches" in _text(p)
    assert "not judged" in _text(p)
    assert "nothing_to_judge" in p


def test_identical_unkeyed_connectors_are_judged(tmp_path):
    p = _mate(tmp_path, mate_fp("J1", HDR4, 5, 5, ["+5V", "A", "B", "GND"])
              + mate_fp("J2", HDR4, 5, 15, ["A", "+5V", "B", "GND"]))
    assert "nothing_to_judge" not in p
    assert {v["kind"] for v in p["violations"]} == {"mate_unkeyed_rail_swap"}


def test_identical_unkeyed_connectors_that_pass_show_as_judged(tmp_path):
    """Two same-net unkeyed headers: the rail-swap rule ran and passed, so the
    report names the group and the corpus reads it as judged, not as a run
    that judged nothing with no reason given."""
    import errata_corpus as ec
    nets = ["+5V", "A", "B", "GND"]
    p = _mate(tmp_path, mate_fp("J1", HDR4, 5, 5, nets)
              + mate_fp("J2", HDR4, 5, 15, nets))
    assert not p["violations"]
    assert "nothing_to_judge" not in p
    assert p["unkeyed_groups"] and "J1, J2" in p["unkeyed_groups"][0]
    assert ec.coverage(p, "check_mate_pins")["status"] == "judged"


def test_identical_connector_no_family_knows_is_named(tmp_path):
    """The rule can't say if they're keyed: same pinout swap as above, unseen."""
    odd = "Vendor:Mystery_1x04"
    p = _mate(tmp_path, mate_fp("J1", odd, 5, 5, ["+5V", "A", "B", "GND"])
              + mate_fp("J2", odd, 5, 15, ["A", "+5V", "B", "GND"]))
    assert not p["violations"]
    assert "J1, J2 (Mystery_1x04): no connector family" in _text(p)


def test_one_connector_no_family_knows_is_not_named(tmp_path):
    p = _mate(tmp_path, mate_fp("J1", "Vendor:Mystery", 5, 5, ["A", "B"]))
    assert "Mystery" not in _text(p)


def test_pair_with_no_nets_is_not_a_clean_pair(tmp_path):
    from test_mate_pins import SKT4
    mate_board(tmp_path / "mate" / "d.kicad_pcb",
               mate_fp("J1", SKT4, 10, 10, [None] * 4, layer="B.Cu"))
    p = _mate(tmp_path, mate_fp("J2", HDR4, 10, 10, [None] * 4),
              {"mating_pairs": [{"ref": "J2", "mate_ref": "J1",
                                 "mate_pcb": "mate/d.kicad_pcb",
                                 "mount": "stack"}]})
    assert p["pairs"][0]["mismatched"] == 0 and p["pairs"][0]["compared"] == 0
    assert "J2 <-> J1: no pin has a net on both sides" in _text(p)
    assert "no mating_pairs" in p["nothing_to_judge"]


# ------------------------------------------------------------------- si

def test_classified_net_not_routed_is_named(tmp_path):
    body = seg(10, 5, 60, 5, "/I2S_BCK") + si_fp(
        "U1", 10, 5, "X", [("1", 0, "/I2S_BCK"), ("2", 2, "/I2S_LRCK")])
    p = si_board(tmp_path, body)
    assert "/I2S_LRCK" in _text(p) and "not routed" in _text(p)
    assert "/I2S_BCK (" not in _text(p)


def test_board_with_no_classified_net_routed_says_so(tmp_path):
    p = si_board(tmp_path, si_fp("U1", 10, 5, "X", [("1", 0, "/I2S_BCK")]))
    assert "no classified net is routed" in p["nothing_to_judge"]


FAST = {"class": "declared", "t_rise_ns": 0.1, "zdiff": 100}


def test_zdiff_net_with_no_mate_is_named(tmp_path):
    body = seg(5, 5, 100, 5, "/LVDS") + si_fp("U1", 5, 5, "X", [("1", 0, "/LVDS")])
    p = si_board(tmp_path, body, {"si": {"nets": {"/LVDS": FAST}}})
    assert "/LVDS (declared): has a Zdiff target but no other half" in _text(p)


def test_zdiff_pair_with_mate_unrouted_is_named(tmp_path):
    body = seg(5, 5, 100, 5, "/C_P") + si_fp(
        "U1", 5, 5, "X", [("1", 0, "/C_P"), ("2", 2, "/C_N")])
    p = si_board(tmp_path, body, {"si": {"nets": {"/C_P": FAST, "/C_N": FAST}}})
    assert "/C_P//C_N: /C_N is not routed, so no Zdiff" in _text(p)
    assert not [v for v in p["violations"] if v["kind"] == "si_pair_uncoupled"]


def test_zdiff_pair_both_routed_is_judged_not_named(tmp_path):
    body = (seg(5, 5, 100, 5, "/C_P") + seg(5, 5.4, 100, 5.4, "/C_N")
            + si_fp("U1", 5, 5, "X", [("1", 0, "/C_P")])
            + si_fp("U2", 5, 5.4, "X", [("1", 0, "/C_N")]))
    far = {**FAST, "zdiff": 400}   # no 0.2/0.2 mm pair is 400 ohm: a real miss
    p = si_board(tmp_path, body, {"si": {"nets": {"/C_P": far, "/C_N": far}}})
    assert "Zdiff" not in _text(p)
    # main crashed here (coupling_stats' tuple read as a dict), so the check
    # errored on any routed transmission-line pair
    hit = [v for v in p["violations"] if v["kind"] == "si_impedance_target"
           and v.get("pair") == ["/C_N", "/C_P"]]
    assert hit and hit[0]["severity"] == "error" and hit[0]["target_ohm"] == 400


# --------------------------------------------------------------- diffpair

def _dp(tmp_path, cons=None, stackup=""):
    pads = "".join(
        f'  (footprint "t:{r}" (layer "F.Cu") (at {x} 5)\n'
        f'    (property "Reference" "{r}" (at 0 0 0))\n'
        f'    (pad "1" smd rect (at 0 0) (size .4 .4) (layers "F.Cu") '
        f'(net "{n}"))\n'
        f'    (pad "2" smd rect (at 0 .4) (size .4 .4) (layers "F.Cu") '
        f'(net "{m}"))\n  )\n'
        for r, x, n, m in (("U1", 5, "/X_P", "/X_N"), ("U2", 25, "/X_P", "/X_N")))
    body = (seg(5, 5, 25, 5, "/X_P") + seg(5, 5.4, 25, 5.4, "/X_N") + pads)
    argv = ["--pcb", str(_pcb(tmp_path, body))]
    if cons is not None:
        argv += ["--constraints", str(_cons(tmp_path, cons))]
    return check_diffpair.run(argv)[0]


def test_auto_found_pair_says_no_target_and_generic_limits(tmp_path):
    p = _dp(tmp_path)
    assert "/X_P//X_N: no impedance target" in _text(p)
    assert "generic 5 mm" in _text(p)


def test_declared_target_is_not_named(tmp_path):
    p = _dp(tmp_path, {"diff_pairs": [{"p": "/X_P", "n": "/X_N",
                                       "impedance_ohm": 90}]})
    assert "no impedance target" not in _text(p)


def test_sense_pair_wants_no_target(tmp_path):
    p = _dp(tmp_path, {"diff_pairs": [{"p": "/X_P", "n": "/X_N",
                                       "kind": "sense"}]})
    assert "no impedance target" not in _text(p)


def test_diffpair_reports_the_assumed_stackup(tmp_path):
    p = _dp(tmp_path)
    assert p["stackup_assumed"] is True
    assert "default FR4" in _text(p)


# --------------------------------------------------------------- cap load

@pytest.mark.parametrize("ref,inv,noninv,out", [
    ("U1", "1IN-", "1IN+", "1OUT"),    # TI's names, unseen before #601
    ("IC1", "-", "+", "~"),            # a non-U ref
    ("A1", "IN-", "IN+", "OUT"),
])
def test_opamp_names_and_refs_are_seen(tmp_path, ref, inv, noninv, out):
    nl = cap_netlist(tmp_path, {ref: "LMV358X", "C1": "10uF", "J1": "Jack"}, {
        "/OUT": [(ref, "1", out), (ref, "2", inv), ("C1", "1", "")],
        "/IN": [(ref, "3", noninv)],
        "/J": [("C1", "2", ""), ("J1", "1", "T")],
        "GND": [("J1", "2", "S")]})
    viols, seen = check_cap_load.check(CAP_PCB, nl)
    assert ref in seen["opamps"]


def test_connector_with_amp_like_pin_names_is_not_an_amp(tmp_path):
    nl = cap_netlist(tmp_path, {"J1": "X", "R1": "X"}, {
        "/A": [("J1", "1", "IN-"), ("R1", "1", "IN-")],
        "/B": [("J1", "2", "IN+"), ("R1", "2", "IN+")],
        "/C": [("J1", "3", "OUT")]})
    _, seen = check_cap_load.check(CAP_PCB, nl)
    assert not seen.get("opamps")


# ------------------------------------------------- tenting, stackup, polarity

def test_wicking_says_when_tenting_was_assumed(tmp_path):
    bare = wick_board(tmp_path, "(setup)", _via(5, 5))
    p = check_wicking.run(["--pcb", str(bare)])[0]
    assert p["checked"][0]["tenting_assumed"] is True
    assert "taken as tented" in _text(p)


def test_wicking_with_stated_tenting_says_nothing(tmp_path):
    pcb = wick_board(tmp_path, _setup(), _via(5, 5))
    p = check_wicking.run(["--pcb", str(pcb)])[0]
    assert p["checked"][0]["tenting_assumed"] is False
    assert "not_judged" not in p


def test_testpoints_says_when_tenting_was_assumed(tmp_path):
    bare = wick_board(tmp_path, "(setup)", _via(5, 5))
    p = check_testpoints.run(["--pcb", str(bare)])[0]
    assert "taken as tented" in _text(p)


def test_vias_reports_the_assumed_stackup(tmp_path):
    p = check_vias.run(["--pcb", str(_pcb(tmp_path, _via(5, "/A")))])[0]
    assert p["stackup_assumed"] is True


def test_polarity_counts_only_parts_it_compared(tmp_path):
    body = ('  (footprint "t:D" (layer "F.Cu") (at 5 5)\n'
            '    (property "Reference" "D1" (at 0 0 0))\n'
            '    (pad "A" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
            '(net "/X"))\n'
            '    (pad "K" smd rect (at 2 0) (size 1 1) (layers "F.Cu") '
            '(net "/Y"))\n  )\n')
    bg = geom.load_board(_pcb(tmp_path, body))
    nl = {"nets": {"/X": [{"ref": "D1", "pin": "1"}],
                   "/Y": [{"ref": "D1", "pin": "2"},
                          {"ref": "D2", "pin": "1"}]}}
    vios: list = []
    facts = dfm_check.check_polarity(bg, nl, vios)
    assert not vios
    assert facts["refs_checked"] == 0
    assert facts["refs_no_common_pads"] == ["D1"]
    assert facts["refs_only_in_netlist"] == ["D2"]


def test_ratings_reason_needs_a_rating_source(tmp_path):
    """Only a part with something to rate it by is held back by its nets: a
    passive with no part number is a passive with no field, and a part on
    nothing but ground isn't counted (#1330)."""
    ws = _ws(tmp_path, [
        ("R1", "", {"1": "NET_A", "2": "NET_B"}),
        ("U1", "", {"1": "NET_A", "2": "NET_C", "3": "NET_D"}),
        ("NT1", "", {"1": "GND", "2": "GND"})], {})
    p = ratings_run(ws)
    assert p["parts_not_compared"] == {
        "passive with no voltage field on its parts row": ["R1"],
        "no part number": ["U1"]}
