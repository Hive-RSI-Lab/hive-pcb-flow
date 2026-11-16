"""S12 acceptance tests: fab outputs, DFM, ordering.

Plan S12 accept criteria:
  - dfm_check.py catches the silk and rotation mutants
        -> test_dfm_catches_silk_over_pad, test_dfm_catches_cpl_rotation
           (the rotation catch is HERMETIC: the committed s7_regen netlist is
            the polarity oracle, so no toolchain is needed to pin V9)
  - zero false positives on the goldens
        -> test_dfm_goldens_clean, test_dfm_negative_control_mutants
  - golden board 2 package uploads clean to JLCPCB's web viewer
        -> package completeness/structure is machine-tested here
           (test_full_package_flow); the actual upload is a human step,
           recorded in PROGRESS.md, since JLC has no public viewer API.
  - CPL rotations spot-checked against JLC's rendered preview
        -> test_cpl_rotation_corrections pins the table-driven maths for the
           polarized packages; the visual preview is the same human step.

Pure tests need no toolchain (committed boards/netlists + synthetic gerbers
written by hand). Tests that export gerbers via kicad-cli are `smoke`.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
REFERENCE = REPO / "reference"
GOLDEN = REPO / "tests" / "golden"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import bom_cpl  # noqa: E402
import checklib  # noqa: E402
import dfm_check  # noqa: E402
import fab_export  # noqa: E402
import fabhash  # noqa: E402
import geom  # noqa: E402
import gerblib  # noqa: E402
import netlist_audit  # noqa: E402
import order_quote  # noqa: E402
import order_submit  # noqa: E402
from _boards import BOARDS as BOARDS_ROOT, need_board  # noqa: E402
from _boards import board_path as real_board_path  # noqa: E402

MANIFEST = yaml.safe_load((GOLDEN / "manifest.yaml").read_text(encoding="utf-8"))
BOARDS = list(MANIFEST["golden_boards"])
GATES_YAML = REFERENCE / "gates.yaml"
BLINKY_NET = REPO / "tests" / "s7_regen" / "blinky2" / "golden.net"
STAGES = REPO / "tests" / "fixtures" / "stages"
PD_PCB = STAGES / "pd_trigger" / "route" / "pd-trigger.kicad_pcb"
PD_GERBERS = STAGES / "pd_trigger" / "fab" / "gerbers"
PD_NET = STAGES / "pd_trigger" / "pd-trigger.net"


def board_path(name: str) -> Path:
    return GOLDEN / name / f"{name}.kicad_pcb"


def mutant_path(name: str, board: str) -> Path:
    return GOLDEN / "mutants" / name / f"{board}.kicad_pcb"


# ------------------------------------------------------- synthetic gerbers

def _fmt(v: float) -> str:
    """mm -> 4.6 fixed-point gerber coordinate."""
    return str(int(round(v * 1e6)))


def write_gerber(path: Path, traces=(), flashes=(), arcs=()) -> None:
    """Minimal RS-274X. traces: [(w, x1, y1, x2, y2)]; flashes: [(dia, x, y)];
    arcs: [(w, x1, y1, x2, y2, i, j, ccw)] with i/j = center offset from the
    start point (G75 multi-quadrant, signed).
    Gerber Y points up, so callers pass gerber-space coordinates."""
    out = ["%FSLAX46Y46*%", "%MOMM*%"]
    ap = 10
    body = []
    for w, x1, y1, x2, y2 in traces:
        out.append(f"%ADD{ap}C,{w:.4f}*%")
        body += [f"D{ap}*", f"X{_fmt(x1)}Y{_fmt(y1)}D02*",
                 f"X{_fmt(x2)}Y{_fmt(y2)}D01*"]
        ap += 1
    for w, x1, y1, x2, y2, i, j, ccw in arcs:
        out.append(f"%ADD{ap}C,{w:.4f}*%")
        body += [f"D{ap}*", "G75*", f"X{_fmt(x1)}Y{_fmt(y1)}D02*",
                 f"{'G03' if ccw else 'G02'}*",
                 f"X{_fmt(x2)}Y{_fmt(y2)}I{_fmt(i)}J{_fmt(j)}D01*",
                 "G01*"]
        ap += 1
    for dia, x, y in flashes:
        out.append(f"%ADD{ap}C,{dia:.4f}*%")
        body += [f"D{ap}*", f"X{_fmt(x)}Y{_fmt(y)}D03*"]
        ap += 1
    out += body + ["M02*"]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_drill(path: Path, holes=()) -> None:
    """Minimal Excellon. holes: [(dia, x, y)] in mm, gerber space."""
    lines = ["M48", "METRIC,TZ"]
    tools = sorted({d for d, _, _ in holes})
    for i, d in enumerate(tools, start=1):
        lines.append(f"T{i}C{d:.3f}")
    lines += ["%", "G90", "G05"]
    for i, d in enumerate(tools, start=1):
        lines.append(f"T{i}")
        for hd, x, y in holes:
            if hd == d:
                lines.append(f"X{x:.3f}Y{y:.3f}")
    lines.append("M30")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def synth_fab(tmp_path: Path, *, traces=(), flashes=(), holes=(),
              silk=(), mask=(), paste=(),
              outline_mm=(0.0, 0.0, 20.0, 20.0)) -> object:
    """Build a minimal 2-layer fab dir and open it with gerblib."""
    d = tmp_path / "synth"
    d.mkdir(exist_ok=True)
    write_gerber(d / "synth-F_Cu.gtl", traces=traces, flashes=flashes)
    write_gerber(d / "synth-B_Cu.gbl")
    write_gerber(d / "synth-F_Silkscreen.gto", traces=silk)
    write_gerber(d / "synth-B_Silkscreen.gbo")
    write_gerber(d / "synth-F_Mask.gts", flashes=mask)
    write_gerber(d / "synth-B_Mask.gbs")
    write_gerber(d / "synth-F_Paste.gtp", flashes=paste)
    write_gerber(d / "synth-B_Paste.gbp")
    x0, y0, x1, y1 = outline_mm
    write_gerber(d / "synth-Edge_Cuts.gm1", traces=[
        (0.1, x0, y0, x1, y0), (0.1, x1, y0, x1, y1),
        (0.1, x1, y1, x0, y1), (0.1, x0, y1, x0, y0)])
    write_drill(d / "synth.drl", holes=holes)
    return gerblib.open_fab(d)


RULES_2L = yaml.safe_load(
    (REFERENCE / "jlc_capabilities.yaml").read_text(encoding="utf-8")
)["design_rules"]["2layer_1oz"]


# =========================================================== pure: rotations

def test_rotations_table_loads_in_order():
    rules = bom_cpl.load_rotations(REFERENCE / "jlc_rotations.csv")
    assert len(rules) > 20
    pats = [p.pattern for p, _ in rules]
    # more specific SOT-323 must precede the broader SOT-23 family entry only
    # if both exist; the contract under test is that order is preserved.
    assert pats == [p.pattern for p, _ in
                    bom_cpl.load_rotations(REFERENCE / "jlc_rotations.csv")]


def test_cpl_rotation_corrections():
    """The table-driven maths for polarized/pin-1-sensitive packages."""
    rules = bom_cpl.load_rotations(REFERENCE / "jlc_rotations.csv")
    # (footprint, kicad rotation) -> (jlc rotation, correction)
    cases = [
        ("LED_0805_2012Metric", 180.0, 0.0, 180.0),
        ("LED_0805_2012Metric", 0.0, 180.0, 180.0),
        ("LQFP-48_7x7mm_P0.5mm", 0.0, 270.0, 270.0),
        ("SOT-23", 90.0, 270.0, 180.0),
        ("D_SOD-123", 0.0, 180.0, 180.0),
        ("C_0603_1608Metric", 90.0, 90.0, 0.0),      # unmatched -> unchanged
    ]
    for fp, base, want_final, want_corr in cases:
        final, corr, _ = bom_cpl.correct_rotation(fp, base, rules)
        assert final == pytest.approx(want_final), fp
        assert corr == pytest.approx(want_corr), fp


def test_correct_rotation_strips_library_prefix():
    rules = bom_cpl.load_rotations(REFERENCE / "jlc_rotations.csv")
    final, corr, pat = bom_cpl.correct_rotation(
        "LED_SMD:LED_0805_2012Metric", 0.0, rules)
    assert (final, corr) == (180.0, 180.0) and pat == "^LED_0805"


def test_easyeda_imported_sot_gets_no_kicad_offset():
    """#971: pd-trigger's CPL went out with D2 (SOT-23) at 180 and Q1
    (SOT-363) at 0 because ^SOT-23 / ^SOT-363 +180 - KiCad-library offsets -
    matched these EasyEDA-imported names. Their pads already sit in the
    LCSC model's frame, so the table must leave them alone."""
    rules = bom_cpl.load_rotations(REFERENCE / "jlc_rotations.csv")
    for fp, base in (("aiee:SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR", 0.0),
                     ("aiee:SOT-363-6_L2.2-W1.3-P0.65-LS2.1-BL", 180.0)):
        final, corr, _ = bom_cpl.correct_rotation(fp, base, rules)
        assert (final, corr) == (base, 0.0), fp


def test_rotation_wraps_mod_360():
    rules = [(__import__("re").compile("^X"), 270.0)]
    final, _, _ = bom_cpl.correct_rotation("XPART", 180.0, rules)
    assert final == pytest.approx(90.0)


# ============================================================ pure: BOM/CPL

POS_CSV = (
    "Ref,Val,Package,PosX,PosY,Rot,Side\n"
    '"C1","100nF","C_0603",1.0,-2.0,0.0,top\n'
    '"C2","100nF","C_0603",3.0,-4.0,90.0,top\n'
    '"D1","LED_red","LED_0805",5.0,-6.0,180.0,bottom\n'
)


def test_parse_pos_csv():
    rows = bom_cpl.parse_pos_csv(POS_CSV)
    assert [r["ref"] for r in rows] == ["C1", "C2", "D1"]
    assert rows[2]["side"] == "bottom" and rows[2]["rot"] == 180.0
    assert rows[0]["x"] == 1.0 and rows[0]["y"] == -2.0


