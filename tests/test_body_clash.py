"""check_body_clash (#608): 3D bodies against each other, their own courtyards,
the outline and mounting holes. Synthetic models; the known answer from a real
board is the shape of hifi core's RCA jack, an ear past a too-small courtyard.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_body_clash as cbc  # noqa: E402

IN = 2.54


def _wrl(path: Path, boxes, close=True) -> Path:
    shapes = []
    quads = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6),
             (0, 2, 6, 4), (1, 3, 7, 5)]
    if not close:
        quads = quads[:1]
    for x0, y0, z0, x1, y1, z1 in boxes:
        pts = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        p = ", ".join(" ".join(f"{v / IN:.6f}" for v in q) for q in pts)
        idx = ", ".join(",".join(str(i) for i in q) + ",-1" for q in quads)
        shapes.append("Shape { geometry IndexedFaceSet {\n coord Coordinate {"
                      f" point [ {p} ] }}\n coordIndex [ {idx} ]\n}} }}\n")
    path.write_text("#VRML V2.0 utf8\n" + "".join(shapes), encoding="utf-8")
    return path


def _fp(ref, x, y, model, *, angle=0, back=False, cy=None, pad=True, name="lib:P",
        extra=""):
    side = "B" if back else "F"
    court = ""
    if cy is not None:
        court = (f'(fp_rect (start {-cy} {-cy}) (end {cy} {cy}) (layer "{side}.CrtYd")'
                 ' (stroke (width 0.05)))')
    pads = f'(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "{side}.Cu"))' if pad else ""
    mod = f'(model "{model}" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))' \
        if model else ""
    return (f'(footprint "{name}" (layer "{side}.Cu") (at {x} {y} {angle})\n'
            f' (property "Reference" "{ref}" (at 0 0) (layer "{side}.SilkS"))\n'
            f' {court} {pads} {extra} {mod})\n')


def _board(tmp_path, *fps):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator test)\n (general (thickness 1.6))\n'
        ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        ' (gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts") (stroke (width 0.1)))\n'
        + "".join(fps) + ")\n", encoding="utf-8")
    return pcb


def _kinds(pcb, cons=None):
    viols, facts = cbc.check(pcb, cons)
    return sorted(v["kind"] for v in viols), viols, facts


# a jack: body +-8, and an ear reaching x +12 at 11-16 mm up, as on hifi core
JACK = [(-8, -8, 0, 8, 8, 20), (8, -2, 11, 12, 2, 16)]
POST = [(-4, -4, 0, 4, 4, 15)]


def test_the_ear_hits_the_neighbour_a_2d_check_cant_see(tmp_path):
    jack = _wrl(tmp_path / "jack.wrl", JACK)
    post = _wrl(tmp_path / "post.wrl", POST)
    # courtyards +-8 and +-4 stand 2 mm apart; the ear runs 2 mm into the post
    pcb = _board(tmp_path, _fp("J1", 50, 50, jack, cy=8),
                 _fp("J2", 64, 50, post, cy=4))
    kinds, viols, _ = _kinds(pcb)
    assert "body_clash" in kinds
    v = next(x for x in viols if x["kind"] == "body_clash")
    assert v["severity"] == "error" and v["refs"] == ["J1", "J2"]
    assert v["overlap"]["x"] == pytest.approx([60.0, 62.0], abs=0.25)
    assert v["overlap"]["z"] == pytest.approx([11.0, 15.0], abs=0.25)
    assert v["depth_mm"] == pytest.approx(2.0, abs=0.25)
    # and the cause: the courtyard left the ear out
    past = next(x for x in viols if x["kind"] == "body_past_courtyard")
    assert past["refs"] == ["J1"] and past["past_mm"] == pytest.approx(4.0, abs=0.2)


def test_moving_the_part_clears_it(tmp_path):
    jack = _wrl(tmp_path / "jack.wrl", JACK)
    post = _wrl(tmp_path / "post.wrl", POST)
    pcb = _board(tmp_path, _fp("J1", 50, 50, jack), _fp("J2", 67, 50, post))
    kinds, _, facts = _kinds(pcb)
    assert "body_clash" not in kinds and "body_tight" not in kinds
    assert facts["bodies"] == 2


def test_two_faces_touching_are_not_a_clash(tmp_path):
    a = _wrl(tmp_path / "a.wrl", [(-2, -2, 0, 2, 2, 2)])
    pcb = _board(tmp_path, _fp("R1", 50, 50, a), _fp("R2", 54, 50, a))
    kinds, _, _ = _kinds(pcb)
    assert "body_clash" not in kinds


def test_a_part_wholly_inside_another_is_a_clash(tmp_path):
    big = _wrl(tmp_path / "big.wrl", [(-5, -5, 0, 5, 5, 10)])
    small = _wrl(tmp_path / "small.wrl", [(-1, -1, 2, 1, 1, 4)])
    pcb = _board(tmp_path, _fp("U1", 50, 50, big), _fp("U2", 50, 50, small))
    kinds, viols, _ = _kinds(pcb)
    assert "body_clash" in kinds
    assert next(v for v in viols if v["kind"] == "body_clash")["depth_mm"] >= 1.5


def test_a_part_under_an_overhang_is_clear(tmp_path):
    # a low chip under the ear: the columns overlap, the solids don't
    jack = _wrl(tmp_path / "jack.wrl", JACK)
    chip = _wrl(tmp_path / "chip.wrl", [(-1, -1, 0, 1, 1, 2)])
    pcb = _board(tmp_path, _fp("J1", 50, 50, jack, cy=13), _fp("C1", 60, 50, chip))
    kinds, _, _ = _kinds(pcb)
    assert "body_clash" not in kinds


def test_a_pin_tail_meets_a_part_on_the_far_side(tmp_path):
    hdr = _wrl(tmp_path / "hdr.wrl", [(-0.4, -0.4, -5, 0.4, 0.4, 8)])
    chip = _wrl(tmp_path / "chip.wrl", [(-2, -2, 0, 2, 2, 2)])
    pcb = _board(tmp_path, _fp("J1", 50, 50, hdr), _fp("U1", 50, 50, chip, back=True))
    kinds, viols, _ = _kinds(pcb)
    assert "body_clash" in kinds
    z = next(v for v in viols if v["kind"] == "body_clash")["overlap"]["z"]
    assert z[0] == pytest.approx(-3.6, abs=0.25) and z[1] <= -1.5


def test_the_far_side_clear_of_a_short_tail_passes(tmp_path):
    hdr = _wrl(tmp_path / "hdr.wrl", [(-0.4, -0.4, -1.5, 0.4, 0.4, 8)])
    chip = _wrl(tmp_path / "chip.wrl", [(-2, -2, 0, 2, 2, 2)])
    pcb = _board(tmp_path, _fp("J1", 50, 50, hdr), _fp("U1", 50, 50, chip, back=True))
    kinds, _, _ = _kinds(pcb)
    assert "body_clash" not in kinds


def test_rotation_turns_the_ear(tmp_path):
    jack = _wrl(tmp_path / "jack.wrl", JACK)
    post = _wrl(tmp_path / "post.wrl", POST)
    # at 90 the footprint's +x points at board -y (file frame, Y down)
    pcb = _board(tmp_path, _fp("J1", 50, 50, jack, angle=90), _fp("J2", 50, 36, post))
    kinds, _, _ = _kinds(pcb)
    assert "body_clash" in kinds
    pcb = _board(tmp_path, _fp("J1", 50, 50, jack, angle=270), _fp("J2", 50, 36, post))
    kinds, _, _ = _kinds(pcb)
    assert "body_clash" not in kinds


def test_tall_parts_almost_touching_warn(tmp_path):
    a = _wrl(tmp_path / "a.wrl", [(-4, -4, 0, 4, 4, 12)])
    pcb = _board(tmp_path, _fp("J1", 50, 50, a), _fp("J2", 58.15, 50, a))
    kinds, viols, _ = _kinds(pcb)
    assert "body_tight" in kinds and "body_clash" not in kinds
    assert next(v for v in viols if v["kind"] == "body_tight")["gap_mm"] \
        == pytest.approx(0.15, abs=0.1)
    low = _wrl(tmp_path / "low.wrl", [(-4, -4, 0, 4, 4, 2)])
    pcb = _board(tmp_path, _fp("C1", 50, 50, low), _fp("C2", 58.15, 50, low))
    assert "body_tight" not in _kinds(pcb)[0]


def test_an_allowed_overlap_needs_its_why(tmp_path):
    big = _wrl(tmp_path / "big.wrl", [(-5, -5, 0, 5, 5, 10)])
    pcb = _board(tmp_path, _fp("BT1", 50, 50, big), _fp("J9", 52, 50, big))
    cons = {"mechanical": {"allow_body_overlap": [
        {"refs": ["J9", "BT1"], "why": "the cell sits in its holder"}]}}
    kinds, _, facts = _kinds(pcb, cons)
    assert "body_clash" not in kinds
    assert list(facts["allowed_overlaps"].values()) == ["the cell sits in its holder"]
    import checklib
    with pytest.raises(checklib.CheckError):
        cbc.check(pcb, {"mechanical": {"allow_body_overlap": [{"refs": ["J9", "BT1"]}]}})


def test_a_connector_mouth_past_the_edge_isnt_past_its_courtyard(tmp_path):
    sma = _wrl(tmp_path / "sma.wrl", [(-3, -3, 0, 12, 3, 6)])
    pcb = _board(tmp_path, _fp("J1", 95, 50, sma, cy=5.5))
    kinds, _, _ = _kinds(pcb)
    assert "body_past_courtyard" not in kinds and "body_off_board" not in kinds
    # the same overhang on something that isn't a connector
    pcb = _board(tmp_path, _fp("U1", 95, 50, sma, cy=20))
    assert "body_off_board" in _kinds(pcb)[0]


def test_an_overlong_model_ear_is_allowed_only_up_to_its_excess(tmp_path):
    """#829: the model's ear is wider than the datasheet body by a known
    amount; an overlap that deep is the model's error, a deeper one is real."""
    big = _wrl(tmp_path / "big.wrl", [(-5, -5, 0, 5, 5, 10)])
    ear = {"refs": ["J1", "J2"], "why": "datasheet body 10 mm; ear 0.6 mm over",
           "up_to_mm": 0.7}
    cons = {"mechanical": {"allow_body_overlap": [ear]}}
    near = _board(tmp_path, _fp("J1", 50, 50, big), _fp("J2", 59.5, 50, big))
    assert "body_clash" in _kinds(near)[0]                 # 0.5 mm, undeclared
    assert "body_clash" not in _kinds(near, cons)[0]
    deep = _board(tmp_path, _fp("J1", 50, 50, big), _fp("J2", 58, 50, big))
    assert "body_clash" in _kinds(deep, cons)[0]           # 2 mm: a real clash


