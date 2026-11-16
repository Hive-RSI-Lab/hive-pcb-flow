"""An edge-launch footprint's board-edge mark and its model (#405).

EasyEDA's BWSMA-KE-P001 edge-launch SMA came out of lib_pull with an open
Edge.Cuts line at the board edge (a cut KiCad tries to close into the
board's outline) and its 3D model turned so the barrel lay across the board
instead of out past the edge. lib_pull's model fit now reads that line as
the edge (fpfix.edge_mark), turns and slides the model until nothing in
the board's band crosses it (check_model_fit.edge_fit), and fpfix rule I
moves the open line to Dwgs.User. A closed Edge.Cuts contour (a cut-out) is
the footprint's real outline and stays. Synthetic models throughout.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_model_fit as cmf  # noqa: E402
import fpfix  # noqa: E402
import modelgeom  # noqa: E402
from test_model_fit import _wrl  # noqa: E402

# Three SMD pads on a line across the board edge's normal; the edge mark is
# the line x = 1.5 (file frame), so the board is x < 1.5.
LEADS = [(-0.5, -0.5, 0.0, 0.5, 0.5, 0.1), (-0.5, -2.9, 0.0, 0.5, -1.9, 0.1),
         (-0.5, 1.9, 0.0, 0.5, 2.9, 0.1)]
# the barrel, its underside 1 mm into the board's band: seated out past
# the edge (OUT), or across the board and, turned, 0.2 mm short of the edge
OUT = LEADS + [(1.5, -1.5, -1.0, 12.0, 1.5, 2.0)]
IN = LEADS + [(-12.0, -1.5, -1.0, -1.3, 1.5, 2.0)]
# a barrel running along the edge, half on the board whichever way it faces
ALONG = LEADS + [(-3.0, -4.0, -1.0, 3.0, -3.2, 2.0)]

EDGE = "\t(fp_line (start 1.5 -3.3) (end 1.5 3.3) (layer Edge.Cuts) (width 0.25))\n"
# a closed slot: four lines meeting end to end
LOOP = "".join(
    f"\t(fp_line (start {a}) (end {b}) (layer Edge.Cuts) (width 0.1))\n"
    for a, b in (("-1.5 -1", "-1.5 1"), ("-1.5 1", "-1 1"),
                 ("-1 1", "-1 -1"), ("-1 -1", "-1.5 -1")))

MOD = '''(module easyeda2kicad:SMA-EDGE (layer F.Cu) (tedit 5DC5F6A4)
\t(pad 1 smd rect (at 0 0 0) (size 1.2 1.2) (layers F.Cu F.Paste F.Mask))
\t(pad 2 smd rect (at 0 -2.4 0) (size 1.2 1.2) (layers F.Cu F.Paste F.Mask))
\t(pad 3 smd rect (at 0 2.4 0) (size 1.2 1.2) (layers F.Cu F.Paste F.Mask))
{lines}\t(model "{path}"
\t\t(offset (xyz 0 0 0))
\t\t(scale (xyz 1 1 1))
\t\t(rotate (xyz 0 0 {rot}))
\t)
)
'''


def _lib(tmp_path, boxes, lines=EDGE, rot=0):
    pretty, shapes = tmp_path / "aiee.pretty", tmp_path / "aiee.3dshapes"
    pretty.mkdir()
    shapes.mkdir()
    wrl = _wrl(shapes / "SMA.wrl", boxes)
    fp = pretty / "SMA-EDGE.kicad_mod"
    fp.write_text(MOD.format(lines=lines, path=wrl, rot=rot), encoding="utf-8")
    return fp, wrl


def _xf(fp):
    import sexpdata
    (m,) = cmf._models(sexpdata.loads(fp.read_text(encoding="utf-8")))
    return m[1:4]


def _pads(fp):
    import sexpdata
    return cmf._pads(sexpdata.loads(fp.read_text(encoding="utf-8")), 0.0)


def test_the_edge_mark_is_the_open_edge_cuts_line():
    assert fpfix.edge_mark(MOD.format(lines=EDGE, path="x", rot=0)) == (
        (1.5, -3.3), (1.5, 3.3))
    # a closed contour is an outline, not a mark; no Edge.Cuts, no mark
    assert fpfix.edge_mark(MOD.format(lines=LOOP, path="x", rot=0)) is None
    assert fpfix.edge_mark(MOD.format(lines="", path="x", rot=0)) is None
    # the mark beside a cut-out is still read
    assert fpfix.edge_mark(MOD.format(lines=LOOP + EDGE, path="x", rot=0)) == (
        (1.5, -3.3), (1.5, 3.3))


def test_a_model_laid_across_the_board_is_turned_out_past_the_edge(tmp_path):
    """The planted fault: #405's barrel pointing into the board."""
    fp, wrl = _lib(tmp_path, IN)
    rep = cmf.fit_footprint(fp)
    assert rep["changed"]
    (row,) = rep["models"]
    assert "model_edge" in row["fixed"] and not row.get("left")
    assert row["edge"]["turned"] and row["edge"]["slide_mm"] == pytest.approx(0.2)
    assert row["edge"]["reach_mm"] == pytest.approx(13.5, abs=0.01)
    offset, scale, rotate = _xf(fp)
    assert rotate[2] % 360 == 180
    mesh, pads = modelgeom.load(wrl), _pads(fp)
    # nothing in the board's band crosses the edge, and the leads still fit
    n = (1.0, 0.0)
    assert cmf._edge_reach(mesh, offset, scale, rotate, pads, (1.5, -3.3), n) \
        <= cmf.EDGE_SLACK_MM
    assert not [h for h in cmf.fit_model(mesh, offset, scale, rotate, False, pads)
                if h["kind"] in cmf.REFIT]
    # fpfix then moves the mark off Edge.Cuts; a second pass changes nothing
    text = fp.read_text(encoding="utf-8")
    out, actions = fpfix.sanitize(text)
    assert [a["action"] for a in actions if a["rule"] == "I"] == [
        "edge_mark_to_dwgs_user"]
    assert "Edge.Cuts" not in out and "(layer Dwgs.User) (width 0.25)" in out
    fp.write_text(out, encoding="utf-8")
    assert not cmf.fit_footprint(fp)["changed"]
    assert fpfix.sanitize(out)[0] == out