def test_build_bom_groups_by_value_footprint_lcsc():
    rows = bom_cpl.parse_pos_csv(POS_CSV)
    bom = bom_cpl.build_bom(rows, {"C1": {"lcsc": "C1525"},
                                   "C2": {"lcsc": "C1525"}})
    caps = [r for r in bom if r["Comment"] == "100nF"]
    assert len(caps) == 1 and caps[0]["Designator"] == "C1,C2"
    assert caps[0]["LCSC"] == "C1525"
    led = [r for r in bom if r["Comment"] == "LED_red"][0]
    assert led["LCSC"] == ""          # no LCSC -> empty, reported separately


def test_build_bom_splits_when_lcsc_differs():
    rows = bom_cpl.parse_pos_csv(POS_CSV)
    bom = bom_cpl.build_bom(rows, {"C1": {"lcsc": "C1"}, "C2": {"lcsc": "C2"}})
    assert len([r for r in bom if r["Comment"] == "100nF"]) == 2


def test_build_cpl_layer_and_rotation():
    rows = bom_cpl.parse_pos_csv(POS_CSV)
    rules = bom_cpl.load_rotations(REFERENCE / "jlc_rotations.csv")
    cpl, audit = bom_cpl.build_cpl(rows, rules)
    by_ref = {r["Designator"]: r for r in cpl}
    assert by_ref["D1"]["Layer"] == "Bottom"
    assert by_ref["C1"]["Layer"] == "Top"
    # bottom: 180 - kicad 180 = 0, then LED_0805's 180 correction
    assert float(by_ref["D1"]["Rotation"]) == pytest.approx(180.0)
    assert by_ref["C1"]["Mid X"] == "1.0000"
    assert {a["ref"] for a in audit} == {"C1", "C2", "D1"}


@pytest.mark.parametrize("payload", [
    {"parts": [{"ref": "U1", "lcsc": "C123"}]},
    [{"refs": ["U1"], "lcsc": "C123"}],
    {"U1": "C123"},
    {"U1": {"lcsc": "C123"}},
])
def test_load_parts_map_shapes(tmp_path, payload):
    p = tmp_path / "parts.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert bom_cpl.load_parts_map(p)["U1"]["lcsc"] == "C123"


# ====================================================== pure: capabilities

def test_rule_key_and_pick():
    caps = dfm_check.load_capabilities(REFERENCE / "jlc_capabilities.yaml")
    assert dfm_check.rule_key(4, 1.0) == "4layer_1oz"
    key, rules = dfm_check.pick_rules(caps, 2, 1.0)
    assert key == "2layer_1oz" and rules["min_trace_width_mm"] == 0.127


def test_pick_rules_unknown_raises():
    caps = dfm_check.load_capabilities(REFERENCE / "jlc_capabilities.yaml")
    with pytest.raises(checklib.CheckError):
        dfm_check.pick_rules(caps, 34, 1.0)  # past JLC's 32


def test_below_tolerance_absorbs_tessellation_but_not_defects():
    # a value exactly at the limit must pass despite micron-scale flattening
    assert not dfm_check._below(0.1498, 0.15)
    assert not dfm_check._below(0.15, 0.15)
    # a real shortfall still fails
    assert dfm_check._below(0.12, 0.15)
    assert dfm_check._below(0.1016, 0.127)


# ========================================================== pure: gerblib

def test_gerblib_classifies_layers(tmp_path):
    fab = synth_fab(tmp_path)
    assert fab.copper_layer_names() == ["F.Cu", "B.Cu"]
    assert set(fab.silk_files) == {"F", "B"}
    assert set(fab.mask_files) == {"F", "B"}
    assert fab.edge_file is not None and len(fab.drill_files) == 1


def test_gerblib_copper_layer_physical_order():
    names = {"B.Cu": 1, "In2.Cu": 2, "F.Cu": 3, "In1.Cu": 4}

    class Fake(gerblib.FabStack):
        def __init__(self):
            self.copper_files = names
    assert Fake().copper_layer_names() == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_gerblib_outline_and_board_space(tmp_path):
    fab = synth_fab(tmp_path, outline_mm=(0.0, -10.0, 20.0, 0.0))
    # gerber y in [-10, 0] -> board y in [0, 10] (gerblib negates y)
    assert fab.outline.bounds == pytest.approx((0.0, 0.0, 20.0, 10.0), abs=0.06)


def test_gerblib_reads_holes_in_board_space(tmp_path):
    fab = synth_fab(tmp_path, holes=[(0.3, 5.0, -5.0)])
    assert len(fab.holes) == 1
    h = fab.holes[0]
    assert (h.x, h.y) == pytest.approx((5.0, 5.0))
    assert h.diameter == pytest.approx(0.3)


def test_gerblib_tessellates_arcs_finely(tmp_path):
    """A round pad must not be read as a coarse polygon: the inscribed radius
    drives the annular-ring measurement (the bug that made blinky2's exactly-
    at-spec 0.6 mm vias look 0.025 mm short)."""
    fab = synth_fab(tmp_path, flashes=[(0.6, 5.0, -5.0)])
    pad = fab.copper("F.Cu").pads[0]
    from shapely.geometry import Point
    inscribed = pad.exterior.distance(Point(5.0, 5.0))
    assert inscribed == pytest.approx(0.3, abs=gerblib.ARC_MAX_ERROR_MM)


# =============================================== pure: seeded gerber defects

def test_dfm_flags_narrow_trace(tmp_path):
    fab = synth_fab(tmp_path, traces=[(0.08, 2.0, -2.0, 8.0, -2.0)])
    vios: list = []
    dfm_check.check_trace_width(fab, RULES_2L, vios)
    assert len(vios) == 1
    v = vios[0]
    assert v["kind"] == "dfm_trace_width" and v["severity"] == "error"
    assert v["width_mm"] == pytest.approx(0.08)
    assert v["pos"] == pytest.approx([5.0, 2.0], abs=1e-3)


def test_dfm_accepts_trace_at_the_limit(tmp_path):
    fab = synth_fab(tmp_path, traces=[(0.127, 2.0, -2.0, 8.0, -2.0)])
    vios: list = []
    dfm_check.check_trace_width(fab, RULES_2L, vios)
    assert vios == []


def test_dfm_flags_tight_clearance(tmp_path):
    # two parallel 0.2 mm traces whose copper edges are 0.05 mm apart
    fab = synth_fab(tmp_path, traces=[(0.2, 2.0, -2.0, 8.0, -2.0),
                                      (0.2, 2.0, -2.25, 8.0, -2.25)])
    vios: list = []
    dfm_check.check_clearance(fab, RULES_2L, vios)
    assert [v["kind"] for v in vios] == ["dfm_clearance"]
    assert vios[0]["clearance_mm"] == pytest.approx(0.05, abs=1e-3)


def test_dfm_ignores_touching_copper(tmp_path):
    """Connected copper is one conductor, not a zero-clearance violation."""
    fab = synth_fab(tmp_path, traces=[(0.3, 2.0, -2.0, 8.0, -2.0),
                                      (0.3, 8.0, -2.0, 8.0, -8.0)])
    vios: list = []
    dfm_check.check_clearance(fab, RULES_2L, vios)
    assert vios == []


def test_dfm_round_caps_join_overlapping_junctions(tmp_path):
    """S14: KiCad emits circular apertures, so trace ends are ROUND. Two
    same-net segments whose endpoints sit 0.05mm apart with 0.1mm cap radius
    OVERLAP - flat-capped buffering split them into phantom islands with a
    fake sub-minimum gap (two false dfm_clearance errors on a KiCad-clean
    board)."""
    fab = synth_fab(tmp_path, traces=[(0.2, 2.0, -2.0, 5.0, -2.0),
                                      (0.2, 5.05, -2.0, 8.0, -2.0)])
    vios: list = []
    dfm_check.check_clearance(fab, RULES_2L, vios)
    assert vios == []  # round caps overlap: one conductor, no phantom gap


def test_dfm_round_caps_expose_trace_end_at_edge(tmp_path):
    """S14 (false-negative direction): a trace END'S round cap reaches w/2
    closer to the board edge than the flat-capped model claimed."""
    # end at x=0.30 with w=0.3 -> round cap reaches x=0.15; JLC edge min 0.3
    fab = synth_fab(tmp_path, traces=[(0.3, 0.30, -5.0, 8.0, -5.0)],
                    outline_mm=(0.0, -20.0, 20.0, 0.0))
    vios: list = []
    dfm_check.check_copper_to_edge(fab, RULES_2L, vios)
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]


def test_dfm_flags_small_drill(tmp_path):
    fab = synth_fab(tmp_path, holes=[(0.12, 5.0, -5.0)])
    vios: list = []
    dfm_check.check_holes(fab, RULES_2L, vios)
    kinds = [v["kind"] for v in vios]
    assert "dfm_hole_size" in kinds
    v = [x for x in vios if x["kind"] == "dfm_hole_size"][0]
    assert v["diameter_mm"] == pytest.approx(0.12)


def test_drill_plating_from_kicad_comments(tmp_path):
    """gerbonara reads KiCad's X2 plating comments as None, so every NPTH
    came back plated; gerblib now reads them (per tool, else per file)."""
    mixed = ("M48\n; #@! TF.FileFunction,MixedPlating,1,2\nMETRIC\n"
             "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n"
             "; #@! TA.AperFunction,NonPlated,NPTH,ComponentDrill\n"
             "T2C0.650\n%\nG90\nG05\nT1\nX1.0Y-1.0\nT2\nX3.0Y-1.0\nM30\n")
    p = tmp_path / "m.drl"
    p.write_text(mixed)
    assert [(h.diameter, h.plated) for h in gerblib.read_drill(p)] == \
        [(0.3, True), (0.65, False)]
    npth = ("M48\n; #@! TF.FileFunction,NonPlated,1,2,NPTH\nMETRIC\n"
            "T1C3.200\n%\nG90\nG05\nT1\nX1.0Y-1.0\nM30\n")
    p.write_text(npth)
    assert [h.plated for h in gerblib.read_drill(p)] == [False]


