"""check_silk's SILK POLARITY rule: a polarity mark on silk (a diode's band,
a cap's "+", an IC's pin-1 dot, a two-row part's outline notch) must sit by
the pad the board says is cathode, + or pin 1, read from the board alone.

The planted fault is a diode's band flipped to its anode; its quiet twin is
the same board with the band by the cathode. Parts whose mark or pad roles
aren't known, and marks that aren't pin-1 marks (a seven-segment display's
decimal point), are not_judged with why, never a finding.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import check_silk as cs  # noqa: E402

KIND = "silk_polarity_wrong"


def _lines(segs, layer="F.SilkS", sx=1):
    return "".join(f'(fp_line (start {sx * a} {b}) (end {sx * c} {d}) '
                   f'(stroke (width 0.12)) (layer "{layer}"))'
                   for a, b, c, d in segs)


def _dot(x, y, r=0.1):
    return (f'(fp_circle (center {x} {y}) (end {x + r} {y}) '
            f'(stroke (width 0.12)) (layer "F.SilkS"))')


def _fp(ref, fpid, pads, silk, size=(0.9, 1.2), x=20.0, y=20.0):
    pad_txt = "".join(
        f'(pad "{n}" smd rect (at {px} {py}) (size {size[0]} {size[1]}) '
        f'(layers "F.Cu" "F.Mask")'
        + (f' (pinfunction "{fn}")' if fn else "") + ")"
        for n, px, py, *rest in pads for fn in [rest[0] if rest else None])
    return (f'(footprint "{fpid}" (layer "F.Cu") (at {x} {y}) '
            f'(property "Reference" "{ref}" (at 0 -4) (layer "F.Fab")) '
            f'{silk} {pad_txt})')


def _board(tmp_path, *fps):
    pcb = tmp_path / "p.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) '
        '(44 "Edge.Cuts" user))\n'
        '(gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill none) '
        '(layer "Edge.Cuts"))\n' + "\n".join(fps) + ")\n", encoding="utf-8")
    return pcb


def _polarity(pcb):
    vios, fact = cs.check_polarity_marks(pcb)
    return [v["refs"][0] for v in vios if v["kind"] == KIND], fact


# A SOD-123 diode, pads named by pin function: pad 1 is the cathode. The
# outline is open at pad 2's end and closed by the band at pad 1's.
SOD = [("1", -1.65, 0, "K"), ("2", 1.65, 0, "A")]
SOD_BAND = [(-2.36, -1, -2.36, 1), (-2.36, -1, 1.65, -1), (-2.36, 1, 1.65, 1)]


def _diode(sx=1, fpid="X:SOD123"):
    return _fp("D1", fpid, SOD, _lines(SOD_BAND, sx=sx))


def test_planted_band_flipped_to_the_anode_is_an_error(tmp_path):
    """The planted fault: the band drawn by pad 2, the anode."""
    payload, _ = cs.run(["--pcb", str(_board(tmp_path, _diode(sx=-1)))])
    hits = [v for v in payload["violations"] if v["kind"] == KIND]
    assert [(v["severity"], v["refs"]) for v in hits] == [("error", ["D1"])]
    assert hits[0]["pin"] == "1" and hits[0]["mark"] == "band"
    assert "pad 2" in hits[0]["msg"]
    assert payload["status"] == "violations"
    assert payload["polarity_marks"]["wrong"] == 1


def test_quiet_twin_band_by_the_cathode_is_clean(tmp_path):
    payload, _ = cs.run(["--pcb", str(_board(tmp_path, _diode()))])
    assert [v for v in payload["violations"] if v["kind"] == KIND] == []
    assert payload["polarity_marks"]["ok"] == 1
    assert payload["polarity_marks"]["wrong"] == 0


def test_band_flip_is_the_only_difference(tmp_path):
    """Only the band's end changes between twin and fault."""
    assert _polarity(_board(tmp_path, _diode()))[0] == []
    assert _polarity(_board(tmp_path, _diode(sx=-1)))[0] == ["D1"]