def _val(v):
    return f'(property "Value" "{v}" (at 0 0) (layer "F.Fab"))'


def test_parts_meant_to_overhang_are_exempt_by_class_or_intent(tmp_path):
    """#830: an IR receiver and an antenna module overhang on purpose; the
    exemption is by part class or a declared why, never by ref."""
    sma = _wrl(tmp_path / "sma.wrl", [(-3, -3, 0, 12, 3, 6)])
    ir = _fp("U18", 95, 50, sma, cy=20, name="aiee:OPTO-TH_IRM-3638T",
             extra=_val("IRM-3638T"))
    esp = _fp("U7", 95, 20, sma, cy=20, name="aiee:WIFI-SMD_ESP32-WROVER-IE8MB",
              extra=_val("ESP32-WROVER-E-N8R8"))
    assert "body_off_board" not in _kinds(_board(tmp_path, ir, esp))[0]
    # planted: the same overhang on a part of neither class still warns,
    # whatever its ref
    for ref in ("U18", "U7"):
        other = _fp(ref, 95, 50, sma, cy=20, extra=_val("74HC595"))
        assert "body_off_board" in _kinds(_board(tmp_path, other))[0]
    # declared intent, with its why
    other = _fp("U3", 95, 50, sma, cy=20, extra=_val("74HC595"))
    cons = {"mechanical": {"allow_off_board": [
        {"refs": ["U3"], "why": "the sensor lens faces out"}]}}
    assert "body_off_board" not in _kinds(_board(tmp_path, other), cons)[0]
    import checklib
    with pytest.raises(checklib.CheckError):
        cbc.check(_board(tmp_path, other),
                  {"mechanical": {"allow_off_board": [{"refs": ["U3"]}]}})