def test_dfm_flags_small_npth():
    """#326: JLC's non-plated holes start at 0.5 mm; a 0.4 mm plated hole
    is fine, the same NPTH is not."""
    from types import SimpleNamespace as NS
    from shapely.geometry import Polygon
    fab = NS(holes=[gerblib.Hole(5, 5, 0.4, plated=True),
                    gerblib.Hole(9, 5, 0.4, plated=False),
                    gerblib.Hole(13, 5, 0.5, plated=False)],
             outline=Polygon())
    vios: list = []
    dfm_check.check_holes(fab, RULES_2L, vios)
    assert [(v["kind"], v.get("plated")) for v in vios] == \
        [("dfm_hole_size", False)]


def test_dfm_flags_hole_to_hole(tmp_path):
    fab = synth_fab(tmp_path, holes=[(0.3, 5.0, -5.0), (0.3, 5.4, -5.0)])
    vios: list = []
    dfm_check.check_holes(fab, RULES_2L, vios)
    assert "dfm_hole_to_hole" in [v["kind"] for v in vios]


def _via_bg(vias):
    from types import SimpleNamespace as NS
    vs = [NS(at=(x, y), diameter=di, drill=dr, net="N", kind="through")
          for di, dr, x, y in vias]
    return NS(vias_of=lambda net=None, layer=None: vs)


def _kinds_sev(vios):
    return sorted((v["kind"], v["severity"]) for v in vios)


def test_jlc_rows_keep_absolute_floors_under_design_floors():
    """#554: abs_min_<key> is JLC's absolute floor, <key> what a design is
    held to; the absolute one is never the stricter."""
    caps = yaml.safe_load((REFERENCE / "jlc_capabilities.yaml").read_text())
    for key, row in caps["design_rules"].items():
        for k, v in row.items():
            if k.startswith("abs_"):
                assert v <= row[k[4:]], (key, k)
    assert RULES_4L["abs_min_annular_ring_mm"] == 0.05
    assert RULES_4L["abs_min_copper_to_edge_mm"] == 0.2
    assert RULES_4L["abs_min_via_hole_to_hole_mm"] == 0.2
    assert RULES_4L["abs_min_hole_to_hole_mm"] == 0.45


def test_dfm_hole_to_hole_split_via_and_pad(tmp_path):
    """#554: JLC's 'via hole-to-hole 0.2, pad hole-to-hole 0.45'. Two board
    vias 0.3 apart are a warning (under the 0.5 a design keeps), the same
    holes as pad holes an error; vias 0.15 apart are an error."""
    holes = [(0.2, 5.0, -5.0), (0.2, 5.5, -5.0),       # gap 0.3
             (0.2, 10.0, -5.0), (0.2, 10.35, -5.0)]    # gap 0.15
    fab = synth_fab(tmp_path, holes=holes)
    vios: list = []
    dfm_check.check_holes(fab, RULES_4L, vios, bg=_via_bg(
        [(0.45, h.diameter, h.x, h.y) for h in fab.holes]))
    assert _kinds_sev(vios) == [("dfm_hole_to_hole", "error"),
                                ("dfm_via_hole_to_hole_preferred", "warning")]
    assert [v["holes"] for v in vios if v["severity"] == "error"] == ["via"]
    vios = []
    dfm_check.check_holes(fab, RULES_4L, vios)          # no vias: pad holes
    assert _kinds_sev(vios) == [("dfm_hole_to_hole", "error")] * 2
    # a castellated hole beside a plated one: the castellated figure
    vios = []
    dfm_check.check_holes(fab, RULES_4L, vios, cast=[(5.0, 5.0, 0.2)],
                          cast_hh=0.6)
    assert sorted(v.get("holes") for v in vios) == ["castellated", "pad"]


def test_castellated_hole_band(tmp_path):
    """A castellated hole beside a plated one: under JLC's capabilities page
    (0.5) fails, between it and the blog's 0.6 warns."""
    def run(gap):
        fab = synth_fab(tmp_path, holes=[(0.6, 5.0, -5.0), (0.6, 5.0 + 0.6 + gap, -5.0)])
        vios = []
        h = fab.holes[0]
        dfm_check.check_holes(fab, RULES_4L, vios, cast=[(h.x, h.y, 0.3)],
                              cast_hh=0.6, cast_abs=0.5)
        return [(v["kind"], v["severity"]) for v in vios if "hole_to_hole" in v["kind"]]
    assert run(0.545) == [("dfm_hole_to_hole_preferred", "warning")]
    assert run(0.45) == [("dfm_hole_to_hole", "error")]
    assert run(0.7) == []


def test_dfm_copper_to_edge_band(tmp_path):
    """#554: JLC's routed edge is 0.2; 0.2-0.3 warns once, under 0.2 fails."""
    fab = synth_fab(tmp_path, traces=[(0.2, 0.35, -5.0, 8.0, -5.0),
                                      (0.2, 0.35, -15.0, 8.0, -15.0)],
                    outline_mm=(0.0, -20.0, 20.0, 0.0))
    vios: list = []
    dfm_check.check_copper_to_edge(fab, RULES_4L, vios)
    assert _kinds_sev(vios) == [("dfm_copper_to_edge_preferred", "warning")]
    assert vios[0]["count"] == 2 and len(vios[0]["items"]) == 2
    vios = []
    dfm_check.check_copper_to_edge(fab, RULES_2L | {
        "abs_min_copper_to_edge_mm": 0.3}, vios)
    assert _kinds_sev(vios) == [("dfm_copper_to_edge", "error")] * 2


def test_dfm_via_ring_band_and_cost_tier(tmp_path):
    """#554: a 0.352/0.2 via (SparkFun's, ring 0.076) is JLC-buildable:
    one ring warning and one cost-tier warning, no error; a 0.28/0.2 via
    (ring 0.04) fails the 0.05 ring."""
    vias = [(0.352, 0.2, 5.0, -5.0), (0.352, 0.2, 8.0, -5.0),
            (0.28, 0.2, 11.0, -5.0)]
    fab = synth_fab(tmp_path, flashes=[(d, x, y) for d, _, x, y in vias],
                    holes=[(r, x, y) for _, r, x, y in vias])
    vios: list = []
    dfm_check.check_annular_ring(fab, RULES_4L, vios, bg=_via_bg(
        [(0.0, h.diameter, h.x, h.y) for h in fab.holes]))
    assert _kinds_sev(vios) == [("dfm_annular_ring", "error"),
                                ("dfm_annular_ring_preferred", "warning")]
    assert [v["count"] for v in vios if v["severity"] == "warning"] == [2]
    vios = []
    dfm_check.check_via_sizes(RULES_4L, vios, _via_bg(vias + [
        (0.45, 0.2, 1, 1), (0.3, 0.15, 2, 2), (0.2, 0.1, 3, 3),
        (2.54, 0.0, 4, 4)]))
    got = sorted((v["kind"], v["severity"], v.get("count")) for v in vios)
    assert got == [("dfm_via_cost_tier", "warning", 1),     # 0.28/0.2
                   ("dfm_via_cost_tier", "warning", 1),     # 0.3/0.15
                   ("dfm_via_cost_tier", "warning", 2),     # 0.352/0.2
                   ("dfm_via_size", "error", None)]         # 0.2/0.1
    # 2 layers: a 0.5/0.25 via is under the 0.6/0.3 design via but at JLC's
    # standard price; a fab row with no absolute floors skips the check
    vios = []
    dfm_check.check_via_sizes(RULES_2L, vios, _via_bg([(0.5, 0.25, 1, 1)]))
    assert _kinds_sev(vios) == [("dfm_via_size_preferred", "warning")]
    vios = []
    dfm_check.check_via_sizes({"min_via_drill_mm": 0.3}, vios,
                              _via_bg([(0.2, 0.1, 1, 1)]))
    assert vios == []


def test_dfm_flags_thin_annular_ring(tmp_path):
    # 0.4 mm pad on a 0.3 mm drill -> 0.05 mm ring (< 0.15 minimum)
    fab = synth_fab(tmp_path, flashes=[(0.4, 5.0, -5.0)],
                    holes=[(0.3, 5.0, -5.0)])
    vios: list = []
    dfm_check.check_annular_ring(fab, RULES_2L, vios)
    assert [v["kind"] for v in vios] == ["dfm_annular_ring"]
    assert vios[0]["ring_mm"] == pytest.approx(0.05, abs=5e-3)


class _ViaBoard:
    """A board stand-in whose vias sit on the fab's holes."""
    def __init__(self, fab):
        from types import SimpleNamespace as NS
        self._v = [NS(at=(h.x, h.y), drill=h.diameter) for h in fab.holes]

    def vias_of(self, net=None, layer=None):
        return self._v


def test_dfm_accepts_annular_ring_at_the_limit(tmp_path):
    fab = synth_fab(tmp_path, flashes=[(0.6, 5.0, -5.0)],
                    holes=[(0.3, 5.0, -5.0)])
    vios: list = []
    dfm_check.check_annular_ring(fab, RULES_2L, vios, bg=_ViaBoard(fab))
    assert vios == []


def test_dfm_pth_ring_is_held_apart_from_the_via_ring(tmp_path):
    """#326: JLC's PTH component ring (0.18 on 2 layers) is not the via
    ring (0.15). The same 0.6/0.3 copper passes as a via, fails as a pad."""
    assert RULES_2L["min_pth_annular_ring_mm"] == pytest.approx(0.18)
    fab = synth_fab(tmp_path, flashes=[(0.6, 5.0, -5.0)],
                    holes=[(0.3, 5.0, -5.0)])
    vios: list = []
    dfm_check.check_annular_ring(fab, RULES_2L, vios)      # no via there
    assert [(v["kind"], v["hole"], v["min_mm"]) for v in vios] == \
        [("dfm_annular_ring", "PTH pad", 0.18)]
    # a vendor row with one ring value holds every hole to it
    one = {k: v for k, v in RULES_2L.items() if k != "min_pth_annular_ring_mm"}
    vios = []
    dfm_check.check_annular_ring(fab, one, vios)
    assert vios == []


