"""S11 acceptance tests: critical-net routing via KRT (route_critical).

Pure tests (constraint parsing, S5 pair-discovery reuse, IPC width numbers,
plan ordering, JSON_SUMMARY parsing, grading floors, stub-pad detach/restore,
missing-tool errors) run with no toolchain and are unmarked. The `smoke` test
builds the placed-unrouted usbbuck4 corpus board (board_init -> place_seed,
the test_place_anneal pattern) and drives KRT + kicad-cli live:
/USB_DP + /USB_DM routed as a coupled pair passing check_diffpair, +3V3/VBUS
at IPC-2152 width, no new DRC errors.

Determinism note: KRT stamps fresh uuid4s, so assertions compare routed nets
and DRC outcomes, never board bytes.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_diffpair  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import route_critical as rc  # noqa: E402
from checklib import CheckError  # noqa: E402


# ---- synthetic board helpers (test_place_anneal pattern) --------------------

def _fp(ref: str, x: float, y: float, pads: str = "") -> str:
    return (f'  (footprint "t:{ref}" (layer "F.Cu")\n'
            f'    (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'{pads}  )\n')


def _pad(num: str, x: float, y: float, net: str | None = None,
         size: float = 0.6) -> str:
    n = f'\n      (net "{net}")' if net else ""
    return (f'    (pad "{num}" smd rect\n      (at {x} {y})\n'
            f'      (size {size} {size})\n      (layers "F.Cu"){n}\n'
            f'      (uuid "uu-{num}-{net or "x"}")\n    )\n')


def _pcb(tmp_path, name: str, body: str, w: float = 60.0, h: float = 40.0) -> Path:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _pair_board(tmp_path) -> Path:
    """U1 P/N pads adjacent, J2 P/N pads adjacent, R9 pull-up stub on P."""
    body = _fp("U1", 40, 20,
               pads=_pad("1", 0, 0, "/USB_DP") + _pad("2", 0, 0.5, "/USB_DM"))
    body += _fp("J2", 10, 20,
                pads=_pad("1", 0, 0, "/USB_DP") + _pad("2", 0, 0.65, "/USB_DM"))
    body += _fp("R9", 45, 30,
                pads=_pad("1", -0.5, 0, "/USB_DP") + _pad("2", 0.5, 0, "+3V3"))
    return _pcb(tmp_path, "pairboard", body)


# ============================================================ pure: widths

def test_power_width_numbers():
    # IPC-2152 minimum (check_current.required_width_mm) * 1.5, floor 0.3
    assert rc.power_width_mm(0.4) == 0.3          # 0.2 * 1.5 = 0.3
    assert rc.power_width_mm(0.5) == 0.375        # 0.25 * 1.5
    assert rc.power_width_mm(2.0) == 1.65         # 1.10 * 1.5
    assert rc.power_width_mm(3.0) == 2.7          # 1.80 * 1.5
    assert rc.power_width_mm(0.05) == 0.3         # clamped to the floor
    # hotter allowance -> narrower requirement, still floored
    assert rc.power_width_mm(0.4, dt_c=20.0) == 0.3
    # thicker copper -> narrower
    assert rc.power_width_mm(2.0, cu_mm=0.070) == pytest.approx(0.825)


def test_power_specs_from_constraints():
    cons = {"power": [{"net": "+3V3", "current_a": 0.4},
                      {"net": "VBUS", "current_a": 0.5, "dt_c": 10},
                      {"net": None, "current_a": 1.0},       # ignored
                      {"net": "/X"}]}                        # no current: ignored
    specs = rc.power_specs(cons)
    assert [(s["net"], s["width_mm"]) for s in specs] == \
        [("+3V3", 0.3), ("VBUS", 0.375)]


# ============================================================ pure: pairing

def test_diff_specs_discovery_reuses_s5_checker():
    cons = {"high_speed": [{"net": "/MCO", "reference": "GND"},
                           {"net": "/USB_DP"}, {"net": "/USB_DM"}]}
    specs = rc.diff_specs(cons, [])
    assert specs == [{"p": "/USB_DP", "n": "/USB_DM", "impedance_ohm": 90}]
    # byte-for-byte the S5 checker's pairing on the same names
    hs = [e["net"] for e in cons["high_speed"]]
    assert [(s["p"], s["n"]) for s in specs] == check_diffpair.discover_pairs(hs)


def test_diff_specs_non_usb_defaults_100_ohm():
    cons = {"high_speed": [{"net": "/LVDS_P"}, {"net": "/LVDS_N"}]}
    specs = rc.diff_specs(cons, [])
    assert specs == [{"p": "/LVDS_P", "n": "/LVDS_N", "impedance_ohm": 100}]


def test_diff_specs_leave_out_sense_pairs():
    """#967: a low-frequency / sense pair wants equal legs, not a coupled
    run at an impedance; route_critical must not route it as a diff pair."""
    cons = {"diff_pairs": [{"p": "/INP", "n": "/INN",
                            "kind": "low_frequency"},
                           {"p": "/USB_DP", "n": "/USB_DM"}],
            "high_speed": [{"net": "/ISNS_P"}, {"net": "/ISNS_N"},
                           {"net": "/INP"}, {"net": "/INN"}]}
    sense: list = []
    specs = rc.diff_specs(cons, [], sense=sense)
    assert [(s["p"], s["n"]) for s in specs] == [("/USB_DP", "/USB_DM")]
    assert sense == [["/INP", "/INN"], ["/ISNS_P", "/ISNS_N"]]
    assert rc.diff_specs(cons, []) == specs


def test_diff_specs_explicit_wins_over_discovery():
    cons = {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DM", "gap_mm": 0.4,
                            "impedance_ohm": 85}],
            "high_speed": [{"net": "/USB_DP"}, {"net": "/USB_DM"}]}
    specs = rc.diff_specs(cons, [])
    assert len(specs) == 1
    assert specs[0]["impedance_ohm"] == 85 and specs[0]["gap_mm"] == 0.4


def test_rf_specs_excludes_pair_halves():
    cons = {"rf": [{"net": "/ANT", "impedance_ohm": 50}],
            "high_speed": [{"net": "/CLK", "impedance_ohm": 50},
                           {"net": "/USB_DP", "impedance_ohm": 90}]}
    specs = rc.rf_specs(cons, {"/USB_DP", "/USB_DM"})
    assert [(s["net"], s["impedance_ohm"]) for s in specs] == \
        [("/ANT", 50), ("/CLK", 50)]


# ============================================================ pure: plan

def test_plan_order_default_only_and_override():
    assert rc.plan_order(None, None) == ["diff", "rf", "power"]
    assert rc.plan_order("power", None) == ["power"]
    assert rc.plan_order(None, {"order": ["power", "diff"]}) == ["power", "diff"]
    assert rc.plan_order("diff", {"order": ["power", "diff"]}) == ["diff"]
    with pytest.raises(CheckError):
        rc.plan_order(None, {"order": ["bogus"]})
    with pytest.raises(CheckError):
        rc.plan_order(None, {"order": []})


# ============================================================ pure: summary

def test_parse_summary_takes_last_line():
    out = ('noise\nJSON_SUMMARY: {"successful": 1}\nmore\n'
           'JSON_SUMMARY: {"successful": 2, "failed_single": []}\n')
    assert rc.parse_summary(out) == {"successful": 2, "failed_single": []}


def test_parse_summary_absent_or_bad():
    assert rc.parse_summary("no summary here\n") is None
    assert rc.parse_summary("JSON_SUMMARY: not-json\n") is None


def test_pair_outcome_and_net_failed():
    summary = {"routed_diff_pairs": ["/USB_D"],
               "pair_reports": [{"pair": "/USB_D", "p_net": "/USB_DP",
                                 "n_net": "/USB_DM", "outcome": "coupled"}]}
    assert rc._pair_outcome(summary, "/USB_DP", "/USB_DM") == (True, "coupled")
    deferred = {"routed_diff_pairs": [],
                "pair_reports": [{"pair": "/USB_D", "p_net": "/USB_DP",
                                  "n_net": "/USB_DM", "outcome": "deferred",
                                  "failure_reason": "no-escape-path"}]}
    ok, reason = rc._pair_outcome(deferred, "/USB_DP", "/USB_DM")
    assert not ok and reason == "no-escape-path"
    assert rc._pair_outcome({}, "/A_P", "/A_N") == (False, "pair-not-detected")

    assert rc._net_failed({"failed_single": ["VBUS"]}, "VBUS")
    assert rc._net_failed(
        {"failed_multipoint": [{"net_name": "+3V3", "failed_pads": [{}]}]},
        "+3V3") == "multipoint: 1 pad(s) unconnected"
    assert rc._net_failed({"failed_single": [], "failed_multipoint": []},
                          "+3V3") is None


# ============================================================ pure: floors

def test_grading_floors_defaults_and_pro(tmp_path):
    assert rc.grading_floors(None) == rc.KICAD_DEFAULTS
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({
        "board": {"design_settings": {"rules": {
            "min_track_width": 0.1, "min_copper_to_edge": 0.3,
            "min_via_diameter": 0.45, "min_through_hole_diameter": 0.2}}},
        "net_settings": {"classes": [
            {"name": "Default", "clearance": 0.127},
            {"name": "Power", "clearance": 0.2}]},
    }), encoding="utf-8")
    floors = rc.grading_floors(pro)
    assert floors == {"clearance": 0.2, "track_width": 0.1,
                      "via_diameter": 0.45, "via_drill": 0.2,
                      "edge_clearance": 0.3}
    # a project without those keys keeps every stock default
    pro2 = tmp_path / "c.kicad_pro"
    pro2.write_text(json.dumps({"board": {"design_settings": {"rules": {
        "min_track_width": 0.1}}}}), encoding="utf-8")
    f2 = rc.grading_floors(pro2)
    assert f2["track_width"] == 0.1 and f2["clearance"] == 0.2
    assert f2["via_diameter"] == 0.5 and f2["edge_clearance"] == 0.5


# ============================================================ pure: geometry

class _FakeStackup:
    dielectrics = [object()]
    copper_thickness = {"F.Cu": 0.035}

    def adjacent(self, layer):
        return None, "In1.Cu"

    def height_between(self, a, b):
        return 0.2104

    def epsilon_between(self, a, b):
        return 4.05


class _FakeBG:
    copper_layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    stackup = _FakeStackup()


def test_pair_geometry_matches_rules_gen_math():
    # same numbers as rules_gen/impedance (field solver) on the S8 4-layer
    # JLC stackup; the closed form gave 0.314 / 0.3669 here
    w, g = rc.pair_geometry(_FakeBG(), 90)
    assert (w, g) == (0.3132, 0.2104)
    assert rc.rf_width(_FakeBG(), 50) == pytest.approx(
        rc.imp.solve_width(50, 0.2104, 0.035, 4.05), abs=1e-4)


def test_pair_geometry_fallback_without_stackup():
    class Bare:
        copper_layers = ["F.Cu"]

        class stackup:
            dielectrics = []
            copper_thickness = {}

            @staticmethod
            def adjacent(layer):
                return None, None
    assert rc.pair_geometry(Bare(), 90) == (rc.FALLBACK_PAIR_WIDTH, 0.2)
    assert rc.rf_width(Bare(), 50) == rc.BASE_TRACK_WIDTH


def test_pair_geometry_pinned_pitch():
    # constraints gap_mm is CENTER-to-center; width + edge gap ~= pitch
    w, g = rc.pair_geometry(_FakeBG(), 90, gap_c2c_mm=0.5)
    assert w + g == pytest.approx(0.5, abs=0.02)


# ============================================================ pure: stub pads

def test_unmatched_stub_pads_finds_pullup(tmp_path):
    bg = geom.BoardGeom.from_file(_pair_board(tmp_path))
    assert rc.unmatched_stub_pads(bg, "/USB_DP", "/USB_DM") == \
        [("R9", "/USB_DP")]


def _usbc_esd_board(tmp_path) -> Path:
    """J1 USB-C lands the pair twice (A6/A7, B6/B7 swapped), D5 an ESD clamp
    taps it (one pad per half + GND), D6 a flow-through clamp (two pads per
    half), U1 the far end."""
    body = _fp("J1", 5, 20, pads=_pad("A6", 0, -0.25, "/USB_DP")
               + _pad("A7", 0, 0.25, "/USB_DM")
               + _pad("B6", -1.7, 0.25, "/USB_DP")
               + _pad("B7", -1.7, -0.25, "/USB_DM"))
    body += _fp("D5", 20, 26, pads=_pad("3", 0, 0, "/USB_DP")
                + _pad("5", 1, 0, "/USB_DM") + _pad("2", 0.5, 1, "GND"))
    body += _fp("U1", 40, 20,
                pads=_pad("1", 0, 0, "/USB_DP") + _pad("2", 0, 0.5, "/USB_DM"))
    return _pcb(tmp_path, "usbc_esd", body)


def test_esd_tap_pads_detaches_clamp_only(tmp_path):
    bg = geom.BoardGeom.from_file(_usbc_esd_board(tmp_path))
    assert rc.esd_tap_pads(bg, "/USB_DP", "/USB_DM") == [
        ("D5", "/USB_DP"), ("D5", "/USB_DM")]


def test_esd_tap_pads_keeps_flow_through_and_two_terminal(tmp_path):
    body = _fp("D6", 20, 20, pads=_pad("1", 0, 0, "/USB_DP")
               + _pad("2", 0, 0.5, "/USB_DM") + _pad("9", 2, 0, "/USB_DP")
               + _pad("8", 2, 0.5, "/USB_DM"))
    body += _fp("U1", 40, 20,
                pads=_pad("1", 0, 0, "/USB_DP") + _pad("2", 0, 0.5, "/USB_DM"))
    bg = geom.BoardGeom.from_file(_pcb(tmp_path, "flow", body))
    assert rc.esd_tap_pads(bg, "/USB_DP", "/USB_DM") == []
    # a clamp that IS one of only two terminals stays a terminal
    body = _fp("D5", 20, 20, pads=_pad("3", 0, 0, "/USB_DP")
               + _pad("5", 1, 0, "/USB_DM"))
    body += _fp("U1", 40, 20,
                pads=_pad("1", 0, 0, "/USB_DP") + _pad("2", 0, 0.5, "/USB_DM"))
    bg = geom.BoardGeom.from_file(_pcb(tmp_path, "two", body))
    assert rc.esd_tap_pads(bg, "/USB_DP", "/USB_DM") == []


def test_duplicate_row_pads_keeps_row_nearest_far_end(tmp_path):
    bg = geom.BoardGeom.from_file(_usbc_esd_board(tmp_path))
    got = rc.duplicate_row_pads(bg, "/USB_DP", "/USB_DM", skip={"D5"})
    assert got == [("J1", "/USB_DP", "B6"), ("J1", "/USB_DM", "B7")]
    # a single-row part is never touched
    assert all(r == "J1" for r, *_ in got)


def test_detach_single_pad_roundtrip(tmp_path):
    original = _usbc_esd_board(tmp_path).read_text(encoding="utf-8")
    detached, restores = rc.detach_stub_pads(
        original, [("J1", "/USB_DP", "B6"), ("J1", "/USB_DM", "B7")])
    s, e = rc._footprint_block(detached, "J1")
    block = detached[s:e]
    assert block.count('(net "/USB_DP")') == 1      # A6 kept, B6 detached
    assert block.count('(net "/USB_DM")') == 1
    a6 = block[rc._pad_block(block, "A6")[0]:rc._pad_block(block, "A6")[1]]
    assert '(net "/USB_DP")' in a6
    assert rc.restore_stub_pads(detached, restores) == original
    with pytest.raises(CheckError):
        rc.detach_stub_pads(original, [("J1", "/USB_DP", "Z9")])


def test_detach_restore_roundtrip(tmp_path):
    original = _pair_board(tmp_path).read_text(encoding="utf-8")
    detached, restores = rc.detach_stub_pads(original, [("R9", "/USB_DP")])
    assert detached != original
    s, e = rc._footprint_block(detached, "R9")
    assert '(net "/USB_DP")' not in detached[s:e]
    assert '(net "+3V3")' in detached[s:e]          # partner pad untouched
    assert rc.restore_stub_pads(detached, restores) == original


def test_detach_handles_numbered_net_nodes(tmp_path):
    text = _pair_board(tmp_path).read_text(encoding="utf-8") \
        .replace('(net "/USB_DP")', '(net 14 "/USB_DP")')
    detached, restores = rc.detach_stub_pads(text, [("R9", "/USB_DP")])
    s, e = rc._footprint_block(detached, "R9")
    assert '"/USB_DP"' not in detached[s:e]
    assert rc.restore_stub_pads(detached, restores) == text


def test_detach_missing_pad_or_ref_raises(tmp_path):
    text = _pair_board(tmp_path).read_text(encoding="utf-8")
    with pytest.raises(CheckError):
        rc.detach_stub_pads(text, [("R9", "/NOPE")])
    with pytest.raises(CheckError):
        rc.detach_stub_pads(text, [("R99", "/USB_DP")])
    with pytest.raises(CheckError):
        rc.restore_stub_pads("unrelated text", [("gone", "orig")])


# ============================================================ pure: T6 KRT facts

CARRIER_COVERAGE = ("Coverage: 1863/13659 frontier cells attributed to "
                    "routed nets; 11796 static/unrippable")


def test_coverage_static_share_parses_carrier_line():
    share = rc.coverage_static_share(
        "noise\n" + CARRIER_COVERAGE + "\nmore\n")
    assert share == pytest.approx(11796 / 13659)      # ~0.86
    assert rc.coverage_static_share("no such line") is None
    assert rc.coverage_static_share("") is None
    # the LAST line wins
    two = (CARRIER_COVERAGE + "\n"
           "Coverage: 5/10 frontier cells attributed to routed nets; "
           "5 static/unrippable\n")
    assert rc.coverage_static_share(two) == pytest.approx(0.5)


def test_parse_dru_rules_and_net_floors(tmp_path):
    dru = tmp_path / "b.kicad_dru"
    dru.write_text("""(version 1)
