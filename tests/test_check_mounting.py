"""check_mounting: a big board with no mounting hole (#1088). Known answers
on small planted-fault boards: the fault and its quiet twins (holes,
declared, castellated, edge card, small board)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_mounting  # noqa: E402
import checklib  # noqa: E402
import constraints_lint  # noqa: E402

LAYERS = """(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (13 "F.Paste" user)
 (15 "B.Paste" user) (5 "F.SilkS" user) (7 "B.SilkS" user)
 (1 "F.Mask" user) (3 "B.Mask" user) (25 "Edge.Cuts" user))"""


def _board(tmp_path, w, h, *items, extra_edge="", name="b") -> Path:
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20241229) (generator "pcbnew")\n'
        f'(general (thickness 1.6)) (paper "A4")\n{LAYERS}\n(setup)\n'
        '(net 0 "") (net 1 "N1") (net 2 "GND")\n'
        f'(gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1) '
        '(type solid)) (fill no) (layer "Edge.Cuts"))\n' + extra_edge
        + "".join(items) + ")\n", encoding="utf-8")
    return pcb


def _hole(x, y, ref="H1", lib="MountingHole:MountingHole_3.2mm_M3",
          drill=3.2, extra_pad="") -> str:
    return (f'(footprint "{lib}" (layer "F.Cu") (at {x} {y})\n'
            f' (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS"))\n'
            f' (pad "" np_thru_hole circle (at 0 0) (size {drill} {drill}) '
            f'(drill {drill}) (layers "*.Cu" "*.Mask")){extra_pad})\n')


def _part(x, y, ref="R1") -> str:
    return (f'(footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") '
            f'(at {x} {y})\n (property "Reference" "{ref}" (at 0 -1.5 0) '
            '(layer "F.SilkS"))\n'
            ' (pad "1" smd rect (at -0.775 0) (size 0.8 0.95) '
            '(layers "F.Cu" "F.Paste" "F.Mask") (net "N1"))\n'
            ' (pad "2" smd rect (at 0.775 0) (size 0.8 0.95) '
            '(layers "F.Cu" "F.Paste" "F.Mask") (net "GND")))\n')


def _check(pcb, cons=None):
    vios, seen = check_mounting.check(pcb, cons)
    return [v["kind"] for v in vios], seen


def test_big_board_with_no_hole_warns(tmp_path):
    kinds, seen = _check(_board(tmp_path, 100, 60, _part(50, 30)))
    assert kinds == ["no_mounting_holes"] and seen["judged"]


def test_long_thin_board_is_judged_by_its_side(tmp_path):
    assert _check(_board(tmp_path, 90, 20, _part(50, 10)))[0] \
        == ["no_mounting_holes"]


def test_small_board_is_not_judged(tmp_path):
    kinds, seen = _check(_board(tmp_path, 45, 40, _part(20, 20)))
    assert kinds == [] and not seen["judged"]


def test_mounting_hole_footprints_pass(tmp_path):
    # the quiet twin of the planted fault: the same board with M3 holes
    pcb = _board(tmp_path, 100, 60, _part(50, 30), _hole(4, 4),
                 _hole(96, 56, ref="H2"))
    kinds, seen = _check(pcb)
    assert kinds == [] and len(seen["mounting_holes"]) == 2


def test_lone_unnamed_hole_and_edge_cuts_window_pass(tmp_path):
    lone = _board(tmp_path, 100, 60, _part(50, 30),
                  _hole(5, 5, ref="X9", lib="Misc:Hole"), name="l")
    win = _board(tmp_path, 100, 60, _part(50, 30), name="w", extra_edge=(
        '(gr_circle (center 5 5) (end 6.6 5) (stroke (width 0.1) '
        '(type solid)) (fill no) (layer "Edge.Cuts"))\n'))
    assert _check(lone)[0] == []
    kinds, seen = _check(win)
    assert kinds == [] and seen["mounting_holes"][0]["how"] == "Edge.Cuts window"


def test_a_part_with_its_own_screw_holes_is_not_a_mount(tmp_path):
    # a D-sub's "MountingHoles" pegs sit with its signal pins
    dsub = ('(footprint "Connector_Dsub:DSUB-9_Female_Horizontal_'
            'P2.77x2.84mm_EdgePinOffset7.70mm_Housed_MountingHolesOffset9.12mm" '
            '(layer "F.Cu") (at 50 30)\n (property "Reference" "J1" (at 0 -3 0) '
            '(layer "F.SilkS"))\n'
            ' (pad "0" thru_hole circle (at -12.5 0) (size 4 4) (drill 3.2) '
            '(layers "*.Cu" "*.Mask") (net "GND"))\n'
            ' (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 1) '
            '(layers "*.Cu" "*.Mask") (net "N1")))\n')
    assert _check(_board(tmp_path, 100, 60, dsub))[0] == ["no_mounting_holes"]


def test_declared_mounting_passes_and_bad_declaration_errors(tmp_path):
    pcb = _board(tmp_path, 100, 60, _part(50, 30))
    ok = {"mechanical": {"mounting": {"by": "rail", "why": "slides into "
                                      "the case's side tabs"}}}
    kinds, seen = _check(pcb, ok)
    assert kinds == [] and seen["mounts_by"]["by"] == "rail"
    with pytest.raises(checklib.CheckError):
        _check(pcb, {"mechanical": {"mounting": {"by": "glue"}}})


def test_edge_card_fingers_pass(tmp_path):
    fingers = "".join(
        f' (pad "{n}" connect rect (at {n * 1.0} 0) (size 0.7 3) '
        f'(layers "F.Cu" "F.Mask") (net "N1"))\n' for n in range(8))
    card = ('(footprint "Custom:Card_Fingers" (layer "F.Cu") (at 40 58)\n'
            ' (property "Reference" "J1" (at 0 -3 0) (layer "F.SilkS"))\n'
            + fingers + ")\n")
    kinds, seen = _check(_board(tmp_path, 100, 60, card))
    assert kinds == [] and seen["mounts_by"]["by"] == "edge_card"


def test_mounts_by_list_matches_the_lint():
    assert tuple(constraints_lint.MOUNTS_BY) == check_mounting.MOUNTS_BY


def test_cli_contract(tmp_path):
    pcb = _board(tmp_path, 100, 60, _part(50, 30))
    out = tmp_path / "r.json"
    assert check_mounting.main(["--pcb", str(pcb), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert rep["script"] == "check_mounting" and rep["status"] == "violations"