def test_every_fab_row_floor_via_meets_its_own_via_ring():
    """#443: JLC 6L said via 0.45/0.2 and ring 0.15 - a 0.125 ring, so
    every floor via failed kicad-cli's annular_width."""
    import vendors
    for vid in vendors.ids():
        caps = yaml.safe_load(vendors.capabilities_path(vid).read_text())
        for key, row in (caps.get("design_rules") or {}).items():
            if not all(k in row for k in ("min_via_diameter_mm",
                                          "min_via_drill_mm",
                                          "min_annular_ring_mm")):
                continue
            ring = (row["min_via_diameter_mm"] - row["min_via_drill_mm"]) / 2
            assert ring >= row["min_annular_ring_mm"] - 1e-9, (vid, key)
            assert row.get("min_pth_annular_ring_mm", 1.0) >= \
                row["min_annular_ring_mm"] - 1e-9, (vid, key)


def test_dfm_annular_ring_unions_overlapping_flashes(tmp_path):
    """S14: a via tangent-overlapping the SMD pad it stitches must be
    measured against the UNION of containing copper - per-flash min invented
    a NEGATIVE ring (hole center barely inside the neighbour pad's edge)
    while the via's own pad ring was fine."""
    # via: 0.6 pad / 0.3 drill at (5, -5); big neighbour pad whose edge
    # passes 0.055mm from the hole center (contains it)
    fab = synth_fab(tmp_path,
                    flashes=[(0.6, 5.0, -5.0), (1.4, 5.645, -5.0)],
                    holes=[(0.3, 5.0, -5.0)])
    vios: list = []
    dfm_check.check_annular_ring(fab, RULES_2L, vios, bg=_ViaBoard(fab))
    assert vios == []  # union ring = the via's own 0.15, not -0.095


def test_dfm_flags_copper_over_board_edge(tmp_path):
    fab = synth_fab(tmp_path, traces=[(0.3, 0.05, -5.0, 8.0, -5.0)],
                    outline_mm=(0.0, -20.0, 20.0, 0.0))
    vios: list = []
    dfm_check.check_copper_to_edge(fab, RULES_2L, vios)
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]


def test_dfm_missing_layer_is_release_error(tmp_path):
    fab = synth_fab(tmp_path)
    (fab.dir / "synth-B_Mask.gbs").unlink()
    fab2 = gerblib.open_fab(fab.dir)
    vios: list = []
    dfm_check.check_release(fab2, ["F.Cu", "B.Cu"], vios, None)
    assert "dfm_missing_layer" in [v["kind"] for v in vios]


def test_dfm_bom_incomplete_is_an_error(tmp_path):
    """U3/codex H1: a machine-placed part nobody can buy blocks the release.
    It was a warning until the assembly classes made 'machine-placed' a fact
    the checker knows (the severity split is pinned in test_assembly.py)."""
    fab = synth_fab(tmp_path)
    vios: list = []
    dfm_check.check_release(fab, ["F.Cu", "B.Cu"], vios,
                            {"missing_lcsc": ["U1", "C3"]})
    v = [x for x in vios if x["kind"] == "dfm_bom_incomplete"]
    assert len(v) == 1 and v[0]["severity"] == "error"
    assert v[0]["refs"] == ["C3", "U1"]


def test_release_missing_inner_layers_board_truth(tmp_path):
    """T6 P9-2 circularity fix: the expected copper set comes from the BOARD,
    so a package that dropped BOTH inner gerbers reads as incomplete - not
    as a valid 2-layer board (which is what counting the audited files did)."""
    fab = synth_fab(tmp_path)                       # F.Cu + B.Cu only
    vios: list = []
    dfm_check.check_release(fab, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                            vios, None)
    v = [x for x in vios if x["kind"] == "dfm_missing_layer"]
    assert len(v) == 1
    assert {"In1.Cu", "In2.Cu"} <= set(v[0]["layers"])
    # both sets are named so a curated export can be waived knowingly
    assert "board declares copper" in v[0]["msg"]
    assert v[0]["expected_copper"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


# ================================= T6 P9-1: copper weight from the stackup

def test_derive_copper_oz_from_stackup_block():
    # pd-trigger fixture: both copper layers at (thickness 0.07) = 2 oz
    assert dfm_check.derive_copper_oz(PD_PCB) == (2.0, "stackup")
    # golden blinky2 has no (stackup ...) block -> today's 1 oz behavior
    assert dfm_check.derive_copper_oz(board_path("blinky2")) == \
        (1.0, "default")


def test_dfm_run_derives_capability_key_from_board():
    """The gate calls run() without copper_oz (gate.py run_dfm builds only
    schematic/parts kwargs): pd-trigger, a 2 oz board, must be judged
    against the 2layer_2oz floors, not the old copper_oz=1.0 default."""
    rep = dfm_check.run(PD_PCB, fab_dir=PD_GERBERS, polarity=False,
                        skip=("copper", "drill", "silk"))
    assert rep["capability_key"] == "2layer_2oz"
    assert rep["copper_oz"] == 2.0
    assert rep["copper_oz_source"] == "stackup"
    # an explicit pin still wins, recorded as such (bench/manifest path)
    rep2 = dfm_check.run(PD_PCB, fab_dir=PD_GERBERS, copper_oz=1.0,
                         polarity=False, skip=("copper", "drill", "silk"))
    assert rep2["capability_key"] == "2layer_1oz"
    assert rep2["copper_oz_source"] == "cli"


def _copper_line(rep) -> list[str]:
    return [x for x in rep.get("not_judged", [])
            if x.startswith("copper weight")]


@pytest.mark.parametrize("thick, oz, line", [
    ("0.07", 2.0, None),
    ("0.105", 1.0, "the stackup gives 3 oz, which the capability table has "
                   "no 2-layer rules for, so the 1 oz floors were used"),
    (None, 1.0, "no thickness in a (stackup) block, so the 1 oz floors "
                "were used"),
])
def test_dfm_copper_weight_fallback_is_named_not_passed(tmp_path, thick, oz,
                                                         line):
    """#892: a board judged at the 1 oz floors because its own weight
    couldn't be keyed says so, and says why: no stackup thickness, or one
    the capability table has no rules for (a 3 oz board is not 'no
    thickness'). A keyed 2 oz board carries no such line."""
    text = PD_PCB.read_text(encoding="utf-8")
    assert "(thickness 0.07)" in text
    if thick is None:
        text = text.replace("(stackup", "(xstackup")
    else:
        text = text.replace("(thickness 0.07)", f"(thickness {thick})")
    pcb = tmp_path / "pd-trigger.kicad_pcb"
    pcb.write_text(text, encoding="utf-8")
    rep = dfm_check.run(pcb, fab_dir=PD_GERBERS, polarity=False,
                        skip=("copper", "drill", "silk"))
    assert rep["copper_oz"] == oz
    assert rep["capability_key"] == f"2layer_{oz:g}oz"
    assert _copper_line(rep) == ([] if line is None
                                 else [f"copper weight: {line}"])
    assert ("copper_oz_stackup" in rep) == (thick == "0.105")
    if thick == "0.105":
        assert rep["copper_oz_stackup"] == 3.0


def test_dfm_copper_weight_pinned_on_the_cli_is_not_a_fallback():
    rep = dfm_check.run(PD_PCB, fab_dir=PD_GERBERS, copper_oz=1.0,
                        polarity=False, skip=("copper", "drill", "silk"))
    assert _copper_line(rep) == []


def test_dfm_not_judged_names_polarity_gaps_and_copper_together(tmp_path):
    """#892: run() builds not_judged from the polarity facts and the copper
    fallback; a part only in the schematic, and no part compared, are both
    named beside the 1 oz line rather than passing as a clean polarity."""
    pcb = tmp_path / "pd-trigger.kicad_pcb"
    pcb.write_text(PD_PCB.read_text(encoding="utf-8")
                   .replace("(stackup", "(xstackup"), encoding="utf-8")
    net = tmp_path / "x.net"
    net.write_text('(export (version "E") (nets (net (code "1") '
                   '(name "/X") (node (ref "Z9") (pin "1")))))\n',
                   encoding="utf-8")
    rep = dfm_check.run(pcb, fab_dir=PD_GERBERS, netlist=net,
                        skip=("copper", "drill", "silk"))
    assert rep["polarity"]["status"] == "checked"
    left = rep["not_judged"]
    assert "polarity: in the schematic, no connected pad on the board: Z9" \
        in left
    assert "polarity: no part could be compared" in left
    assert left[-1].startswith("copper weight: no thickness")


# ==================================== T6 P9-2: unclosable Edge.Cuts outline

def test_dfm_open_outline_is_error(tmp_path):
    """gerblib returns an EMPTY polygon for an unclosable Edge.Cuts and both
    edge-distance checks early-return on it - the silence must be an error."""
    fab = synth_fab(tmp_path)                       # closed rectangle: clean
    vios: list = []
    dfm_check.check_outline(fab, vios)
    assert vios == []

    d = tmp_path / "open"
    d.mkdir()
    for name in ("o-F_Cu.gtl", "o-B_Cu.gbl", "o-F_Silkscreen.gto",
                 "o-B_Silkscreen.gbo", "o-F_Mask.gts", "o-B_Mask.gbs"):
        write_gerber(d / name)
    write_gerber(d / "o-Edge_Cuts.gm1",             # two sides only: open
                 traces=[(0.1, 0.0, 0.0, 20.0, 0.0),
                         (0.1, 20.0, 0.0, 20.0, 20.0)])
    write_drill(d / "o.drl")
    fab2 = gerblib.open_fab(d)
    vios2: list = []
    dfm_check.check_outline(fab2, vios2)
    assert [v["kind"] for v in vios2] == ["dfm_open_outline"]
    assert vios2[0]["severity"] == "error"


# ================== U1: outline snap + arc interpolation (carrier retro)

CARRIER_GERBERS = real_board_path("lumina-carrier") / "fab" / "gerbers"
RULES_4L = yaml.safe_load(
    (REFERENCE / "jlc_capabilities.yaml").read_text(encoding="utf-8")
)["design_rules"]["4layer_1oz"]


def _edge_kinds(vios):
    return [v for v in vios
            if v["kind"] in ("dfm_copper_to_edge", "dfm_hole_to_edge")]


def test_carrier_outline_closes_and_edge_checks_run_clean():
    """Retro 2026-08-07 known answer: the SHIPPED carrier gerbers' corner
    joints disagree by 1e-6 mm (gerber 4.6-format round-off), which emptied
    the outline and silently disabled copper-to-edge + hole-to-edge at P9.
    The snapped, arc-interpolated outline must close at the true arc area
    (7992.27 mm2, cross-checked in the retro against the .kicad_pcb's own
    arc-aware 7992.23 mm2) and both edge checks must run and report zero."""
    need_board("lumina-carrier")
    fab = gerblib.open_fab(CARRIER_GERBERS)
    o = fab.outline
    assert not o.is_empty
    assert o.area == pytest.approx(7992.27, abs=0.05)
    assert (o.bounds[2] - o.bounds[0], o.bounds[3] - o.bounds[1]) \
        == pytest.approx((100.0, 80.0), abs=1e-3)
    vios: list = []
    dfm_check.check_outline(fab, vios)
    dfm_check.check_copper_to_edge(fab, RULES_4L, vios)
    dfm_check.check_holes(fab, RULES_4L, vios)
    assert _edge_kinds(vios) == []
    assert [v for v in vios if v["kind"] == "dfm_open_outline"] == []


def test_outline_snaps_nanometre_corner_gaps(tmp_path):
    """Synthetic twin of the carrier defect: corners that miss by 1e-6 mm
    (one nanometre, the gerber format's own resolution) must still close."""
    d = tmp_path / "snap"
    d.mkdir()
    e = 1e-6
    write_gerber(d / "snap-F_Cu.gtl")
    write_gerber(d / "snap-Edge_Cuts.gm1", traces=[
        (0.1, 0.0, 0.0, 10.0, 0.0),
        (0.1, 10.0 + e, 0.0 - e, 10.0, 10.0),
        (0.1, 10.0 + e, 10.0 + e, 0.0, 10.0),
        (0.1, 0.0 - e, 10.0 + e, 0.0, 0.0)])
    fab = gerblib.open_fab(d)
    assert not fab.outline.is_empty
    assert fab.outline.area == pytest.approx(100.0, abs=0.01)


def _lite_main_outline(d: Path, miss_nm: int, flashes=()) -> object:
    """lite-main's outline as KiCad's Gerber writer puts it at board_init's
    non-round origin (#858): a 100 x 90 mm board with 2 mm corner arcs at
    x = 22.864999, and the first arc's end, recomputed from its centre,
    missing the top edge's start by miss_nm nm."""
    x0, y0, w, h, r = 22.864999, -60.323, 100.0, 90.0, 2.0
    x1, y1 = x0 + w, y0 - h
    e = miss_nm * 1e-6
    write_gerber(d / "lm-F_Cu.gtl", flashes=flashes)
    write_gerber(d / "lm-Edge_Cuts.gm1", traces=[
        (0.1, x0 + r, y0, x1 - r, y0), (0.1, x1, y0 - r, x1, y1 + r),
        (0.1, x1 - r, y1, x0 + r, y1), (0.1, x0, y1 + r, x0, y0 - r)],
        arcs=[(0.1, x0, y0 - r, x0 + r + e, y0 + e, r, 0.0, False),
              (0.1, x1 - r, y0, x1, y0 - r, 0.0, -r, False),
              (0.1, x1, y1 + r, x1 - r, y1, -r, 0.0, False),
              (0.1, x0 + r, y1, x0, y1 + r, 0.0, r, False)])
    return gerblib.open_fab(d)


def test_outline_joins_kicad_corner_arcs_missing_by_51_nm(tmp_path):
    """#858 planted fault and quiet twin: a corner arc 51 nm off the edge
    (the worst KiCad wrote over 40 shifted copies of lite-main) straddles
    the 0.01 um snap grid and read as an open outline, so neither edge check
    ran. Joined, it closes at the board's area: the flash 0.1 mm off the
    edge fires and the one 1 mm in does not."""
    x0, y0 = 22.864999, -60.323
    hot, legal = (x0 + 50.0, y0 - 0.1), (x0 + 50.0, y0 - 45.0)
    d = tmp_path / "lm"
    d.mkdir()
    fab = _lite_main_outline(d, 51, flashes=[(0.2, *hot), (0.2, *legal)])
    assert fab.outline.area == pytest.approx(
        100.0 * 90.0 - (4 - math.pi) * 2.0 ** 2, abs=0.01)
    vios: list = []
    dfm_check.check_outline(fab, vios)
    dfm_check.check_copper_to_edge(fab, {"min_copper_to_edge_mm": 0.3}, vios)
    c2e = [v for v in vios if v["kind"] == "dfm_copper_to_edge"]
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]
    assert c2e[0]["pos"] == pytest.approx([hot[0], -hot[1]], abs=0.3)


