"""check_connector_fit: leads, locating posts and the front edge against the
part's mechanical drawing (the extraction's `mechanical` block), and
check_ep_paste: exposed-pad paste coverage and paste over vias. Synthetic
boards, each built as the hifi-receiver-2 review found the fault (DFM-1: an
RCA footprint with no post holes; DFM-9: paste windows on an EP's vias) and
then fixed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_connector_fit as ccf  # noqa: E402
import check_ep_paste as cep  # noqa: E402
import datasheet_extract  # noqa: E402
import verify_all  # noqa: E402


def _board(tmp_path, body, setup="") -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)
    (13 "F.Paste" user) (15 "F.Mask" user) (31 "F.CrtYd" user)
    (33 "F.Fab" user))
  (setup {setup})
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return p


def _tht(num, x, y, drill, kind="thru_hole", size=None):
    size = size or drill + 0.6
    return (f'    (pad "{num}" {kind} circle (at {x} {y}) (size {size} {size})'
            f' (drill {drill}) (layers "*.Cu" "*.Mask"))\n')


def rca(drill=1.0, posts=False, fab_front=5.0, lcsc="C9999"):
    """An RCA-jack-like part at (15, 17): three leads, a body whose front
    (F.Fab) reaches local y = fab_front, optionally two post holes."""
    pads = _tht("1", 0, 0, drill) + _tht("2", -2.5, -2, drill) \
        + _tht("3", 2.5, -2, drill)
    if posts:
        pads += _tht("", -4, 2, 1.6, "np_thru_hole", 1.6) \
            + _tht("", 4, 2, 1.6, "np_thru_hole", 1.6)
    return (f'  (footprint "t:RCA_TEST" (layer "F.Cu")\n    (at 15 17)\n'
            f'    (property "Reference" "J1" (at 0 0 0))\n'
            f'    (property "LCSC" "{lcsc}" (at 0 0 0))\n'
            f'    (attr through_hole)\n'
            f'    (fp_rect (start -5.5 -3.5) (end 5.5 {fab_front + 0.25})'
            f' (stroke (width 0.05)) (fill no) (layer "F.CrtYd"))\n'
            f'    (fp_rect (start -5 -3) (end 5 {fab_front})'
            f' (stroke (width 0.1)) (fill no) (layer "F.Fab"))\n'
            f'{pads})\n')


def _parts(tmp_path, mech) -> Path:
    d = tmp_path / "parts"
    d.mkdir(exist_ok=True)
    ext = {"mpn": "RCA-TEST", "lcsc": "C9999",
           "pinout": [{"pin": "1", "name": "SIG", "type": "passive"}],
           "mechanical": mech}
    (d / "C9999.json").write_text(json.dumps(ext), encoding="utf-8")
    return d


# the drawing: 0.8 x 0.5 mm leads (0.94 mm diagonal), two 1.5 mm posts, the
# body front 2 mm past the PCB edge line
MECH = {"pins": [{"w_mm": 0.8, "t_mm": 0.5, "hole_mm": 1.2}],
        "posts": [{"count": 2, "dia_mm": 1.5}],
        "edge": {"overhang_mm": 2.0}, "source": "p3 fig 2"}


def _kinds(vs, sev=None):
    return sorted(v["kind"] for v in vs if sev is None or v["severity"] == sev)


def test_drawing_schema_validates_mechanical(tmp_path):
    import jsonschema
    ext = {"mpn": "X", "pinout": [{"pin": "1", "name": "A", "type": "passive"}],
           "mechanical": MECH}
    jsonschema.validate(ext, datasheet_extract.DATASHEET_SCHEMA)
    bad = {**ext, "mechanical": {"posts": [{"dia_mm": 1.0}]}}  # no count
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, datasheet_extract.DATASHEET_SCHEMA)


def test_footprint_off_its_drawing_fails_each_way(tmp_path):
    # DFM-1: 1.0 mm holes for 0.94 mm-diagonal leads, no post holes, and
    # the body's front 3 mm past the edge where the drawing says 2
    pcb = _board(tmp_path, rca(drill=1.0, posts=False, fab_front=6.0))
    vs, facts = ccf.check(pcb, _parts(tmp_path, MECH))
    assert _kinds(vs, "error") == ["edge_overhang_off", "post_holes_missing",
                                   "tht_pin_tight", "tht_pin_tight",
                                   "tht_pin_tight"]
    assert _kinds(vs, "warning") == ["tht_hole_under_spec"] * 3
    edge = facts["edges"][0]
    assert edge["ref"] == "J1" and edge["facing"] == "+y"
    assert edge["body"] == "fab" and edge["overhang_mm"] == pytest.approx(3.0)
    tight = next(v for v in vs if v["kind"] == "tht_pin_tight")
    assert tight["gap_mm"] == pytest.approx(1.0 - 0.9434, abs=1e-3)


def test_footprint_on_its_drawing_passes(tmp_path):
    pcb = _board(tmp_path, rca(drill=1.2, posts=True, fab_front=5.0))
    vs, facts = ccf.check(pcb, _parts(tmp_path, MECH))
    assert vs == []
    assert facts["edges"][0]["overhang_mm"] == pytest.approx(2.0)
    assert facts["parts"][0]["posts"] == [{"count": 2, "holes": 2}]
    assert facts["unverified"] == []


def test_small_post_holes_are_named_and_loose_holes_warn(tmp_path):
    body = rca(drill=1.8, posts=True).replace("(drill 1.6)", "(drill 1.2)")
    pcb = _board(tmp_path, body)
    vs, _ = ccf.check(pcb, _parts(tmp_path, {**MECH, "edge": {
        "overhang_mm": 2.0}}))
    post = next(v for v in vs if v["kind"] == "post_holes_missing")
    assert "1.20 mm too small" in post["msg"] and post["holes"] == 0
    assert _kinds(vs, "warning") == ["tht_hole_loose"] * 3


def test_no_drawing_is_listed_not_failed(tmp_path):
    pcb = _board(tmp_path, rca())
    vs, facts = ccf.check(pcb, None)
    # RCA is a mating family (rca_jack, #891), so its edge is measured, but
    # with no drawing there is no overhang to hold it to
    assert vs == []
    assert [e["ref"] for e in facts["edges"]] == ["J1"]
    assert "drawing_overhang_mm" not in facts["edges"][0]
    assert facts["unverified"] == [{"ref": "J1", "lcsc": "C9999",
                                    "why": "no extraction"}]


def test_slot_takes_a_blade_per_axis():
    assert ccf.clearance((1.0, 1.0), (0.5, 0.8)) == pytest.approx(
        1.0 - 0.9434, abs=1e-4)
    assert ccf.clearance((0.7, 1.5), (0.5, 1.2)) == pytest.approx(0.2)
    assert ccf.clearance((1.5, 0.7), (0.6, 1.45)) == pytest.approx(0.05)


# ------------------------------------------------------------- EP paste

def qfn(ep_layers='"F.Cu" "F.Paste" "F.Mask"', windows=False, extra=""):
    """An 8-lead QFN-like part at (15, 10) with a 3 x 3 mm exposed pad."""
    pads = ""
    for i, (x, y) in enumerate([(-1.0, -2.0), (1.0, -2.0), (2.0, -1.0),
                                (2.0, 1.0), (1.0, 2.0), (-1.0, 2.0),
                                (-2.0, 1.0), (-2.0, -1.0)]):
        w, h = (0.3, 0.8) if abs(y) == 2.0 else (0.8, 0.3)
        pads += (f'    (pad "{i + 1}" smd rect (at {x} {y}) (size {w} {h})'
                 ' (layers "F.Cu" "F.Paste" "F.Mask"))\n')
    pads += (f'    (pad "9" smd rect (at 0 0) (size 3 3) (layers {ep_layers})'
             f'{extra})\n')
    if windows:
        for x, y in ((-0.8, -0.8), (0.8, -0.8), (0.8, 0.8), (-0.8, 0.8)):
            pads += (f'    (pad "" smd rect (at {x} {y}) (size 1.2 1.2)'
                     ' (layers "F.Paste"))\n')
    return ('  (footprint "t:QFN_TEST" (layer "F.Cu")\n    (at 15 10)\n'
            '    (property "Reference" "U1" (at 0 0 0))\n'
            '    (attr smd)\n' + pads + ')\n')


def via(x=15, y=10, extra=""):
    return (f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
            f'{extra} (net 0))\n')


def test_full_paste_over_a_via_fails(tmp_path):
    vs, facts = cep.check(_board(tmp_path, qfn() + via()))
    assert _kinds(vs, "error") == ["paste_on_via"]
    assert _kinds(vs, "warning") == ["ep_paste_high"]
    ep = facts["exposed_pads"][0]
    assert (ep["ref"], ep["pad"], ep["coverage"], ep["vias_in_pad"]) == \
        ("U1", "9", 1.0, 1)


def test_window_pane_paste_clear_of_the_via_passes(tmp_path):
    vs, facts = cep.check(_board(tmp_path, qfn('"F.Cu" "F.Mask"', True)
                                 + via()))
    assert vs == []
    ep = facts["exposed_pads"][0]
    assert ep["coverage"] == pytest.approx(4 * 1.44 / 9, abs=1e-3)
    assert ep["apertures"] == 4 and facts["vias_under_paste"] == 0


def test_a_via_under_a_paste_window_names_the_exposed_pad(tmp_path):
    # hive-pcb-flow #1150: a window is a paste-only pad with no number, so
    # the error named "U1 pad " and its pads "U1:"
    vs, _ = cep.check(_board(tmp_path, qfn('"F.Cu" "F.Mask"', True)
                             + via(14.2, 9.2)))
    assert _kinds(vs) == ["paste_on_via"]
    assert "the paste of U1 pad 9's paste window:" in vs[0]["msg"]
    assert vs[0]["pads"] == ["U1:9"]


def test_a_via_under_an_ep_and_its_window_lists_the_pad_once(tmp_path):
    vs, _ = cep.check(_board(tmp_path, qfn(windows=True) + via(14.2, 9.2)))
    assert _kinds(vs) == ["ep_paste_high", "paste_on_via"]
    v = next(v for v in vs if v["kind"] == "paste_on_via")
    assert "the paste of U1 pad 9, U1 pad 9's paste window:" in v["msg"]
    assert v["pads"] == ["U1:9"]


def _two_eps(body):
    # pad 9 split into two exposed pads, 9 on the left and 10 on the right
    eps = "".join(f'    (pad "{n}" smd rect (at {x} 0) (size 1.4 3)'
                  ' (layers "F.Cu" "F.Mask"))\n'
                  for n, x in (("9", -0.8), ("10", 0.8)))
    one = ('    (pad "9" smd rect (at 0 0) (size 3 3)'
           ' (layers "F.Cu" "F.Mask"))\n')
    return body.replace(one, eps)


def _with_window(body, x, size=0.6):
    win = (f'    (pad "" smd rect (at {x} 0) (size {size} {size})'
           ' (layers "F.Paste"))\n')
    return body.replace("    (attr smd)\n", "    (attr smd)\n" + win)


def test_a_window_takes_the_number_of_the_ep_it_lies_on(tmp_path):
    # two exposed pads, each with its own windows; the via is under pad 10's
    wins = "".join(f'    (pad "" smd rect (at {x} {y}) (size 1 1)'
                   ' (layers "F.Paste"))\n'
                   for x in (-0.8, 0.8) for y in (-0.8, 0.8))
    body = _two_eps(qfn('"F.Cu" "F.Mask"')).replace(
        "    (attr smd)\n", "    (attr smd)\n" + wins)
    vs, _ = cep.check(_board(tmp_path, body + via(15.8, 9.2)))
    assert _kinds(vs) == ["paste_on_via"]
    assert "the paste of U1 pad 10's paste window:" in vs[0]["msg"]
    assert vs[0]["pads"] == ["U1:10"]


def test_a_window_over_two_eps_takes_the_one_under_the_via(tmp_path):
    # the window lies half on pad 9 and half on pad 10; the via is on 10
    body = _with_window(_two_eps(qfn('"F.Cu" "F.Mask"')), 0, 1.2)
    vs, _ = cep.check(_board(tmp_path, body + via(15.4, 10)))
    v = next(v for v in vs if v["kind"] == "paste_on_via")
    assert "the paste of U1 pad 10's paste window:" in v["msg"]
    assert v["pads"] == ["U1:10"]


def test_a_window_on_an_unnumbered_ep_names_the_exposed_pad(tmp_path):
    # an exposed pad with no number is still found by its size and place
    body = qfn('"F.Cu" "F.Mask"', True).replace('(pad "9" ', '(pad "" ')
    vs, _ = cep.check(_board(tmp_path, body + via(14.2, 9.2)))
    assert _kinds(vs) == ["paste_on_via"]
    assert "the paste of U1 exposed pad's paste window:" in vs[0]["msg"]
    assert vs[0]["pads"] == ["U1:"]


def test_a_via_under_a_window_clear_of_the_ep_is_only_listed(tmp_path):
    body = _with_window(qfn('"F.Cu" "F.Mask"', True), 3)
    vs, facts = cep.check(_board(tmp_path, body + via(18, 10)))
    assert vs == []
    assert facts["vias_in_paste"] == [
        {"at": [18.0, 10.0], "side": "F", "pads": ["U1:"]}]


def test_a_window_clear_of_the_ep_keeps_a_plain_name(tmp_path):
    # the via is under the exposed pad's own paste and a window beside it
    body = _with_window(qfn(), 1.85)
    vs, _ = cep.check(_board(tmp_path, body + via(16.52, 10)))
    assert _kinds(vs) == ["ep_paste_high", "paste_on_via"]
    v = next(v for v in vs if v["kind"] == "paste_on_via")
    assert "the paste of U1 paste window, U1 pad 9:" in v["msg"]
    assert v["pads"] == ["U1:", "U1:9"]


def test_no_paste_on_the_ep_and_a_plugged_via(tmp_path):
    vs, _ = cep.check(_board(tmp_path, qfn('"F.Cu" "F.Mask"')))
    assert _kinds(vs) == ["ep_paste_missing"]
    vs, facts = cep.check(_board(tmp_path, qfn() + via(
        extra=" (capping yes) (filling yes)")))
    assert _kinds(vs) == ["ep_paste_high"] and facts["vias_under_paste"] == 1


def _gfx_windows(prefix="fp", layer="F.Paste", fill="yes", at=(0, 0)):
    out = ""
    for x, y in ((-0.8, -0.8), (0.8, -0.8), (0.8, 0.8), (-0.8, 0.8)):
        x, y = x + at[0], y + at[1]
        out += (f'    ({prefix}_rect (start {x - 0.6} {y - 0.6}) '
                f'(end {x + 0.6} {y + 0.6}) (stroke (width 0) (type default))'
                f' (fill {fill}) (layer "{layer}"))\n')
    return out


def test_paste_graphics_over_the_ep_count(tmp_path):
    # hive-hwde #544: Eagle's tCream windows arrive as F.Paste graphics in the
    # footprint, with the pad's own paste off
    body = qfn('"F.Cu" "F.Mask"').rstrip()[:-1] + _gfx_windows() + ")\n"
    vs, facts = cep.check(_board(tmp_path, body + via()))
    assert vs == []
    ep = facts["exposed_pads"][0]
    assert ep["coverage"] == pytest.approx(4 * 1.44 / 9, abs=1e-3)
    assert ep["graphic_apertures"] == 4
    # an outline on F.Paste with no width prints nothing; a B.Paste one is
    # the other side's stencil
    for fill, layer in (("no", "F.Paste"), ("yes", "B.Paste")):
        body = qfn('"F.Cu" "F.Mask"').rstrip()[:-1] + \
            _gfx_windows(fill=fill, layer=layer) + ")\n"
        vs, _ = cep.check(_board(tmp_path, body))
        assert _kinds(vs) == ["ep_paste_missing"]


def test_board_paste_graphic_over_a_via_fails(tmp_path):
    # a board-level window (footprint at 15, 10) counts, and one on the via
    # is paste_on_via
    body = qfn('"F.Cu" "F.Mask"') + _gfx_windows("gr", at=(15, 10)) + \
        '  (gr_circle (center 15 10) (end 15.3 10) (stroke (width 0)) ' \
        '(fill yes) (layer "F.Paste"))\n'
    vs, facts = cep.check(_board(tmp_path, body + via()))
    assert _kinds(vs) == ["paste_on_via"]
    assert "board paste graphic" in vs[0]["msg"]
    assert facts["exposed_pads"][0]["graphic_apertures"] == 5


def test_paste_margin_ratio_shrinks_the_aperture(tmp_path):
    # pad ratio -0.2: 3 mm - 2 x 0.6 = 1.8 mm square, 36 % of the pad, and
    # off the via at (16.2, 11.2)
    vs, facts = cep.check(_board(tmp_path, qfn(
        extra=" (solder_paste_margin_ratio -0.2)") + via(16.2, 11.2)))
    assert facts["exposed_pads"][0]["coverage"] == pytest.approx(0.36,
                                                                 abs=1e-3)
    assert vs == []
    # the board's pad_to_paste_clearance applies when the pad has none
    vs, facts = cep.check(_board(tmp_path, qfn(),
                                 setup="(pad_to_paste_clearance -0.5)"))
    assert facts["exposed_pads"][0]["coverage"] == pytest.approx(4 / 9,
                                                                 abs=1e-3)


def test_via_in_a_small_pad_is_listed_not_failed(tmp_path):
    vs, facts = cep.check(_board(tmp_path, qfn('"F.Cu" "F.Mask"', True)
                                 + via(16, 8)))  # under lead pad 2
    assert vs == []
    assert facts["vias_in_paste"] == [{"at": [16.0, 8.0], "side": "F",
                                       "pads": ["U1:2"]}]


def test_both_checks_join_the_verify_suite():
    names = [c["name"] for c in verify_all.load_checks()]
    assert "check_connector_fit" in names and "check_ep_paste" in names


# ------------------------------------------- drawing pages: render and OCR

def test_pdf_render_lists_image_only_pages(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter()
    w.add_blank_page(width=200, height=200)   # a scan: no text layer
    pdf = tmp_path / "ds.pdf"
    with pdf.open("wb") as fh:
        w.write(fh)
    out = tmp_path / "payload.json"
    rc = datasheet_extract.main(["--pdf", str(pdf), "--render",
                                 str(tmp_path / "pages"), "--out", str(out)])
    assert rc == 0
    r = json.loads(out.read_text())["rendered"]
    assert r["pages"] == [{"page": 0, "why": "image_only"}]
    if r["renderer"] is None:
        assert r["rendered"] == []      # no pdftoppm: listed, not drawn
    else:
        png = Path(r["rendered"][0]["png"])
        assert png.is_file() and png.name == "page-001.png"


def test_fp_verify_holds_a_library_footprint_to_the_drawing(tmp_path):
    import fp_verify
    mod = tmp_path / "RCA_TEST.kicad_mod"
    pads = _tht("1", 0, 0, 1.0) + _tht("2", -2.5, -2, 1.0) \
        + _tht("3", 2.5, -2, 1.0)
    mod.write_text('(footprint "RCA_TEST" (layer "F.Cu")\n'
                   '  (fp_rect (start -5.5 -3.5) (end 5.5 5.5) (stroke'
                   ' (width 0.05)) (fill no) (layer "F.CrtYd"))\n'
                   f'{pads})\n', encoding="utf-8")
    ds = {"mpn": "RCA-TEST", "pinout": [], "land_pattern": {"pad_count": 3},
          "mechanical": MECH}
    vs, facts, _ = fp_verify.verify(mod, ds, 0.05, 0.1)
    assert _kinds(vs, "error") == ["post_holes_missing"] + ["tht_pin_tight"] * 3
    assert facts["mechanical"]["posts"] == [{"count": 2, "holes": 0}]


# ------------------------------------------------- IPC-2222 hole vs lead (#474)

def header(drill=1.0, n=5, fpid="Connector_PinHeader_2.54mm:"
           "PinHeader_2x05_P2.54mm_Vertical"):
    """A 2xN 2.54 mm header at (10, 5) with no extraction."""
    pads = "".join(_tht(str(2 * i + r + 1), 2.54 * r, 2.54 * i, drill, size=1.7)
                   for i in range(n) for r in range(2))
    return (f'  (footprint "{fpid}" (layer "F.Cu")\n    (at 10 5)\n'
            f'    (property "Reference" "J3" (at 0 0 0))\n'
            f'    (attr through_hole)\n{pads})\n')


def test_stock_header_drill_under_ipc2222_warns_once(tmp_path):
    # 0.64 mm square post: 0.905 mm diagonal, level C minimum 1.055 mm
    vs, facts = ccf.check(_board(tmp_path, header(1.0)), None)
    assert _kinds(vs) == ["tht_hole_ipc2222"]
    w = vs[0]
    assert w["severity"] == "warning" and w["refs"] == ["J3"]
    assert w["min_hole_mm"] == pytest.approx(1.055, abs=1e-3)
    assert w["gap_mm"] == pytest.approx(0.095, abs=1e-3)
    assert w["pads"] == [str(i) for i in range(1, 11)]
    assert "1.10 mm" in w["msg"]
    assert facts["parts"][0]["lead_source"] == "stock"


def test_stock_header_at_ipc2222_passes(tmp_path):
    vs, _ = ccf.check(_board(tmp_path, header(1.1)), None)
    assert vs == []


def test_stock_header_drill_under_the_lead_is_an_error(tmp_path):
    vs, _ = ccf.check(_board(tmp_path, header(0.9)), None)
    assert _kinds(vs, "error") == ["tht_pin_tight"] * 10
    assert _kinds(vs, "warning") == []


def test_two_mm_header_uses_its_own_post(tmp_path):
    # 0.5 mm post: 0.707 diagonal + 0.15 = 0.857; 0.8 mm drill warns
    fp = "Connector_PinHeader_2.00mm:PinHeader_1x04_P2.00mm_Vertical"
    vs, _ = ccf.check(_board(tmp_path, header(0.8, n=2, fpid=fp)), None)
    assert _kinds(vs) == ["tht_hole_ipc2222"]
    assert vs[0]["min_hole_mm"] == pytest.approx(0.857, abs=1e-3)


def test_drawing_lead_between_tolerance_and_ipc2222_warns(tmp_path):
    # 0.94 mm diagonal in a 1.05 mm hole: goes in (0.11 > 0.08), under IPC C
    pcb = _board(tmp_path, rca(drill=1.05, posts=True, fab_front=5.0))
    mech = {**MECH, "pins": [{"w_mm": 0.8, "t_mm": 0.5}]}
    vs, _ = ccf.check(pcb, _parts(tmp_path, mech))
    assert _kinds(vs) == ["tht_hole_ipc2222"]
    assert vs[0]["pads"] == ["1", "2", "3"]


def test_a_square_lead_is_measured_across_its_diagonal():
    assert ccf.clearance((1.0, 1.0), (0.64, 0.64), rect=True) == \
        pytest.approx(1.0 - 0.9051, abs=1e-4)
    assert ccf.clearance((1.0, 1.0), (0.64, 0.64), rect=False) == \
        pytest.approx(0.36)


# --- solder-wire pads held to the wire their copper feed needs (#881: oasis
# J1, 2.5 mm^2 holes fed by a pour's 0.5 mm thermal spokes) ---------------

def _wire(size="2.5sqmm", drill=3.0, pad_extra="", fp_extra=""):
    pad = drill + 1.4
    pads = "".join(
        f'    (pad "{n}" thru_hole circle (at {x} 0) (size {pad} {pad})'
        f' (drill {drill}) (layers "*.Cu" "*.Mask") (net "{net}"){pad_extra})\n'
        for n, x, net in (("1", 0, "GND"), ("2", 8.8, "+18V")))
    return (f'  (footprint "Connector_Wire:SolderWire-{size}_1x02_P8.8mm"'
            f' (layer "F.Cu")\n    (at 10 10)\n'
            f'    (property "Reference" "J1" (at 0 0 0))\n'
            f'    (attr through_hole){fp_extra}\n{pads})\n')


def _seg(x0, x1, w, net):
    return (f'  (segment (start {x0} 10) (end {x1} 10) (width {w})'
            f' (layer "F.Cu") (net "{net}"))\n')


def _pour(net, connect=""):
    pts = "(pts (xy 15 5) (xy 25 5) (xy 25 15) (xy 15 15))"
    return (f'  (zone (net "{net}") (layer "F.Cu") {connect}'
            f'(fill yes (thermal_bridge_width 0.5)) (polygon {pts})'
            f'\n    (filled_polygon (layer "F.Cu") {pts}))\n')


def _loose(tmp_path, body):
    viols, facts = ccf.check(_board(tmp_path, body), None)
    return [v for v in viols if v["kind"] == "tht_hole_loose"], facts


def test_solder_wire_holes_too_big_for_their_feed(tmp_path):
    # 0.5 mm tracks carry ~2 A: a 0.5 mm^2 wire (0.9 mm), in 3.0 mm holes
    loose, facts = _loose(tmp_path, _wire() + _seg(2, 10, 0.5, "GND")
                          + _seg(18.8, 28, 0.5, "+18V"))
    assert len(loose) == 1 and loose[0]["refs"] == ["J1"]
    assert loose[0]["pads"] == ["1", "2"] and loose[0]["wire_mm2"] == 0.5
    assert loose[0]["severity"] == "warning"
    fact = next(p for p in facts["parts"] if p["ref"] == "J1")
    assert fact["lead_source"] == "wire"


def test_solder_wire_pour_spokes_count_as_feed(tmp_path):
    # the oasis case: +18V's only feed is a thermal-relief pour (4 x 0.5 mm)
    loose, facts = _loose(tmp_path, _wire() + _seg(2, 10, 0.5, "GND")
                          + _pour("+18V"))
    assert len(loose) == 1
    fact = next(p for p in facts["parts"] if p["ref"] == "J1")
    assert fact["wire"]["feed_mm"] == 2.0 and fact["wire"]["mm2"] == 0.75


def test_solder_wire_sized_to_its_feed_passes(tmp_path):
    # a 0.5 mm^2 footprint (1.15 mm drill) on 0.5 mm tracks: a fit
    loose, facts = _loose(tmp_path, _wire("0.5sqmm", 1.15)
                          + _seg(2, 10, 0.5, "GND")
                          + _seg(18.8, 28, 0.5, "+18V"))
    assert loose == []
    assert any(p["ref"] == "J1" for p in facts["parts"])


@pytest.mark.parametrize("why", ["solid zone", "solid pad", "no feed"])
def test_solder_wire_unbounded_feed_not_judged(tmp_path, why):
    # a pour tied solid (zone or pad) carries any current; no copper at all
    # says nothing: neither is judged
    body = {"solid zone": _wire() + _seg(2, 10, 0.5, "GND")
            + _pour("+18V", "(connect_pads yes (clearance 0.5)) "),
            "solid pad": _wire(pad_extra=" (zone_connect 2)")
            + _seg(2, 10, 0.5, "GND") + _pour("+18V"),
            "no feed": _wire()}[why]
    loose, facts = _loose(tmp_path, body)
    assert loose == []
    assert not any(p["ref"] == "J1" for p in facts["parts"])