def test_kicad_name_gives_the_cathode_without_pin_functions(tmp_path):
    """No pin functions: a KiCad D_ footprint's pad 1 is K."""
    pads = [(n, x, y) for n, x, y, _ in SOD]
    good = _fp("D1", "Diode_SMD:D_SOD-123", pads, _lines(SOD_BAND))
    bad = _fp("D1", "Diode_SMD:D_SOD-123", pads, _lines(SOD_BAND, sx=-1))
    assert _polarity(_board(tmp_path, good))[0] == []
    assert _polarity(_board(tmp_path, bad))[0] == ["D1"]


def test_unknown_pad_roles_are_not_judged(tmp_path):
    """A diode whose pads say nothing about which is the cathode."""
    pads = [(n, x, y) for n, x, y, _ in SOD]
    refs, fact = _polarity(_board(tmp_path, _fp(
        "D1", "X:DIODE", pads, _lines(SOD_BAND, sx=-1))))
    assert refs == [] and fact["not_judged"] == 1
    (row,) = fact["not_judged_refs"]
    assert row["refs"] == ["D1"] and "not known" in row["why"]


# An electrolytic: pad 1 is +, and a "+" is drawn beside it.
CPELEC = [("1", -1.8, 0), ("2", 1.8, 0)]
CPELEC_PLUS = [(-3, -1.56, -2.5, -1.56), (-2.75, -1.81, -2.75, -1.31)]
CPELEC_BODY = [(-2.26, -1.195563, -2.26, -1.06), (-2.26, -1.195563, -1.195563, -2.26),
               (-2.26, 1.195563, -2.26, 1.06), (-2.26, 1.195563, -1.195563, 2.26),
               (-1.195563, -2.26, 2.26, -2.26), (-1.195563, 2.26, 2.26, 2.26),
               (2.26, -2.26, 2.26, -1.06), (2.26, 2.26, 2.26, 1.06)]


@pytest.mark.parametrize("sx,want", [(1, []), (-1, ["C1"])],
                         ids=["plus_by_plus_pad", "plus_by_minus_pad"])
def test_a_caps_plus_on_the_wrong_pad(sx, want, tmp_path):
    silk = _lines(CPELEC_PLUS, sx=sx) + _lines(CPELEC_BODY)
    pcb = _board(tmp_path, _fp("C1", "Capacitor_SMD:CP_Elec_4x5.4", CPELEC,
                               silk, size=(2.6, 1.6)))
    assert _polarity(pcb)[0] == want


# A four-pad IC with pin 1 at the lower left and one dot.
QUAD = [("1", -1.0, 1.3), ("2", -1.0, -1.3), ("3", 1.0, -1.3), ("4", 1.0, 1.3)]


@pytest.mark.parametrize("dot,want", [((-1.6, 1.9), []), ((1.6, -1.9), ["U1"])],
                         ids=["dot_at_pin_1", "dot_at_the_far_corner"])
def test_an_ic_dot_at_the_wrong_corner(dot, want, tmp_path):
    pcb = _board(tmp_path, _fp("U1", "X:IC4", QUAD, _dot(*dot)))
    refs, fact = _polarity(pcb)
    assert refs == want and fact["wrong"] == len(want)


def test_two_dots_say_nothing_sure_about_pin_1(tmp_path):
    """Two mark-like shapes, one by pin 1: not a finding, not_judged."""
    pcb = _board(tmp_path, _fp("U1", "X:IC4", QUAD,
                               _dot(-1.6, 1.9) + _dot(1.6, -1.9)))
    refs, fact = _polarity(pcb)
    assert refs == [] and fact["not_judged"] == 1
    assert "2 mark-like shapes" in fact["not_judged_refs"][0]["why"]


# An SOIC-8 with no dot: its outline notch marks pin 1's end of the rows.
SOIC = ([(str(i + 1), -2.7, -1.905 + 1.27 * i) for i in range(4)]
        + [(str(8 - i), 2.7, -1.905 + 1.27 * i) for i in range(4)])
SOIC_BODY = [(-1.95, -2.5, -0.5, -2.5), (0.5, -2.5, 1.95, -2.5),
             (-1.95, 2.5, -0.5, 2.5), (0.5, 2.5, 1.95, 2.5)]