def test_outline_gap_over_the_join_stays_open(tmp_path):
    """#858: only ends within 0.1 um are joined. An arc 1 um off its edge is
    a real gap, still read as open and reported."""
    d = tmp_path / "gap"
    d.mkdir()
    fab = _lite_main_outline(d, 1000)
    assert fab.outline.is_empty
    vios: list = []
    dfm_check.check_outline(fab, vios)
    assert [v["kind"] for v in vios] == ["dfm_open_outline"]


def test_rounded_corner_outline_measures_the_arc_not_the_chord(tmp_path):
    """gerblib used to flatten Edge.Cuts arcs to chords, cutting a 3.0 mm
    rounded corner ~0.879 mm inboard - two false copper_to_edge errors on
    every rounded-corner board once the snap landed (retro s4). The fixture
    puts, along the corner diagonal: a flash 0.40 mm off the TRUE arc but
    only ~0.08 mm off the CHORD (fired falsely before), a flash genuinely
    0.10 mm off the arc (must still fire), and a hole crossing the chord but
    0.75 mm off the arc (fired falsely before)."""
    d = tmp_path / "round"
    d.mkdir()
    c, r = 17.0, 3.0            # arc center (both axes); corner at (20,20)
    sq2 = math.sqrt(2.0)
    legal = (c + 2.4 / sq2, c + 2.4 / sq2)   # clears the arc by 0.40 mm
    hot = (c + 2.7 / sq2, c + 2.7 / sq2)     # 0.10 mm off the arc: violation
    write_gerber(d / "round-F_Cu.gtl",
                 flashes=[(0.4, *legal), (0.4, *hot)])
    write_gerber(d / "round-Edge_Cuts.gm1", traces=[
        (0.1, 0.0, 0.0, 20.0, 0.0), (0.1, 20.0, 0.0, 20.0, c),
        (0.1, c, 20.0, 0.0, 20.0), (0.1, 0.0, 20.0, 0.0, 0.0)],
        arcs=[(0.1, 20.0, c, c, 20.0, -r, 0.0, True)])
    write_drill(d / "round.drl",
                holes=[(0.5, c + 2.0 / sq2, c + 2.0 / sq2)])
    fab = gerblib.open_fab(d)
    # true rounded-rect area: 400 - corner cut (r^2 - pi r^2 / 4)
    assert fab.outline.area == pytest.approx(
        400.0 - (r * r - math.pi * r * r / 4.0), abs=0.01)
    vios: list = []
    dfm_check.check_copper_to_edge(fab, {"min_copper_to_edge_mm": 0.3}, vios)
    dfm_check.check_holes(fab, {"min_hole_to_edge_mm": 0.4}, vios)
    c2e = [v for v in vios if v["kind"] == "dfm_copper_to_edge"]
    h2e = [v for v in vios if v["kind"] == "dfm_hole_to_edge"]
    assert len(c2e) == 1                     # the hot flash only
    assert c2e[0]["pos"] == pytest.approx([hot[0], -hot[1]], abs=0.3)
    assert h2e == []


def test_dfm_open_outline_through_run(tmp_path):
    dst = tmp_path / "gerbers"
    shutil.copytree(PD_GERBERS, dst)
    write_gerber(dst / "pd-trigger-Edge_Cuts.gm1",  # replace with open contour
                 traces=[(0.1, 0.0, 0.0, 20.0, 0.0),
                         (0.1, 20.0, 0.0, 20.0, 20.0)])
    rep = dfm_check.run(PD_PCB, fab_dir=dst, polarity=False,
                        skip=("copper", "drill", "silk"))
    assert "dfm_open_outline" in [v["kind"] for v in rep["violations"]]
    assert rep["status"] == "violations"


# ============================ T6 P9-3: tented pad (paste, no mask opening)

def test_gerblib_paste_accessor(tmp_path):
    fab = synth_fab(tmp_path, paste=[(0.5, 5.0, -5.0)])
    lg = fab.paste("F")
    assert lg is not None and len(lg.pads) == 1
    assert fab.paste("B") is not None and fab.paste("B").pads == []


def test_dfm_flags_tented_pad(tmp_path):
    """A paste aperture with no mask opening = tented pad = unassemblable.
    Gating on paste flashes keeps tented vias / paste-less test points out
    of scope: they have no stencil aperture."""
    fab = synth_fab(tmp_path, paste=[(0.5, 5.0, -5.0)],
                    mask=[(1.0, 12.0, -12.0)])      # only opening: elsewhere
    vios: list = []
    dfm_check.check_pad_tented(fab, vios)
    assert [v["kind"] for v in vios] == ["dfm_pad_tented"]
    assert vios[0]["severity"] == "error"
    assert vios[0]["pos"] == pytest.approx([5.0, 5.0], abs=0.05)

    (tmp_path / "ok").mkdir()
    fab2 = synth_fab(tmp_path / "ok", paste=[(0.5, 5.0, -5.0)],
                     mask=[(1.0, 5.0, -5.0)])       # opening present: clean
    vios2: list = []
    dfm_check.check_pad_tented(fab2, vios2)
    assert vios2 == []


