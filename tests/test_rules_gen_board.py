"""rules_gen on a dense 2-layer power board (lite-main, hifi-receiver-2):
plane_fed rails sized for their taps, neck-down rules where a power net meets
pads narrower than its width, and power-class clearance no wider than the
pins. Pure tests on a synthetic board file; no KiCad needed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_current  # noqa: E402
import geom  # noqa: E402
import rules_gen  # noqa: E402

CAP = yaml.safe_load((SKILL / "reference" / "jlc_capabilities.yaml")
                     .read_text("utf-8"))["design_rules"]["2layer_1oz"]
STACKUP = yaml.safe_load((SKILL / "reference" / "stackups.yaml")
                         .read_text("utf-8"))["stackups"]["JLC2313_1.6"]

PCB = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "VBUS") (net 2 "CC1") (net 3 "GND") (net 4 "+5V")
  (footprint "t:U1" (layer "F.Cu") (at 10 10)
    (property "Reference" "U1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 0.25 1.0) (layers "F.Cu") (net "VBUS"))
    (pad "2" smd rect (at 0.4 0) (size 0.25 1.0) (layers "F.Cu") (net "CC1"))
    (pad "3" smd rect (at 0.8 0) (size 0.25 1.0) (layers "F.Cu") (net "+5V"))
    (pad "4" smd rect (at 1.6 0) (size 0.6 1.0) (layers "F.Cu") (net "GND")))
  (footprint "t:J1" (layer "F.Cu") (at 20 10)
    (property "Reference" "J1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 3.0 3.0) (layers "F.Cu") (net "VBUS"))
    (pad "2" smd rect (at 4 0) (size 3.0 3.0) (layers "F.Cu") (net "GND")))
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
)
"""