def _notch(sy):
    return (f'(fp_arc (start -0.5 {sy * 2.5}) (mid 0 {sy * 2.0}) '
            f'(end 0.5 {sy * 2.5}) (stroke (width 0.12)) (layer "F.SilkS"))')


@pytest.mark.parametrize("sy,want", [(-1, []), (1, ["U1"])],
                         ids=["notch_at_pin_1s_end", "notch_at_the_far_end"])
def test_an_outline_notch_at_the_wrong_end(sy, want, tmp_path):
    body = _lines(SOIC_BODY)
    pcb = _board(tmp_path, _fp("U1", "Package_SO:SOIC-8", SOIC,
                               body + _notch(sy), size=(1.5, 0.6)))
    refs, fact = _polarity(pcb)
    assert refs == want
    if want:
        (v,), _ = cs.check_polarity_marks(pcb)
        assert v["mark"] == "notch" and "away from pin 1" in v["msg"]


# A ten-pin seven-segment display: its decimal point is a dot by pad 5, a
# corner pad, but it is part of the figure drawn inside the pads (training
# boards' 7-segment displays read this way before the figure rule).
DISPLAY = ([(str(i + 1), -5 + 2.5 * i, 3) for i in range(5)]
           + [(str(10 - i), -5 + 2.5 * i, -3) for i in range(5)])
EIGHT = [(-1, -2, 1, -2), (1, -2, 1, 0), (1, 0, 1, 2), (1, 2, -1, 2),
         (-1, 2, -1, 0), (-1, 0, -1, -2), (-1, 0, 1, 0)]


@pytest.mark.parametrize("figure", [True, False], ids=["display", "no_figure"])
def test_a_displays_decimal_point_is_not_a_pin_1_mark(figure, tmp_path):
    silk = (_lines(EIGHT) if figure else "") + _dot(4.0, 2.0, 0.2)
    pcb = _board(tmp_path, _fp("U1", "X:7SEG", DISPLAY, silk))
    refs, fact = _polarity(pcb)
    if figure:
        assert refs == [] and fact["not_judged"] == 1
        assert "figure" in fact["not_judged_refs"][0]["why"]
    else:
        # the same dot with no figure round it is an ordinary wrong dot
        assert refs == ["U1"]


def test_a_dot_by_a_mid_row_pad_is_not_judged(tmp_path):
    """One mark by pad 4, mid-row: a tick, not a corner's pin-1 mark."""
    pcb = _board(tmp_path, _fp("U1", "X:IC10", DISPLAY, _dot(2.5, 3.8)))
    refs, fact = _polarity(pcb)
    assert refs == [] and "mid-row" in fact["not_judged_refs"][0]["why"]


def test_unpolarised_parts_are_left_out(tmp_path):
    """A resistor has no polarity: neither counted nor not_judged."""
    pcb = _board(tmp_path, _fp("R1", "Resistor_SMD:R_0603", [
        ("1", -0.8, 0), ("2", 0.8, 0)], _lines(SOD_BAND)))
    refs, fact = _polarity(pcb)
    assert refs == [] and fact == {"ok": 0, "wrong": 0, "not_judged": 0,
                                   "not_judged_refs": []}


def test_a_kicad5_board_is_not_judged_not_passed(tmp_path):
    """The part reader reads `footprint` blocks only: a KiCad 5 board's
    `module` blocks give it nothing, which must not read as a clean pass."""
    pcb = tmp_path / "k5.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20171130) (host pcbnew 5.1.6)\n'
        '  (module Diode_SMD:D_SOD-123 (layer F.Cu) (at 20 20)\n'
        '    (fp_text reference D1 (at 0 -2) (layer F.SilkS))\n'
        '    (pad 1 smd rect (at -1.65 0) (size 0.9 1.2) (layers F.Cu))\n'
        '    (pad 2 smd rect (at 1.65 0) (size 0.9 1.2) (layers F.Cu)))\n'
        ')\n', encoding="utf-8")
    refs, fact = _polarity(pcb)
    assert refs == [] and fact["ok"] == 0 and fact["not_judged"] == 1
    assert "KiCad 5" in fact["not_judged_board"]