def test_a_body_over_a_mounting_hole(tmp_path):
    chip = _wrl(tmp_path / "chip.wrl", [(-1, -1, 0, 1, 1, 2)])
    hole = '(pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu"))'
    # 1 mm of the chip inside the 5.76 mm head circle
    near = _fp("C1", 53.5, 50, chip)
    far = _fp("C1", 60, 50, chip)
    mh = _fp("H1", 50, 50, None, pad=False, extra=hole, name="lib:MountingHole_3.2mm")
    kinds, viols, _ = _kinds(_board(tmp_path, mh, near))
    assert "body_over_hole" in kinds
    assert next(v for v in viols if v["kind"] == "body_over_hole")["refs"] == ["C1", "H1"]
    assert "body_over_hole" not in _kinds(_board(tmp_path, mh, far))[0]
    # a high overhang only matters inside the drill's own width
    ear = _wrl(tmp_path / "ear.wrl", [(-1, -1, 8, 1, 1, 9)])
    kinds, _, _ = _kinds(_board(tmp_path, mh, _fp("U1", 53.5, 50, ear)))
    assert "body_over_hole" not in kinds
    kinds, _, _ = _kinds(_board(tmp_path, mh, _fp("U1", 51.5, 50, ear)))
    assert "body_over_hole" in kinds


