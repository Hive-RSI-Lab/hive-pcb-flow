"""S8 acceptance tests: board setup + reference data.

Plan S8 accept criteria:
  - from golden board 2's netlist + a constraints.json: initialized board passes
    DRC setup checks (schematic parity 0, no courtyard/setup violations; the
    unrouted board's unconnected_items are expected and excluded)
  - generated rules demonstrably enforced: a violating test track fails DRC with
    the custom rule named

Pure tests (reference-data validation, rules_gen/impedance/board_init logic) run
with no toolchain and are unmarked; live-kicad-cli tests carry `smoke`.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO
SCRIPTS = SKILL / "scripts"
REFERENCE = SKILL / "reference"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import fabfloors  # noqa: E402
import impedance as imp  # noqa: E402
import rules_gen  # noqa: E402
import board_init  # noqa: E402
import kc  # noqa: E402


# ============================================================ reference data

def test_capabilities_yaml_valid():
    cap = yaml.safe_load((REFERENCE / "jlc_capabilities.yaml").read_text("utf-8"))
    dr = cap["design_rules"]
    for cls in ("2layer_1oz", "4layer_1oz", "4layer_2oz", "6layer_1oz"):
        assert cls in dr, f"missing capability class {cls}"
    required = {"min_trace_width_mm", "min_clearance_mm", "min_via_drill_mm",
                "min_via_diameter_mm", "min_annular_ring_mm", "min_hole_to_hole_mm",
                "min_copper_to_edge_mm", "min_silk_width_mm"}
    for cls, row in dr.items():
        assert required <= set(row), f"{cls} missing {required - set(row)}"
        assert 0 < row["min_trace_width_mm"] < 1
    # cited source present
    assert cap["meta"]["source_urls"]
    # 4-layer is finer than 2-layer (sanity vs JLC published tiers)
    assert dr["4layer_1oz"]["min_trace_width_mm"] < dr["2layer_1oz"]["min_trace_width_mm"]


def test_stackups_yaml_valid():
    st = yaml.safe_load((REFERENCE / "stackups.yaml").read_text("utf-8"))
    assert st["defaults"][2] in st["stackups"]
    assert st["defaults"][4] in st["stackups"]
    for name, s in st["stackups"].items():
        coppers = [ly for ly in s["stack"] if ly["type"] == "copper"]
        assert len(coppers) == s["layers"], f"{name}: copper count != layers"
        # the declared lamination total is exactly what the stack sums to,
        # and the ORDERED (nominal) thickness is within 5% of it - JLC's
        # real 1.6 mm laminations sum to 1.58-1.66 mm.
        total = sum(ly["thickness_mm"] for ly in s["stack"])
        assert abs(total - s["stack_total_mm"]) < 1e-6, f"{name}: sums to {total}"
        # JLC's high-frequency line states FINISHED thickness (mask and
        # plating included), so those entries carry the allowance explicitly
        slack = float(s.get("finished_allowance_mm", 0.0))
        assert abs(total - s["thickness_mm"]) / s["thickness_mm"] < 0.05 \
            or 0 <= s["thickness_mm"] - total <= slack, \
            f"{name}: nominal {s['thickness_mm']} vs lamination {total}"
        for ci in s.get("controlled_impedance", []):
            assert ci["width_mm"] > 0
            if ci["kind"] == "diff":
                assert ci["gap_mm"] > 0
    # the 4-layer default has real copper layer names geom will match
    names = [ly["name"] for ly in st["stackups"][st["defaults"][4]]["stack"]
             if ly["type"] == "copper"]
    assert names == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_stackups_carry_verification_provenance():
    """T1: every entry says where its numbers came from, and no default
    points at a stackup JLC does not sell (the JLC04161H-3313 phantom sized
    a real 100R board; LEARNINGS 2026-07-30 [stackup][ordering])."""
    st = yaml.safe_load((REFERENCE / "stackups.yaml").read_text("utf-8"))
    for name, s in st["stackups"].items():
        assert "available" in s, f"{name}: no availability flag"
        prov = s.get("provenance")
        assert isinstance(prov, dict), f"{name}: no provenance block"
        assert prov.get("method") in {"jlc_open_api", "jlc_calculator_template",
                                      "vendor_page", "design_intent", "none"}, \
            f"{name}: unknown provenance method {prov.get('method')!r}"
        assert prov.get("verified"), f"{name}: provenance has no date"
        if prov["method"] == "jlc_open_api":
            assert prov.get("template_code"), f"{name}: no JLC template code"
            assert prov.get("endpoint"), f"{name}: no endpoint recorded"
        if prov["method"] == "design_intent":
            # Our lamination, not the fab's: never claimed verified or measured.
            assert prov.get("live_verified") is False, name
            assert s.get("epsilon_r_assumed") is True, name
            assert prov.get("source", "").startswith("https://"), name
        if s["available"] is False:
            ret = s.get("retired") or {}
            assert ret.get("reason"), f"{name}: retired without a reason"
            assert ret.get("replacements"), f"{name}: retired with no successor"
    for layers, name in st["defaults"].items():
        assert st["stackups"][name]["available"] is True, \
            f"defaults[{layers}] = {name} is not available"
        assert st["stackups"][name]["layers"] == layers


def test_controlled_impedance_matches_stack():
    """The published width/gap are impedance.py's field-solver output for the
    stack's own outer dielectric, and `closed_form` is the fallback's -
    regenerate this table whenever the stack changes."""
    st = yaml.safe_load((REFERENCE / "stackups.yaml").read_text("utf-8"))
    for name, s in st["stackups"].items():
        rows = s.get("controlled_impedance") or []
        if not rows or not s["available"] or s.get("vendor"):
            continue   # other fabs' tables: test_vendor_stackup_rows_regenerate
        h, er, oz = rules_gen.outer_microstrip_params(s)
        t = imp.CU_OZ_MM.get(oz, 0.035)
        for ci in rows:
            for method, row in (("field", ci), ("closed_form", ci["closed_form"])):
                tag = f"{name}/{ci['profile']}/{method}"
                if ci["kind"] == "single":
                    w = imp.solve_width(ci["impedance_ohm"], h, t, er, method=method)
                    assert abs(w - row["width_mm"]) < 5e-4, tag
                else:
                    w, g = imp.diff_pair(float(ci["impedance_ohm"]), h, t, er,
                                         method=method)
                    assert abs(w - row["width_mm"]) < 5e-4, f"{tag} width"
                    assert abs(g - row["gap_mm"]) < 5e-4, f"{tag} gap"


VENDOR_STACKS = {n: s for n, s in yaml.safe_load(
    (REFERENCE / "stackups.yaml").read_text("utf-8"))["stackups"].items()
    if s.get("vendor")}


def _same(a, b, tag):
    """Two controlled_impedance tables equal, floats to 5e-4 mm / 0.1 ohm."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), f"{tag}: {a} vs {b}"
        for k in a:
            _same(a[k], b[k], f"{tag}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), f"{tag}: length"
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{tag}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert abs(float(a) - float(b)) < (0.051 if "z_ohm" in tag else 5e-4), \
            f"{tag}: {a} vs {b}"
    else:
        assert a == b, f"{tag}: {a!r} vs {b!r}"


def test_vendor_stackups_present():
    """#446: PCBWay 4/6/8L, OSH Park 4/6L, AISLER 4/6/8L, each with its
    vendor's profile, a public source and outer se_50/diff_90/diff_100."""
    import vendors
    have = {(s["vendor"], s["layers"]) for s in VENDOR_STACKS.values()}
    want = {("pcbway", 4), ("pcbway", 6), ("pcbway", 8), ("oshpark", 4),
            ("oshpark", 6), ("aisler", 4), ("aisler", 6), ("aisler", 8)}
    assert want <= have, want - have
    for name, s in VENDOR_STACKS.items():
        vendors.load(s["vendor"])                  # a real profile
        assert name.startswith(s["vendor"].upper() + "-"), name
        prov = s["provenance"]
        assert prov["method"] in ("vendor_page", "design_intent")
        assert prov["source"].startswith("https://"), name
        for ly in s["stack"]:
            if ly["type"] != "copper":
                assert ly["epsilon_r"] > 3 and ly["thickness_mm"] > 0, name
        outer = {r["profile"] for r in s["controlled_impedance"]
                 if r["layer"] == "outer"}
        assert outer == {"se_50", "diff_90", "diff_100"}, name
        if s["layers"] >= 6:
            assert any(r["layer"] != "outer" for r in s["controlled_impedance"]), \
                f"{name}: no stripline rows"


@pytest.mark.skipif(not imp.SOLVER_OK, reason="field solver needs numpy/scipy")
@pytest.mark.parametrize("name", sorted(VENDOR_STACKS))
def test_vendor_stackup_rows_regenerate(name):
    """Each vendor table is stackup_rows.py's output for its own stack, at
    its vendor's min trace (regenerate with `hwde stackup_rows --stackup`)."""
    import stackup_rows
    s = VENDOR_STACKS[name]
    _same(s["controlled_impedance"], stackup_rows.rows_for(s), name)
    floor = stackup_rows.fab_floor(s)
    for r in s["controlled_impedance"]:
        assert r["width_mm"] >= floor - 1e-9, f"{name} {r['layer']} {r['profile']}"
        if r.get("gap_solved"):
            assert abs(r["width_mm"] - floor) < 1e-9
    assert _gaps_under_clearance(s) == [], name


def _gaps_under_clearance(s):
    """Rows whose pair gap is under the fab's clearance (outer layers: the
    outer clearance): a gap no fab draws (issue #741)."""
    import stackup_rows
    g_out, g_in = stackup_rows.gap_floor(s)
    return [f"{r['layer']} {r['profile']} gap {r['gap_mm']}"
            for r in s["controlled_impedance"]
            if r.get("gap_mm") is not None
            and r["gap_mm"] < (g_out if r["layer"] == "outer" else g_in) - 1e-9]


def test_a_gap_under_the_clearance_is_caught():
    """Planted fault for the gap floor: PCBWay 8L's diff_90 as it shipped
    before #741 (0.13 mm against 6 mil) fails the check, and rows_for never
    gives it again."""
    import copy
    import stackup_rows
    s = copy.deepcopy(VENDOR_STACKS["PCBWAY-8L-1.6-2116"])
    row = next(r for r in s["controlled_impedance"]
               if r["layer"] == "outer" and r["profile"] == "diff_90")
    row["gap_mm"] = 0.13
    assert _gaps_under_clearance(s) == ["outer diff_90 gap 0.13"]
    assert stackup_rows.gap_floor(s) == (0.1524, 0.1524)
    fresh = next(r for r in stackup_rows.rows_for(s)
                 if r["layer"] == "outer" and r["profile"] == "diff_90")
    assert fresh["gap_mm"] == pytest.approx(0.1524)


def test_pair_gap_holds_the_clearance():
    assert rules_gen.pair_gap(0.1) == 0.13
    assert rules_gen.pair_gap(0.5) == 0.30
    assert rules_gen.pair_gap(0.1, 0.1524) == 0.1524
    assert rules_gen.pair_gap(0.2, 0.1524) == 0.2


@pytest.mark.skipif(not imp.SOLVER_OK, reason="field solver needs numpy/scipy")
@pytest.mark.parametrize("name", sorted(n for n, s in VENDOR_STACKS.items()
                                        if s.get("fab_table")))
def test_vendor_fab_table_agrees(name):
    """The fab's own impedance table vs impedance.py at the fab's geometry:
    hwde_z_ohm is reproducible, and within the fab's +-10 % of target unless
    the row says why not (`disagrees`, measured on hw-lab zcoupon)."""
    s = VENDOR_STACKS[name]
    h, er, oz = rules_gen.outer_microstrip_params(s)
    t = imp.CU_OZ_MM[oz]
    for f in s["fab_table"]:
        if f["layer"] == "outer":
            z = (imp.microstrip_z0(f["width_mm"], h, t, er) if f["kind"] == "single"
                 else imp.zdiff(f["width_mm"], f["gap_mm"], h, t, er))
        else:
            geo = rules_gen.stripline_layers(
                s, {"planes": [{"layer": r} for r in f["reference"]]})[f["layer"]]
            res = imp.field_stripline(f["width_mm"], geo["h_base"], geo["h_fill"],
                                      geo["t"], geo["er_base"], geo["er_fill"],
                                      f.get("gap_mm"))
            z = res["zdiff" if f["kind"] == "diff" else "z0"]
        tag = f"{name} {f['layer']} {f['kind']} {f['impedance_ohm']}"
        assert abs(z - f["hwde_z_ohm"]) < 0.11, f"{tag}: {z:.2f}"
        off = abs(z - f["impedance_ohm"]) / f["impedance_ohm"]
        if f.get("disagrees"):
            assert off > 0.05, f"{tag}: agrees now ({z:.1f}); drop `disagrees`"
        else:
            assert off <= 0.10, f"{tag}: {z:.1f} ohm"


def test_vendor_fab_floors():
    """A vendor stackup's floors are its vendor's (#446): OSH Park 4L draws
    5 mil, keys the vendor doesn't publish come from JLC and are named, and
    a build the vendor has no rules for is refused, not given JLC's."""
    assert fabfloors.vendor_of({"vendor": "oshpark"}) == "oshpark"
    assert fabfloors.vendor_of({}) is None
    assert fabfloors.vendor_of({"vendor": "jlcpcb"}) is None
    cls, row = fabfloors.profile(4, 1.0, vendor="oshpark")
    assert cls == "oshpark:4layer_1oz"
    assert row["min_trace_width_mm"] == pytest.approx(0.127)
    assert "min_hole_to_hole_mm" in row["_floors_from_jlc"]
    _, jlc = fabfloors.profile(4, 1.0)
    assert row["min_hole_to_hole_mm"] == jlc["min_hole_to_hole_mm"]
    assert fabfloors.check_pro(
        {"board": {"design_settings": {"rules": fabfloors.pro_rules(row),
                                       "rule_severities": fabfloors.pro_rule_severities()}}},
        row) == []
    _, pw8 = fabfloors.profile(8, 1.0, vendor="pcbway")
    assert pw8["min_trace_width_mm"] == pytest.approx(0.127)
    with pytest.raises(fabfloors.FabFloorError, match="aisler publishes no design rules for 8layer"):
        fabfloors.profile(8, 1.0, vendor="aisler")


def test_rules_gen_vendor_stackup(tmp_path):
    """rules_gen --stackup OSHPARK-...: OSH Park's floors in the DRU baseline."""
    dru = tmp_path / "b.kicad_dru"
    rc = rules_gen.main(["--layers", "4", "--stackup", "OSHPARK-4L-1.6-FR408HR",
                         "--baseline-only", "--out-dru", str(dru),
                         "--out", str(tmp_path / "r.json")])
    assert rc == 0
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["capability_class"] == "oshpark:4layer_1oz"
    assert re.search(r'aiee_track_width_floor.*?\(min 0\.127', dru.read_text(),
                     re.S)


@pytest.mark.skipif(not imp.SOLVER_OK, reason="field solver needs numpy/scipy")
def test_outer_pair_under_vendor_floor_pins_width():
    """A 100 ohm outer pair on PCBWay 8L's 0.11 mm prepreg solves under
    PCBWay's 0.127 mm: rules_gen pins the width and solves the gap."""
    s = VENDOR_STACKS["PCBWAY-8L-1.6-2116"]
    pairs = [{"base": "D", "p": "D_P", "n": "D_N", "impedance_ohm": 100}]
    _, free = rules_gen.diff_pair_rules(pairs, s, {}, floor=0.0)
    assert free[0]["width_mm"] < 0.127
    rules, facts = rules_gen.diff_pair_rules(pairs, s, {}, floor=0.127)
    f = facts[0]
    assert f["width_mm"] == pytest.approx(0.127) and f.get("gap_solved")
    h, er, oz = rules_gen.outer_microstrip_params(s)
    assert imp.zdiff(f["width_mm"], f["gap_mm"], h, imp.CU_OZ_MM[oz], er) \
        == pytest.approx(100, abs=1.0)
    gap_rule = next(r for r in rules if r.name == "aiee_diff_gap_D")
    assert gap_rule.minv == pytest.approx(f["gap_mm"], abs=1e-4)


def test_rotations_csv_valid():
    rows = []
    with open(REFERENCE / "jlc_rotations.csv", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("regex,"):
                continue
            rx, rot = line.rsplit(",", 1)
            re.compile(rx)                     # each pattern must compile
            rows.append((rx, float(rot)))
    table = dict(rows)
    assert table["^SOT-23"] == 180.0
    assert table["^QFN-"] == 270.0
    assert table["^LQFP-"] == 270.0
    assert len(rows) >= 30


def test_dru_templates_exist_and_parse():
    for name in ("jlc_2layer_1oz", "jlc_4layer_1oz"):
        p = REFERENCE / "design_rules" / f"{name}.kicad_dru"
        text = p.read_text("utf-8")
        assert text.startswith("(version 1)")
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        opens = sum(ln.startswith("(rule ") for ln in lines)
        assert opens and opens == lines.count(")")  # each rule closes
        assert "track_width" in text and "clearance" in text


def test_dru_templates_match_generator(tmp_path):
    """Drift guard: committed templates == rules_gen --baseline-only output."""
    for name, layers in (("jlc_2layer_1oz", 2), ("jlc_4layer_1oz", 4)):
        out = tmp_path / f"{name}.kicad_dru"
        rc = rules_gen.main(["--layers", str(layers), "--copper-oz", "1.0",
                             "--baseline-only", "--out-dru", str(out),
                             "--out", str(tmp_path / "r.json")])
        assert rc == 0
        committed = (REFERENCE / "design_rules" / f"{name}.kicad_dru").read_text("utf-8")
        assert out.read_text("utf-8") == committed, f"{name} drifted from generator"


# ============================================================ impedance.py

def test_impedance_microstrip_reference():
    # classic: 50 ohm microstrip on 1.6 mm FR4 (er~4.2) ~ 2.9-3.1 mm
    for method in ("field", "closed_form"):
        w = imp.solve_width(50, 1.6, 0.035, 4.2, method=method)
        assert 2.8 < w < 3.2
        assert abs(imp.microstrip_z0(w, 1.6, 0.035, 4.2, method) - 50) < 0.1


def test_impedance_monotonic():
    z = [imp.microstrip_z0(w, 0.2104, 0.035, 4.05) for w in (0.1, 0.2, 0.4, 0.8)]
    assert z == sorted(z, reverse=True)   # Z0 falls as width grows


def test_impedance_diff_roundtrip():
    h, t, er = 0.2104, 0.035, 4.05
    for zt in (90, 100):
        w, s = imp.diff_pair(zt, h, t, er)
        assert 0.1 < w < 1.0 and 0.1 < s < 0.6
        assert abs(imp.zdiff(w, s, h, t, er) - zt) < 0.5
        w, s = imp.diff_pair(zt, h, t, er, method="closed_form")
        assert abs(imp._zdiff(w, s, h, t, er) - zt) < 0.5


def test_impedance_geometry_for():
    g = imp.geometry_for({"impedance_ohm": 90, "kind": "diff"}, 0.2104, 4.05, 1.0)
    assert g["width_mm"] > 0 and g["gap_mm"] > 0


# ============================================================ rules_gen logic

def test_capability_class():
    assert rules_gen.capability_class(4, 1.0) == "4layer_1oz"
    assert rules_gen.capability_class(2, 2.0) == "2layer_2oz"


def _cap(cls="4layer_1oz"):
    return yaml.safe_load((REFERENCE / "jlc_capabilities.yaml").read_text("utf-8"))["design_rules"][cls]


def _stackup(name="JLC04161H-1080B"):
    return yaml.safe_load((REFERENCE / "stackups.yaml").read_text("utf-8"))["stackups"][name]


def test_baseline_rules_shape():
    rules = rules_gen.baseline_rules(_cap())
    names = [r.name for r in rules]
    assert "aiee_track_width_floor" in names
    assert "aiee_clearance_floor" in names
    kinds = {r.constraint for r in rules}
    assert {"track_width", "clearance", "hole_size", "via_diameter",
            "annular_width", "edge_clearance", "hole_to_hole"} <= kinds


def test_power_rules_widths():
    cons = json.loads((GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    rules, facts = rules_gen.power_rules(cons, cu_mm=0.035)
    byn = {f["net"]: f for f in facts}
    assert byn["+3V3"]["min_width_mm"] == pytest.approx(0.20, abs=0.01)   # 0.4 A
    assert byn["VBUS"]["min_width_mm"] == pytest.approx(0.25, abs=0.01)   # 0.5 A
    r = {x.name: x for x in rules}["aiee_pwr_width_3V3"]
    assert "A.NetName == '+3V3'" in r.condition and r.constraint == "track_width"


def test_detect_diff_pairs():
    cons = json.loads((GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    pairs = rules_gen.detect_diff_pairs(cons)
    assert len(pairs) == 1
    dp = pairs[0]
    assert set([dp["p"], dp["n"]]) == {"/USB_DP", "/USB_DM"}
    assert dp["impedance_ohm"] == 90            # USB default
    # /MCO is single-ended, must NOT be paired
    assert all("MCO" not in p["p"] for p in pairs)


def test_rules_ordering_baseline_before_specific():
    cons = json.loads((GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    rules, _ = rules_gen.build(cons, _cap(), _stackup(), baseline_only=False)
    names = [r.name for r in rules]
    # every floor rule precedes every per-net rule (later rule wins -> specifics last)
    last_floor = max(i for i, n in enumerate(names) if n.endswith("_floor"))
    first_specific = min(i for i, n in enumerate(names)
                         if n.startswith("aiee_pwr_") or n.startswith("aiee_diff_"))
    assert last_floor < first_specific


def test_net_classes():
    cons = json.loads((GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    _, report = rules_gen.build(cons, _cap(), _stackup(), baseline_only=False)
    cnames = {c["name"] for c in report["classes"]}
    assert "Diff90" in cnames
    pats = {(p["netclass"], p["pattern"]) for p in report["patterns"]}
    assert ("Diff90", "/USB_DP") in pats
    # +3V3 (0.4 A -> 0.20 mm) needs nothing wider than the Default class;
    # VBUS (0.5 A -> 0.25 mm) gets its own class, at ITS width.
    assert ("Default", "+3V3") in pats
    assert ("Pwr_0p25mm", "VBUS") in pats
    assert {c["name"]: c["track_width"] for c in report["classes"]
            if c["name"].startswith("Pwr_")} == {"Pwr_0p25mm": 0.25}


def test_power_class_clearance_is_the_fab_floor():
    """A power class carries the fab floor clearance, not a blanket 0.2 mm
    that a power net on a fine-pitch part (CH224Q VBUS, 0.176 mm between
    pads) can never meet at its own pads; HV nets still get theirs."""
    cons = {"power": [{"net": "VBUS", "current_a": 3.0},
                      {"net": "V48", "current_a": 3.0}],
            "voltages": [{"net": "V48", "voltage": 48}]}
    cap = _cap()
    _, report = rules_gen.build(cons, cap, _stackup(), baseline_only=False)
    by = {c["name"]: c["clearance"] for c in report["classes"]}
    cls = {p["pattern"]: p["netclass"] for p in report["patterns"]}
    assert by[cls["VBUS"]] == pytest.approx(cap["min_clearance_mm"])
    assert by[cls["V48"]] > 0.2


def test_rule_names_unique_when_nets_fold_together():
    """_safe folds + and - to _, so A_SPK_L+ / A_SPK_L- shared one rule
    name in the .kicad_dru (hi-fi core board)."""
    cons = {"power": [{"net": "/amp_a/A_SPK_L+", "current_a": 3.0},
                      {"net": "/amp_a/A_SPK_L-", "current_a": 3.0}]}
    rules, report = rules_gen.build(cons, _cap(), _stackup(),
                                    baseline_only=False)
    names = [r.name for r in rules]
    assert len(names) == len(set(names)), names
    assert "aiee_pwr_width_amp_a_A_SPK_L" in names
    assert "aiee_pwr_width_amp_a_A_SPK_L_2" in names
    assert report["renamed_rules"] == [
        {"from": "aiee_pwr_width_amp_a_A_SPK_L",
         "to": "aiee_pwr_width_amp_a_A_SPK_L_2"}]


def test_power_netclasses_split_by_current():
    """A 20 mA rail must NOT inherit a 5 A trunk's width: one class per
    required width, thin rails on Default. Flattening them into one wide
    "Power" class is what fed Freerouting 1.75 mm traces for /VDD
    (LEARNINGS 2026-07-28 [routing][rules_gen][freerouting])."""
    cons = {"power": [{"net": "VBUS", "current_a": 5.0},
                      {"net": "/VDD", "current_a": 0.02},
                      {"net": "/VIND", "current_a": 0.05}]}
    cap = _cap("2layer_2oz")
    _, report = rules_gen.build(cons, cap, _stackup("JLC2313_1.6_2oz"),
                                baseline_only=False)
    by_net = {f["net"]: f for f in report["power"]}
    assert by_net["VBUS"]["class_width_mm"] > 1.0          # the 5 A trunk
    assert by_net["/VDD"]["netclass"] == "Default"
    assert by_net["/VIND"]["netclass"] == "Default"
    assert by_net["VBUS"]["netclass"] != by_net["/VDD"]["netclass"]
    widths = {c["name"]: c["track_width"] for c in report["classes"]}
    assert len(widths) == 1 and by_net["VBUS"]["netclass"] in widths
    # and the DRU still holds every net to its OWN minimum
    rules, _ = rules_gen.build(cons, cap, _stackup("JLC2313_1.6_2oz"), False)
    dru = {r.name: r.minv for r in rules}
    assert dru["aiee_pwr_width_VBUS"] > dru["aiee_pwr_width_VDD"]


# ============================================================ board_init logic

MINI_NET = """(export (version "E")
  (components
    (comp (ref "R1") (value "10k") (footprint "Resistor_SMD:R_0603_1608Metric"))
    (comp (ref "C1") (value "100nF") (footprint "Capacitor_SMD:C_0603_1608Metric")))
  (nets
    (net (code "1") (name "+3V3") (node (ref "R1") (pin "1")) (node (ref "C1") (pin "1")))
    (net (code "2") (name "GND") (node (ref "R1") (pin "2")) (node (ref "C1") (pin "2")))))
"""


def test_resolve_origin(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    assert board_init.resolve_origin("auto", pcb) == (None, "auto")
    assert board_init.resolve_origin("keep", pcb)[0] is None   # no board yet
    assert board_init.resolve_origin("12.5, -3", pcb) == ([12.5, -3.0], "given")
    pcb.write_text("""(kicad_pcb
  (gr_line (start 23.5 41) (end 103.5 41) (layer "Edge.Cuts") (width 0.1))
  (gr_arc (start 20.5 44) (mid 21.38 41.88) (end 23.5 41) (layer "Edge.Cuts"))
  (gr_line (start 20.5 44) (end 20.5 120) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 0) (end 5 5) (layer "F.SilkS") (width 0.1))
)""", encoding="utf-8")
    assert board_init.resolve_origin("keep", pcb) == ([20.5, 41.0], "kept from b.kicad_pcb")
    with pytest.raises(RuntimeError, match="bad --origin"):
        board_init.resolve_origin("left", pcb)


def test_parse_netlist(tmp_path):
    n = tmp_path / "m.net"
    n.write_text(MINI_NET, encoding="utf-8")
    comps, netmap = board_init.parse_netlist(n)
    assert {c["ref"] for c in comps} == {"R1", "C1"}
    assert comps[0]["fp"] == "Resistor_SMD:R_0603_1608Metric"
    assert netmap["R1.1"] == "+3V3" and netmap["C1.2"] == "GND"


def test_parse_netlist_missing_fp_raises(tmp_path):
    bad = MINI_NET.replace('(footprint "Resistor_SMD:R_0603_1608Metric")', "")
    n = tmp_path / "bad.net"
    n.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        board_init.parse_netlist(n)


def test_parse_netlist_custom_fields(tmp_path):
    """Custom symbol fields (LCSC...) are extracted; native ones excluded.

    S14 finding: symbols carrying an LCSC field made board_init's parity
    self-check fail (footprint_symbol_field_mismatch x N) because the fields
    never reached the footprints. parse_netlist must surface them.
    """
    with_fields = MINI_NET.replace(
        '(comp (ref "R1") (value "10k") '
        '(footprint "Resistor_SMD:R_0603_1608Metric"))',
        '(comp (ref "R1") (value "10k") '
        '(footprint "Resistor_SMD:R_0603_1608Metric")\n'
        '      (fields (field (name "LCSC") "C25804")\n'
        '              (field (name "Footprint") "Resistor_SMD:R_0603")\n'
        '              (field (name "Datasheet") "url")))')
    n = tmp_path / "f.net"
    n.write_text(with_fields, encoding="utf-8")
    comps, _ = board_init.parse_netlist(n)
    by_ref = {c["ref"]: c for c in comps}
    assert by_ref["R1"]["fields"] == {"LCSC": "C25804"}  # natives excluded
    assert by_ref["C1"]["fields"] == {}


def test_parse_netlist_dnp_and_exclude_from_bom(tmp_path):
    """#439: (in_bom no) reaches the netlist as a valueless
    `(property (name "exclude_from_bom"))`, like dnp; parse_netlist surfaces
    both so the worker can mirror them onto the footprint."""
    txt = MINI_NET.replace(
        '(footprint "Resistor_SMD:R_0603_1608Metric"))',
        '(footprint "Resistor_SMD:R_0603_1608Metric")\n'
        '      (property (name "exclude_from_bom")) (property (name "dnp")))')
    n = tmp_path / "x.net"
    n.write_text(txt, encoding="utf-8")
    by_ref = {c["ref"]: c for c in board_init.parse_netlist(n)[0]}
    assert by_ref["R1"]["dnp"] and by_ref["R1"]["exclude_from_bom"]
    assert not by_ref["C1"]["dnp"] and not by_ref["C1"]["exclude_from_bom"]


def test_transient_silk_partition():
    """S14: cross-footprint silk at shelf positions is transient (placement
    re-positions); single-footprint silk (own silk over own pad) is a library
    defect and must still fail init."""
    cross = {"check": "silk_overlap", "refs": ["D2", "U1"]}
    cross2 = {"check": "silk_over_copper", "refs": ["C11", "C5"]}
    own = {"check": "silk_over_copper", "refs": ["D2"]}
    other = {"check": "courtyards_overlap", "refs": ["A", "B"]}
    edge = {"check": "silk_edge_clearance", "refs": ["SW1"]}
    suffixed = {"check": "silk_over_copper", "refs": ["R9", "R2B"]}
    assert board_init._is_transient_silk(cross)
    assert board_init._is_transient_silk(cross2)
    assert not board_init._is_transient_silk(own)
    assert not board_init._is_transient_silk(other)
    assert board_init._is_transient_silk(edge)      # edge = positional
    assert board_init._is_transient_silk(suffixed)  # R2B extracted (kc fix)


def _parity(msg="No corresponding pin found in schematic", items=None):
    if items is None:                       # [] means "no items", not "the default"
        items = ["unconnected-(U1-PA0)"]
    return {"source": "parity", "check": "net_conflict", "msg": msg,
            "items": [{"msg": m} for m in items]}


def test_rejects_unconnected_nets_needs_every_signal():
    """The retry trigger. "True when EVERY parity violation is KiCad refusing a
    pad that carries an `unconnected-(...)` pseudo-net" - so anything that is
    not that exact signature must come back False and let the real defect fail
    init, and an empty parity list is not a match either."""
    ok = {"setup_violations": [_parity(), _parity(items=["unconnected-(U2-PB1)"]),
                               {"source": "drc", "check": "clearance"}]}
    assert board_init._rejects_unconnected_nets(ok)

    # "The `unconnected-` guard is what keeps a genuine pad-not-in-symbol defect
    # from being silently retried away": same msg, but no pseudo-net in items.
    assert not board_init._rejects_unconnected_nets(
        {"setup_violations": [_parity(items=["/main/SDA"])]})
    assert not board_init._rejects_unconnected_nets(
        {"setup_violations": [_parity(items=[])]})
    # one good, one genuine -> not every violation matches
    assert not board_init._rejects_unconnected_nets(
        {"setup_violations": [_parity(), _parity(items=["+3V3"])]})
    # right items, wrong message / wrong check
    assert not board_init._rejects_unconnected_nets(
        {"setup_violations": [_parity(msg="Pad missing net given by schematic")]})
    assert not board_init._rejects_unconnected_nets(
        {"setup_violations": [dict(_parity(), check="missing_footprint")]})
    # no parity violations at all is not a reason to rebuild
    assert not board_init._rejects_unconnected_nets({"setup_violations": []})
    assert not board_init._rejects_unconnected_nets(
        {"setup_violations": [{"source": "drc", "check": "net_conflict"}]})


def test_build_stackup_block():
    block = board_init.build_stackup_block(_stackup())
    assert block.lstrip().startswith("(stackup")
    for cu in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
        assert f'"{cu}"' in block
    assert "dielectric 1" in block and "epsilon_r" in block
    assert "copper_finish" in block


def test_last_json_helper():
    assert board_init._last_json('noise\n{"a": 1}\n')["a"] == 1
    assert board_init._last_json("nothing here") is None


# ==================================== board_swig footprint root (no-container host)

def _board_swig_module():
    """Import lib/board_swig.py with a stub pcbnew - resolve_default_fp_root
    is pure pathlib/os/re and needs neither KiCad nor a real board."""
    import importlib.util
    import types
    sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
    spec = importlib.util.spec_from_file_location(
        "board_swig", SCRIPTS / "lib" / "board_swig.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_default_fp_root_prefers_highest_kicad_env_var(tmp_path):
    mod = _board_swig_module()
    k9, k10 = tmp_path / "k9", tmp_path / "k10"
    k9.mkdir()
    k10.mkdir()
    got = mod.resolve_default_fp_root(
        {"KICAD9_FOOTPRINT_DIR": str(k9), "KICAD10_FOOTPRINT_DIR": str(k10)},
        executable="/usr/bin/python3")
    assert got == k10


def test_resolve_default_fp_root_skips_a_var_naming_a_missing_dir(tmp_path):
    mod = _board_swig_module()
    k9 = tmp_path / "k9"
    k9.mkdir()
    got = mod.resolve_default_fp_root(
        {"KICAD10_FOOTPRINT_DIR": str(tmp_path / "does-not-exist"),
         "KICAD9_FOOTPRINT_DIR": str(k9)},
        executable="/usr/bin/python3")
    assert got == k9


def test_resolve_default_fp_root_falls_back_to_executable_layout():
    mod = _board_swig_module()
    got = mod.resolve_default_fp_root({}, executable="/opt/kicad/bin/python")
    assert got == Path("/opt/kicad/share/kicad/footprints")


# ==================================================== T1: fab floors (one source)

def test_fabfloors_profile_and_rules():
    cls, cap = fabfloors.profile(4, 1.0)
    assert cls == "4layer_1oz"
    rules = fabfloors.pro_rules(cap)
    assert rules["min_track_width"] == cap["min_trace_width_mm"] == 0.1016
    assert rules["min_hole_to_hole"] == cap["min_hole_to_hole_mm"] == 0.5
    assert set(rules) == set(fabfloors.PRO_RULE_KEYS)
    with pytest.raises(fabfloors.FabFloorError, match="99layer_1oz"):
        fabfloors.profile(99, 1.0)


def test_fabfloors_check_pro_catches_the_shipped_defects():
    """The two defects that shipped on real boards: a floor BELOW the fab
    profile, and a floor at severity warning (LEARNINGS 2026-07-29 /
    2026-07-30 [board_init][rules_gen][dfm][gates])."""
    _, cap = fabfloors.profile(4, 1.0)
    good = board_init.build_pro("b.kicad_pro", cap)
    assert fabfloors.check_pro(good, cap) == []

    sub_fab = json.loads(json.dumps(good))
    sub_fab["board"]["design_settings"]["rules"]["min_track_width"] = 0.1
    msgs = fabfloors.check_pro(sub_fab, cap)
    assert any("min_track_width" in m and "BELOW" in m for m in msgs)

    warn = json.loads(json.dumps(good))
    warn["board"]["design_settings"]["rule_severities"]["hole_to_hole"] = "warning"
    assert any("hole_to_hole" in m for m in fabfloors.check_pro(warn, cap))

    empty = fabfloors.check_pro({}, cap)
    assert len(empty) == len(fabfloors.PRO_RULE_KEYS) + len(fabfloors.FLOOR_SEVERITIES)


@pytest.mark.parametrize("layers,oz", [(2, 1.0), (2, 2.0), (4, 1.0), (4, 2.0)])
def test_board_init_pro_floors_at_error(tmp_path, layers, oz):
    """board_init's project file must never ship a floor below the chosen
    JLC profile, and the checks that enforce them are ERROR, not warning."""
    _, cap = fabfloors.profile(layers, oz)
    pro_path = tmp_path / "b.kicad_pro"
    floors = board_init.write_pro(pro_path, cap)
    pro = json.loads(pro_path.read_text("utf-8"))
    assert fabfloors.check_pro(pro, cap) == []
    assert floors["min_track_width"] == cap["min_trace_width_mm"]
    sev = pro["board"]["design_settings"]["rule_severities"]
    assert sev["hole_to_hole"] == "error" and sev["track_width"] == "error"
    # the library-noise suppressions survive alongside the floors
    assert sev["lib_footprint_issues"] == "ignore"


def test_board_init_write_pro_refuses_sub_fab(tmp_path, monkeypatch):
    """A regression guard on build_pro itself: if it ever emits a floor
    below the profile again, write_pro raises instead of writing."""
    _, cap = fabfloors.profile(4, 1.0)
    # board_init imports the module as lib.fabfloors - patch ITS binding
    monkeypatch.setattr(board_init.fabfloors, "pro_rules",
                        lambda c: {**{k: float(c[v]) for k, v
                                      in fabfloors.PRO_RULE_KEYS.items()},
                                   "min_track_width": 0.1})
    with pytest.raises(RuntimeError, match="sub-fab floors"):
        board_init.write_pro(tmp_path / "b.kicad_pro", cap)
    assert not (tmp_path / "b.kicad_pro").exists()


def test_rules_gen_pro_writes_same_floors_and_severities(tmp_path):
    """rules_gen --pro and board_init must agree by construction: same
    single source, so a re-run of either cannot lower the floors."""
    _, cap = fabfloors.profile(4, 1.0)
    pro_path = tmp_path / "b.kicad_pro"
    board_init.write_pro(pro_path, cap)
    before = json.loads(pro_path.read_text("utf-8"))
    rules_gen.update_pro(pro_path, [], [], cap)
    after = json.loads(pro_path.read_text("utf-8"))
    ds_b = before["board"]["design_settings"]
    ds_a = after["board"]["design_settings"]
    assert ds_a["rules"] == ds_b["rules"]
    assert fabfloors.check_pro(after, cap) == []
    assert ds_a["rule_severities"]["lib_footprint_mismatch"] == "ignore"
    assert after["net_settings"]["classes"][0]["name"] == "Default"


def test_rules_gen_pro_repairs_a_sub_fab_project(tmp_path):
    """The lumina-carrier shape: a project file already carrying KiCad's
    0.25 mm hole floor at warning is REPAIRED, not merged around."""
    _, cap = fabfloors.profile(4, 1.0)
    pro_path = tmp_path / "b.kicad_pro"
    pro_path.write_text(json.dumps({"board": {"design_settings": {
        "rules": {"min_track_width": 0.1, "min_hole_to_hole": 0.25},
        "rule_severities": {"hole_to_hole": "warning"}}}}), encoding="utf-8")
    rules_gen.update_pro(pro_path, [], [], cap)
    pro = json.loads(pro_path.read_text("utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == 0.5
    assert fabfloors.check_pro(pro, cap) == []


# ============================================================ smoke: live kicad

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


def _drc(cli, pcb, parity=False):
    return kc.run_drc(cli, pcb, parity=parity)


def _rule_names(report):
    out = []
    for v in report["violations"]:
        m = re.search(r"rule '([^']+)'", v["msg"] or "")
        if m:
            out.append(m.group(1))
    return out


@pytest.fixture()
def usbbuck4_net(cli, tmp_path_factory):
    """Export golden board 2's netlist once (input to board_init)."""
    d = tmp_path_factory.mktemp("net")
    out = d / "usbbuck4.net"
    r = kc.export_netlist(cli, GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch", out)
    assert r["status"] == "pass"
    return out


@pytest.mark.smoke
def test_board_init_end_to_end(cli, usbbuck4_net, tmp_path):
    """ACCEPTANCE: netlist -> initialized board, parity clean, no setup violations."""
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4", "--mounting-holes", "4",
        "--schematic", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch"),
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    sc = r["self_check"]
    assert sc["parity_count"] == 0            # every part+net imported correctly
    assert sc["setup_violations"] == []        # no courtyard/short/mask/silk
    assert sc["unconnected_count"] > 0         # unrouted by design
    assert r["components"] == 23 and r["mounting_holes"] == 4

    # stackup was injected and geom reads it as authoritative (not FR4-assumed)
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import geom
    bg = geom.load_board(tmp_path / "kicad" / "usbbuck4.kicad_pcb")
    assert bg.stackup.assumed is False
    assert bg.stackup.copper_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


@pytest.mark.smoke
def test_board_init_writes_profile_floors(cli, usbbuck4_net, tmp_path):
    """ACCEPTANCE (T1): the project file board_init ships satisfies the
    chosen JLC profile at ERROR severity - and the board it just built is
    still DRC-clean under those (stricter) floors."""
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--schematic", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch"),
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["fab_profile"] == "4layer_1oz" and r["copper_oz"] == 1.0
    _, cap = fabfloors.profile(4, 1.0)
    pro = json.loads((tmp_path / "kicad" / "usbbuck4.kicad_pro").read_text("utf-8"))
    assert fabfloors.check_pro(pro, cap) == []
    # the two values that shipped wrong on real boards
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_track_width"] == 0.1016 and rules["min_hole_to_hole"] == 0.5
    assert r["self_check"]["setup_violations"] == []


@pytest.mark.smoke
def test_board_init_refuses_unavailable_stackup(cli, usbbuck4_net, tmp_path):
    """A stackup JLC does not sell must fail LOUDLY by name, with its
    replacement - never silently size a board (the old JLC04161H-3313 phantom;
    7628G is the retired entry kept for this)."""
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--stackup", "JLC04161H-7628G", "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 2 and r["status"] == "error"
    assert "available: false" in r["error"]
    assert "JLC04161H-1080B" in r["error"]          # names the replacement


def test_rules_gen_refuses_unavailable_stackup(tmp_path, capsys):
    rc = rules_gen.main(["--layers", "4", "--stackup", "JLC04161H-7628G",
                         "--out-dru", str(tmp_path / "b.kicad_dru")])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "JLC04161H-1080B" in out["error"]


def _edge_shapes(pcb: Path, kind: str) -> int:
    """Count `kind` primitives that sit on Edge.Cuts."""
    txt = pcb.read_text(encoding="utf-8")
    return sum(1 for m in re.finditer(r"\(%s\b" % kind, txt)
               if "Edge.Cuts" in txt[m.start():m.start() + 400])


@pytest.mark.smoke
def test_board_init_rounded_corners(cli, usbbuck4_net, tmp_path):
    """MECH-01: --corner-radius draws the outline as 4 lines + 4 corner arcs
    (pcbnew has no filleted-rect primitive) and geom must still read one closed
    outline polygon from it. Mounting holes move out of the rounded quadrant."""
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4", "--margin", "10",
        "--mounting-holes", "4", "--corner-radius", "4",
        "--schematic", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch"),
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["setup_violations"] == []
    assert r["self_check"]["parity_count"] == 0
    assert r["corner_radius"] == 4.0            # inset 5.0 > 4.0, no clamp

    pcb = tmp_path / "kicad" / "usbbuck4.kicad_pcb"
    assert (_edge_shapes(pcb, "gr_line"), _edge_shapes(pcb, "gr_arc"),
            _edge_shapes(pcb, "gr_rect")) == (4, 4, 0)

    # the arc/line loop must remain a valid closed outline downstream
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import geom
    bg = geom.load_board(pcb)
    assert bg.outline is not None and bg.outline.area > 0


def _init_board(tmp_path, netlist, *extra):
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(netlist), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--outline", "100x80", "--corner-radius", "3", "--mounting-holes", "4",
        "--schematic", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch"),
        "--out-report", str(rep), *extra])
    return rc, json.loads(rep.read_text("utf-8"))


@pytest.mark.smoke
def test_board_init_keeps_or_pins_the_origin(cli, usbbuck4_net, tmp_path):
    """A re-run keeps the outline where the board has it (lite-main's planes
    and pre-route ops were written in absolute coordinates); --origin X,Y
    pins it and --origin auto is the packed position."""
    rc, first = _init_board(tmp_path, usbbuck4_net)
    assert rc == 0 and first["origin_source"].startswith("auto")
    o1 = first["outline_origin"]
    # a wider margin packs the parts differently: auto would move the origin
    rc, auto = _init_board(tmp_path, usbbuck4_net, "--margin", "9", "--origin", "auto")
    assert rc == 0 and auto["outline_origin"] != o1
    rc, kept = _init_board(tmp_path, usbbuck4_net, "--margin", "9")
    assert rc == 0 and kept["status"] == "pass", kept
    assert kept["outline_origin"] == auto["outline_origin"]   # kept from the last run
    assert kept["origin_source"] == "kept from usbbuck4.kicad_pcb"
    rc, pinned = _init_board(tmp_path, usbbuck4_net, "--origin", "10,20")
    assert rc == 0 and pinned["status"] == "pass", pinned
    assert pinned["outline_origin"] == [10.0, 20.0]
    assert pinned["outline_bbox"] == [10.0, 20.0, 110.0, 100.0]
    assert pinned["self_check"]["setup_violations"] == []


@pytest.mark.smoke
def test_board_init_edge_notch(cli, usbbuck4_net, tmp_path):
    """MECH: a --cutout touching an outline edge reshapes the perimeter into a
    notch (the LUMINA daughter boards' RJ45 relief). geom must read the notched
    polygon, not the bounding rectangle."""
    import math
    from shapely.geometry import Point
    rc, r = _init_board(tmp_path, usbbuck4_net, "--cutout", "6,0,30,26")
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["setup_violations"] == []
    assert len(r["cutouts"]) == 1 and r["outline_origin"] is not None

    sys.path.insert(0, str(SCRIPTS / "lib"))
    import geom
    o = geom.load_board(tmp_path / "kicad" / "usbbuck4.kicad_pcb").outline
    # 100x80, less the 30x26 notch, less four r=3 corners
    assert o.area == pytest.approx(8000 - 780 - (4 - math.pi) * 9, abs=0.5)
    ox, oy = o.bounds[0], o.bounds[1]
    assert not o.contains(Point(ox + 21, oy + 10))   # inside the notch
    assert o.contains(Point(ox + 21, oy + 40))       # below the notch


@pytest.mark.smoke
def test_board_init_swaps_a_hole_fp_whose_vias_break_the_fab_floor(
        cli, usbbuck4_net, tmp_path):
    """Issue #28: KiCad's M2.5 _Pad_Via ring has its own drills 0.425 mm
    apart, under JLC's 0.5 mm hole-to-hole floor - 36 DRC errors on
    ultra-front. board_init takes the same-size _Pad sibling instead and says
    so; the board it builds is clean."""
    rc, r = _init_board(tmp_path, usbbuck4_net, "--mounting-hole-fp",
                        "MountingHole:MountingHole_2.7mm_M2.5_Pad_Via")
    assert rc == 0 and r["status"] == "pass", r
    assert r["mounting_holes"] == 4
    assert r["mounting_hole_fp"] == "MountingHole:MountingHole_2.7mm_M2.5_Pad"
    assert any("swapped for MountingHole:MountingHole_2.7mm_M2.5_Pad" in n
               for n in r["worker_notes"])
    assert r["self_check"]["setup_violations"] == []


@pytest.mark.smoke
def test_board_init_wide_fixed_outline_stays_on_the_page(
        cli, usbbuck4_net, tmp_path):
    """Issue #28: a fixed outline wider than the packed parts was centred on
    them and started at negative X (ultra-front 110x40 at x -9.945), cutting
    PDF plots off. It now starts in the positive quadrant, the aux and grid
    origin at its lower-left corner."""
    rc, r = _init_board(tmp_path, usbbuck4_net, "--outline", "160x80")
    assert rc == 0 and r["status"] == "pass", r
    x1, y1, x2, y2 = r["outline_bbox"]
    assert x1 >= 5.0 and y1 >= 5.0
    assert (x2 - x1, y2 - y1) == pytest.approx((160, 80), abs=1e-3)
    assert r["aux_origin"] == [x1, y2]
    txt = (tmp_path / "kicad" / "usbbuck4.kicad_pcb").read_text("utf-8")
    m = re.search(r"\(aux_axis_origin ([\d.-]+) ([\d.-]+)\)", txt)
    assert m and (float(m.group(1)), float(m.group(2))) == pytest.approx((x1, y2))
    assert r["self_check"]["setup_violations"] == []


@pytest.mark.smoke
def test_board_init_cutout_over_corner_is_rejected(cli, usbbuck4_net, tmp_path):
    """A notch running into a rounded corner would self-intersect the outline
    and fail polygonize downstream. It must be skipped loudly at the source."""
    import math
    rc, r = _init_board(tmp_path, usbbuck4_net, "--cutout", "0,0,10,10")
    assert rc == 0 and r["status"] == "pass", r
    assert any("corner radius" in n for n in r["worker_notes"]), r["worker_notes"]

    sys.path.insert(0, str(SCRIPTS / "lib"))
    import geom
    o = geom.load_board(tmp_path / "kicad" / "usbbuck4.kicad_pcb").outline
    # untouched rounded rectangle - the cutout was not drawn
    assert o.area == pytest.approx(8000 - (4 - math.pi) * 9, abs=0.5)


@pytest.mark.smoke
def test_board_init_corner_radius_clamped_to_hole_inset(cli, usbbuck4_net,
                                                        tmp_path):
    """A radius past the mounting-hole inset must shrink the RADIUS, not move
    the hole: parts are packed around the holes at that inset, so relocating a
    hole inward lands it in a neighbour's courtyard (H1 vs C1)."""
    rep = tmp_path / "report.json"
    rc = board_init.main([                       # default margin 6 -> inset 3.0
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--mounting-holes", "4", "--corner-radius", "4",
        "--mounting-hole-fp", "MountingHole:MountingHole_3.2mm_M3",  # NPTH
        "--schematic", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch"),
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["setup_violations"] == []
    assert r["corner_radius"] == 3.0
    assert any("clamped to the mounting-hole inset" in n
               for n in r["worker_notes"])
    # a bare NPTH hole has no copper, so nothing to put on a net
    assert r["mounting_hole_net"] is None


@pytest.mark.smoke
def test_board_init_square_corners_on_request(cli, usbbuck4_net, tmp_path):
    """A brief that wants square corners and no holes still gets them:
    --corner-radius 0 and --mounting-holes 0 beat the rounded default."""
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--corner-radius", "0", "--mounting-holes", "0",
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["corner_radius"] == 0.0
    assert r["mounting_holes"] == 0 and r["mounting_hole_fp"] is None
    pcb = tmp_path / "kicad" / "usbbuck4.kicad_pcb"
    assert _edge_shapes(pcb, "gr_rect") == 1
    assert _edge_shapes(pcb, "gr_arc") == 0
    assert "MountingHole" not in pcb.read_text(encoding="utf-8")


@pytest.mark.smoke
def test_board_init_defaults_round_corners_and_plated_holes(cli, usbbuck4_net,
                                                           tmp_path):
    """Owner, #ai-ee 1790534754.490679: "nice mounting holes with exposed
    copper and vias in them and rounded edges". With no flags a board big
    enough gets 2 mm corners and four M3 Pad_Via holes on GND, clear of the
    edge by the fab's copper floor, off the BOM/CPL, and still DRC/parity
    clean."""
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--schematic", str(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch"),
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["setup_violations"] == []
    assert r["self_check"]["parity_count"] == 0
    assert r["corner_radius"] == 2.0
    assert r["mounting_holes"] == 4 and r["mounting_hole_net"] == "GND"
    assert r["mounting_hole_inset"] == 3.55     # 3.2 ring + 0.3 floor + 0.05
    assert r["mounting_hole_fp"] == "MountingHole:MountingHole_3.2mm_M3_Pad_Via"
    pcb = tmp_path / "kicad" / "usbbuck4.kicad_pcb"
    assert (_edge_shapes(pcb, "gr_line"), _edge_shapes(pcb, "gr_arc")) == (4, 4)

    sys.path.insert(0, str(SCRIPTS / "lib"))
    import placelib
    from shapely.geometry import Point
    model = placelib.PlaceModel(pcb)
    x1, y1, x2, y2 = r["outline_bbox"]
    holes = [f for ref, f in model.footprints.items() if ref.startswith("H")]
    assert len(holes) == 4
    for h in holes:
        assert h.is_mechanical and "board_only" in h.attrs
        assert {p.net for p in h.pads} == {"GND"}
        ring = Point(h.pos).buffer(3.2)          # the 6.4 mm exposed pad
        rx1, ry1, rx2, ry2 = ring.bounds
        assert min(rx1 - x1, ry1 - y1, x2 - rx2, y2 - ry2) >= 0.3 - 1e-6


@pytest.mark.smoke
def test_board_init_small_board_gets_no_default_holes(cli, usbbuck4_net,
                                                      tmp_path):
    """Below the 30 mm shorter side "auto" adds no holes (four M3 rings would
    eat the board), and the default radius shrinks to a tenth of that side.
    An explicit count still wins on the same board."""
    rep = tmp_path / "report.json"
    board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "4",
        "--outline", "18x15", "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))    # parts overflow: status moot
    assert r["mounting_holes"] == 0 and r["corner_radius"] == 1.5, r
    assert any("mounting holes auto: 0" in n for n in r["worker_notes"])

    board_init.main([
        "--netlist", str(usbbuck4_net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad2"), "--layers", "4",
        "--outline", "18x15", "--mounting-holes", "2",
        "--out-report", str(rep)])
    assert json.loads(rep.read_text("utf-8"))["mounting_holes"] == 2


def test_board_init_fixed_outline_too_tight_gets_no_default_holes(cli,
                                                                  tmp_path):
    """A fixed outline is not widened for the holes, so "auto" adds none when
    the parts sit closer to an edge than a corner hole needs (a hole there
    would overlap a corner part's courtyard and fail the self-check)."""
    net = REPO / "tests" / "s7_regen" / "blinky2" / "kicad" / "blinky2.net"
    rep = tmp_path / "report.json"
    # auto outline with holes off = parts bbox + the 5 mm margin each side
    assert board_init.main([
        "--netlist", str(net), "--name", "blinky2",
        "--out", str(tmp_path / "k0"), "--layers", "2",
        "--mounting-holes", "0", "--out-report", str(rep)]) == 0
    x1, y1, x2, y2 = json.loads(rep.read_text("utf-8"))["outline_bbox"]
    w, h = round(x2 - x1, 2), round(y2 - y1, 2)
    assert min(w, h) >= 30, (w, h)     # big enough that size is not why
    assert board_init.main([
        "--netlist", str(net), "--name", "blinky2",
        "--out", str(tmp_path / "k1"), "--layers", "2",
        "--outline", f"{w}x{h}", "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "pass" and r["mounting_holes"] == 0, r
    assert any("the fixed outline leaves" in n for n in r["worker_notes"])
    assert "--mounting-holes 4" in r["mounting_holes_skipped"], r

    # the kept case: the same parts in a roomier fixed outline get holes
    assert board_init.main([
        "--netlist", str(net), "--name", "blinky2",
        "--out", str(tmp_path / "k2"), "--layers", "2",
        "--outline", f"{w + 10}x{h + 10}", "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "pass" and r["mounting_holes"] == 4, r
    assert r["mounting_holes_skipped"] is None, r


@pytest.mark.smoke
def test_board_init_lcsc_fields_and_inplace_schematic(cli, usbbuck4_net, tmp_path):
    """S14 regressions: (a) symbols with custom LCSC fields must init to a
    parity-CLEAN board (fields copied onto footprints, hidden); (b) --schematic
    pointing at the file already in the out dir must not SameFileError."""
    # inject an LCSC field into every comp's existing (fields ...) block
    txt = usbbuck4_net.read_text(encoding="utf-8")
    txt = txt.replace("(fields\n",
                      '(fields\n\t\t\t\t(field\n\t\t\t\t\t(name "LCSC") "C999")\n')
    net2 = tmp_path / "with_lcsc.net"
    net2.write_text(txt, encoding="utf-8")

    out_dir = tmp_path / "kicad"
    out_dir.mkdir()
    # schematic already IN the out dir under the board's name (the P4 layout)
    sch_dst = out_dir / "usbbuck4.kicad_sch"
    shutil.copy(GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch", sch_dst)

    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(net2), "--name", "usbbuck4",
        "--out", str(out_dir), "--layers", "4", "--mounting-holes", "0",
        "--schematic", str(sch_dst),          # samefile as the copy target
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["parity_count"] == 0   # LCSC fields no longer trip it

    board_text = (out_dir / "usbbuck4.kicad_pcb").read_text(encoding="utf-8")
    assert '"LCSC"' in board_text and '"C999"' in board_text
    # fields default visible-on-silk in pcbnew; the worker must hide them
    import re as _re
    for m in _re.finditer(r'\(property "LCSC"[\s\S]{0,400}?\(hide yes\)',
                          board_text):
        break
    else:
        pytest.fail("LCSC footprint fields are not hidden")


@pytest.mark.smoke
def test_board_init_mirrors_in_bom(cli, tmp_path):
    """#439: a symbol with (in_bom no) must give a footprint with
    exclude_from_bom, else parity reports footprint_symbol_mismatch and the
    unconnected-net retry is blocked."""
    sch = tmp_path / "usbbuck4.kicad_sch"
    txt = (GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch").read_text("utf-8")
    sch.write_text(txt.replace("(in_bom yes)", "(in_bom no)"), "utf-8")
    net = tmp_path / "u.net"
    assert kc.export_netlist(cli, sch, net)["status"] == "pass"
    assert "exclude_from_bom" in net.read_text("utf-8")
    rep = tmp_path / "report.json"
    rc = board_init.main([
        "--netlist", str(net), "--name", "usbbuck4",
        "--out", str(tmp_path / "kicad"), "--layers", "2",
        "--mounting-holes", "0", "--schematic", str(sch),
        "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["parity_count"] == 0
    board = (tmp_path / "kicad" / "usbbuck4.kicad_pcb").read_text("utf-8")
    assert board.count("exclude_from_bom") >= r["components"]


def _prep_golden(tmp_path, board="usbbuck4"):
    """Copy a golden board + pro + sch into tmp for mutation."""
    d = tmp_path / board
    d.mkdir()
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_sch"):
        src = GOLDEN / board / f"{board}{ext}"
        if src.exists():
            shutil.copy(src, d / f"{board}{ext}")
    return d / f"{board}.kicad_pcb"


@pytest.mark.smoke
def test_rules_gen_clean_golden(cli, tmp_path):
    """Generated rules must NOT false-positive on the clean golden."""
    pcb = _prep_golden(tmp_path)
    rc = rules_gen.main([
        "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
        "--layers", "4", "--out-dru", str(pcb.with_suffix(".kicad_dru")),
        "--out", str(tmp_path / "r.json")])
    assert rc == 0
    rep = _drc(cli, pcb, parity=True)
    assert rep["counts"]["total"] == 0, rep["violations"]


@pytest.mark.smoke
def test_rules_gen_enforced(cli, tmp_path):
    """ACCEPTANCE: a violating test track fails DRC with the custom rule named.

    Narrow a +3V3 track to 0.15 mm - below the per-net power rule (0.20 mm) but
    ABOVE the fab floor (0.1016 mm) - so ONLY aiee_pwr_width_3V3 may fire, which
    also proves the specific rule overrides the generic floor (later rule wins).
    """
    pcb = _prep_golden(tmp_path)
    rules_gen.main(["--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
                    "--layers", "4", "--out-dru", str(pcb.with_suffix(".kicad_dru")),
                    "--out", str(tmp_path / "r.json")])
    text = pcb.read_text("utf-8")
    pat = re.compile(r'(\(segment\s+\(start[^)]*\)\s+\(end[^)]*\)\s+)'
                     r'\(width [\d.]+\)(\s+\(layer[^)]*\)\s+\(net "\+3V3"\))', re.S)
    m = pat.search(text)
    assert m, "no +3V3 segment found to narrow"
    pcb.write_text(text[:m.start()] + m.group(1) + "(width 0.15)" + m.group(2)
                   + text[m.end():], encoding="utf-8")

    rep = _drc(cli, pcb)
    names = _rule_names(rep)
    assert "aiee_pwr_width_3V3" in names, rep["violations"]
    # the floor rule must NOT also fire (0.15 > 0.1016): specific rule won
    assert "aiee_track_width_floor" not in names
    # the offending violation is on +3V3, type track_width
    hit = [v for v in rep["violations"] if "aiee_pwr_width_3V3" in (v["msg"] or "")]
    assert hit and hit[0]["check"] == "track_width" and hit[0]["net"] == "+3V3"


@pytest.mark.smoke
@pytest.mark.parametrize("board,template,layers", [
    ("blinky2", "jlc_2layer_1oz", 2),
    ("usbbuck4", "jlc_4layer_1oz", 4),
])
def test_baseline_template_no_false_positive(cli, tmp_path, board, template, layers):
    pcb = _prep_golden(tmp_path, board)
    shutil.copy(REFERENCE / "design_rules" / f"{template}.kicad_dru",
                pcb.with_suffix(".kicad_dru"))
    rep = _drc(cli, pcb)
    assert rep["counts"]["total"] == 0, rep["violations"]


# ================================= T6 P5-1: copper-oz derived from the stackup

FIXTURES = REPO / "tests" / "fixtures" / "stages"


def test_rules_gen_derives_copper_oz_from_stackup(tmp_path):
    """The pd-trigger trap: --stackup JLC2313_1.6_2oz WITHOUT --copper-oz
    previously selected the 1-oz capability row (sub-fab floors) and 1-oz
    IPC-2152 widths (VBUS 3.5 mm instead of 1.75 - the pad-entry disaster
    class). The weight must come from the stackup, like board_init."""
    out = tmp_path / "r.json"
    rc = rules_gen.main([
        "--constraints", str(FIXTURES / "pd_trigger" / "constraints.json"),
        "--layers", "2", "--stackup", "JLC2313_1.6_2oz",
        "--out-dru", str(tmp_path / "pd.kicad_dru"), "--out", str(out)])
    assert rc == 0
    r = json.loads(out.read_text("utf-8"))
    assert r["capability_class"] == "2layer_2oz"
    assert r["copper_oz"] == 2.0
    byn = {f["net"]: f for f in r["power"]}
    assert byn["VBUS"]["min_width_mm"] == pytest.approx(1.75, abs=0.01)
    assert "Pwr_1p75mm" in r["classes"]
    # and the DRU floors are the 2-oz row's, not the 1-oz row's
    dru = (tmp_path / "pd.kicad_dru").read_text("utf-8")
    assert fabfloors.check_dru(dru, _cap("2layer_2oz")) == []


def test_rules_gen_refuses_copper_oz_stackup_mismatch(tmp_path, capsys):
    """An explicit --copper-oz contradicting an explicitly named stackup is
    the T1 'never quietly become 1 oz' rule - exit 2 naming both values."""
    rc = rules_gen.main(["--layers", "2", "--stackup", "JLC2313_1.6_2oz",
                         "--copper-oz", "1",
                         "--out-dru", str(tmp_path / "x.kicad_dru")])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "JLC2313_1.6_2oz" in out["error"]
    assert "1" in out["error"] and "2" in out["error"]


def test_rules_gen_no_stackup_no_flag_falls_back_1oz(tmp_path):
    """No resolvable stackup (6-layer has no default) + no flag = today's
    1.0 fallback, keeping template generation paths intact."""
    out = tmp_path / "r.json"
    rc = rules_gen.main(["--layers", "6", "--baseline-only",
                         "--out-dru", str(tmp_path / "b.kicad_dru"),
                         "--out", str(out)])
    assert rc == 0
    r = json.loads(out.read_text("utf-8"))
    assert r["capability_class"] == "6layer_1oz"
    assert r["copper_oz"] == 1.0


# ==================== T6 P5-3: HV clearance rules from voltages/voltage_pairs

def test_voltage_rules_hv_net():
    """48 V net -> one named clearance rule at the IPC-2221 outer value
    (coating none: max(B2 0.60, A6 0.40) = 0.60 mm), emitted AFTER the
    baseline so later-wins resolves it over the generic floor."""
    cons = {"voltages": [{"net": "V48", "voltage": 48},
                         {"net": "GND", "voltage": 0}]}
    cap = _cap("2layer_2oz")
    rules, report = rules_gen.build(cons, cap, _stackup("JLC2313_1.6_2oz"), False)
    byname = {r.name: r for r in rules}
    rule = byname["aiee_hv_48v_V48"]
    assert rule.constraint == "clearance"
    assert rule.minv == pytest.approx(0.60)
    assert rule.condition == "A.NetName == 'V48'"
    names = [r.name for r in rules]
    assert names.index("aiee_hv_48v_V48") > names.index("aiee_clearance_floor")
    assert report["hv_rules"][0]["net"] == "V48"


def test_voltage_rules_subthreshold_emits_nothing():
    """pd-trigger class: every net at or under 30 V -> zero HV rules
    (bench-neutral by construction)."""
    cons = {"voltages": [{"net": "VBUS", "voltage": 20},
                         {"net": "/VDD", "voltage": 3.3}]}
    rules, report = rules_gen.build(cons, _cap("2layer_2oz"),
                                    _stackup("JLC2313_1.6_2oz"), False)
    assert not [r for r in rules if r.name.startswith("aiee_hv_")]
    assert report["hv_rules"] == []


def test_voltage_rules_explicit_pair_and_waiver():
    """voltage_pairs: an explicit >30 V pair gets a pair-scoped rule (114 V,
    coating none -> A6 row 0.80 dominates B2 0.60); an explicit sub-threshold
    pair against a derived-HV net RESTORES the baseline clearance for exactly
    that pair (the check_creepage waiver, mirrored in the DRU) and is emitted
    after the net rule so later-wins applies the override."""
    cons = {"voltages": [{"net": "V48", "voltage": 48}],
            "voltage_pairs": [{"a": "/TAP_A", "b": "/TAP_B", "voltage": 114},
                              {"a": "V48", "b": "/SNS", "voltage": 10}]}
    cap = _cap("2layer_2oz")
    rules, report = rules_gen.build(cons, cap, _stackup("JLC2313_1.6_2oz"), False)
    byname = {r.name: r for r in rules}
    pair = byname["aiee_hv_114v_TAP_A__TAP_B"]
    assert pair.minv == pytest.approx(0.80)
    assert pair.condition == "A.NetName == '/TAP_A' && B.NetName == '/TAP_B'"
    waived = byname["aiee_hv_waived_V48__SNS"]
    assert waived.minv == pytest.approx(cap["min_clearance_mm"])
    names = [r.name for r in rules]
    assert names.index("aiee_hv_waived_V48__SNS") > names.index("aiee_hv_48v_V48")
    facts = {f["rule"]: f for f in report["hv_rules"]}
    assert facts["aiee_hv_waived_V48__SNS"]["waiver"] is True
    assert facts["aiee_hv_114v_TAP_A__TAP_B"]["waiver"] is False


@pytest.mark.smoke
def test_voltage_rules_enforced_live(cli, tmp_path):
    """Live proof on pinned 10.0.3: declaring VBUS at 48 V makes the
    generated aiee_hv rule fire as named clearance violations on a board
    routed at normal clearances - HV spacing is DRC-enforced, not digest
    prose (the carrier previously hand-authored this rule shape)."""
    pcb = _prep_golden(tmp_path)
    cons = tmp_path / "cons.json"
    cons.write_text(json.dumps(
        {"voltages": [{"net": "VBUS", "voltage": 48},
                      {"net": "GND", "voltage": 0}]}), encoding="utf-8")
    rc = rules_gen.main(["--constraints", str(cons), "--layers", "4",
                         "--out-dru", str(pcb.with_suffix(".kicad_dru")),
                         "--out", str(tmp_path / "r.json")])
    assert rc == 0
    rep = _drc(cli, pcb)
    hits = [v for v in rep["violations"]
            if "aiee_hv_48v_VBUS" in (v["msg"] or "")]
    assert hits, "generated HV rule did not fire"
    assert all(v["check"] == "clearance" for v in hits)


def test_voltage_rules_coating_selects_rows():
    """soldermask moves tracks to B4 but lands stay A6 (mask relief exposes
    them): 48 V -> max(B4 0.13, A6 0.40) = 0.40 mm."""
    assert rules_gen.hv_clearance_mm(48, "none") == pytest.approx(0.60)
    assert rules_gen.hv_clearance_mm(48, "soldermask") == pytest.approx(0.40)


# ========== HV clearance reaches the .kicad_pro netclasses (PCB-0018, 2026-09-28)

def test_hv_nets_get_their_own_netclass():
    """The DSN export hands Freerouting netclasses only, so every net with
    an aiee_hv_* rule must sit in a class carrying that clearance: a variant
    of its power class for a wide HV rail, HV_<c>mm for a signal. A
    sub-threshold net and a waived pair keep their plain class."""
    cons = {"power": [{"net": "VM", "current_a": 5.0},
                      {"net": "+3V3", "current_a": 0.1}],
            "voltages": [{"net": "VM", "voltage": 48},
                         {"net": "/SW", "voltage": 48},
                         {"net": "+3V3", "voltage": 3.3}],
            "voltage_pairs": [{"a": "/TAP_A", "b": "/TAP_B", "voltage": 114},
                              {"a": "VM", "b": "/SNS", "voltage": 10}]}
    cap = _cap("2layer_2oz")
    _, report = rules_gen.build(cons, cap, _stackup("JLC2313_1.6_2oz"), False)
    cls = {c["name"]: c for c in report["classes"]}
    by_net = {p["pattern"]: p["netclass"] for p in report["patterns"]}
    vm_width = next(f for f in report["power"] if f["net"] == "VM")["class_width_mm"]
    vm_cls = cls[by_net["VM"]]
    assert by_net["VM"] == rules_gen.power_class_name(vm_width) + "_HV_0p6mm"
    assert vm_cls["clearance"] == pytest.approx(0.60)
    assert vm_cls["track_width"] == pytest.approx(vm_width)
    assert cls[by_net["/SW"]]["clearance"] == pytest.approx(0.60)
    assert by_net["/SW"] == "HV_0p6mm"
    # an explicit 114 V pair raises both nets (stricter than the DRU pair rule)
    assert cls[by_net["/TAP_A"]]["clearance"] == pytest.approx(0.80)
    assert by_net["/TAP_A"] == by_net["/TAP_B"]
    # kept on their plain class: a 3.3 V rail and the waived /SNS
    assert by_net["+3V3"] == "Default"
    assert "/SNS" not in by_net
    # the plain power class VM left is not emitted empty; the report follows
    assert rules_gen.power_class_name(vm_width) not in cls
    assert next(f for f in report["power"] if f["net"] == "VM")["netclass"] \
        == by_net["VM"]


def test_update_pro_floors_class_clearance_at_hole_clearance(tmp_path):
    """4layer_1oz: 0.1016 clearance + a 0.45/0.2 via ring (0.125) sits a
    track 0.2266 from the hole, inside KiCad's 0.25 hole clearance. Default
    goes to 0.125; a Diff class stops at its pair gap; a pro whose own hole
    clearance is small keeps the fab clearance."""
    _, cap = fabfloors.profile(4, 1.0)
    diff = {**rules_gen.default_class(cap), "name": "Diff90",
            "diff_pair_gap": 0.11}
    pro_path = tmp_path / "b.kicad_pro"
    board_init.write_pro(pro_path, cap)
    rules_gen.update_pro(pro_path, [diff], [], cap)
    got = {c["name"]: c["clearance"] for c in
           json.loads(pro_path.read_text("utf-8"))["net_settings"]["classes"]}
    assert got == {"Default": pytest.approx(0.125), "Diff90": pytest.approx(0.11)}
    assert diff["clearance"] == cap["min_clearance_mm"]   # input not mutated

    pro = json.loads(pro_path.read_text("utf-8"))
    pro["board"]["design_settings"]["rules"]["min_hole_clearance"] = 0.2
    pro_path.write_text(json.dumps(pro), encoding="utf-8")
    rules_gen.update_pro(pro_path, [], [], cap)
    got = json.loads(pro_path.read_text("utf-8"))["net_settings"]["classes"][0]
    assert got["clearance"] == pytest.approx(cap["min_clearance_mm"])


def test_published_hole_clearance_and_class_via_ring(tmp_path):
    """advancedpcb publishes copper-to-hole (0.1524 mm): it reaches
    min_hole_clearance and check_pro holds the project to it, where KiCad's
    0.25 default used to stand in. A vendor row whose minimum pad leaves
    less than its own ring on the minimum drill (a workspace file copied
    from the sheet: 0.2032 on 0.127 is a 0.038 ring, floor 0.0635) gets a
    netclass via of drill + 2 rings. A JLC row states no copper-to-hole
    and is unchanged."""
    _, cap = fabfloors.profile(4, 1.0, vendor="advancedpcb")
    assert fabfloors.pro_rules(cap)["min_hole_clearance"] == pytest.approx(0.1524)
    assert rules_gen.class_via_diameter(cap) == pytest.approx(0.254)
    pro_path = tmp_path / "b.kicad_pro"
    board_init.write_pro(pro_path, cap)
    rules_gen.update_pro(pro_path, [], [], cap)
    pro = json.loads(pro_path.read_text("utf-8"))
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_hole_clearance"] == pytest.approx(0.1524)
    assert pro["net_settings"]["classes"][0]["via_diameter"] == pytest.approx(0.254)
    assert fabfloors.check_pro(pro, cap) == []
    rules["min_hole_clearance"] = 0.1
    assert any("min_hole_clearance" in m for m in fabfloors.check_pro(pro, cap))

    sheet = dict(cap, min_via_diameter_mm=0.2032)
    assert rules_gen.class_via_diameter(sheet) == pytest.approx(0.254)

    _, jlc = fabfloors.profile(4, 1.0)
    assert "min_hole_clearance" not in fabfloors.pro_rules(jlc)
    assert rules_gen.class_via_diameter(jlc) == pytest.approx(jlc["min_via_diameter_mm"])

@pytest.mark.smoke
def test_hv_clearance_reaches_the_dsn(cli, tmp_path):
    """Live proof on the route path: rules_gen --pro with VBUS at 48 V, then
    the same export_dsn verb route_auto runs - the DSN class holding VBUS
    carries the HV clearance and Default carries the hole floor."""
    import routelib
    bp = env.find_kicad_python(cli)
    if bp is None:
        pytest.skip("KiCad bundled python not found")
    pcb = _prep_golden(tmp_path)
    cons = tmp_path / "cons.json"
    cons.write_text(json.dumps(
        {"voltages": [{"net": "VBUS", "voltage": 48},
                      {"net": "GND", "voltage": 0}]}), encoding="utf-8")
    rc = rules_gen.main(["--constraints", str(cons), "--layers", "4",
                         "--out-dru", str(pcb.with_suffix(".kicad_dru")),
                         "--pro", str(pcb.with_suffix(".kicad_pro")),
                         "--out", str(tmp_path / "r.json")])
    assert rc == 0
    dsn = tmp_path / "b.dsn"
    stage = tmp_path / "stage"
    stage.mkdir()
    routelib.run_worker(bp, {"verb": "export_dsn", "board": str(pcb),
                             "dsn": str(dsn)}, stage)
    text = dsn.read_text(encoding="utf-8")
    assert re.search(r"\(resolution\s+um\s+\d+\)", text)
    classes = {}
    for m in re.finditer(r"\(class\s+(\S+)(.*?)\(circuit.*?\(clearance\s+([\d.]+)\)",
                         text, re.S):
        for net in m.group(2).split():
            classes[net.strip('"')] = (m.group(1), float(m.group(3)))
    assert classes["VBUS"] == ("HV_0p6mm", pytest.approx(600))
    assert classes["GND"][1] == pytest.approx(125)


# ============================ T6 P5-4: V12 guard - inner disallow for pairs

def test_diff_outer_only_rule_emitted():
    """Every solved pair carries an inner-layer 'disallow track' rule -
    impedance.py has no stripline model, so inner routing of a solved pair
    must be a NAMED violation, not a silent impedance error."""
    cons = json.loads((GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    rules, _ = rules_gen.build(cons, _cap(), _stackup(), baseline_only=False)
    byname = {r.name: r for r in rules}
    rule = byname["aiee_diff_outer_only_USB"]
    assert rule.constraint == "disallow track"
    assert rule.layer == "inner"
    assert "A.NetName == '/USB_DP'" in rule.condition
    assert "A.NetName == '/USB_DM'" in rule.condition
    rendered = rule.render()
    assert "(layer inner)" in rendered
    assert "(constraint disallow track)" in rendered
    # no pairs -> no disallow rules
    rules2, _ = rules_gen.build({}, _cap(), _stackup(), baseline_only=False)
    assert not [r for r in rules2 if "outer_only" in r.name]


@pytest.mark.smoke
def test_diff_outer_only_enforced(cli, tmp_path):
    """Live proof of the (layer inner) + disallow syntax on pinned 10.0.3:
    an In1.Cu segment on /USB_DP fails DRC naming the rule (check id
    items_not_allowed, remediation ref of the same name)."""
    pcb = _prep_golden(tmp_path)
    rules_gen.main(["--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
                    "--layers", "4", "--out-dru", str(pcb.with_suffix(".kicad_dru")),
                    "--out", str(tmp_path / "r.json")])
    text = pcb.read_text("utf-8")
    seg = ('\t(segment\n\t\t(start 141.4 116.25)\n\t\t(end 140.8 116.25)\n'
           '\t\t(width 0.25)\n\t\t(layer "In1.Cu")\n\t\t(net "/USB_DP")\n'
           '\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")\n\t)\n')
    i = text.rfind(")")
    pcb.write_text(text[:i] + seg + text[i:], encoding="utf-8")
    rep = _drc(cli, pcb)
    assert "aiee_diff_outer_only_USB" in _rule_names(rep), rep["violations"]
    hit = [v for v in rep["violations"]
           if "aiee_diff_outer_only_USB" in (v["msg"] or "")]
    assert hit and hit[0]["check"] == "items_not_allowed"


# ============ #323: inner stripline layers get solved widths, not the guard

_SIX_PLANES = [{"layer": "In1.Cu", "net": "GND"}, {"layer": "In3.Cu", "net": "+3V3"},
               {"layer": "In4.Cu", "net": "GND"}]


def _ddr_cons():
    return {"planes": _SIX_PLANES,
            "diff_pairs": [{"p": "/CK_P", "n": "/CK_N", "base": "CK", "impedance_ohm": 100},
                           {"p": "/USB_DP", "n": "/USB_DM", "base": "USB",
                            "impedance_ohm": 90}],
            "high_speed": [{"net": "/D0", "reference": "GND", "impedance_ohm": 50},
                           {"net": "/D1", "reference": "GND", "impedance_ohm": 50},
                           {"net": "/CK_P", "reference": "GND", "impedance_ohm": 50}]}


def test_stripline_layers_need_a_pour_both_sides():
    stk = _stackup("JLC06161H-3313")
    sl = rules_gen.stripline_layers(stk, {"planes": _SIX_PLANES})
    # In2 sits between In1 (GND) and In3 (+3V3); In1/In3/In4 are pours
    assert list(sl) == ["In2.Cu"]
    g = sl["In2.Cu"]
    assert g["planes"] == ["In1.Cu", "In3.Cu"]
    # the trace sits on the 0.55 core, embedded in the 2116 prepreg
    assert g["h_base"] == pytest.approx(0.55) and g["h_fill"] == pytest.approx(0.1088)
    # planes_gen's 6-layer defaults (In1, In2 pours) leave no stripline layer
    assert rules_gen.stripline_layers(stk, {}) == {}
    # a 4-layer board's inner layers are its planes
    assert rules_gen.stripline_layers(_stackup(), {}) == {}


def test_stripline_pair_widths_and_guards():
    cap = _cap("6layer_1oz")
    floor = float(cap["min_trace_width_mm"])
    rules, rep = rules_gen.build(_ddr_cons(), cap, _stackup("JLC06161H-3313"), False)
    byname = {r.name: r for r in rules}
    ck = next(f for f in rep["diff_pairs"] if f["pair"][0] == "/CK_P")
    g = ck["layers"]["In2.Cu"]
    assert g["zdiff_ohm"] == pytest.approx(100, abs=1)
    assert g["width_mm"] >= floor - 1e-9
    usb = next(f for f in rep["diff_pairs"] if f["pair"][0] == "/USB_DP")
    assert usb["layers"]["In2.Cu"]["zdiff_ohm"] == pytest.approx(90, abs=1)
    w = byname["aiee_diff_w_CK_In2"]
    assert w.constraint == "track_width" and w.layer == '"In2.Cu"'
    assert "(opt %.4fmm)" % g["width_mm"] in w.render()
    assert '(layer "In2.Cu")' in byname["aiee_diff_gap_CK_In2"].render()
    # the guard moves to the inner layers that are not stripline
    assert "aiee_diff_outer_only_CK" not in byname
    for lay in ("In1", "In3", "In4"):
        assert byname[f"aiee_diff_outer_only_CK_{lay}"].constraint == "disallow track"
    assert "aiee_diff_outer_only_CK_In2" not in byname


def test_stripline_single_ended_rule_no_class():
    rules, rep = rules_gen.build(_ddr_cons(), _cap("6layer_1oz"),
                                 _stackup("JLC06161H-3313"), False)
    z = rep["impedance"]
    # /CK_P is a pair member: sized as the pair, not single-ended
    assert [f["net"] for f in z] == ["/D0", "/D1"]
    assert z[0]["outer_width_mm"] == pytest.approx(0.155, abs=0.01)
    assert z[0]["layers"]["In2.Cu"]["z0_ohm"] == pytest.approx(50, abs=1)
    r = {x.name: x for x in rules}["aiee_z50_w_In2"]
    assert "'/D0'" in r.condition and "'/D1'" in r.condition
    assert "'/CK_P'" not in r.condition
    # no single-ended netclass: an RF board keeps the classes it draws with
    assert not [c for c in rep["classes"] if c["name"].startswith("Z")]


def test_single_ended_impedance_inner_guard():
    """#1108: a 50 ohm net on an inner layer with no solved stripline is a
    named DRC error (the pair's guard), not a silent miss at the default
    width; the stripline layer keeps its width rule and no guard."""
    rules, _ = rules_gen.build(_ddr_cons(), _cap("6layer_1oz"),
                               _stackup("JLC06161H-3313"), False)
    byname = {r.name: r for r in rules}
    for lay in ("In1", "In3", "In4"):
        g = byname[f"aiee_z50_outer_only_{lay}"]
        assert g.constraint == "disallow track" and g.layer == f'"{lay}.Cu"'
        assert "'/D0'" in g.condition and "'/CK_P'" not in g.condition
    assert "aiee_z50_outer_only_In2" not in byname
    assert "aiee_z50_outer_only" not in byname
    # no stripline layer at all: one guard over every inner layer
    cons, stk = _ddr_cons(), _stackup("JLC06161H-3313")
    pairs = rules_gen.detect_diff_pairs(cons)
    zr, _ = rules_gen.impedance_rules({**cons, "planes": []}, stk, pairs, 0.0889)
    guards = [r for r in zr if r.constraint == "disallow track"]
    assert [(r.name, r.layer) for r in guards] == [("aiee_z50_outer_only", "inner")], zr


def test_stripline_width_necks_down_in_bga_fields():
    """BGA escape stubs run at the fab floor inside the field (#325): the
    stripline width rules exclude every ball grid's courtyard."""
    cons, stk = _ddr_cons(), _stackup("JLC06161H-3313")
    pairs = rules_gen.detect_diff_pairs(cons)
    dr, _ = rules_gen.diff_pair_rules(pairs, stk, cons, 0.0889, ["U100", "U200"])
    zr, _ = rules_gen.impedance_rules(cons, stk, pairs, 0.0889, ["U100", "U200"])
    for r in dr + zr:
        if r.constraint in ("track_width", "diff_pair_gap") and r.layer:
            assert r.condition.endswith("&& !(A.intersectsCourtyard('U100') || "
                                        "A.intersectsCourtyard('U200'))"), r.name
    plain, _ = rules_gen.impedance_rules(cons, stk, pairs, 0.0889, [])
    assert "intersectsCourtyard" not in plain[0].condition


@pytest.mark.smoke
def test_stripline_width_enforced(cli, tmp_path):
    """Live proof of the layer-scoped track_width rule on pinned KiCad: with
    F.Cu and In2.Cu declared pours, In1.Cu is stripline (90 ohm: 0.27 mm);
    a 0.6 mm /USB_DP segment there fails DRC naming aiee_diff_w_USB_In1, not
    the guard."""
    pcb = _prep_golden(tmp_path)
    cons = json.loads((GOLDEN / "usbbuck4" / "constraints.json").read_text("utf-8"))
    cons["planes"] = [{"layer": "F.Cu", "net": "GND"}, {"layer": "In2.Cu", "net": "GND"}]
    cpath = tmp_path / "c.json"
    cpath.write_text(json.dumps(cons), encoding="utf-8")
    assert rules_gen.main(["--constraints", str(cpath), "--layers", "4",
                           "--out-dru", str(pcb.with_suffix(".kicad_dru")),
                           "--out", str(tmp_path / "r.json")]) == 0
    text = pcb.read_text("utf-8")
    seg = ('\t(segment\n\t\t(start 141.4 116.25)\n\t\t(end 140.8 116.25)\n'
           '\t\t(width 0.6)\n\t\t(layer "In1.Cu")\n\t\t(net "/USB_DP")\n'
           '\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000002")\n\t)\n')
    i = text.rfind(")")
    pcb.write_text(text[:i] + seg + text[i:], encoding="utf-8")
    rep = _drc(cli, pcb)
    names = _rule_names(rep)
    assert "aiee_diff_w_USB_In1" in names, rep["violations"]
    assert "aiee_diff_outer_only_USB" not in names


# ========================= T6 P5-5: check_dru (ladder row 160 - the DRU half)

def test_check_dru_passes_generated_dru(tmp_path):
    cap = _cap("2layer_2oz")
    rules, _ = rules_gen.build({}, cap, _stackup("JLC2313_1.6_2oz"), False)
    dru = rules_gen.render_dru(rules)
    assert fabfloors.check_dru(dru, cap) == []


def test_check_dru_catches_dropped_and_lowered_floors():
    """The lumina-carrier failure path: a hand-written DRU that drops or
    lowers an aiee_* floor must produce one NAMED failure per defect."""
    cap = _cap("2layer_2oz")
    dru = rules_gen.render_dru(rules_gen.baseline_rules(cap))
    # drop the hole-to-hole rule entirely
    block = fabfloors._rule_block(dru, "aiee_hole_to_hole_floor")
    dropped = dru.replace(block, "")
    msgs = fabfloors.check_dru(dropped, cap)
    assert any("aiee_hole_to_hole_floor" in m and "missing" in m for m in msgs)
    # lower the track-width floor below the profile
    lowered = dru.replace(f"(min {cap['min_trace_width_mm']:.4f}mm)",
                          "(min 0.1000mm)", 1)
    msgs = fabfloors.check_dru(lowered, cap)
    assert any("aiee_track_width_floor" in m and "BELOW" in m for m in msgs)


def test_check_dru_parses_hand_edited_style():
    """Hand-edited DRUs close rules on the last clause's line and carry
    parens inside condition strings (carrier style) - the balanced scan must
    still find the floors."""
    cap = _cap("4layer_1oz")
    hand = "(version 1)\n"
    for name, key in fabfloors.DRU_FLOOR_KEYS.items():
        hand += (f'(rule "{name}"\n'
                 f'\t(constraint clearance (min {cap[key]:.4f}mm))\n'
                 f'\t(condition "!(A.Type == \'Pad\' && B.Type == \'Pad\')")'
                 f'\t(severity error))\n')
    assert fabfloors.check_dru(hand, cap) == []


def test_rules_gen_check_dru_cli(tmp_path, capsys):
    """--check-dru: exit 0 clean / 1 with named failures (SPEC 6), resolving
    the capability row with the same stackup-derived copper weight."""
    cap = _cap("2layer_2oz")
    dru_path = tmp_path / "b.kicad_dru"
    dru_path.write_text(
        rules_gen.render_dru(rules_gen.baseline_rules(cap)), encoding="utf-8")
    rc = rules_gen.main(["--check-dru", str(dru_path), "--layers", "2",
                         "--stackup", "JLC2313_1.6_2oz"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "check_dru" and out["status"] == "pass"
    assert out["capability_class"] == "2layer_2oz" and out["failures"] == []

    bad = dru_path.read_text("utf-8").replace('(rule "aiee_clearance_floor"',
                                              '(rule "renamed_away"')
    dru_path.write_text(bad, encoding="utf-8")
    rc = rules_gen.main(["--check-dru", str(dru_path), "--layers", "2",
                         "--stackup", "JLC2313_1.6_2oz"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "fail"
    assert any("aiee_clearance_floor" in f for f in out["failures"])


def test_baseline_rules_share_the_check_dru_map():
    """Writer/checker anti-drift: baseline_rules emits exactly the rule set
    check_dru asserts, with minimums from the same capability keys."""
    cap = _cap("4layer_1oz")
    rules = {r.name: r for r in rules_gen.baseline_rules(cap)}
    assert set(rules) == set(fabfloors.DRU_FLOOR_KEYS)
    for name, key in fabfloors.DRU_FLOOR_KEYS.items():
        assert rules[name].minv == cap[key]


# ==================== T6 P5-7: stackup freshness warning (V18, ladder row 169)

def _fresh_date(*ymd):
    import datetime
    return datetime.date(*ymd)


def test_stackup_freshness_warns_when_stale():
    s = _stackup("JLC04161H-1080B")           # jlc_open_api, verified 2026-08-06
    verified = s["provenance"]["verified"]
    f = board_init.stackup_freshness(s, today=_fresh_date(2026, 9, 15))
    assert f is not None and f["verified"] == verified
    assert f["age_days"] > board_init.FRESHNESS_MAX_AGE_DAYS
    assert "getImpedanceTemplateSettingList" in f["warning"]


def test_stackup_freshness_quiet_when_fresh_or_uncontrolled():
    s = _stackup("JLC04161H-1080B")
    verified = s["provenance"]["verified"]
    y, m, d = (int(x) for x in verified.split("-"))
    fresh_day = _fresh_date(y, m, d) + __import__("datetime").timedelta(days=7)
    assert board_init.stackup_freshness(s, today=fresh_day) is None
    # vendor_page 2-layer stackups never warn (no controlled impedance to rot)
    s2 = _stackup("JLC2313_1.6_2oz")
    assert board_init.stackup_freshness(s2, today=_fresh_date(2030, 1, 1)) is None


def test_stackup_freshness_always_warns_on_design_intent():
    """A design-intent stack (the fab publishes none: advancedpcb) is our
    lamination with an assumed epsilon_r; no verified date makes it the
    fab's, so board_init says so on every run."""
    for name in ("ADVANCEDPCB-4L-1.2-FR4", "ADVANCEDPCB-6L-1.2-FR4"):
        f = board_init.stackup_freshness(_stackup(name),
                                         today=_fresh_date(2026, 11, 10))
        assert f and f["age_days"] is None, name
        assert "confirm" in f["warning"], name

def test_bench_score_p5_excludes_freshness():
    """stackup_freshness is today-dependent - it must NEVER enter the bench
    metric set (score_p5 cherry-picks its fields; pin them)."""
    src = (SCRIPTS / "bench.py").read_text("utf-8")
    m = re.search(r"def score_p5\(ctx\):(.*?)\ndef \w+\(", src, re.S)
    assert m, "score_p5 not found in bench.py"
    body = m.group(1)
    assert "stackup_freshness" not in body
    for key in ("setup_violations", "not_clean", "transient_silk"):
        assert key in body   # the pinned penalty set is intact


@pytest.mark.smoke
def test_rules_gen_pro_write_keeps_board_clean(cli, tmp_path):
    """Writing net_settings into the .kicad_pro must not break the board
    (LEARNINGS [kicad]: a bad pro blob silently disables overrides)."""
    pcb = _prep_golden(tmp_path)
    rc = rules_gen.main([
        "--constraints", str(GOLDEN / "usbbuck4" / "constraints.json"),
        "--layers", "4", "--out-dru", str(pcb.with_suffix(".kicad_dru")),
        "--pro", str(pcb.with_suffix(".kicad_pro")), "--out", str(tmp_path / "r.json")])
    assert rc == 0
    pro = json.loads(pcb.with_suffix(".kicad_pro").read_text("utf-8"))
    names = {c["name"] for c in pro["net_settings"]["classes"]}
    assert names >= {"Default", "Pwr_0p25mm", "Diff90"}
    # ... and the written floors still satisfy the fab profile
    assert fabfloors.check_pro(pro, _cap()) == []
    rep = _drc(cli, pcb, parity=True)
    assert rep["counts"]["total"] == 0, rep["violations"]


def test_board_init_retry_keeps_project_settings(tmp_path, monkeypatch):
    """The unconnected-nets retry re-runs the worker, which can leave a default
    .kicad_pro; board_init must rewrite its own so the ignore settings (and
    with them the lib_footprint_issues suppression) survive to the self-check."""
    net = tmp_path / "m.net"
    net.write_text(MINI_NET, encoding="utf-8")
    out = tmp_path / "k"
    runs = []

    class CP:
        def __init__(self, out_):
            self.stdout, self.stderr = out_, ""

    def fake_run(cmd, **kw):
        job = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        pcb = Path(job["out"])
        pcb.write_text("(kicad_pcb (version 20260101))\n", encoding="utf-8")
        # the worker's save drops a default project file over ours
        pcb.with_suffix(".kicad_pro").write_text('{"board": {}}', encoding="utf-8")
        runs.append(bool(job.get("skip_unconnected_nets")))
        return CP(json.dumps({"status": "pass", "nets": 2, "bbox": [0, 0, 1, 1]}))

    seen = []

    def fake_check(cli, pcb, has_sch):
        pro = json.loads(pcb.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        seen.append(pro["board"]["design_settings"]["rule_severities"]
                    ["lib_footprint_issues"])
        return {"clean": True, "drc": {}, "setup_violations": [],
                "parity_count": 0, "unconnected_count": 0}

    monkeypatch.setattr(board_init.env, "find_kicad_cli", lambda: Path("kc"))
    monkeypatch.setattr(board_init.env, "find_kicad_python", lambda c: Path("bp"))
    monkeypatch.setattr(board_init.subprocess, "run", fake_run)
    monkeypatch.setattr(board_init, "inject_stackup", lambda *a: None)
    monkeypatch.setattr(board_init, "mode_outline_guard", lambda *a, **k: None)
    monkeypatch.setattr(board_init, "self_check", fake_check)
    # first check asks for the retry, the second is clean
    calls = iter([True, False])
    monkeypatch.setattr(board_init, "_rejects_unconnected_nets",
                        lambda c: next(calls))
    rc = board_init.main(["--netlist", str(net), "--name", "b", "--out", str(out),
                          "--mounting-holes", "0", "--workspace", str(tmp_path)])
    assert rc == 0
    assert runs == [False, True]
    assert seen == ["ignore", "ignore"]


def test_2oz_multilayer_outer_spacing_is_a_dru_rule_on_outer_layers_only():
    """JLC 2 oz multilayer 0.15/0.15 holds the 2 oz copper; 0.5 oz inner keeps
    the row's floor (#627). A 1 oz row writes no such rule."""
    cap = _cap("4layer_2oz")
    assert cap["min_clearance_mm"] == 0.1016
    assert fabfloors.outer_clearance(cap) == 0.15
    rule = {r.name: r for r in rules_gen.baseline_rules(cap)}[
        fabfloors.OUTER_CLEARANCE_RULE]
    assert (rule.constraint, rule.minv, rule.layer) == ("clearance", 0.15, "outer")
    assert fabfloors.OUTER_CLEARANCE_RULE not in {
        r.name for r in rules_gen.baseline_rules(_cap("4layer_1oz"))}
    assert rules_gen.default_class(cap)["clearance"] == 0.15
    # the row is shared by every multilayer 2 oz count
    assert fabfloors.outer_clearance(_cap("6layer_2oz")) == 0.15
    # 2-layer 2 oz has only outer copper: JLC's 0.16 is the row itself
    assert _cap("2layer_2oz")["min_clearance_mm"] == 0.16
    assert fabfloors.outer_clearance(_cap("2layer_2oz")) == 0.16


def test_check_dru_holds_the_outer_floor_only_where_the_row_has_one():
    cap = _cap("4layer_2oz")
    dru = "(version 1)\n" + "\n".join(
        r.render() for r in rules_gen.baseline_rules(cap))
    assert fabfloors.check_dru(dru, cap) == []
    gone = dru.replace(f'"{fabfloors.OUTER_CLEARANCE_RULE}"', '"renamed"')
    assert any(fabfloors.OUTER_CLEARANCE_RULE in m
               for m in fabfloors.check_dru(gone, cap))
    low = dru.replace("(constraint clearance (min 0.1500mm))",
                      "(constraint clearance (min 0.1016mm))")
    assert any(fabfloors.OUTER_CLEARANCE_RULE in m
               for m in fabfloors.check_dru(low, cap))
    every_layer = dru.replace("\t(layer outer)\n", "")
    assert any(fabfloors.OUTER_CLEARANCE_RULE in m
               for m in fabfloors.check_dru(every_layer, cap))
    one_oz = _cap("4layer_1oz")
    dru1 = "(version 1)\n" + "\n".join(
        r.render() for r in rules_gen.baseline_rules(one_oz))
    assert fabfloors.check_dru(dru1, one_oz) == []


def test_voltage_rules_soldermask_splits_track_and_land_rows():
    """soldermask at 54 V: tented tracks sit on B4 (0.13 mm, floored to the
    fab clearance) and only a gap with an exposed land needs A6 (0.50 mm).
    The net-wide rule carries the track row and an outer-layer _land rule
    after it carries the land row, the split check_creepage adjudicates;
    the fact (netclass, isolation floors) keeps the conservative max."""
    cons = {"coating": "soldermask",
            "voltages": [{"net": "/SW", "voltage": 54}]}
    cap = _cap("2layer_2oz")
    rules, report = rules_gen.build(cons, cap, _stackup("JLC2313_1.6_2oz"), False)
    byname = {r.name: r for r in rules}
    base, land = byname["aiee_hv_54v_SW"], byname["aiee_hv_54v_SW_land"]
    floor = rules_gen.fabfloors.outer_clearance(cap)
    assert base.minv == pytest.approx(max(0.13, floor))
    assert base.condition == "A.NetName == '/SW'"
    assert land.minv == pytest.approx(0.50) and land.layer == "outer"
    assert land.condition == ("A.NetName == '/SW' && "
                              "(A.Type == 'Pad' || B.Type == 'Pad')")
    names = [r.name for r in rules]
    assert names.index("aiee_hv_54v_SW_land") > names.index("aiee_hv_54v_SW")
    (fact,) = report["hv_rules"]
    assert fact["clearance_mm"] == pytest.approx(0.50)
    assert fact["land_rule"] == "aiee_hv_54v_SW_land"