def test_dfm_pad_tented_mutant_fixture():
    """Frozen known answer: mutant_paste is the blinky2 cpl-rotation gerber
    set with exactly ONE mask-opening flash deleted (C8 pad 1, F.Mask); its
    paste aperture is untouched -> exactly one dfm_pad_tented error there,
    and the inherited cpl_polarity catch is unaffected."""
    d = STAGES / "mutant_paste"
    rep = dfm_check.run(d / "blinky2.kicad_pcb", fab_dir=d / "gerbers",
                        netlist=BLINKY_NET)
    tented = [v for v in rep["violations"] if v["kind"] == "dfm_pad_tented"]
    assert len(tented) == 1
    assert tented[0]["severity"] == "error"
    assert tented[0]["pos"] == pytest.approx([118.4, 129.625], abs=0.5)
    assert any(v["kind"] == "cpl_polarity" for v in rep["violations"])
    # the untouched source set stays clean (zero false positives)
    clean = gerblib.open_fab(STAGES / "mutant_cpl" / "gerbers")
    vios: list = []
    dfm_check.check_pad_tented(clean, vios)
    assert vios == []


def test_dfm_silk_sliver_is_warning_real_overlap_error(tmp_path):
    """S14: micron-sliver silk inside a mask opening (library body outlines)
    is a WARNING (fab auto-clips); substantial ink on solder surface stays an
    ERROR. Threshold SILK_OVERLAP_ERROR_MM2."""
    # sliver: 0.15mm silk stroke crossing a 1mm mask opening edge by ~0.02mm
    fab = synth_fab(tmp_path, silk=[(0.15, 3.0, -4.49, 7.0, -4.49)],
                    mask=[(1.0, 5.0, -5.0)])
    vios: list = []
    dfm_check.check_silk(fab, RULES_2L, vios)
    hits = [v for v in vios if v["kind"] == "dfm_silk_over_pad"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "warning"
    assert hits[0]["overlap_mm2"] < dfm_check.SILK_OVERLAP_ERROR_MM2

    # substantial: the same stroke straight through the opening centre
    (tmp_path / "e").mkdir()
    fab2 = synth_fab(tmp_path / "e", silk=[(0.3, 3.0, -5.0, 7.0, -5.0)],
                     mask=[(1.0, 5.0, -5.0)])
    vios2: list = []
    dfm_check.check_silk(fab2, RULES_2L, vios2)
    hits2 = [v for v in vios2 if v["kind"] == "dfm_silk_over_pad"]
    assert len(hits2) == 1
    assert hits2[0]["severity"] == "error"
    assert hits2[0]["overlap_mm2"] >= dfm_check.SILK_OVERLAP_ERROR_MM2


def test_board_lcsc_map_from_footprint_fields(tmp_path):
    """S14: the board's own per-footprint LCSC fields are the primary
    ref->LCSC source for the BOM (board_init copies them from the symbols)."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20260206) (generator "t")\n'
        '  (footprint "aiee:C0603" (at 10 10)\n'
        '    (property "Reference" "C1" (at 0 -4 0))\n'
        '    (property "Value" "100nF 50V X7R" (at 0 4 0))\n'
        '    (property "LCSC" "C14663" (at 0 0 0) (hide yes)))\n'
        '  (footprint "aiee:LQFP-48" (at 30 20)\n'
        '    (property "Reference" "U1" (at 0 -5 0))\n'
        '    (property "LCSC" "C8734" (at 0 0 0) (hide yes)))\n'
        '  (footprint "aiee:HDR" (at 40 20)\n'
        '    (property "Reference" "J1" (at 0 -5 0))))\n',
        encoding="utf-8")
    m = bom_cpl.board_lcsc_map(pcb)
    assert m["C1"]["lcsc"] == "C14663" and m["U1"]["lcsc"] == "C8734"
    assert "J1" not in m  # no LCSC field -> stays missing (honest)
    # per-ref parts.json overrides the board field
    merged = {**m, **{"C1": {"lcsc": "C99999", "mpn": None}}}
    assert merged["C1"]["lcsc"] == "C99999"


# ================================================ hermetic: CPL polarity (V9)

def test_polarity_clean_on_golden():
    """Zero false positives: the golden's pads match its schematic exactly."""
    nl = netlist_audit.parse_netlist(BLINKY_NET)
    bg = geom.load_board(board_path("blinky2"))
    vios: list = []
    facts = dfm_check.check_polarity(bg, nl, vios)
    assert vios == []
    assert facts["refs_checked"] == 17


def test_dfm_catches_cpl_rotation():
    """S12's designated mutant (manifest: dfm_check owns cpl-rotation).

    kicad's --schematic-parity does NOT flag this board (V9 / LEARNINGS), so
    the per-pad-number comparison is the only thing standing between a
    backwards LED and the fab.
    """
    expect = MANIFEST["mutants"]["cpl-rotation"]["expect"]
    nl = netlist_audit.parse_netlist(BLINKY_NET)
    bg = geom.load_board(mutant_path("cpl-rotation", "blinky2"))
    vios: list = []
    dfm_check.check_polarity(bg, nl, vios)
    assert len(vios) == 1
    v = vios[0]
    assert v["kind"] == "cpl_polarity" and v["severity"] == "error"
    assert v["refs"] == [expect["ref"]]
    assert v["rotation_delta_deg"] == pytest.approx(
        expect["rotation_delta_deg"])
    # both pads reported, with board-vs-schematic nets named
    assert {m["pad"] for m in v["mismatches"]} == {"1", "2"}
    assert {m["board"] for m in v["mismatches"]} == {"/LED_A", "GND"}


def test_polarity_clean_on_non_rotation_mutants():
    """The other blinky2 mutants must not trip the polarity check."""
    nl = netlist_audit.parse_netlist(BLINKY_NET)
    for mut in ("silk-over-pad", "undersized-power-trace", "decoupler-moved"):
        bg = geom.load_board(mutant_path(mut, "blinky2"))
        vios: list = []
        dfm_check.check_polarity(bg, nl, vios)
        assert vios == [], f"{mut} falsely flagged by polarity"


# ================================================================= gates

def test_gates_yaml_has_dfm_gate():
    gates = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))["gates"]
    assert "dfm" in gates
    g = gates["dfm"]
    assert g["tool"] == "dfm" and g["phase"] == "P9"
    assert g["fail_severities"] == ["error"]     # warnings must not fail


# ============================================================ pure: ordering

def test_quote_pcb_cost_scales_with_qty_and_layers():
    pricing = order_quote.load_pricing(REFERENCE / "jlc_pricing.yaml")
    c2 = order_quote.pcb_cost(pricing, 2, 5, 50, 40, "HASL", "green")
    c4 = order_quote.pcb_cost(pricing, 4, 5, 50, 40, "HASL", "green")
    assert c4["total"] > c2["total"]
    c2_30 = order_quote.pcb_cost(pricing, 2, 30, 50, 40, "HASL", "green")
    assert c2_30["total"] > c2["total"]


def test_quote_oversize_adds_cost():
    pricing = order_quote.load_pricing(REFERENCE / "jlc_pricing.yaml")
    small = order_quote.pcb_cost(pricing, 2, 5, 50, 40, "HASL", "green")
    big = order_quote.pcb_cost(pricing, 2, 5, 150, 140, "HASL", "green")
    assert small["oversize"] == 0.0 and big["oversize"] > 0.0


def test_quote_assembly_feeders_only_for_extended_parts():
    pricing = order_quote.load_pricing(REFERENCE / "jlc_pricing.yaml")
    basic = order_quote.assembly_cost(pricing, 5, 10, 40, 0)
    ext = order_quote.assembly_cost(pricing, 5, 10, 40, 3)
    assert basic["feeders"] == 0.0 and ext["feeders"] > 0.0
    assert ext["total"] > basic["total"]


def test_quote_extended_count_per_distinct_parts_json(tmp_path):
    """P10-2: the pipeline's parts.json (S6 per-DISTINCT-part shape: `basic`
    flag, NO refs/ref keys) must count Extended parts - the old ref-based
    mapping produced n_extended 0 on every pipeline board (13 of 24 Extended
    on pd-trigger priced at $0). The feeder fee is per UNIQUE part."""
    parts = {"parts": [
        {"mpn": "A", "lcsc": "C1", "basic": True, "qty_per_board": 2},
        {"mpn": "B", "lcsc": "C2", "basic": False, "qty_per_board": 1},
        {"mpn": "C", "lcsc": "C3", "basic": False, "qty_per_board": 4},
    ]}
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps(parts), encoding="utf-8")
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"],
                          assembly=True, parts_json=pj)
    assert rep["spec"]["n_extended_parts"] == 2
    assert rep["spec"]["n_extended_source"] == "per_distinct_entries"
    row = rep["matrix"][0]
    assert row["assembly"]["feeders"] == 6.00    # 2 unique Extended x 3.00
    assert row["assembly"]["n_extended_parts"] == 2


def test_quote_extended_count_dedupes_repeated_lcsc(tmp_path):
    """A part repeated across entries (same lcsc) is ONE feeder fee."""
    parts = {"parts": [
        {"mpn": "B", "lcsc": "C2", "basic": False},
        {"mpn": "B", "lcsc": "C2", "basic": False},
    ]}
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps(parts), encoding="utf-8")
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"],
                          assembly=True, parts_json=pj)
    assert rep["spec"]["n_extended_parts"] == 1


def test_quote_extended_count_per_ref_fallback_dedupes_by_lcsc(tmp_path):
    """Per-ref shapes WITHOUT any basic/type flag: unique parts (by lcsc)
    count as Extended - the conservative (higher-fee) direction."""
    parts = [
        {"refs": ["R1", "R2", "R3"], "lcsc": "C10"},
        {"ref": "U1", "lcsc": "C11"},
    ]
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps(parts), encoding="utf-8")
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"],
                          assembly=True, parts_json=pj)
    assert rep["spec"]["n_extended_parts"] == 2  # 2 unique lcsc, not 4 refs
    assert rep["spec"]["n_extended_source"] == \
        "per_ref_lcsc_fallback_unflagged_as_extended"


