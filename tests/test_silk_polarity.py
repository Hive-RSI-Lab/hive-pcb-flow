"""check_silk's SILK POLARITY rule: a polarity mark on silk (a diode's band,
a cap's "+", an IC's pin-1 dot, a two-row part's outline notch) must sit by
the pad the board says is cathode, + or pin 1, read from the board alone.

The planted fault is a diode's band flipped to its anode; its quiet twin is
the same board with the band by the cathode. Parts whose mark or pad roles
aren't known, and marks that aren't pin-1 marks (a seven-segment display's
decimal point), are not_judged with why, never a finding. A KiCad 5 board's
`module` parts are read the same way as `footprint` ones.
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


@pytest.mark.parametrize("sx", [1, -1])
def test_an_imported_led_named_only_led_is_not_judged(sx, tmp_path):
    """An LCSC LED-SMD_... footprint, pads unnamed: KiCad's pin 1 = K is only
    a guess for it (hifi-receiver-2's has its anode on pad 1), so its cut
    corner is not judged at either end, rather than called wrong."""
    pads = [("1", 0.75, 0), ("2", -0.75, 0)]
    cut = [(-1.7, -0.4, -1.7, 0.4), (-1.7, 0.4, -1.4, 0.7),
           (-1.4, -0.7, -1.7, -0.4), (-1.4, 0.7, -0.3, 0.7),
           (-0.3, -0.7, -1.4, -0.7), (0.2, -0.7, 1.47, -0.7),
           (1.47, -0.7, 1.47, 0.7), (1.47, 0.7, 0.2, 0.7)]
    refs, fact = _polarity(_board(tmp_path, _fp(
        "D3", "X:LED-SMD_L1.6-W0.8-R-RD", pads, _lines(cut, sx=sx),
        size=(0.6, 0.8))))
    assert refs == [] and fact["ok"] == 0 and fact["not_judged"] == 1
    (row,) = fact["not_judged_refs"]
    assert "guessed" in row["why"]


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


# A KiCad 5 board writes its parts as `module` blocks, pads with numbered
# nets and unquoted names, and arcs as centre + start + sweep. This SOT-23-6
# is CMM2-JLC rev A's U3 (an SRV05-4) as its board file has it, at 180
# degrees, with a pin-1 dot added: by pad 1 (the quiet twin) or by pad 6
# (the planted fault).
K5_SOT236 = """  (module Package_TO_SOT_SMD:SOT-23-6 (layer F.Cu) (tedit 5A02FF57) (tstamp 5F04A80D)
    (at 38.5 34.6 180)
    (attr smd)
    (fp_text reference U3 (at 0 -2.9) (layer F.SilkS)
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value SRV05-4 (at 0 2.9) (layer F.Fab)
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_line (start -0.9 1.61) (end 0.9 1.61) (layer F.SilkS) (width 0.12))
    (fp_line (start 0.9 -1.61) (end -1.55 -1.61) (layer F.SilkS) (width 0.12))
    {dot}
    (pad 5 smd rect (at 1.1 0 180) (size 1.06 0.65) (layers F.Cu F.Paste F.Mask) (net 146 +5V))
    (pad 6 smd rect (at 1.1 -0.95 180) (size 1.06 0.65) (layers F.Cu F.Paste F.Mask) (net 203 /USB_HOST_D-))
    (pad 4 smd rect (at 1.1 0.95 180) (size 1.06 0.65) (layers F.Cu F.Paste F.Mask) (net 202 /USB_HOST_D+))
    (pad 3 smd rect (at -1.1 0.95 180) (size 1.06 0.65) (layers F.Cu F.Paste F.Mask) (net 200 /USB_C_D+))
    (pad 2 smd rect (at -1.1 0 180) (size 1.06 0.65) (layers F.Cu F.Paste F.Mask) (net 1 GND))
    (pad 1 smd rect (at -1.1 -0.95 180) (size 1.06 0.65) (layers F.Cu F.Paste F.Mask) (net 201 /USB_C_D-))
  )
"""
K5_DOT = {"pad_1": "(fp_circle (center -1.75 -1.5) (end -1.65 -1.5) (layer F.SilkS) (width 0.12))",
          "pad_6": "(fp_circle (center 1.75 -1.5) (end 1.85 -1.5) (layer F.SilkS) (width 0.12))",
          # the same dot by pad 1, drawn as a closed KiCad 5 arc
          "arc_pad_1": "(fp_arc (start -1.75 -1.5) (end -1.65 -1.5) (angle 360) (layer F.SilkS) (width 0.12))",
          "none": ""}


def _k5_board(tmp_path, *modules, name="k5.kicad_pcb"):
    pcb = tmp_path / name
    pcb.write_text(
        "(kicad_pcb (version 20171130) (host pcbnew 5.1.6)\n"
        "  (layers (0 F.Cu signal) (31 B.Cu signal) (37 F.SilkS user) (44 Edge.Cuts user))\n"
        "  (gr_line (start 0 0) (end 60 0) (layer Edge.Cuts) (width 0.1))\n"
        "  (gr_line (start 60 0) (end 60 50) (layer Edge.Cuts) (width 0.1))\n"
        "  (gr_line (start 60 50) (end 0 50) (layer Edge.Cuts) (width 0.1))\n"
        "  (gr_line (start 0 50) (end 0 0) (layer Edge.Cuts) (width 0.1))\n"
        + "".join(modules) + ")\n", encoding="utf-8")
    return pcb


def test_planted_kicad5_pin_1_dot_by_pad_6_is_an_error(tmp_path):
    """The planted fault on a KiCad 5 board: the SOT-23-6's dot by pad 6."""
    pcb = _k5_board(tmp_path, K5_SOT236.format(dot=K5_DOT["pad_6"]))
    payload, _ = cs.run(["--pcb", str(pcb)])
    hits = [v for v in payload["violations"] if v["kind"] == KIND]
    assert [(v["severity"], v["refs"]) for v in hits] == [("error", ["U3"])]
    assert "pad 6" in hits[0]["msg"]
    assert payload["polarity_marks"]["wrong"] == 1


@pytest.mark.parametrize("dot", ["pad_1", "arc_pad_1"])
def test_quiet_twin_kicad5_dot_by_pad_1_is_clean(dot, tmp_path):
    """The quiet twin: the same KiCad 5 part with its dot by pad 1 (a circle
    or a closed arc) is one ok part, not a not_judged board."""
    payload, _ = cs.run(["--pcb", str(_k5_board(tmp_path, K5_SOT236.format(dot=K5_DOT[dot])))])
    assert [v for v in payload["violations"] if v["kind"] == KIND] == []
    assert payload["polarity_marks"]["ok"] == 1
    assert "not_judged_board" not in payload["polarity_marks"]


def test_a_kicad5_part_with_no_mark_is_not_judged_not_passed(tmp_path):
    """CMM2-JLC rev A's U3 as drawn: only KiCad's long silk edge, no dot."""
    refs, fact = _polarity(_k5_board(tmp_path, K5_SOT236.format(dot=K5_DOT["none"])))
    assert refs == [] and fact["ok"] == 0 and fact["not_judged"] == 1


def test_a_kicad5_module_reads_as_its_kicad10_footprint(tmp_path):
    """The same bottom-side diode as a KiCad 5 module and as the KiCad 10
    footprint KiCad converts it to: the same pads, nets, side and silk
    (the arc's mid and end worked out from its centre and sweep)."""
    import cpl_verify as cv
    k5 = _k5_board(tmp_path, """  (module Diode_SMD:D_SOD-123 (layer B.Cu) (at 20 20 90)
    (fp_text reference D7 (at 0 2) (layer B.SilkS) (effects (font (size 1 1)) (justify mirror)))
    (fp_line (start -2.36 1) (end 1.65 1) (layer B.SilkS) (width 0.12))
    (fp_arc (start 0 0) (end 2 0) (angle 90) (layer B.SilkS) (width 0.12))
    (pad 1 smd rect (at -1.65 0 90) (size 0.9 1.2) (layers B.Cu B.Paste B.Mask) (net 4 GND))
    (pad 2 smd rect (at 1.65 0 90) (size 0.9 1.2) (layers B.Cu B.Paste B.Mask) (net 5 "Net-(D7-Pad2)"))
  )
""")
    k10 = tmp_path / "k10.kicad_pcb"
    k10.write_text(
        '(kicad_pcb (version 20260206) (generator "pcbnew")\n'
        '(footprint "Diode_SMD:D_SOD-123" (layer "B.Cu") (at 20 20 90)\n'
        '  (property "Reference" "D7" (at 0 2 90) (layer "B.SilkS"))\n'
        '  (fp_line (start -2.36 1) (end 1.65 1) (stroke (width 0.12)) (layer "B.SilkS"))\n'
        '  (fp_arc (start 2 0) (mid 1.414214 1.414214) (end 0 2) (stroke (width 0.12)) (layer "B.SilkS"))\n'
        '  (pad "1" smd rect (at -1.65 0 90) (size 0.9 1.2) (layers "B.Cu" "B.Paste" "B.Mask") (net "GND"))\n'
        '  (pad "2" smd rect (at 1.65 0 90) (size 0.9 1.2) (layers "B.Cu" "B.Paste" "B.Mask") (net "Net-(D7-Pad2)"))\n'
        '))\n', encoding="utf-8")
    a, b = cv.board_footprints(k5)["D7"], cv.board_footprints(k10)["D7"]
    assert (a.side, a.rot, a.x, a.y) == (b.side, b.rot, b.x, b.y) == ("bottom", 90.0, 20.0, 20.0)
    assert [(p.number, p.x, p.y, p.net) for p in a.pads] == \
        [(p.number, p.x, p.y, p.net) for p in b.pads] == \
        [("1", -1.65, 0.0, "GND"), ("2", 1.65, 0.0, "Net-(D7-Pad2)")]
    rnd = lambda fp: sorted(tuple(round(v, 4) for pt in s for v in pt) for s in fp.silk)
    assert rnd(a) == rnd(b)


def test_a_kicad5_arc_sweeps_clockwise_on_screen():
    """KiCad 5's (start CENTRE) (end START) (angle SWEEP): a positive sweep
    turns clockwise on screen, i.e. toward +y from +x with y down."""
    import cpl_verify as cv
    import sexpdata
    a, m, e = cv._arc3(sexpdata.loads("(fp_arc (start 1 1) (end 3 1) (angle 90))"))
    assert a == (3.0, 1.0)
    assert m == pytest.approx((1 + 2 ** 0.5, 1 + 2 ** 0.5))
    assert e == pytest.approx((1.0, 3.0))