def test_no_model_is_not_a_pass(tmp_path):
    pcb = _board(tmp_path, _fp("J1", 50, 50, tmp_path / "gone.wrl"), _fp("R1", 60, 50, None))
    kinds, viols, facts = _kinds(pcb)
    assert kinds == ["body_unjudged"] and viols[0]["refs"] == ["J1"]
    assert facts["not_judged"] == ["J1", "R1"] and facts["bodies"] == 0
    pcb2 = _board(tmp_path, _fp("J1", 50, 50, None), _fp("J2", 60, 50, None))
    v2 = cbc.check(pcb2)[0]
    assert len(v2) == 1 and v2[0]["refs"] == ["J1", "J2"]
    payload, _ = cbc.run(["--pcb", str(pcb)])
    assert payload["nothing_to_judge"]


def test_model_dir_finds_a_model_by_name(tmp_path):
    d = tmp_path / "other.3dshapes"
    d.mkdir()
    _wrl(d / "m.wrl", [(-1, -1, 0, 1, 1, 1)])
    pcb = _board(tmp_path, _fp("J1", 50, 50, "${KIPRJMOD}/nowhere/m.wrl"))
    assert cbc.check(pcb)[1]["bodies"] == 0
    assert cbc.check(pcb, None, [d])[1]["bodies"] == 1