@pytest.fixture
def bg(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(PCB, encoding="utf-8")
    return geom.BoardGeom.from_file(p)


def _w(a, dt=10.0):
    return round(check_current.required_width_mm(a, dt, 0.035), 4)


def test_plane_fed_rail_is_sized_for_its_taps():
    cons = {"power": [{"net": "VBUS", "current_a": 5.0, "plane_fed": True},
                      {"net": "PVDD", "current_a": 5.0, "plane_fed": True,
                       "via_amps": 1.0},
                      {"net": "GND", "current_a": 5.0, "plane_fed": True,
                       "tap_current_a": 0.3},
                      {"net": "+5V", "current_a": 1.0}],
            "planes": [{"net": "VBUS", "layer": "F.Cu"},
                       {"net": "GND", "layer": "B.Cu"}]}
    rules, facts = rules_gen.power_rules(cons, 0.035)
    by = {f["net"]: f for f in facts}
    assert by["VBUS"]["tap_current_a"] == 0.5          # check_current's default
    assert by["VBUS"]["min_width_mm"] == _w(0.5)
    assert by["VBUS"]["trunk_width_mm"] == _w(5.0) > 2.0
    assert by["PVDD"]["min_width_mm"] == _w(1.0)        # its via_amps
    assert by["GND"]["min_width_mm"] == _w(0.3)         # its tap_current_a
    assert by["+5V"]["min_width_mm"] == _w(1.0)         # not plane_fed
    assert "plane_fed" not in by["+5V"]
    assert by["VBUS"]["plane_declared"] and not by["PVDD"]["plane_declared"]
    dru = {r.name: r.minv for r in rules}
    assert dru["aiee_pwr_width_VBUS"] == _w(0.5)
    # the tap never claims more than the rail
    _, f2 = rules_gen.power_rules(
        {"power": [{"net": "X", "current_a": 0.2, "plane_fed": True}]}, 0.035)
    assert f2[0]["min_width_mm"] == _w(0.2)


def test_rules_gen_warns_plane_fed_without_a_plane(tmp_path, capsys):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"power": [
        {"net": "VBUS", "current_a": 5.0, "plane_fed": True}]}))
    assert rules_gen.main(["--constraints", str(c), "--layers", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["warnings"]) == 1 and "VBUS" in out["warnings"][0]


def test_power_class_clearance_is_the_fab_floor(bg):
    cons = {"power": [{"net": "VBUS", "current_a": 1.0},
                      {"net": "+5V", "current_a": 2.0}]}
    _, report = rules_gen.build(cons, CAP, STACKUP, False, bg)
    assert {c["clearance"] for c in report["classes"]} == \
        {CAP["min_clearance_mm"]}


def test_neck_rules_at_narrow_pads(bg):
    cons = {"power": [{"net": "VBUS", "current_a": 3.0},
                      {"net": "GND", "current_a": 0.3}]}
    rules, report = rules_gen.build(cons, CAP, STACKUP, False, bg)
    necks = {(n["net"], n["ref"]): n for n in report["necks"]}
    # VBUS (>2 mm at 3 A) meets U1's 0.25 mm pin and J1's 3 mm pad: only
    # U1 gets a neck, to the pin's own width
    assert set(necks) == {("VBUS", "U1")}
    assert necks[("VBUS", "U1")]["neck_mm"] == 0.25
    names = [r.name for r in rules]
    i_w = names.index("aiee_pwr_width_VBUS")
    i_n = names.index("aiee_pwr_neck_VBUS_U1")
    assert i_n > i_w                                  # later rule wins
    r = rules[i_n]
    assert r.constraint == "track_width" and r.minv == 0.25
    assert "A.intersectsCourtyard('U1')" in r.condition
    assert "A.NetName == 'VBUS'" in r.condition
    # GND's width fits every pad: no neck
    assert not any(n["net"] == "GND" for n in report["necks"])


def test_rules_gen_reads_the_board_beside_the_pro(tmp_path, capsys):
    (tmp_path / "b.kicad_pcb").write_text(PCB, encoding="utf-8")
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"power": [{"net": "VBUS", "current_a": 3.0}]}))
    pro = tmp_path / "b.kicad_pro"
    assert rules_gen.main(["--constraints", str(c), "--layers", "2",
                           "--pro", str(pro)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pcb"] == str(tmp_path / "b.kicad_pcb")
    assert [n["ref"] for n in out["necks"]] == ["U1"]
    assert rules_gen.main(["--constraints", str(c), "--layers", "2",
                           "--pcb", str(tmp_path / "nope.kicad_pcb")]) == 2


def test_package_ratings_emit_a_footprint_scoped_clearance_after_hv():
    """constraints package_ratings -> one courtyard-scoped clearance rule
    per part, after the HV rules (later wins in KiCad), in the shape
    check_creepage honours as a dru_footprint_scope waiver."""
    import check_creepage as cc
    cons = {"voltages": [{"net": "VIN", "voltage": 54}],
            "package_ratings": [{"ref": "U2", "clearance_mm": 0.1,
                                 "why": "LM5069 VIN pin at 0.5 mm pitch"}]}
    rules, report = rules_gen.build(cons, CAP, STACKUP, False)
    names = [r.name for r in rules]
    pkg = names.index("aiee_pkg_rating_U2")
    assert pkg > max(i for i, n in enumerate(names) if n.startswith("aiee_hv_"))
    r = rules[pkg]
    assert r.constraint == "clearance" and r.minv == 0.1
    # #649: zones are left out, so a pour under the part keeps the HV rule
    assert r.condition == ("A.intersectsCourtyard('U2') && "
                           "B.intersectsCourtyard('U2') && "
                           "A.Type != 'Zone' && B.Type != 'Zone'")
    assert report["package_rules"][0]["why"].startswith("LM5069")
    # check_creepage can read the scope back, zones excluded
    sc = cc.parse_footprint_scope(r.condition)
    assert sc["ref"] == "U2" and sc["courtyard"] and sc["no_zone"]


def test_package_ratings_reject_a_wildcard_ref():
    with pytest.raises(ValueError):
        rules_gen.package_rules({"package_ratings": [{"ref": "U*", "clearance_mm": 0.1}]})


def test_sense_pairs_get_no_impedance_rules():
    cons = {"diff_pairs": [{"p": "/a_KP", "n": "/a_KN", "kind": "sense"},
                           {"p": "/USB_DP", "n": "/USB_DM"}]}
    pairs = rules_gen.detect_diff_pairs(cons)
    assert [p["p"] for p in pairs] == ["/USB_DP"]


def test_package_rating_pads_only_scopes_to_the_parts_own_pads():
    (r,), _ = rules_gen.package_rules({"package_ratings": [
        {"ref": "J3", "clearance_mm": 0.09, "pads_only": True}]})
    assert r.condition == "A.memberOfFootprint('J3') && B.memberOfFootprint('J3')"


def test_package_rating_layer_keeps_the_other_side_at_its_floor():
    """The courtyard term is layer-blind: a 0.13 rating on a top-side QFN
    also lowered B.Cu under it below JLC's 2 oz outer 0.15 (dfm_release
    caught it, DRC can't). `layer` scopes the rule to the part's side."""
    (r,), (f,) = rules_gen.package_rules({"package_ratings": [
        {"ref": "U4", "clearance_mm": 0.13, "layer": "F.Cu"}]})
    assert '\t(layer "F.Cu")' in r.render()
    assert f["layer"] == "F.Cu"
    (r,), _ = rules_gen.package_rules({"package_ratings": [
        {"ref": "U4", "clearance_mm": 0.13}]})
    assert "(layer" not in r.render()
    with pytest.raises(ValueError):
        rules_gen.package_rules({"package_ratings": [
            {"ref": "U4", "clearance_mm": 0.13, "layer": "In1.Cu"}]})


def test_sense_width_rule_only_for_declared_sense_lines():
    """#1042: a rail with sense_pads gets one track_width rule at the fab
    minimum, held to its tracks inside route_critical's sense marks; a rail
    without them gets none."""
    cons = {"power": [
        {"net": "KP", "current_a": 3.0, "sense_pads": {"R911.1": "C911.1"}},
        {"net": "+5V", "current_a": 3.0}]}
    rules, facts = rules_gen.sense_width_rules(cons, CAP)
    [r] = rules
    assert r.name == "aiee_sense_width_KP" and r.constraint == "track_width"
    assert "A.NetName == 'KP'" in r.condition
    assert f"enclosedByArea('{rules_gen.SENSE_MARK}_*')" in r.condition
    assert facts == [{"net": "KP", "width_mm": CAP["min_trace_width_mm"],
                      "rule": r.name, "sense_pads": ["R911.1"]}]