(rule "aiee_clearance_floor"
\t(constraint clearance (min 0.1524mm))
)
(rule "aiee_pwr_width_VBUS"
\t(constraint track_width (min 1.7500mm))
\t(condition "A.NetName == 'VBUS' && A.Type == 'track'")
)
(rule "aiee_hv_clearance"
\t(constraint clearance (min 0.635mm))
\t(condition "A.NetName == 'V48_RAW' || B.NetName == 'V48_RAW'")
)
""", encoding="utf-8")
    rules = rc.parse_dru_rules(dru.read_text(encoding="utf-8"))
    assert {r["name"] for r in rules} == {
        "aiee_clearance_floor", "aiee_pwr_width_VBUS", "aiee_hv_clearance"}
    base = next(r for r in rules if r["name"] == "aiee_clearance_floor")
    assert base["nets"] == [] and base["min_mm"] == 0.1524
    assert base["condition"] is None and base["layer"] is None
    hv = next(r for r in rules if r["name"] == "aiee_hv_clearance")
    assert hv["condition"] == ("A.NetName == 'V48_RAW' || "
                               "B.NetName == 'V48_RAW'")
    assert rc.dru_net_floors(dru, "track_width") == {"VBUS": 1.75}
    assert rc.dru_net_floors(dru, "clearance") == {"V48_RAW": 0.635}
    assert rc.dru_net_floors(None, "clearance") == {}
    assert rc.dru_net_floors(tmp_path / "nope.kicad_dru", "clearance") == {}


def test_build_net_clearances_class_plus_dru(tmp_path):
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {
        "classes": [{"name": "Default", "clearance": 0.15},
                    {"name": "HV", "clearance": 0.4}],
        "netclass_patterns": [{"netclass": "HV", "pattern": "V48*"}],
    }}), encoding="utf-8")
    dru = tmp_path / "b.kicad_dru"
    dru.write_text("""(rule "hv"
\t(constraint clearance (min 0.635mm))
\t(condition "A.NetName == 'V48_RAW' || B.NetName == 'V48_RAW'")
)
""", encoding="utf-8")
    nets = {"V48_RAW", "V48_RTN", "/SIG", "GND"}
    m = rc.build_net_clearances(pro, dru, nets)
    # class gives both V48 nets 0.4; the DRU raises V48_RAW to 0.635;
    # nothing is ever capped DOWN (max of class and DRU)
    assert m == {"V48_RAW": 0.635, "V48_RTN": 0.4}
    # no sources at all -> None (no file emitted)
    assert rc.build_net_clearances(None, None, nets) is None
    # unparseable pro -> fail OPEN (None), never wrong values
    bad = tmp_path / "bad.kicad_pro"
    bad.write_text("{not json", encoding="utf-8")
    assert rc.build_net_clearances(bad, dru, nets) is None


def test_power_net_clearances_raises_track_nets_only(tmp_path):
    """KRT fractional-margin workaround: the power runs keep half a grid step
    more off nets already carrying tracks (usbbuck4: VBUS 0.1854 mm from the
    off-grid USB_DM diagonal against 0.2), raising nothing else."""
    shared = tmp_path / "net_clearances.json"
    shared.write_text(json.dumps({"/USB_DM": 0.3, "V48": 0.635}),
                      encoding="utf-8")
    ctx = {"work": tmp_path, "floors": {"clearance": 0.2}, "grid_step": 0.05,
           "net_clearances": shared}
    out = rc.power_net_clearances(ctx, ["/USB_DP", "/USB_DM"])
    m = json.loads(out.read_text(encoding="utf-8"))
    # a net at the floor and a net with its own clearance both gain 0.025;
    # a net with no tracks keeps its value
    assert m == {"/USB_DP": 0.225, "/USB_DM": 0.325, "V48": 0.635}
    # the shared map the diff/rf runs use is untouched
    assert json.loads(shared.read_text(encoding="utf-8")) == \
        {"/USB_DM": 0.3, "V48": 0.635}
    # nothing routed yet -> the shared map (or no file) goes through as is
    assert rc.power_net_clearances(ctx, []) == shared
    assert rc.power_net_clearances({**ctx, "net_clearances": None}, []) is None
    # no shared map -> the raised nets alone
    out2 = rc.power_net_clearances({**ctx, "net_clearances": None},
                                   ["/USB_DP"])
    assert json.loads(out2.read_text(encoding="utf-8")) == {"/USB_DP": 0.225}


def _seg(x1, y1, x2, y2, net, uid, w=0.2):
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) '
            f'(layer "F.Cu") (net "{net}") (uuid "{uid}"))\n')


def test_normalize_power_widths_widens_only_where_clearance_holds(
        tmp_path, monkeypatch):
    """#957: the voice board's +5V (1.56 mm floor) was widened blind round
    its buck - 18 shorts, 40 mask bridges. Planted faults: a foreign GND pad
    beside a narrow power segment, a segment by the board edge, and a second
    power net whose widen would hit the first net's widened copper. Each is
    left narrow and listed; the free segment is widened, the crumb dropped. (uuids: hex, as
    route_critical._SEG_BLOCK_RE reads them.)"""
    seen = []
    monkeypatch.setattr(rc.route_edit, "apply_ops",
                        lambda path, ops: seen.extend(ops))
    body = _fp("C1", 12, 20.6, pads=_pad("2", 0, 0, "GND"))
    body += _seg(10, 10, 14, 10, "P5", "f1")
    body += _seg(10, 20, 14, 20, "P5", "b1")
    body += _seg(20, 10, 20.02, 10, "P5", "c1")
    body += _seg(20, 39.6, 24, 39.6, "P5", "e1")
    body += _seg(30, 5, 34, 5, "P5", "a1")
    body += _seg(30, 6, 34, 6, "P9", "a2")
    body += _seg(30, 15, 34, 15, "P9", "d1", w=1.0)
    pcb = _pcb(tmp_path, "norm", body)
    specs = [{"net": "P5", "ipc_min_mm": 1.0}, {"net": "P9", "ipc_min_mm": 1.0}]
    r = rc.normalize_power_widths(pcb, specs, clearance=0.1,
                                  edge_clearance=0.25)
    removed = {o["uuid"] for o in seen if o["op"] == "remove"}
    assert removed == {"f1", "c1", "a1"}
    adds = [o for o in seen if o["op"] == "add_track"]
    assert [(o["start"], o["width"], o["net"]) for o in adds] == \
        [([10.0, 10.0], 1.0, "P5"), ([30.0, 5.0], 1.0, "P5")]
    assert r["widened"] == 2 and r["crumbs_removed"] == 1
    assert [(b["net"], b["start"]) for b in r["blocked"]] == \
        [("P5", [10.0, 20.0]), ("P5", [20.0, 39.6]), ("P9", [30.0, 6.0])]
    assert r["blocked"][0]["width_mm"] == 0.2
    assert r["blocked"][0]["want_mm"] == 1.0
    # no edge clearance given: the edge segment widens; copper that would
    # overlap the pad outright stays blocked even at zero clearance
    seen.clear()
    r0 = rc.normalize_power_widths(pcb, specs[:1])
    assert r0["widened"] == 3
    assert [b["start"] for b in r0["blocked"]] == [[10.0, 20.0]]


def test_power_width_blocked_one_error_per_net():
    blocked = [{"net": "+5V", "layer": "F.Cu", "start": [1, 2], "end": [3, 2],
                "width_mm": 0.2, "want_mm": 1.5576}] * 3
    blocked += [{"net": "+9V", "layer": "B.Cu", "start": [5, 5],
                 "end": [6, 5], "width_mm": 0.2, "want_mm": 1.248}]
    vs = rc.power_width_blocked(blocked)
    assert [(v["net"], v["severity"], v["kind"]) for v in vs] == \
        [("+5V", "error", "power_width_blocked"),
         ("+9V", "error", "power_width_blocked")]
    assert "3 segment(s)" in vs[0]["msg"]
    assert "1.5576 mm floor" in vs[0]["msg"]
    assert rc.power_width_blocked([]) == []


def test_run_krt_passes_net_clearances_and_captures_stdout(tmp_path,
                                                          monkeypatch):
    seen = {}

    class CP:
        returncode = 0
        stdout = 'pre\nJSON_SUMMARY: {"successful": 1}\n'
        stderr = ""

    def fake_run(args, **kw):
        seen["args"] = args
        return CP()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    sink: list[str] = []
    ncl = tmp_path / "ncl.json"
    summary = rc.run_krt(tmp_path, "route.py", tmp_path / "b.kicad_pcb",
                         tmp_path / "o.kicad_pcb", ["--nets", "X"],
                         rc.KICAD_DEFAULTS, tmp_path / "fab.txt", 0.05, 60,
                         net_clearances=ncl, stdout_sink=sink)
    assert summary == {"successful": 1}
    a = seen["args"]
    assert "--net-clearances" in a and str(ncl) in a
    assert a.index("--net-clearances") < a.index("--nets")  # before extra
    assert sink and "JSON_SUMMARY" in sink[0]
    # omitted -> flag absent
    rc.run_krt(tmp_path, "route.py", tmp_path / "b.kicad_pcb",
               tmp_path / "o.kicad_pcb", [], rc.KICAD_DEFAULTS,
               tmp_path / "fab.txt", 0.05, 60)
    assert "--net-clearances" not in seen["args"]


def _retry_ctx(tmp_path) -> dict:
    pcb = _pair_board(tmp_path)
    staged = tmp_path / "staged.kicad_pcb"
    shutil.copy2(pcb, staged)
    return {"staged": staged, "work": tmp_path, "krt": tmp_path,
            "floors": dict(rc.KICAD_DEFAULTS),
            "fab_file": tmp_path / "fab.txt",
            "grid_step": 0.05, "timeout_s": 60}


def test_iteration_ladder_retries_failed_nets_once(tmp_path, monkeypatch):
    """LEARNINGS 1433/1504 as script behavior: no-route + static frontier
    -> ONE retry, failed nets only, at 4M iterations."""
    ctx = _retry_ctx(tmp_path)
    calls = []

    def fake_krt(krt, script, staged, out, extra, floors, fab, grid, t_s,
                 net_clearances=None, stdout_sink=None, layer_widths=None):
        calls.append(list(extra))
        shutil.copy2(staged, out)
        if len(calls) == 1:
            if stdout_sink is not None:
                stdout_sink.append("No route found after 200000 iterations "
                                  "(forward)\n" + CARRIER_COVERAGE + "\n")
            return {"failed_single": ["+3V3"]}
        return {"failed_single": []}

    monkeypatch.setattr(rc, "run_krt", fake_krt)
    facts, violations = rc._route_single_item(
        ctx, "power", ["+3V3"], ["--layers", "F.Cu"], {"+3V3": {}})
    assert violations == []
    assert len(calls) == 2
    assert calls[1][:2] == ["--nets", "+3V3"]
    assert "--max-iterations" in calls[1]
    assert calls[1][calls[1].index("--max-iterations") + 1] == "4000000"
    assert "--max-probe-iterations" in calls[1]
    assert facts[0]["iteration_retry"] is True
    retry = facts[0]["krt_retry"]
    assert retry["kept"] is True and retry["nets"] == ["+3V3"]
    assert retry["static_share"] == pytest.approx(11796 / 13659)


def test_no_retry_when_frontier_is_rippable(tmp_path, monkeypatch):
    """Low static share -> a rip set may genuinely help: no blind retry;
    the violation carries the share so the router can triage."""
    ctx = _retry_ctx(tmp_path)
    calls = []

    def fake_krt(krt, script, staged, out, extra, floors, fab, grid, t_s,
                 net_clearances=None, stdout_sink=None, layer_widths=None):
        calls.append(list(extra))
        shutil.copy2(staged, out)
        if stdout_sink is not None:
            stdout_sink.append(
                "No route found after 200000 iterations (forward)\n"
                "Coverage: 9000/10000 frontier cells attributed to routed "
                "nets; 1000 static/unrippable\n")
        return {"failed_single": ["+3V3"]}

    monkeypatch.setattr(rc, "run_krt", fake_krt)
    facts, violations = rc._route_single_item(
        ctx, "power", ["+3V3"], [], {"+3V3": {}})
    assert len(calls) == 1                       # no retry
    assert facts == []
    assert violations[0]["kind"] == "critical_route_failed"
    assert violations[0]["static_share"] == pytest.approx(0.1)
    assert violations[0]["iteration_retry"] is False


def test_relative_work_dir_is_resolved(tmp_path, monkeypatch):
    """LEARNINGS 1318: KRT runs with cwd=<plugins dir>, so a relative
    --work-dir must be resolved before anything is staged into it."""
    pcb = _pair_board(tmp_path)
    (tmp_path / "constraints.json").write_text(json.dumps(
        {"power": [{"net": "+3V3", "current_a": 0.4}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rc, "_tools", lambda: (Path("cli"), Path("krt")))
    drc = {"counts": {"total": 0, "by_severity": {}, "by_source": {}},
           "violations": []}
    monkeypatch.setattr(rc.kc, "run_drc", lambda *a, **k: dict(drc))
    seen = {}

    def fake_power(ctx, specs):
        seen["work"] = ctx["work"]
        return [], []

    monkeypatch.setattr(rc, "route_power_item", fake_power)
    payload, _ = rc.run(["--pcb", str(pcb), "--work-dir", "relwork"])
    assert payload["status"] == "pass"
    assert seen["work"].is_absolute()
    assert seen["work"] == (tmp_path / "relwork").resolve()


# ============================================================ pure: CLI/tools

def test_missing_krt_is_a_check_error(monkeypatch):
    monkeypatch.setattr(rc.env, "find_kicad_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(rc.env, "find_krt", lambda: None)
    with pytest.raises(CheckError, match="KiCadRoutingTools"):
        rc._tools()


def test_cli_missing_board_exits_2():
    assert rc.main(["--pcb", "no/such/board.kicad_pcb"]) == 2


def test_no_critical_items_is_a_pass(tmp_path):
    # a board with no constraints sidecar has nothing critical: exit 0,
    # board untouched, and no toolchain is required to say so
    pcb = _pair_board(tmp_path)
    raw = pcb.read_bytes()
    rep = tmp_path / "r.json"
    assert rc.main(["--pcb", str(pcb), "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text(encoding="utf-8"))
    assert r["status"] == "pass"
    assert r["facts"]["routed"] == [] and r["facts"]["board_updated"] is False
    assert pcb.read_bytes() == raw


def test_only_filter_skips_other_kinds(tmp_path):
    # --only power on a diff-only constraints file -> nothing to do, skipped
    # reported; no KRT needed
    pcb = _pair_board(tmp_path)
    (tmp_path / "constraints.json").write_text(json.dumps(
        {"high_speed": [{"net": "/USB_DP"}, {"net": "/USB_DM"}]}),
        encoding="utf-8")
    rep = tmp_path / "r.json"
    assert rc.main(["--pcb", str(pcb), "--only", "power",
                    "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text(encoding="utf-8"))
    assert r["facts"]["skipped"] == [
        {"kind": "diff", "count": 1, "why": "not in --only/plan"}]


# ============================================================ smoke: corpus

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


def test_find_krt_follows_a_release_root_to_plugins(tmp_path, monkeypatch):
    """HWDE_KRT_DIR at the Hive's tools folder or its release root (no
    route_diff.py there) still finds the plugins dir."""
    plugins = tmp_path / "kicad-routing-tools" / "KiCadRoutingTools-0.19.0" \
        / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "route_diff.py").write_text("", encoding="utf-8")
    for pin in (plugins, plugins.parent, plugins.parent.parent):
        monkeypatch.setenv("HWDE_KRT_DIR", str(pin))
        assert env.find_krt() == plugins, pin


@pytest.fixture(scope="session")
def krt_dir() -> Path:
    k = env.find_krt()
    if k is None:
        pytest.skip("KiCadRoutingTools not vendored (tools/krt)")
    return k


@pytest.fixture(scope="session")
def placed_board(cli, tmp_path_factory) -> Path:
    """usbbuck4 netlist -> board_init (4 layer) -> place_seed --apply: the
    placed-unrouted S11 input (test_place_anneal's seeded_board pattern)."""
    import board_init
    import kc
    import place_seed
    d = tmp_path_factory.mktemp("critical")
    net = d / "usbbuck4.net"
    kc.export_netlist(cli, GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch", net)
    rc_init = board_init.main([
        "--netlist", str(net), "--name", "usbbuck4",
        "--out", str(d / "kicad"), "--layers", "4", "--mounting-holes", "4"])
    assert rc_init == 0
    pcb = d / "kicad" / "usbbuck4.kicad_pcb"
    for name in ("constraints.json", "decoupling.json"):
        shutil.copy2(GOLDEN / "usbbuck4" / name, pcb.parent / name)
    payload, _ = place_seed.run([
        "--pcb", str(pcb), "--ops-out", str(d / "seed_ops.json"), "--apply"])
    assert payload["status"] == "pass"
    return pcb


@pytest.mark.smoke
def test_critical_routing_acceptance_usbbuck4(placed_board, krt_dir, tmp_path):
    """Plan S11 acceptance: the USB pair routes as a COUPLED pair passing the
    S5 check, power nets route at IPC width, and DRC gains no new errors."""
    rep = tmp_path / "report.json"
    assert rc.main(["--pcb", str(placed_board),
                    "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text(encoding="utf-8"))
    assert r["status"] == "pass"
    facts = r["facts"]

    kinds = {(f["kind"], tuple(f["nets"])) for f in facts["routed"]}
    assert ("diff", ("/USB_DP", "/USB_DM")) in kinds
    assert ("power", ("+3V3",)) in kinds and ("power", ("VBUS",)) in kinds

    # DRC gained nothing; unconnected only dropped
    assert facts["drc_delta"]["new"] == {}
    assert facts["drc_delta"]["after"].get("unconnected_items", 0) < \
        facts["drc_delta"]["before"].get("unconnected_items", 0)

    # S5 diff-pair check passed on the routed pair (uncoupled <= 5 mm)
    assert facts["diffpair_check"] == "pass"
    dp = facts["diffpair_facts"][0]
    assert max(dp["uncoupled_p_mm"], dp["uncoupled_n_mm"]) <= 5.0
    assert dp["skew_mm"] <= 5.0

    # board really carries the copper, on the preferred outer layer
    bg = geom.BoardGeom.from_file(placed_board)
    for net in ("/USB_DP", "/USB_DM"):
        tks = bg.tracks_of(net=net)
        assert tks, f"{net} has no copper"
        assert {t.layer for t in tks} == {"F.Cu"}
    for f in facts["routed"]:
        if f["kind"] != "power":
            continue
        net, want = f["nets"][0], f["width_mm"]
        tks = bg.tracks_of(net=net)
        assert tks, f"{net} has no copper"
        # trunk at IPC width (taps may legitimately neck down at fine pads)
        assert max(t.width for t in tks) == pytest.approx(want, abs=1e-3)

    # the pull-up stub pad the KRT workaround detached is back on its net
    assert [list(s) for s in
            [tuple(x) for f in facts["routed"] if f["kind"] == "diff"
             for x in f["stub_pads_detached"]]] == [["R3", "/USB_DP"]]
    assert len(bg.pads_of(net="/USB_DP")) == 3


def _kp_board(d: Path, sense: bool) -> Path:
    """blinky2 (routed) plus an unrouted rail KP from L911 to C911 and the
    feedback divider top R911 below the rail's middle, where a tree router
    tees it in. sense: declare R911.1 -> C911.1 as the sense line (#1043)."""
    sys.path.insert(0, str(GOLDEN / "mutations"))
    import sense_tap_teed_off_rail as m
    text = (GOLDEN / "blinky2" / "blinky2.kicad_pcb").read_text(encoding="utf-8")
    at = text.index("\t(segment\n")
    parts = (m._fp("L911", "4.7uH", 122, 134, 1, [("1", 0, 2, "KP")])
             + m._fp("C911", "22u", 134, 134, 2, [("1", 0, 2, "KP")])
             + m._fp("R911", "100k", 127, 137.5, 3,
                     [("1", 0, 1, "KP"), ("2", 2, 1, "KFB")]))
    pcb = d / "blinky2.kicad_pcb"
    pcb.write_text(text[:at] + parts + text[at:], encoding="utf-8")
    shutil.copy2(GOLDEN / "blinky2" / "blinky2.kicad_pro", d)
    cons = json.loads((GOLDEN / "blinky2" / "constraints.json")
                      .read_text(encoding="utf-8"))
    cons["power"].append({"net": "KP", "current_a": 0.1, "pdn": False,
                          **({"sense_pads": SENSE_SPEC[0]["sense_pads"]}
                             if sense else {})})
    (d / "constraints.json").write_text(json.dumps(cons), encoding="utf-8")
    return pcb


@pytest.mark.smoke
@pytest.mark.parametrize("sense", [False, True])
def test_live_krt_rail_keeps_the_sense_pad_off_the_rail(sense, cli, krt_dir,
                                                        tmp_path):
    """#1043 planted fault: told nothing, KRT runs rail KP L911 -> R911.1 ->
    C911, so the sense line reads 7 mm short of C911.1 (sense_tap_off_pad).
    Given sense_pads, route_critical keeps R911.1 off the rail tree and lays
    its own line to C911.1, and check_kelvin finds it clean."""
    import check_kelvin
    pcb = _kp_board(tmp_path, sense)
    rep = tmp_path / "report.json"
    assert rc.main(["--pcb", str(pcb), "--only", "power",
                    "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text(encoding="utf-8"))
    assert r["status"] == "pass", r["violations"]
    vios, facts = check_kelvin.judge_sense(geom.load_board(pcb), SENSE_SPEC)
    if not sense:
        assert [v["kind"] for v in vios] == ["sense_tap_off_pad"]
        return
    assert vios == [] and facts[0]["reached"]
    lines = [f for f in r["facts"]["routed"] if f["kind"] == "sense"]
    assert [(f["sense"], f["tap"], f["reached"]) for f in lines] == \
        [("R911.1", "C911.1", True)]


def test_nets_in_new_errors_names_the_routed_nets_that_broke_drc():
    """lite-main (hifi-receiver-2): KRT laid /OUT_L_N across the TPA3118's
    pins; only the nets in errors whose type grew are named."""
    def v(check, sev, *msgs):
        return {"check": check, "severity": sev,
                "items": [{"msg": m} for m in msgs]}
    before = {"violations": [
        v("clearance", "error", "Track [/OLD] on F.Cu, length 1 mm",
          "Pad 1 [GND] of U1 on F.Cu")]}
    after = {"violations": before["violations"] + [
        v("shorting_items", "error", "Pad 28 [GND] of U8 on F.Cu",
          "Track [/OUT_L_N] on F.Cu, length 0.5657 mm"),
        v("shorting_items", "error", "Via [/OUT_R_P] on F.Cu - B.Cu",
          "Pad 1 [/BS_R_P] of C66 on F.Cu"),
        v("silk_overlap", "warning", "Track [+5V] on F.Cu, length 2 mm"),
        v("clearance", "error", "Track [+3V3] on F.Cu, length 1 mm",
          "Pad 2 [GND] of U2 on F.Cu")]}
    nets = {"/OUT_L_N", "/OUT_R_P", "+5V", "+3V3"}
    got = rc.nets_in_new_errors(before, after, nets)
    # clearance grew too (1 -> 2), so +3V3 counts; the warning does not
    assert got == {"/OUT_L_N", "/OUT_R_P", "+3V3"}
    # a net not routed in this run is never named
    assert rc.nets_in_new_errors(before, after, {"GND"}) == set()


def test_rip_new_copper_keeps_old_and_other_nets(tmp_path, monkeypatch):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("""(kicad_pcb (version 20260206) (generator "t")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "") (net 1 "A") (net 2 "B")
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1) (uuid "old-a"))
  (segment (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") (net 1) (uuid "new-a"))
  (segment (start 0 2) (end 1 2) (width 0.2) (layer "F.Cu") (net 2) (uuid "new-b"))
  (via (at 2 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "new-va"))
)
""")
    seen = []
    monkeypatch.setattr(rc.route_edit, "apply_ops",
                        lambda p, ops: seen.extend(ops))
    n = rc.rip_new_copper(pcb, {"old-a"}, {"A"})
    assert n == 2
    assert sorted(o["uuid"] for o in seen) == ["new-a", "new-va"]
    assert all(o["op"] == "remove" for o in seen)
    # haos-voice pass 6: a net that failed part-way has no fact but its
    # partial copper is new - named so the rollback can rip it
    assert rc._new_copper_nets(pcb, {"old-a"}) == {"A", "B"}
    assert rc._new_copper_nets(pcb, {"old-a", "new-b"}) == {"A"}


# ============================================================ pure: bus (#546)

def test_bus_is_opt_in():
    assert "bus" not in rc.plan_order(None, None)
    assert rc.plan_order("bus", None) == ["bus"]
    assert rc.plan_order(None, {"order": ["diff", "bus"]}) == ["diff", "bus"]


def test_bus_specs_from_length_groups():
    nets = {"/D0", "/D1", "/DM", "/DQS_P", "/DQS_N", "/A0", "/CS"}
    groups = {"length_groups": [
        {"name": "B0", "nets": ["D[0-1]", "DM"], "ref_nets": ["DQS_P", "DQS_N"]},
        {"name": "DQS", "nets": ["DQS_P", "DQS_N"]},
        {"name": "CMD", "nets": ["A0", "CS", "GONE"]},
        {"name": "CS_MATCH", "nets": ["CS"]},
    ]}
    got = rc.bus_specs(groups, nets, {"/DQS_P", "/DQS_N"})
    assert got == [{"name": "B0", "nets": ["/D0", "/D1", "/DM"]},
                   {"name": "CMD", "nets": ["/A0", "/CS"]}]
    assert rc.bus_specs({}, nets, set()) == []


def test_nets_starving_thermals_names_new_copper_next_to_the_pad(monkeypatch):
    """haos-voice pass 6: /OSPI0_D5 on B.Cu cut one of C342's two GND
    spokes; the error names only GND, so the near new copper is named."""
    from types import SimpleNamespace as NS
    from shapely.geometry import LineString, Point

    def trk(net, layer, a, b, uuid):
        return NS(net=net, layer=layer, uuid=uuid,
                  poly=LineString([a, b]).buffer(0.05))

    def via(net, at, uuid):
        return NS(net=net, uuid=uuid, poly=Point(at).buffer(0.127))

    tracks = [trk("/D5", "B.Cu", (65.55, 43.95), (66.95, 45.35), "n1"),
              trk("/TOP", "F.Cu", (65.1, 44.3), (65.1, 44.8), "n2"),
              trk("/FAR", "B.Cu", (70, 50), (71, 50), "n3"),
              trk("/OLD", "B.Cu", (65.1, 44.3), (65.1, 44.8), "o1")]
    vias = [via("/V", (65.6, 44.52), "n4")]
    pads = [NS(center=(65.1, 44.52), size=(0.6, 0.5))]
    bg = NS(tracks_of=lambda: tracks, vias_of=lambda: vias,
            pads_of=lambda: pads)
    monkeypatch.setattr(rc.geom.BoardGeom, "from_file",
                        staticmethod(lambda p: bg))
    starved = {"check": "starved_thermal", "severity": "error",
               "items": [{"msg": "Zone [GND] on B.Cu, priority 0",
                          "pos": [9.5, 9.5]},
                         {"msg": "Pad 2 [GND] of C342 on B.Cu",
                          "pos": [65.1, 44.52]}]}
    before = {"violations": []}
    after = {"violations": [starved]}
    nets = {"/D5", "/TOP", "/FAR", "/OLD", "/V"}
    got = rc.nets_starving_thermals(before, after, nets, Path("x"), {"o1"})
    # the B.Cu track and the via are near; /TOP is on another
    # layer, /FAR is far, /OLD's copper was there before the run
    assert got == {"/D5", "/V"}
    # no growth in starved_thermal: nothing named
    assert rc.nets_starving_thermals(after, after, nets, Path("x"),
                                     {"o1"}) == set()


# ============================================================ #1043 sense lines

def _sense_board(tmp_path, name="sense", extra=""):
    """A rail KP from L911 to its output capacitor C911 on a 1 mm track, and
    R911's sense pad (unrouted) above it. A GND track walls the way from
    R911.1 to C911.1 down to the rail's edge: the short way runs on the
    rail's own copper, the long way goes round the wall's far end."""
    body = _fp("L911", 10, 20, pads=_pad("1", 0, 0, "KP", size=2))
    body += _fp("C911", 22, 20, pads=_pad("1", 0, 0, "KP", size=2))
    body += _fp("R911", 15, 17.5, pads=_pad("1", 0, 0, "KP", size=1)
                + _pad("2", -2, 0, "KFB", size=1))
    body += _seg(10, 20, 22, 20, "KP", "a1", w=1.0)
    body += _seg(17.5, 12, 17.5, 19.2, "GND", "c1") + extra
    return _pcb(tmp_path, name, body)


SENSE_SPEC = [{"net": "KP", "sense_pads": {"R911.1": "C911.1"}}]


def _lay(pcb, ops):
    """Write route_edit add_track ops into the board text as segments."""
    text = pcb.read_text(encoding="utf-8")
    segs = "".join(_seg(*o["start"], *o["end"], o["net"], f"e{k:x}",
                        w=o["width"])
                   for k, o in enumerate(ops) if o["op"] == "add_track")
    assert all(o["op"] == "add_track" for o in ops)
    pcb.write_text(text.rstrip()[:-1] + segs + ")\n", encoding="utf-8")
    return geom.BoardGeom.from_file(pcb)


def test_power_specs_carry_sense_pads():
    specs = rc.power_specs({"power": [
        {"net": "KP", "current_a": 0.1,
         "sense_pads": {"R911.1": "C911.1"}},
        {"net": "KQ", "current_a": 0.1}]})
    by = {s["net"]: s for s in specs}
    assert by["KP"]["sense_pads"] == {"R911.1": "C911.1"}
    assert by["KQ"]["sense_pads"] == {}
    assert rc.sense_pairs(specs) == [("KP", "R911.1", "C911.1")]


def test_sense_pad_targets_skip_bad_names_and_routed_pads(tmp_path):
    bg = geom.BoardGeom.from_file(_sense_board(tmp_path))
    assert rc.sense_pad_targets(bg, SENSE_SPEC) == [("R911", "KP", "1")]
    bad = [{"net": "KP", "sense_pads": {"R911.2": "C911.1",   # not on KP
                                        "R999.1": "C911.1",   # no such pad
                                        "R911.1": "R911.1"}}]  # same pad
    assert rc.sense_pad_targets(bg, bad) == []
    done = geom.BoardGeom.from_file(_sense_board(
        tmp_path, "done", _seg(15, 17.5, 15, 15, "KP", "b1")))
    assert rc.sense_pad_targets(done, SENSE_SPEC) == []


def test_sense_line_is_walled_off_its_rail(tmp_path):
    """#1043 planted fault: routed with its rail as own-net copper, the sense
    line from R911.1 takes the short way on the rail track, short of C911.1 (the voice
    board's R113/R201, read 6-16 mm off their pads). With fence_rail the same
    route lands on C911.1 on copper of its own."""
    import check_kelvin
    pcb = _sense_board(tmp_path)
    bg = geom.BoardGeom.from_file(pcb)
    sense = check_kelvin._pad_named(bg, "R911.1")
    tap = check_kelvin._pad_named(bg, "C911.1")

    bare = rc.mazeroute.Router(bg, layers=["F.Cu"])
    teed = _lay(_sense_board(tmp_path, "teed"),
                bare.route("KP", "R911.1", "C911.1"))
    w = check_kelvin.sense_walk(teed, check_kelvin._pad_named(teed, "R911.1"),
                                check_kelvin._pad_named(teed, "C911.1"))
    assert w["joins"], "the unfenced route should tee off the rail"

    router = rc.mazeroute.Router(bg, layers=["F.Cu"])
    assert rc.fence_rail(router, "KP", sense, tap) == 2   # L911.1 + track
    assert {it.net for it in router.obs.items if it.ref in ("L911",)} \
        == {rc.SENSE_FENCE}
    ops = router.route("KP", "R911.1", "C911.1")
    laid = _lay(pcb, ops)
    w = check_kelvin.sense_walk(laid, check_kelvin._pad_named(laid, "R911.1"),
                                check_kelvin._pad_named(laid, "C911.1"))
    assert w == {"reached": True, "joins": []}
    facts = [{"kind": "sense", "nets": ["KP"], "sense": "R911.1",
              "tap": "C911.1"}]
    assert rc.sense_verdicts(laid, facts) == []
    assert facts[0]["joins"] == 0 and facts[0]["reached"] is True


def test_sense_verdicts_flag_a_teed_line(tmp_path):
    """Planted fault: a sense line that tees into the rail track 7 mm short
    of its tap, or stops short of it, is sense_route_off_pad."""
    teed = geom.BoardGeom.from_file(_sense_board(
        tmp_path, "teed", _seg(15, 17.5, 15, 20, "KP", "b1")))
    facts = [{"kind": "sense", "nets": ["KP"], "sense": "R911.1",
              "tap": "C911.1"}]
    v = rc.sense_verdicts(teed, facts)
    assert [x["kind"] for x in v] == ["sense_route_off_pad"]
    assert v[0]["refs"] == ["C911", "R911"] and v[0]["joins"] == 1
    short = geom.BoardGeom.from_file(_sense_board(
        tmp_path, "short", _seg(15, 17.5, 15, 15, "KP", "b1")))
    v = rc.sense_verdicts(short, facts)
    assert [x["kind"] for x in v] == ["sense_route_off_pad"]
    assert "never reaches" in v[0]["msg"]
    # pre-routed lines are check_kelvin's to judge, not route_critical's
    assert rc.sense_verdicts(teed, [{**facts[0], "pre_routed": True}]) == []


def test_sense_keepout_covers_the_line_not_the_tap(tmp_path):
    from shapely.geometry import Point, Polygon
    import check_kelvin
    pcb = _sense_board(tmp_path, "keep", _seg(15, 17.5, 22, 17.5, "KP", "n1")
                       + _seg(22, 17.5, 22, 20, "KP", "n2"))
    bg = geom.BoardGeom.from_file(pcb)
    sense = check_kelvin._pad_named(bg, "R911.1")
    tap = check_kelvin._pad_named(bg, "C911.1")
    areas = rc.sense_keepout(bg, "KP", {"n1", "n2"}, sense, tap, 0.2)
    assert areas and {a["layers"][0] for a in areas} == {"F.Cu"}
    assert all(a["name"].startswith("aiee_sense_R911_1_") and a["pours"]
               and not a["tracks"] and not a["vias"] for a in areas)
    shape = Polygon(areas[0]["poly"]) if len(areas) == 1 else None
    assert shape is not None
    assert shape.contains(Point(15, 17.5))        # the sense pad
    assert shape.contains(Point(16, 17.5))        # the line
    assert shape.contains(Point(16, 17.65))       # its clearance
    assert not shape.intersects(tap.poly)         # the tap stays poured
    assert not shape.contains(Point(16, 20))      # the rail is not walled
    # the old rail track is not the line's
    assert rc.sense_keepout(bg, "KP", set(), sense, tap, 0.2)[0]["poly"] \
        != areas[0]["poly"]


def test_normalize_leaves_the_sense_pad_fenced(tmp_path, monkeypatch):
    """#1043: a narrow rail segment beside the sense pad is not widened onto
    it - the pad is detached while the rail routes, so without the fence the
    widen would see free space there."""
    seen = []
    monkeypatch.setattr(rc.route_edit, "apply_ops",
                        lambda path, ops: seen.extend(ops))
    body = _fp("R911", 12, 20.6, pads=_pad("1", 0, 0, "KP"))
    body += _seg(10, 20, 14, 20, "KP", "b1")
    pcb = _pcb(tmp_path, "fence", body)
    specs = [{"net": "KP", "ipc_min_mm": 1.0}]
    r = rc.normalize_power_widths(pcb, specs, clearance=0.1)
    assert r["widened"] == 1 and r["blocked"] == []
    seen.clear()
    r = rc.normalize_power_widths(pcb, specs, clearance=0.1,
                                  fence={("R911", "1")})
    assert r["widened"] == 0
    assert [b["start"] for b in r["blocked"]] == [[10.0, 20.0]]