def test_open_shells_still_clash(tmp_path):
    """Four walls and no lid or floor, as a STEP's curved parts read: open."""
    def walls(path, x0, y0, x1, y1, z1):
        pts = [(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        # gaps in the side walls too, so no closed shell survives
        quads = [(0, 1, 5, 4), (2, 3, 7, 6)]
        p = ", ".join(" ".join(f"{v / IN:.6f}" for v in q) for q in pts)
        idx = ", ".join(",".join(str(i) for i in q) + ",-1" for q in quads)
        path.write_text("#VRML V2.0 utf8\nShape { geometry IndexedFaceSet {\n"
                        f" coord Coordinate {{ point [ {p} ] }}\n"
                        f" coordIndex [ {idx} ]\n}} }}\n", encoding="utf-8")
        return path
    a = walls(tmp_path / "a.wrl", -2, -2, 2, 2, 4)
    solid = _wrl(tmp_path / "s.wrl", [(-2, -2, 0, 2, 2, 4)])
    # two open walls only bound along y, so the scan can't call it inside
    pcb = _board(tmp_path, _fp("U1", 50, 50, a))
    assert cbc.check(pcb)[1]["open_shells"] == ["U1"]
    closed = _board(tmp_path, _fp("U1", 50, 50, solid))
    assert cbc.check(closed)[1]["open_shells"] == []


def test_scan_fill_keeps_a_notch_out():
    g = np.zeros((12, 12, 12), dtype=bool)
    # a U seen from the side: floor and two uprights, open at the top
    g[1:11, 1:11, 1] = True
    g[1, 1:11, 1:11] = True
    g[10, 1:11, 1:11] = True
    g[1:11, 1, 1:11] = True
    g[1:11, 10, 1:11] = True
    f = cbc._scan_fill(g)
    assert not f[5, 5, 5]       # open upward: no surface above it
    g[1:11, 1:11, 10] = True
    f = cbc._scan_fill(g)
    assert f[5, 5, 5]


def test_rasterise_fills_a_box_exactly():
    tris = []
    x0, y0, z0, x1, y1, z1 = 0, 0, 0, 4, 2, 1
    pts = np.array([(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)],
                   dtype=np.float32)
    for q in ((0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6),
              (0, 2, 6, 4), (1, 3, 7, 5)):
        tris += [(pts[q[0]], pts[q[1]], pts[q[2]]), (pts[q[0]], pts[q[2]], pts[q[3]])]
    b = cbc.Body("X", False, (0, 0), np.array(tris), (0.0, 0.0, 0.0, 4.0, 2.0, 1.0))
    cbc.rasterise(b)
    assert b.sealed
    # 40 x 20 x 10 cells of solid, give or take a cell of surface each side
    assert 40 * 20 * 10 <= int(b.cells.sum()) <= 42 * 22 * 12
    assert not b.cells[0, 0, 0]


# --parts: a part whose drawing puts its front past the PCB edge line (the
# extraction's mechanical.edge.overhang_mm, as check_connector_fit reads it)

def _lcsc(code, prop="LCSC"):
    return f'(property "{prop}" "{code}" (at 0 0) (layer "F.Fab"))'


def _extraction(tmp_path, code, edge):
    d = tmp_path / "parts"
    d.mkdir(exist_ok=True)
    (d / f"{code}.json").write_text(
        json.dumps({"mechanical": {"edge": edge}}), encoding="utf-8")
    (d / "parts.json").write_text('{"parts": []}', encoding="utf-8")
    return d


SMA = [(-3, -3, 0, 12, 3, 6)]          # at x 95: 7 mm past the outline at 100


def test_a_part_drawn_past_the_edge_may_stand_out_that_far(tmp_path):
    sma = _wrl(tmp_path / "sma.wrl", SMA)
    pcb = _board(tmp_path, _fp("U5", 95, 50, sma, cy=5.5, extra=_lcsc("C123")))
    # without the drawing: past the outline and past its courtyard
    kinds = _kinds(pcb)[0]
    assert "body_off_board" in kinds and "body_past_courtyard" in kinds
    parts = _extraction(tmp_path, "C123", {"overhang_mm": 7.0})
    viols, facts = cbc.check(pcb, None, (), parts)
    kinds = sorted(v["kind"] for v in viols)
    assert "body_off_board" not in kinds and "body_past_courtyard" not in kinds
    assert facts["drawn_edges"] == {"U5": 7.0}
    # parts.json stands for its folder; the "LCSC Part" name and a lower-case
    # code are read as check_connector_fit reads them
    pcb2 = _board(tmp_path, _fp("U5", 95, 50, sma, cy=5.5,
                                extra=_lcsc("c123", "LCSC Part")))
    assert cbc.check(pcb2, None, (), parts / "parts.json")[1][
        "drawn_edges"] == {"U5": 7.0}


def test_planted_a_body_farther_out_than_its_drawing_still_warns(tmp_path):
    sma = _wrl(tmp_path / "sma.wrl", SMA)
    pcb = _board(tmp_path, _fp("U5", 95, 50, sma, cy=5.5, extra=_lcsc("C123")))
    parts = _extraction(tmp_path, "C123", {"overhang_mm": 6.0})
    viols = cbc.check(pcb, None, (), parts)[0]
    hit = [v for v in viols if v["kind"] == "body_off_board"]
    assert len(hit) == 1 and hit[0]["drawing_overhang_mm"] == 6.0
    assert "C123.json" in hit[0]["msg"]
    # the drawing's own tolerance widens the allowance
    parts = _extraction(tmp_path, "C123", {"overhang_mm": 6.0, "tol_mm": 1.5})
    assert "body_off_board" not in [
        v["kind"] for v in cbc.check(pcb, None, (), parts)[0]]


def test_parts_with_no_drawn_edge_are_judged_as_before(tmp_path):
    sma = _wrl(tmp_path / "sma.wrl", SMA)
    pcb = _board(tmp_path, _fp("U5", 95, 50, sma, cy=5.5, extra=_lcsc("C9")),
                 _fp("U6", 95, 20, sma, cy=5.5))
    for edge in ({"overhang_mm": 0}, {"overhang_mm": -1.0}):
        parts = _extraction(tmp_path, "C9", edge)
        viols, facts = cbc.check(pcb, None, (), parts)
        assert sorted(v["refs"][0] for v in viols
                      if v["kind"] == "body_off_board") == ["U5", "U6"]
        assert facts["drawn_edges"] == {}
    assert "drawn_edges" not in cbc.check(pcb)[1]


def test_a_parts_dir_or_drawing_it_cant_read_is_an_error(tmp_path):
    import checklib
    sma = _wrl(tmp_path / "sma.wrl", SMA)
    pcb = _board(tmp_path, _fp("U5", 95, 50, sma, cy=5.5, extra=_lcsc("C123")))
    with pytest.raises(checklib.CheckError, match="parts dir not found"):
        cbc.check(pcb, None, (), tmp_path / "nowhere")
    parts = _extraction(tmp_path, "C123", {"overhang_mm": "far"})
    with pytest.raises(checklib.CheckError, match="overhang_mm"):
        cbc.check(pcb, None, (), parts)


def test_cli_takes_parts(tmp_path):
    sma = _wrl(tmp_path / "sma.wrl", SMA)
    pcb = _board(tmp_path, _fp("U5", 95, 50, sma, cy=5.5, extra=_lcsc("C123")))
    parts = _extraction(tmp_path, "C123", {"overhang_mm": 7.0})
    out = tmp_path / "r.json"
    assert cbc.main(["--pcb", str(pcb), "--parts", str(parts),
                     "--out", str(out)]) == 0
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["drawn_edges"] == {"U5": 7.0}
    assert cbc.main(["--pcb", str(pcb), "--parts", str(tmp_path / "nowhere"),
                     "--out", str(out)]) == 2


# ------------------------------------------- raised parts (#1187)

def _tht(pins) -> str:
    return "".join(f'(pad "{i + 1}" thru_hole circle (at {x} {y}) (size 1.7 1.7)'
                   ' (drill 1.0) (layers "*.Cu"))' for i, (x, y) in enumerate(pins))


ROW = [(0, i * IN) for i in range(5)]                  # a 1x05 at 2.54
NANO = [(x, y) for x in (0, 6 * IN) for _, y in ROW]   # two rows 15.24 apart
SOCKET = [(-1.27, -4 * IN - 1.27, 0, 1.27, 1.27, 8.5)]   # model y is up
# a module's PCB as KiCad draws it, on the board
BOARD_PCB = [(-1.27, -4 * IN - 1.27, 0, 6 * IN + 1.27, 1.27, 1.6)]


def _plugged(tmp_path, under, module=True):
    """U1 on two PinSocket_1x05 strips J1 (x 40) and J2 (x 55.24); C1 stands
    between them under the module."""
    sock = _wrl(tmp_path / "sock.wrl", SOCKET)
    mod = _wrl(tmp_path / "nano.wrl", BOARD_PCB) if module else None
    cap = _wrl(tmp_path / "cap.wrl", under)
    sname = "Connector_PinSocket_2.54mm:PinSocket_1x05_P2.54mm_Vertical"
    return _board(
        tmp_path,
        _fp("J1", 40, 50, sock, pad=False, name=sname, extra=_tht(ROW)),
        _fp("J2", 40 + 6 * IN, 50, sock, pad=False, name=sname, extra=_tht(ROW)),
        _fp("U1", 40, 50, mod, pad=False, name="Module:Arduino_Nano",
            extra=_tht(NANO)),
        _fp("C1", 40 + 3 * IN, 50 + 2 * IN, cap))


def test_planted_tall_part_under_a_module_on_sockets_fails(tmp_path):
    # a 12 mm can under a module whose PCB stands 8.5 + 2.54 mm up
    pcb = _plugged(tmp_path, [(-3, -3, 0, 3, 3, 12)])
    kinds, viols, facts = _kinds(pcb)
    v = next(x for x in viols if x["kind"] == "body_under_module")
    assert v["severity"] == "error" and set(v["refs"]) == {"U1", "C1"}
    assert v["overlap"]["z"][0] == pytest.approx(11.04, abs=0.25)
    assert facts["raised"]["U1"]["standoff_mm"] == pytest.approx(11.04, abs=0.01)
    assert facts["raised"]["U1"]["sockets"] == ["J1", "J2"]
    assert "body_clash" not in kinds          # the module and its sockets


def test_low_part_under_a_module_on_sockets_passes(tmp_path):
    # 5 mm fits under the 11 mm gap; drawn at the board, it would have hit
    pcb = _plugged(tmp_path, [(-3, -3, 0, 3, 3, 5)])
    kinds, _, facts = _kinds(pcb)
    assert "body_under_module" not in kinds and "body_clash" not in kinds
    assert facts["raised"]["U1"]["lift_mm"] == pytest.approx(11.04, abs=0.45)


def test_a_module_with_no_model_on_sockets_is_its_underside(tmp_path):
    pcb = _plugged(tmp_path, [(-3, -3, 0, 3, 3, 12)], module=False)
    kinds, _, facts = _kinds(pcb)
    assert "body_under_module" in kinds
    assert "U1" not in facts["not_judged"]
    assert facts["raised"]["U1"]["drawn"].startswith("its courtyard")
    low = _plugged(tmp_path, [(-3, -3, 0, 3, 3, 5)], module=False)
    assert "body_under_module" not in _kinds(low)[0]


def test_a_stated_standoff_lifts_a_module_with_no_sockets(tmp_path):
    mod = _wrl(tmp_path / "nano.wrl", BOARD_PCB)
    cap = _wrl(tmp_path / "cap.wrl", [(-3, -3, 0, 3, 3, 5)])
    pcb = _board(tmp_path,
                 _fp("U1", 40, 50, mod, pad=False, name="Module:Arduino_Nano",
                     extra=_tht(NANO)),
                 _fp("C1", 40 + 3 * IN, 50 + 2 * IN, cap))
    kinds, _, facts = _kinds(pcb)
    assert "body_clash" in kinds               # drawn flat, as the design says
    assert facts["standoff_unstated"] == ["U1"]
    cons = {"mechanical": {"module_standoff": [
        {"refs": ["U1"], "height_mm": 8.0, "why": "on 8 mm spacers"}]}}
    kinds, _, facts = _kinds(pcb, cons)
    assert "body_clash" not in kinds and "body_under_module" not in kinds
    assert facts["raised"]["U1"]["standoff_mm"] == 8.0
    flat = {"mechanical": {"module_standoff": [
        {"refs": ["U1"], "height_mm": 0, "why": "soldered flat"}]}}
    kinds, _, facts = _kinds(pcb, flat)
    assert "body_clash" in kinds and "standoff_unstated" not in facts
    import checklib
    with pytest.raises(checklib.CheckError, match="module_standoff"):
        cbc.check(pcb, {"mechanical": {"module_standoff": [
            {"refs": ["U1"], "height_mm": 8.0}]}})


def test_a_plug_in_module_with_no_model_and_no_height_is_not_judged(tmp_path):
    pcb = _board(tmp_path, _fp("U1", 40, 50, None, pad=False,
                               name="Module:Arduino_Nano", extra=_tht(NANO)))
    _, _, facts = _kinds(pcb)
    assert "U1" in facts["not_judged"] and "raised" not in facts


DIP8 = [(x, y * IN) for x in (0, 3 * IN) for y in range(4)]
CHIP = [(0.6, -8.6, 0.5, 7.0, 1.0, 3.8)]     # a bare DIP-8 body, on the board
DIP_NAME = "Package_DIP:DIP-8_W7.62mm_Socket"


def test_planted_overhang_meets_a_socketed_chip_fails(tmp_path):
    # a ledge at 5-7 mm over the chip: clear of the bare chip KiCad draws,
    # into the chip as it sits in its 4.19 mm socket
    chip = _wrl(tmp_path / "DIP-8_W7.62mm.wrl", CHIP)
    ledge = _wrl(tmp_path / "ledge.wrl", [(-3, -3, 0, 3, 3, 7), (-12, -3, 5, -3, 3, 7)])
    pcb = _board(tmp_path, _fp("U1", 40, 50, chip, pad=False, name=DIP_NAME,
                               extra=_tht(DIP8)),
                 _fp("J1", 40 + 3 * IN + 8, 50 + 1.5 * IN, ledge))
    kinds, viols, facts = _kinds(pcb)
    assert "body_clash" in kinds
    assert facts["raised_dips"]["U1"]["lift_mm"] == pytest.approx(4.19 - 0.5, abs=0.45)
    assert facts["raised_dips"]["U1"]["socket"].startswith("drawn")


def test_overhang_clear_of_a_socketed_chip_passes(tmp_path):
    chip = _wrl(tmp_path / "DIP-8_W7.62mm.wrl", CHIP)
    ledge = _wrl(tmp_path / "ledge.wrl", [(-3, -3, 0, 3, 3, 10), (-12, -3, 8.5, -3, 3, 10)])
    pcb = _board(tmp_path, _fp("U1", 40, 50, chip, pad=False, name=DIP_NAME,
                               extra=_tht(DIP8)),
                 _fp("J1", 40 + 3 * IN + 8, 50 + 1.5 * IN, ledge))
    assert "body_clash" not in _kinds(pcb)[0]


def test_a_socket_model_alone_is_not_judged_for_its_chip(tmp_path):
    sock = _wrl(tmp_path / "DIP-8_W7.62mm_Socket.wrl",
                [(-1.27, -3 * IN - 1.27, 0, 3 * IN + 1.27, 1.27, 4.2)])
    pcb = _board(tmp_path, _fp("U1", 40, 50, sock, pad=False, name=DIP_NAME,
                               extra=_tht(DIP8)))
    _, _, facts = _kinds(pcb)
    assert "U1" in facts["not_judged"] and "chip" in facts["not_judged_why"]["U1"]
    assert facts["bodies"] == 1                 # the socket itself is judged
