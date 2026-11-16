"""#856: keepout_gen --edge-band - a copper keep-out band (no tracks, no
vias) along the board edge, following the real outline (was ultra-front's
pinned edge_keepout.py, which drew four rectangles on the outline's bounding
box).

Pure tests cover the band on a notched and a windowed board, the hole-free
cut KiCad rule areas need, and the refusals. The smoke test works on a copy
of pinned ultra-front: the stand-in's four rectangles are removed and the
band redrawn by keepout_gen, which stays inside the rounded outline where the
rectangles stick out, covers every bit of them that lies on the board, finds
no copper already in it, survives a rerun and goes with --edge-band 0.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import constraints_lint  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import keepout_gen  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

CU = ["F.Cu", "B.Cu"]
# 40 x 30 with a 10 x 10 notch out of its top-right corner
NOTCHED = Polygon([(0, 0), (30, 0), (30, 10), (40, 10), (40, 30), (0, 30)])
UF = REPO / "pinned" / "ultra-front" / "kicad"


def _union(areas):
    return unary_union([Polygon(a["poly"]) for a in areas])


# ------------------------------------------------------------ pure

def test_band_follows_a_notch_not_the_bbox():
    areas, band = keepout_gen.plan_edge_band([NOTCHED], 0.5, CU)
    drawn = _union(areas)
    assert drawn.symmetric_difference(band).area < 1e-4
    assert drawn.difference(NOTCHED).area < 1e-6      # never off the board
    assert not drawn.intersects(Point(35, 5))          # the notch: bbox only
    assert drawn.contains(Point(35, 10.3))             # its inner edge
    assert drawn.contains(Point(29.7, 5))
    assert not drawn.contains(Point(35, 10.7))         # 0.7 in: past W
    assert band.area == pytest.approx(NOTCHED.length * 0.5, rel=0.02)
    for a in areas:
        assert a["layers"] == CU and a["name"].startswith(keepout_gen.EDGE)
        assert a["tracks"] and a["vias"] and not a["pours"]
        assert "pads" not in a and "footprints" not in a


def test_band_rings_a_window_and_cuts_holes_out():
    outer = box(0, 0, 40, 30)
    win = box(15, 10, 25, 20)
    areas, band = keepout_gen.plan_edge_band([outer, win], 0.5, CU)
    drawn = _union(areas)
    assert drawn.contains(Point(14.7, 15)) and drawn.contains(Point(20, 20.3))
    assert not drawn.intersects(Point(20, 15))          # the window itself
    assert not drawn.contains(Point(14, 15))
    assert len(areas) >= 4                              # two rings, each cut
    assert all(Polygon(a["poly"]).is_valid for a in areas)
    assert drawn.symmetric_difference(band).area < 1e-4
    # a face outside the board (a second board in the file) is not a window
    _, lone = keepout_gen.plan_edge_band([outer, box(50, 0, 60, 10)], 0.5, CU)
    plain = keepout_gen.edge_band([outer], 0.5)
    assert lone.symmetric_difference(plain).area < 1e-9
    assert band.area > plain.area + win.exterior.length * 0.5 - 0.01


def test_hole_free_keeps_the_area():
    ring = box(0, 0, 10, 10).difference(box(1, 1, 9, 9))
    parts = keepout_gen.hole_free(ring)
    assert len(parts) >= 2 and not any(p.interiors for p in parts)
    assert sum(p.area for p in parts) == pytest.approx(ring.area)


@pytest.mark.parametrize("faces, width, msg", [
    ([NOTCHED], 15.1, "covers the whole board"),
    ([], 0.5, "no closed Edge.Cuts outline"),
    ([Polygon()], 0.5, "no closed Edge.Cuts outline"),
])
def test_band_refusals(faces, width, msg):
    with pytest.raises(CheckError, match=msg):
        keepout_gen.plan_edge_band(faces, width, CU)


class _Args:
    def __init__(self, edge_band=None, edge_layers=None):
        self.edge_band, self.edge_layers = edge_band, edge_layers


def test_band_request_cli_over_constraints():
    cons = {"placement": {"edge_band_mm": 1.0, "edge_band_layers": ["B.Cu"]}}
    assert keepout_gen._band_request(_Args(), {}, CU) == (None, None)
    assert keepout_gen._band_request(_Args(), cons, CU) == (1.0, ["B.Cu"])
    assert keepout_gen._band_request(_Args(0.5, "F.Cu"), cons, CU) == \
        (0.5, ["F.Cu"])
    assert keepout_gen._band_request(_Args(0.5), {}, CU) == (0.5, CU)
    with pytest.raises(CheckError, match="not copper layers"):
        keepout_gen._band_request(_Args(0.5, "In1.Cu"), {}, CU)
    for bad in (-1, True, "0.5", float("nan")):
        with pytest.raises(CheckError, match="must be a number"):
            keepout_gen._band_request(
                _Args(), {"placement": {"edge_band_mm": bad}}, CU)


def test_constraints_lint_knows_the_band(tmp_path):
    p = tmp_path / "constraints.json"
    p.write_text(json.dumps({"placement": {
        "edge_band_mm": 0.5, "edge_band_layers": ["F.Cu", "B.Cu"]}}))
    violations, _ = constraints_lint.lint_file(p)
    assert not violations, violations
    p.write_text(json.dumps({"placement": {"edge_band_mm": "wide"}}))
    violations, _ = constraints_lint.lint_file(p)
    assert violations


# ------------------------------------------------------------ smoke (KiCad)

@pytest.fixture
def uf(tmp_path):
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        pytest.skip("KiCad bundled python not installed")
    dst = tmp_path / "uf"
    shutil.copytree(UF, dst, ignore=shutil.ignore_patterns("gen"))
    pcb = dst / "ultra-front.kicad_pcb"
    # take the stand-in's four rectangles off, as if it never ran
    routelib.run_worker(bp, {"verb": "set_rule_areas", "board": str(pcb),
                             "out": str(pcb), "prefix": "edge-keepout",
                             "areas": []}, tmp_path)
    return pcb


def _run(pcb, *extra):
    payload, _ = keepout_gen.run(["--pcb", str(pcb), *extra])
    return payload["facts"]


def _band_on(pcb):
    return [ra for ra in geom.BoardGeom.from_file(pcb).rule_areas
            if ra["name"].startswith(keepout_gen.EDGE)]


def test_band_replaces_edge_keepout_on_ultra_front(uf):
    pinned = geom.BoardGeom.from_file(UF / "ultra-front.kicad_pcb")
    rects = unary_union([ra["outline"] for ra in pinned.rule_areas
                         if ra["name"] == "edge-keepout"])
    board = pinned.outline
    assert rects.difference(board).area > 0.05   # bbox corners off the board
    assert not _band_on(uf)

    # the stand-in's 0.5 strips start 0.05 in: 0.55 covers them
    facts = _run(uf, "--edge-band", "0.55", "--edge-layers", "F.Cu,B.Cu")
    band = facts["edge_band"]
    assert band["layers"] == CU and band["pieces"] >= 2
    assert band["inside"] == {"tracks": 0, "vias": 0}  # routed to keep out
    got = _band_on(uf)
    drawn = unary_union([ra["outline"] for ra in got])
    assert drawn.difference(board).area < 0.01          # follows the arcs
    assert rects.intersection(board).difference(drawn).area < 0.01
    for ra in got:
        assert ra["layers"] == tuple(CU)
        assert ra["flags"]["tracks"] == "not_allowed"
        assert ra["flags"]["vias"] == "not_allowed"
        assert ra["flags"]["copperpour"] == "allowed"
        assert ra["flags"]["pads"] == "allowed"

    # a rerun naming no band keeps it; --dry-run touches nothing
    kept = _run(uf)["edge_band"]
    assert kept["kept"] and kept["area_mm2"] == pytest.approx(
        band["area_mm2"], rel=1e-3)
    assert len(_band_on(uf)) == len(got)
    raw = uf.read_bytes()
    _run(uf, "--edge-band", "2", "--dry-run")
    assert uf.read_bytes() == raw

    # a band that catches routed copper says so
    wide = _run(uf, "--edge-band", "3")["edge_band"]
    assert wide["inside"]["tracks"] + wide["inside"]["vias"] > 0

    # --edge-band 0 removes it
    gone = _run(uf, "--edge-band", "0")
    assert gone["edge_band"] is None and not _band_on(uf)