def test_a_model_already_out_past_the_edge_is_left_byte_for_byte(tmp_path):
    """The quiet twin: the same part, its barrel facing out."""
    fp, _ = _lib(tmp_path, OUT)
    text = fp.read_text(encoding="utf-8")
    rep = cmf.fit_footprint(fp)
    assert not rep["changed"] and "edge" not in rep["models"][0]
    assert fp.read_text(encoding="utf-8") == text


def test_with_no_edge_mark_the_model_is_not_turned(tmp_path):
    """The same barrel-in model with a closed cut-out and no edge line: no
    edge to face, so the fit leaves it, and the cut-out stays on Edge.Cuts."""
    fp, _ = _lib(tmp_path, IN, lines=LOOP)
    text = fp.read_text(encoding="utf-8")
    assert not cmf.fit_footprint(fp)["changed"]
    assert fp.read_text(encoding="utf-8") == text
    out, actions = fpfix.sanitize(text)
    assert not [a for a in actions if a["rule"] == "I"]
    assert out.count("(layer Edge.Cuts)") == 4


def test_a_model_no_turn_clears_is_left_and_listed(tmp_path):
    fp, _ = _lib(tmp_path, ALONG)
    text = fp.read_text(encoding="utf-8")
    rep = cmf.fit_lib(fp.parent)
    assert rep["changed"] == 0 and fp.read_text(encoding="utf-8") == text
    assert [h["kind"] for h in rep["left"]] == ["model_into_board_edge"]


def test_lib_pull_reports_the_edge_fit(tmp_path):
    import lib_pull
    fp, _ = _lib(tmp_path, IN)
    rep = lib_pull._model_fit(fp.parent, ["SMA-EDGE"], dry_run=False)
    assert rep["changed"] == 1
    (row,) = rep["fixed"]
    assert row["edge"]["turned"] and "model_edge" in row["fixed"]
    auto = lib_pull._autofix(fp.parent, ["SMA-EDGE"], dry_run=False)
    assert "Edge.Cuts" not in fp.read_text(encoding="utf-8")
    assert [a["action"] for r in auto["results"] for a in r["actions"]
            if a["rule"] == "I"] == ["edge_mark_to_dwgs_user"]
