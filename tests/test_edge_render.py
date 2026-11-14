"""#950: a render of each edge connector, framed on its mouth, for the
release review.

#911 caught the laptop hub's backwards microSD socket (J6) by eye in the end:
a person looking in across the board edge at J6 sees the closed back of the
housing where the card should go. render.py --views edge writes one such
picture per J/P/CN part within matinglib.EDGE_NEAR_MM of the outline, and
labels each with what check_mating says should face the camera.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import kc  # noqa: E402
import render  # noqa: E402
from test_edge_connector import _sd, _unlisted  # noqa: E402
from test_mating import _board, _fp  # noqa: E402


def _plan(pcb, ref="J6"):
    return next(p for p in render.edge_plans(pcb) if p["ref"] == ref)


def test_planted_backwards_sd_socket_render_shows_its_back(tmp_path):
    """The planted fault: the laptop's J6 turned 180, mouth into the board.
    The camera still looks in across the edge J6 sits at (its bottom edge,
    +y), and the render is labelled as showing the back of the housing."""
    p = _plan(_sd(tmp_path, 180))
    assert p["edge"] == "+y" and p["shows"] == "back"
    assert p["rotate"] == f"{render.EDGE_TILT},0,0"


def test_sd_socket_facing_the_edge_shows_its_mouth(tmp_path):
    p = _plan(_sd(tmp_path, 0))
    assert (p["edge"], p["shows"], p["family"]) == ("+y", "mouth", "card_slot")
    # framed on the part: J6's centre (15, 13.25) from the board's (15, 10),
    # in cm with y up; zoomed in on the small board
    x, y, z = (float(v) for v in p["pivot"].split(","))
    assert (x, z) == (0.0, 0.0) and y == pytest.approx(-0.325, abs=0.01)
    assert 1.0 <= p["zoom"] <= 10.0


def test_unknown_edge_connector_is_rendered_and_says_unjudged(tmp_path):
    assert _plan(_unlisted(tmp_path))["shows"] == "unjudged"


def test_a_connector_away_from_the_edge_gets_no_render(tmp_path):
    pcb = _board(tmp_path, _fp("J6", "SOCKET-SMD_8P-UNLISTED", 15.0, 10.0,
                               cy=(-2, -2, 2, 2)))
    assert render.edge_plans(pcb) == []


def test_camera_sits_outside_each_edge():
    assert render._edge_camera((0.0, 1.0)) == 0
    assert render._edge_camera((0.0, -1.0)) == 180
    assert render._edge_camera((-1.0, 0.0)) == 90
    assert render._edge_camera((1.0, 0.0)) == -90


def test_edge_is_a_view_name():
    assert render.parse_views("review,edge") == ["top", "bottom", "iso",
                                                 "edge"]
    with pytest.raises(ValueError):
        render.parse_views("edges")


def test_render_png_passes_pivot_and_zoom(tmp_path, monkeypatch):
    seen = {}

    def fake(cli, args, env=None):
        seen["args"] = args
        raise RuntimeError("stop")
    monkeypatch.setattr(kc, "render_env", lambda cli: ({}, ""))
    monkeypatch.setattr(kc, "run_cli", fake)
    pcb = _sd(tmp_path, 0)
    with pytest.raises(RuntimeError):
        kc.render_png(Path("kicad-cli"), pcb, tmp_path / "o.png",
                      rotate="-60,0,0", pivot="0,-0.3,0", zoom=4.5)
    a = seen["args"]
    assert a[a.index("--pivot") + 1] == "0,-0.3,0"
    assert a[a.index("--zoom") + 1] == "4.5"


@pytest.mark.slow
def test_edge_renders_are_written_per_connector(tmp_path):
    from lib import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")
    pcb = _sd(tmp_path, 180)
    r = render.render_views(pcb, ["edge"], tmp_path / "renders", width=400,
                            height=300, quality="basic")
    assert r["status"] == "pass", r
    (o,) = r["outputs"]
    assert o["view"] == "edge_J6" and o["shows"] == "back"
    assert Path(o["path"]).name == "b_edge_J6.png"
    assert Path(o["path"]).stat().st_size > 0


# --------------------------------------------- #1052: the mouth overlay
# The pinned boards' connector models are not in git, so their edge pictures
# show bare pads and the reviewer cannot see a mouth. The picture of a part
# with no model in it gets an arrow the way its mouth faces.

def _fake_render(monkeypatch, missing=()):
    from PIL import Image

    def fake(cli, pcb, out, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (400, 300), (40, 60, 40)).save(out)
        return {"status": "pass", "outputs": [str(out)],
                "models_missing": list(missing), "models_relinked": []}
    monkeypatch.setattr(kc, "render_png", fake)


def _render(pcb, tmp_path):
    (o,) = render.render_edges(pcb, tmp_path / "r", Path("kicad-cli"),
                               width=400, height=300, quality="basic")
    return o


def _arrow_side(png):
    """'below' or 'above' the centre: where the overlay's magenta lies,
    caption band (the top rows) left out."""
    from PIL import Image
    im = Image.open(png).convert("RGB")
    w, h = im.size
    ys = [y for y in range(h // 6, h) for x in range(w)
          if im.getpixel((x, y)) == render.OVERLAY_RGB]
    assert ys, "no overlay drawn"
    return "below" if sum(ys) / len(ys) > h / 2 else "above"


def test_screen_dir_maps_board_axes_into_the_edge_picture():
    # looking in across the bottom edge (+y): the plain top view, tilted
    assert render.screen_dir("+y", "+y") == (0, 1)     # toward the viewer
    assert render.screen_dir("-y", "+y") == (0, -1)    # into the board
    assert render.screen_dir("+x", "+y") == (1, 0)
    # across the top edge the camera is turned 180: +x reads leftward
    assert render.screen_dir("+x", "-y") == (-1, 0)
    assert render.screen_dir("-y", "-y") == (0, 1)
    # across the left edge (-x): board +y (down the board) is on the right
    assert render.screen_dir("+y", "-x") == (1, 0)
    assert render.screen_dir("-x", "-x") == (0, 1)


def test_planted_backwards_socket_without_model_gets_arrow_into_board(
        tmp_path, monkeypatch):
    """The planted fault: J6 turned 180, mouth into the board, and no 3D
    model, as on the pinned boards. Before #1052 the picture showed bare
    pads; now an arrow points up the picture, away from the viewer."""
    _fake_render(monkeypatch)
    o = _render(_sd(tmp_path, 180), tmp_path)
    assert (o["shows"], o["mouth"], o["overlay"]) == ("back", "-y",
                                                       "mouth_arrow")
    assert _arrow_side(o["path"]) == "above"


def test_socket_facing_the_edge_gets_arrow_toward_viewer(tmp_path,
                                                         monkeypatch):
    _fake_render(monkeypatch)
    o = _render(_sd(tmp_path, 0), tmp_path)
    assert (o["shows"], o["overlay"]) == ("mouth", "mouth_arrow")
    assert _arrow_side(o["path"]) == "below"


def _with_model(tmp_path, path):
    pcb = _sd(tmp_path, 180)
    t = pcb.read_text()
    t = t.replace('(attr smd)', f'(attr smd)\n    (model "{path}")', 1)
    pcb.write_text(t)
    return pcb


def test_part_whose_model_is_in_the_picture_gets_no_overlay(tmp_path,
                                                            monkeypatch):
    (tmp_path / "sd.wrl").write_text("#VRML V2.0 utf8\n")
    _fake_render(monkeypatch)
    o = _render(_with_model(tmp_path, "${KIPRJMOD}/sd.wrl"), tmp_path)
    assert o["overlay"] is None


def test_part_whose_model_file_is_missing_gets_the_overlay(tmp_path,
                                                           monkeypatch):
    gone = "${KIPRJMOD}/../lib/x.3dshapes/sd.wrl"
    _fake_render(monkeypatch, missing=[gone])
    o = _render(_with_model(tmp_path, gone), tmp_path)
    assert o["overlay"] == "mouth_arrow"


def test_unjudged_connector_gets_a_caption_only(tmp_path, monkeypatch):
    _fake_render(monkeypatch)
    o = _render(_unlisted(tmp_path), tmp_path)
    assert (o["shows"], o["overlay"]) == ("unjudged", "caption")