def test_quote_skips_parts_jlc_does_not_place(tmp_path):
    """dnp / board_feature lines carry no LCSC and no basic flag: they are
    never fed, so no feeder fee, and their refs add no parts or joints."""
    refs = sorted({p.ref for p in order_quote.geom.load_board(
        board_path("blinky2")).pads_of() if p.net is not None})
    parts = {"parts": [
        {"mpn": "B", "lcsc": "C2", "basic": False},
        {"mpn": "land", "lcsc": "", "assembly_class": "dnp",
         "refdes": [refs[0]]},
    ]}
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps(parts), encoding="utf-8")
    base = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"],
                           assembly=True)
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"],
                          assembly=True, parts_json=pj)
    assert rep["spec"]["n_extended_parts"] == 1
    assert rep["spec"]["n_parts"] == base["spec"]["n_parts"] - 1
    assert rep["spec"]["n_joints"] < base["spec"]["n_joints"]


def test_quote_no_extended_source_key_without_assembly():
    """The bench (score_p10) runs without assembly: its spec dict must stay
    byte-identical - no n_extended_source key leaks in."""
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"])
    assert "n_extended_source" not in rep["spec"]
    assert rep["spec"]["n_extended_parts"] == 0


def test_quote_calibration_surfaced_from_measured_points():
    """P10-4: meta.measured_vs_api in jlc_pricing.yaml -> every quote payload
    carries the OBSERVED estimate-vs-API ratio and the disclaimer says
    LOWER BOUND (the 1.9-3.1x underestimate was L0 prose only)."""
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"])
    cal = rep["calibration"]
    assert cal["observed_underestimate"] == \
        "1.9-3.1x low vs live API calculate (measured)"
    boards = {p["board"] for p in cal["points"]}
    assert boards == {"pd-trigger", "lumina-carrier"}
    assert "LOWER BOUND" in rep["disclaimer"]


def test_quote_no_calibration_without_measured_points(tmp_path):
    pricing = yaml.safe_load(
        (REFERENCE / "jlc_pricing.yaml").read_text(encoding="utf-8"))
    pricing["meta"].pop("measured_vs_api", None)
    pp = tmp_path / "pricing.yaml"
    pp.write_text(yaml.safe_dump(pricing), encoding="utf-8")
    rep = order_quote.run(board_path("blinky2"), [5], ["HASL"], ["green"],
                          pricing_path=pp)
    assert "calibration" not in rep
    assert "LOWER BOUND" not in rep["disclaimer"]


def test_order_submit_reports_incomplete_package(tmp_path):
    man = order_submit.run(board_path("blinky2"), tmp_path)
    assert man["status"] == "incomplete"
    assert "gerber zip" in man["missing"]
    assert man["payment"].startswith("HUMAN")


def test_order_submit_hashes_and_human_steps(tmp_path):
    (tmp_path / "b_gerbers.zip").write_bytes(b"PK\x03\x04zip")
    (tmp_path / "BOM.csv").write_text("Comment\n", encoding="utf-8")
    (tmp_path / "CPL.csv").write_text("Designator\n", encoding="utf-8")
    man = order_submit.run(board_path("blinky2"), tmp_path)
    assert man["status"] == "ready_for_human"
    assert man["missing"] == []
    assert len(man["artifacts"]["gerber_zip"]["sha256"]) == 64
    # JLCDFM (V6) and the polarity preview must both be named for the human
    joined = " ".join(man["human_steps"]).lower()
    assert "jlcdfm" in joined and "polarized" in joined


# ================================================== smoke: live kicad-cli

@pytest.fixture(scope="module")
def fab_dirs(tmp_path_factory):
    """Export each golden's fab package once and reuse it."""
    out = {}
    base = tmp_path_factory.mktemp("fab")
    for name in BOARDS:
        d = base / name
        man = fab_export.run(board_path(name), d, make_zip=True)
        out[name] = (d, man)
    return out


# ======================================== T1: design hash vs file hash (fabhash)

def _pkg(path, *, when="2026-07-28T11:55:55", version="10.0.3",
         x="X250000", extra=None):
    """A minimal but REAL fab package (gerber + Excellon + job file) carrying
    the volatile stamps KiCad writes into every export."""
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("b-F_Cu.gtl",
                    f"%TF.GenerationSoftware,KiCad,Pcbnew,{version}*%\n"
                    f"%TF.CreationDate,{when}-07:00*%\n"
                    "%TF.ProjectId,b,70642d74-7269-4676,rev?*%\n"
                    f"G04 Created by KiCad (PCBNEW {version}) date {when}*\n"
                    f"{x}Y250000D02*\nM02*\n")
        zf.writestr("b.drl",
                    f"M48\n; DRILL file KiCad {version} date {when}\n"
                    f"; #@! TF.CreationDate,{when}-07:00\n"
                    f"; #@! TF.GenerationSoftware,Kicad,Pcbnew,{version}\n"
                    "T1C0.300\nM30\n")
        zf.writestr("b-job.gbrjob", json.dumps(
            {"Header": {"GenerationSoftware": {"Vendor": "KiCad",
                                               "Version": version},
                        "CreationDate": f"{when}-07:00"},
             "GeneralSpecs": {"LayerNumber": 2, "BoardThickness": 1.6}},
            indent=2))
        for name, text in (extra or {}).items():
            zf.writestr(name, text)


def test_design_hash_ignores_export_stamps_not_geometry(tmp_path):
    """The zip sha256 is NOT a design fingerprint: every export restamps the
    timestamp headers, which is how an approved quote self-invalidated
    (LEARNINGS 2026-07-30 [fab_export][order_submit][jlcapi])."""
    a, b, c = (tmp_path / f"{n}.zip" for n in "abc")
    _pkg(a)
    _pkg(b, when="2026-08-06T09:01:02", version="10.0.4")   # re-export only
    _pkg(c, x="X260000")                                    # one track moved
    assert fabhash.file_sha256(a) != fabhash.file_sha256(b)
    assert fabhash.design_hash(a) == fabhash.design_hash(b)
    assert fabhash.design_hash(a) != fabhash.design_hash(c)


def test_design_hash_sees_layer_set_changes(tmp_path):
    """Adding, renaming or dropping a layer changes the design hash - member
    NAMES are hashed, not just their content."""
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    _pkg(a)
    _pkg(b, extra={"b-B_Cu.gbl": "%FSLAX46Y46*%\nM02*\n"})
    assert fabhash.design_hash(a) != fabhash.design_hash(b)


def test_normalize_member_strips_exactly_the_volatile_lines():
    gerber = ("%TF.GenerationSoftware,KiCad,Pcbnew,10.0.3*%\n"
              "%TF.CreationDate,2026-07-28T11:55:55-07:00*%\n"
              "%TF.ProjectId,b,uuid,rev?*%\n"
              "G04 Created by KiCad (PCBNEW 10.0.3) date 2026-07-28*\n"
              "X250000Y250000D02*\n")
    out = fabhash.normalize_member("b.gtl", gerber.encode()).decode()
    assert "CreationDate" not in out and "GenerationSoftware" not in out
    assert "Created by KiCad" not in out
    assert "%TF.ProjectId" in out and "X250000Y250000D02*" in out

    drill = ("M48\n; DRILL file KiCad 10.0.3 date 2026-07-28T11:55:55\n"
             "; FORMAT={-:-/ absolute / metric / decimal}\n"
             "; #@! TF.CreationDate,2026-07-28T11:55:55-07:00\nT1C0.300\n")
    out = fabhash.normalize_member("b.drl", drill.encode()).decode()
    assert "DRILL file" not in out and "CreationDate" not in out
    assert "; FORMAT=" in out and "T1C0.300" in out       # design content kept

    job = json.dumps({"Header": {"CreationDate": "x",
                                 "GenerationSoftware": {"Version": "10.0.3"}},
                      "GeneralSpecs": {"BoardThickness": 1.6}})
    out = fabhash.normalize_member("b-job.gbrjob", job.encode()).decode()
    assert "CreationDate" not in out and "GenerationSoftware" not in out
    assert "BoardThickness" in out


def test_design_hash_falls_back_to_bytes_for_non_zip(tmp_path):
    """Fail-safe: an unreadable package binds to its exact bytes."""
    p, q = tmp_path / "p.zip", tmp_path / "q.zip"
    p.write_bytes(b"PK\x03\x04NOTAZIP")
    q.write_bytes(b"PK\x03\x04NOTAZIP-2")
    assert fabhash.design_hash(p) == fabhash.design_hash(p)
    assert fabhash.design_hash(p) != fabhash.design_hash(q)


@pytest.mark.smoke
def test_design_hash_stable_across_real_reexports(tmp_path, fab_dirs):
    """The claim that matters, against KiCad's own output: exporting the SAME
    board again yields the same design hash."""
    first = Path(fab_dirs["blinky2"][1]["gerber_zip"])
    time.sleep(1.1)                       # force a different timestamp stamp
    again = fab_export.run(board_path("blinky2"), tmp_path / "again",
                           make_zip=True)
    second = Path(again["gerber_zip"])
    assert fabhash.file_sha256(first) != fabhash.file_sha256(second)
    assert fabhash.design_hash(first) == fabhash.design_hash(second)


@pytest.mark.smoke
def test_fab_export_layer_set_and_zip(fab_dirs):
    d, man = fab_dirs["blinky2"]
    assert man["status"] == "pass" and man["layer_count"] == 2
    names = {f["name"] for f in man["files"]}
    for want in ("blinky2-F_Cu.gtl", "blinky2-B_Cu.gbl", "blinky2-F_Mask.gts",
                 "blinky2-F_Silkscreen.gto", "blinky2-Edge_Cuts.gm1",
                 "blinky2.drl"):
        assert want in names, want
    assert Path(man["gerber_zip"]).exists()
    assert len(man["gerber_zip_sha256"]) == 64
    assert all(len(f["sha256"]) == 64 for f in man["files"])


@pytest.mark.smoke
def test_fab_export_four_layer_stackup_order(fab_dirs):
    _, man = fab_dirs["usbbuck4"]
    assert man["layer_count"] == 4
    assert man["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    names = {f["name"] for f in man["files"]}
    assert "usbbuck4-In1_Cu.g1" in names and "usbbuck4-In2_Cu.g2" in names


@pytest.mark.smoke
def test_bom_cpl_on_golden(fab_dirs, tmp_path):
    rep = bom_cpl.run(board_path("blinky2"), tmp_path)
    assert rep["n_parts"] == 17 and rep["n_placed"] == 17
    assert Path(rep["bom"]).exists() and Path(rep["cpl"]).exists()
    assert Path(rep["bom_full"]).exists()
    d1 = [a for a in rep["rotation_audit"] if a["ref"] == "D1"][0]
    # golden D1 sits at 180 deg; the LED_0805 correction brings it to JLC 0
    assert d1["base_rot"] == pytest.approx(180.0)
    assert d1["final_rot"] == pytest.approx(0.0)
    # no parts.json and no LCSC fields on the board: nothing here is buyable,
    # so U3 reports violations instead of the old pass-beside-bom_complete-false
    assert rep["missing_lcsc"] and not rep["bom_complete"]
    assert rep["status"] == "violations"
    # the two pin headers are through-hole, and still in the placed count
    assert rep["class_counts"] == {"smt_placed": 15, "tht": 2}


@pytest.mark.smoke
@pytest.mark.parametrize("name", BOARDS)
def test_dfm_goldens_clean(fab_dirs, name):
    """Zero FALSE POSITIVES on every golden (warnings are advisory)."""
    d, _ = fab_dirs[name]
    rep = dfm_check.run(board_path(name), fab_dir=d,
                        schematic=GOLDEN / name / f"{name}.kicad_sch")
    errors = [v for v in rep["violations"] if v["severity"] == "error"]
    assert errors == [], errors
    assert rep["status"] == "pass"
    assert rep["polarity"]["status"] == "checked"


@pytest.mark.smoke
def test_dfm_catches_silk_over_pad(tmp_path):
    """The other mutant dfm_check must catch, at manifest coordinates."""
    expect = MANIFEST["mutants"]["silk-over-pad"]["expect"]
    pcb = mutant_path("silk-over-pad", "blinky2")
    rep = dfm_check.run(pcb, netlist=BLINKY_NET)
    hits = [v for v in rep["violations"] if v["kind"] == "dfm_silk_over_pad"]
    assert hits, "silk-over-pad not caught"
    v = hits[0]
    assert v["severity"] == "error" and v["refs"] == [expect["ref"]]
    assert v["pos"][0] == pytest.approx(expect["pos"][0], abs=0.1)
    assert v["pos"][1] == pytest.approx(expect["pos"][1], abs=0.1)
    assert rep["status"] == "violations"


@pytest.mark.smoke
def test_dfm_end_to_end_catches_rotation_mutant():
    """The hermetic polarity unit test, re-proven through the full CLI path."""
    rep = dfm_check.run(mutant_path("cpl-rotation", "blinky2"),
                        netlist=BLINKY_NET)
    hits = [v for v in rep["violations"] if v["kind"] == "cpl_polarity"]
    assert len(hits) == 1 and hits[0]["refs"] == ["D1"]
    assert rep["status"] == "violations"


@pytest.mark.smoke
@pytest.mark.parametrize("mut,board", [
    ("undersized-power-trace", "blinky2"),
    ("decoupler-moved", "blinky2"),
    ("diffpair-skew", "usbbuck4"),
    ("missing-return-via", "usbbuck4"),
])
def test_dfm_negative_control_mutants(mut, board):
    """Mutants owned by OTHER checks must not raise dfm errors."""
    rep = dfm_check.run(mutant_path(mut, board),
                        schematic=GOLDEN / board / f"{board}.kicad_sch")
    errors = [v for v in rep["violations"] if v["severity"] == "error"]
    assert errors == [], f"{mut}: {errors}"


@pytest.mark.smoke
def test_dfm_missing_inner_layers_end_to_end(fab_dirs, tmp_path):
    """T6 P9-2: a 4-layer export that silently dropped BOTH inner gerbers
    must fail release completeness AND stay under the 4-layer capability
    table (the board's declared layer set is the truth, not the file count)."""
    _, man = fab_dirs["usbbuck4"]
    dst = tmp_path / "gerbers"
    shutil.copytree(Path(man["gerber_dir"]), dst)
    dropped = [f for f in dst.iterdir()
               if "In1_Cu" in f.name or "In2_Cu" in f.name]
    assert len(dropped) == 2
    for f in dropped:
        f.unlink()
    rep = dfm_check.run(board_path("usbbuck4"), fab_dir=dst, polarity=False)
    miss = [v for v in rep["violations"] if v["kind"] == "dfm_missing_layer"]
    assert len(miss) == 1
    assert {"In1.Cu", "In2.Cu"} <= set(miss[0]["layers"])
    assert rep["capability_key"].startswith("4layer")
    assert rep["layer_count"] == 4
    assert rep["status"] == "violations"


@pytest.mark.smoke
def test_dfm_gate_passes_golden_and_fails_mutant(tmp_path):
    import gate
    gates = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))["gates"]
    ok = gate.evaluate("dfm", gates["dfm"],
                       gate.run_report_for_gate(gates["dfm"],
                                                board_path("blinky2")))
    assert ok["status"] == "pass" and ok["failing_count"] == 0

    # stage the mutant beside blinky2's schematic so the gate finds its oracle
    staged = tmp_path / "mut"
    staged.mkdir()
    src = mutant_path("cpl-rotation", "blinky2")
    (staged / "blinky2.kicad_pcb").write_bytes(src.read_bytes())
    (staged / "blinky2.kicad_sch").write_bytes(
        (GOLDEN / "blinky2" / "blinky2.kicad_sch").read_bytes())
    bad = gate.evaluate("dfm", gates["dfm"],
                        gate.run_report_for_gate(gates["dfm"],
                                                 staged / "blinky2.kicad_pcb"))
    assert bad["status"] == "fail"
    assert any(f["kind"] == "cpl_polarity" for f in bad["failing"])


@pytest.mark.smoke
def test_full_package_flow(tmp_path, monkeypatch):
    """P9 -> P10: export, BOM/CPL, quote, order manifest. This is the machine-
    checkable half of 'the package uploads clean'; the upload itself is the
    documented human step."""
    for var in order_submit.API_ENV:      # hermetic: host creds must not flip
        monkeypatch.delenv(var, raising=False)
    pcb = board_path("usbbuck4")
    fab = tmp_path / "fab"
    man = fab_export.run(pcb, fab, make_zip=True)
    assert man["status"] == "pass"
    # the golden carries no LCSC fields anywhere, so give it a BOM-of-record
    # map - "the package uploads clean" means every placed part is buyable (U3)
    scout = bom_cpl.run(pcb, tmp_path / "scout")
    parts_json = tmp_path / "parts.json"
    parts_json.write_text(json.dumps({"parts": [
        {"refdes": [r["Designator"] for r in scout["cpl_rows"]],
         "lcsc": "C0000"}]}), encoding="utf-8")
    bc = bom_cpl.run(pcb, fab, parts_json=parts_json)
    assert bc["status"] == "pass" and bc["bom_complete"] is True
    q = order_quote.run(pcb, [5, 30], ["HASL"], ["green"], assembly=True)
    assert q["estimated"] is True and q["spec"]["layers"] == 4
    assert q["matrix"] and q["cheapest"]["qty"] == 5
    sub = order_submit.run(pcb, fab, quote=None, qty=5)
    assert sub["status"] == "ready_for_human"
    assert sub["artifacts"]["gerber_zip"]["sha256"] == man["gerber_zip_sha256"]
    assert sub["api"]["available"] is False        # no credentials configured


@pytest.mark.smoke
def test_dfm_runtime_budget(fab_dirs):
    """The whole DFM pass stays well inside a gate's patience on the densest
    golden (rf4 carries ~90 vias and a coplanar pour)."""
    d, _ = fab_dirs["rf4"]
    t0 = time.time()
    dfm_check.run(board_path("rf4"), fab_dir=d,
                  schematic=GOLDEN / "rf4" / "rf4.kicad_sch")
    assert time.time() - t0 < 30.0


def test_mouse_bite_rows_are_not_hole_pairs():
    """JLC asks for 0.5-0.8 mm unplated holes 0.2-0.3 mm apart on a tab; a
    plated pair, or a tighter or odd-sized row, is still held to the rule."""
    from types import SimpleNamespace as H
    from dfm_check import _mouse_bite_pair as pair
    bite = H(plated=False, diameter=0.5)
    assert pair(bite, H(plated=False, diameter=0.5))
    assert not pair(bite, H(plated=True, diameter=0.5))
    assert not pair(bite, H(plated=False, diameter=0.6))
    assert not pair(H(plated=False, diameter=0.4), H(plated=False, diameter=0.4))


def test_dfm_2oz_outer_floor_holds_outer_copper_only(tmp_path):
    """#627: JLC's 2 oz figure holds F.Cu/B.Cu; a 0.5 oz inner layer at the same
    gap passes on the row's own floor."""
    from shapely.geometry import box

    class Layer:
        def __init__(self):
            self.gap = 0.12

        def components(self):
            return [box(0, 0, 5, 1), box(0, 1 + self.gap, 5, 2)]

    class Fab:
        def copper_layer_names(self):
            return ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

        def copper(self, name):
            return Layer()

    rows = yaml.safe_load((REFERENCE / "jlc_capabilities.yaml").read_text(
        encoding="utf-8"))["design_rules"]
    vios: list = []
    dfm_check.check_clearance(Fab(), rows["4layer_2oz"], vios)
    assert sorted(v["layer"] for v in vios) == ["B.Cu", "F.Cu"]
    assert {v["min_mm"] for v in vios} == {0.15}
    vios = []
    dfm_check.check_clearance(Fab(), rows["4layer_1oz"], vios)
    assert vios == []
