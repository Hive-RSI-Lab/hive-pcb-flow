"""CPL placement per part: the LCSC model's pin 1 on the board's pad 1.

Offline: the models are cached EasyEDA records in tests/fixtures/easyeda
(C116592 TPS563201 SOT-23-6, C106675 a THT electrolytic, C106245 a 0603 cap),
plus one synthetic LED model whose numbering is the reverse of KiCad's. The
board is a tiny hand-written .kicad_pcb, so nothing needs KiCad or a network.
"""
from __future__ import annotations

import csv
import json
import math
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import bom_cpl  # noqa: E402
import checklib  # noqa: E402
import cpl_render  # noqa: E402
import cpl_verify as cv  # noqa: E402
import dfm_check  # noqa: E402
import easyeda  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "easyeda"

# The imported (easyeda2kicad) footprint keeps the model's own geometry.
IMPORTED_SOT236 = [("1", 1.35, 0.95), ("2", 1.35, 0.0), ("3", 1.35, -0.95),
                   ("4", -1.35, -0.95), ("5", -1.35, 0.0), ("6", -1.35, 0.95)]
# KiCad's library SOT-23-6: pins 1-3 down the left, pin 1 top-left.
KICAD_SOT236 = [("1", -1.1375, -0.95), ("2", -1.1375, 0.0),
                ("3", -1.1375, 0.95), ("4", 1.1375, 0.95), ("5", 1.1375, 0.0),
                ("6", 1.1375, -0.95)]


def _fp(ref, fpid, x, y, r, pads, size=(1.1, 0.6), layer="F.Cu", silk=None):
    if layer == "B.Cu":
        # KiCad's flip: a bottom footprint's local pads are stored mirrored in y
        pads = [(n, px, -py) for n, px, py in pads]
    pad_txt = "".join(
        f'(pad "{n}" smd rect (at {px} {py} {r}) (size {size[0]} {size[1]}) '
        f'(layers "{layer}"))' for n, px, py in pads)
    silk = silk or '(fp_line (start -1 -1) (end 1 -1) (layer "F.SilkS"))'
    return (f'(footprint "{fpid}" (layer "{layer}") (at {x} {y} {r}) '
            f'(property "Reference" "{ref}") {silk} {pad_txt})')


BOARD = "(kicad_pcb (version 20240108) " + " ".join([
    _fp("U1", "aiee:SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BR", 10, 10, 90,
        IMPORTED_SOT236),
    _fp("U2", "Package_TO_SOT_SMD:SOT-23-6", 20, 10, 0, KICAD_SOT236),
    _fp("D1", "LED_SMD:LED_0603_1608Metric", 30, 10, 0,
        [("1", -0.7875, 0), ("2", 0.7875, 0)], size=(0.875, 0.95)),
    _fp("C1", "aiee:C0603", 40, 10, 0, [("1", -0.7, 0), ("2", 0.7, 0)],
        size=(0.8, 0.9)),
    _fp("C2", "aiee:CAP-TH_BD13.0-P5.00-D1.2-FD", 50, 10, 0,
        [("1", -2.54, 0), ("2", 2.54, 0)], size=(2.0, 2.0)),
    _fp("X1", "aiee:SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BR", 60, 10, 0,
        IMPORTED_SOT236),
    _fp("U3", "aiee:SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BR", 70, 10, 0,
        IMPORTED_SOT236, layer="B.Cu"),
    _fp("C3", "aiee:C0603", 80, 10, 90, [("1", -0.7, 0), ("2", 0.7, 0)],
        size=(0.8, 0.9), layer="B.Cu"),
    _fp("C4", "aiee:C0603", 90, 10, 45, [("1", -0.7, 0), ("2", 0.7, 0)],
        size=(0.8, 0.9), layer="B.Cu"),
    _fp("D2", "LED_SMD:LED_0603_1608Metric", 100, 10, 0,
        [("1", -0.7875, 0), ("2", 0.7875, 0)], size=(0.875, 0.95),
        layer="B.Cu"),
    # a 0201-style land: two unnumbered paste-only pads beside the copper ones
    _fp("C5", "aiee:C0603", 110, 10, 90,
        [("", -0.72, 0), ("", 0.72, 0), ("1", -0.7, 0), ("2", 0.7, 0)],
        size=(0.8, 0.9), layer="B.Cu"),
]) + ")"

# A synthetic LED whose pad 1 is the ANODE (KiCad's LED pad 1 is the cathode).
LED_MODEL = {"lcsc": "C900001", "title": "TEST-LED", "package": "LED0603",
             "origin": [4000, 3000],
             "pads": ["PAD~RECT~3997.244~3000~3.4~3.7~1~~1~0~~0~a",
                      "PAD~RECT~4002.756~3000~3.4~3.7~1~~2~0~~0~b"],
             "silk": [],
             "pins": ["P~show~0~1~-30~0~180~g1~0^^-30~0^^M -30 0 h 10~#880000"
                      "^^0~-17~3~0~A~start~~~#0000FF^^0~-21~-1~0~1~end",
                      "P~show~0~2~30~0~0~g2~0^^30~0^^M 30 0 h -10~#880000"
                      "^^0~17~3~0~K~end~~~#0000FF^^0~21~-1~0~2~start"]}

LCSC = {"U1": "C116592", "U2": "C116592", "D1": "C900001", "C1": "C106245",
        "C2": "C106675", "U3": "C116592", "C3": "C106245", "C4": "C106245", "C5": "C106245",
        "D2": "C900001"}


@pytest.fixture
def ws(tmp_path):
    cache = tmp_path / "easyeda"
    shutil.copytree(FIX, cache)
    (cache / "C900001.json").write_text(json.dumps(LED_MODEL))
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(BOARD)
    return pcb, cache


def _cpl(**rots):
    """CPL rows at each footprint's own position (board y down -> CPL y up)."""
    pos = {"U1": 10, "U2": 20, "D1": 30, "C1": 40, "C2": 50, "X1": 60,
           "U3": 70, "C3": 80, "C4": 90, "D2": 100, "C5": 110}
    return {r: {"x": pos[r], "y": 10.0, "rot": float(v), "layer": "Top"}
            for r, v in rots.items()}


def _no_mark_warning_aside(vios):
    """The vios other than the no-mark warning: BOARD's parts carry no dot."""
    return [v for v in vios if v["kind"] != "cpl_no_silk_mark"]


def _verdicts(pcb, cache, cpl):
    rep = cv.verify(pcb, cpl, LCSC, cache, fetch=False)
    return {p["ref"]: p for p in rep["parts"]}, rep


def test_imported_footprint_needs_no_correction(ws):
    """The owner's case: an easyeda2kicad SOT-23-6 at 90 deg must ship at 90.
    The '^SOT-23,180' table row shipped 270 - pin 1 opposite the mark."""
    pcb, cache = ws
    v, _ = _verdicts(pcb, cache, _cpl(U1=90))
    assert v["U1"]["verdict"] == "ok" and v["U1"]["expected_rot"] == [90.0]
    v, rep = _verdicts(pcb, cache, _cpl(U1=270))
    assert v["U1"]["verdict"] == "wrong_rotation"
    assert rep["failed"] == ["U1"] and rep["status"] == "violations"


def test_kicad_library_footprint_needs_180_for_this_model(ws):
    pcb, cache = ws
    v, _ = _verdicts(pcb, cache, _cpl(U2=180))
    assert v["U2"]["verdict"] == "ok"
    v, _ = _verdicts(pcb, cache, _cpl(U2=0))
    assert v["U2"]["verdict"] == "wrong_rotation"
    assert v["U2"]["expected_rot"] == [180.0]


def test_polarity_uses_pin_roles_not_numbers(ws):
    """The LED model's pad 1 is its anode. By number it fits at 0 deg, but
    that puts the anode on KiCad's cathode pad: the role fit wins."""
    pcb, cache = ws
    v, _ = _verdicts(pcb, cache, _cpl(D1=0))
    assert v["D1"]["verdict"] == "wrong_polarity"
    assert v["D1"]["corrections"] == [180.0]
    assert v["D1"]["number_corrections"] == [0.0]
    assert "cathode" in v["D1"]["why"]
    v, _ = _verdicts(pcb, cache, _cpl(D1=180))
    assert v["D1"]["verdict"] == "ok"
    v, _ = _verdicts(pcb, cache, _cpl(D1=90))
    assert v["D1"]["verdict"] == "wrong_rotation"


def test_nonpolar_two_pad_part_either_way_round(ws):
    pcb, cache = ws
    for rot in (0, 180):
        v, _ = _verdicts(pcb, cache, _cpl(C1=rot))
        assert v["C1"]["verdict"] == "ok", rot
    v, _ = _verdicts(pcb, cache, _cpl(C1=90))
    assert v["C1"]["verdict"] == "wrong_rotation"


def test_polar_cap_is_strict_about_180(ws):
    """The electrolytic's model names its pins 1/2, so polarity falls back to
    pad numbers - and a 180-degree turn is NOT accepted the way it is for C1."""
    pcb, cache = ws
    v, _ = _verdicts(pcb, cache, _cpl(C2=0))
    assert v["C2"]["verdict"] == "ok" and v["C2"]["polar"]
    assert v["C2"]["basis"].startswith("pad number")
    v, _ = _verdicts(pcb, cache, _cpl(C2=180))
    assert v["C2"]["verdict"] == "wrong_rotation"


def test_no_data_fails_loudly(ws):
    pcb, cache = ws
    v, rep = _verdicts(pcb, cache, _cpl(X1=0, U3=180, U1=90))
    assert v["X1"]["verdict"] == "no_model"
    assert v["U3"]["verdict"] == "ok"    # read from below, 180 - 0 + 0
    assert v["U1"]["verdict"] == "ok"
    assert sorted(rep["failed"]) == ["X1"]


def test_bottom_two_pad_part_either_way_round(ws):
    """C3 at KiCad 90: 180 - 90 + 0 = 90, a non-polar part also at 270."""
    pcb, cache = ws
    for r in (90, 270):
        v, _ = _verdicts(pcb, cache, _cpl(C3=r))
        assert v["C3"]["verdict"] == "ok", r
    v, _ = _verdicts(pcb, cache, _cpl(C3=0))
    assert v["C3"]["verdict"] == "wrong_rotation"
    assert sorted(v["C3"]["expected_rot"]) == [90.0, 270.0]


def test_bottom_two_pad_part_with_paste_pads(ws):
    """Unnumbered paste-only pads aren't pads a lead lands on: still two pads."""
    pcb, cache = ws
    v, _ = _verdicts(pcb, cache, _cpl(C5=90))
    assert v["C5"]["verdict"] == "ok", v["C5"]


def test_bottom_multi_pad_part_gets_real_verdicts(ws):
    """twin-side's R1: a bottom IC at KiCad 0 goes in at 180. At 0 it's wrong."""
    pcb, cache = ws
    v, rep = _verdicts(pcb, cache, _cpl(U3=180))
    assert v["U3"]["verdict"] == "ok" and v["U3"]["expected_rot"] == [180.0]
    assert rep["bottom_preview"]["refs"] == ["U3"]
    assert "preview" in rep["bottom_preview"]["note"]
    assert "not from a JLC statement" in rep["bottom_preview"]["note"]
    v, rep = _verdicts(pcb, cache, _cpl(U3=0))
    assert v["U3"]["verdict"] == "wrong_rotation"
    assert "needs 180" in v["U3"]["why"]
    assert rep["bottom_preview"] is None


def test_bottom_polar_part_wrong_polarity(ws):
    """The LED model's pad 1 is its anode, KiCad's the cathode: the role fit
    decides, on the bottom as on the top (test_polarity_uses_pin_roles...)."""
    pcb, cache = ws
    v, _ = _verdicts(pcb, cache, _cpl(D2=180))
    assert v["D2"]["verdict"] == "wrong_polarity"
    assert v["D2"]["expected_rot"] == [0.0]
    v, _ = _verdicts(pcb, cache, _cpl(D2=0))
    assert v["D2"]["verdict"] == "ok"
    pads = [("1", -0.7875, 0.0), ("2", 0.7875, 0.0)]
    # no roles in this model, so polarity goes by number: 180 only, never 0
    named = _model(pads, package="LED0603")
    fp = _kfp("D3", [(n, x, -y) for n, x, y in pads], name="aiee:LED0603")
    fp.side = "bottom"
    a = cv.analyse(fp, named)
    assert a["verdict"] == "ok" and a["corrections"] == [0.0]
    assert cv.derive_rotation(fp, named) == 180.0


def test_top_regression_formula_has_no_bottom_term(ws):
    pcb, cache = ws
    fps = cv.board_footprints(pcb)
    model = easyeda.get("C116592", cache, fetch=False)
    assert cv.derive_rotation(fps["U1"], model) == 90.0   # KiCad 90 + 0
    assert cv.derive_rotation(fps["U2"], model) == 180.0  # KiCad 0 + 180
    v, rep = _verdicts(pcb, cache, _cpl(U1=90, U2=180))
    assert v["U1"]["verdict"] == v["U2"]["verdict"] == "ok"
    assert rep["bottom_preview"] is None
    assert cv.cpl_rotation("top", 90.0, 180.0) == 270.0
    assert cv.cpl_rotation("bottom", 90.0, 180.0) == 270.0
    assert cv.cpl_rotation("bottom", 0.0, 90.0) == 270.0
    assert cv.cpl_rotation("bottom", 30.0, 0.0) == 150.0


ASYM = [("1", -1.0, -0.65), ("2", -1.0, 0.65), ("3", 1.0, 0.65),
        ("4", 1.0, 0.0), ("5", 1.0, -0.65)]


def _flipped_fp(r, c=0.0):
    """ASYM's land pattern (the model turned by c), flipped to the bottom as
    KiCad does it, at KiCad rotation r."""
    land = [(n, *cv.rot(x, y, c)) for n, x, y in ASYM]
    fp = _kfp("U9", [(n, x, -y) for n, x, y in land], rot=float(r))
    fp.side = "bottom"
    return fp


@pytest.mark.parametrize("r", [0, 45, 90, 135, 180, 270, 315])
@pytest.mark.parametrize("c", [0, 90, 180, 270])
def test_bottom_rotation_lands_every_pin_seen_from_below(r, c):
    """The oracle doesn't use the fit: board pads from the footprint's own
    transform, mirrored (x -> -x), against the model turned by what the check
    says. Every pin lands on its own pad, and no other turn lands them."""
    model = _model(ASYM)
    fp = _flipped_fp(r, c)
    assert cv.analyse(fp, model)["corrections"] == [float(c)]
    got = cv.derive_rotation(fp, model)
    assert got == (180 - r + c) % 360
    board = {p.number: cv.rot(p.x, p.y, fp.rot) for p in fp.pads}
    below = {n: (-x, y) for n, (x, y) in board.items()}
    for th in range(0, 360, 45):
        err = max(math.hypot(cv.rot(p.x, p.y, th)[0] - below[p.number][0],
                             cv.rot(p.x, p.y, th)[1] - below[p.number][1])
                  for p in model.pads)
        assert (err < 1e-6) == (th == got), (th, got, err)


def test_bottom_footprint_not_flipped_has_no_fit():
    """A land pattern stored unmirrored on the bottom is the part's mirror
    image. No turn seats it, so it's never passed."""
    fp = _kfp("U9", ASYM)
    fp.side = "bottom"
    assert cv.analyse(fp, _model(ASYM))["verdict"] == "no_fit"


def test_bottom_placement_offset_mirrors_the_model_centre():
    """A footprint origin off the part's centre on the bottom. The model's
    centre comes back from the view below with x negated, and the offset that
    results puts its pads on the board's."""
    pads = [("1", -1.5, -1.0), ("2", 1.5, -1.0), ("3", -1.5, 1.0), ("4", 1.0, 1.0)]
    model = _model(pads)
    # the land pattern sits 1.87 mm down the footprint's y, and 1.2 across,
    # flipped, then turned 90
    fp = _kfp("J2", [(n, x + 1.2, -(y + 1.87)) for n, x, y in pads], rot=90.0)
    fp.side = "bottom"
    want = cv.derive_rotation(fp, model)
    assert want == 90.0
    dx, dy = cv.placement_offset(fp, model, want)
    mc = [sum(p.x for p in model.pads) / 4, sum(p.y for p in model.pads) / 4]
    ex, ey = cv.rot(*mc, want)
    kc = [sum(p.x for p in fp.pads) / 4, sum(p.y for p in fp.pads) / 4]
    kx, ky = cv.rot(*kc, fp.rot)
    assert abs((dx - ex) - kx) < 1e-6 and abs((dy + 0) - (ky - ey)) < 1e-6
    # at the moved position the check reads no offset
    row = cv.check_part(fp, model, {"x": fp.x + dx, "y": fp.y + dy, "rot": want})
    assert row["verdict"] == "ok" and row["offset_mm"] == 0.0
    # at the footprint origin it reads the whole offset
    row = cv.check_part(fp, model, {"x": fp.x, "y": fp.y, "rot": want})
    assert row["offset_mm"] == pytest.approx(math.hypot(dx, dy), abs=1e-3)


def test_bom_cpl_bottom_rows_pass_cpl_verify_and_use_the_table_term(ws, tmp_path):
    """bom_cpl writes, cpl_verify agrees; a part with no model takes the
    package table's correction on the 180 - r base."""
    pcb, cache = ws
    parts = [{"ref": "U3", "package": "x", "x": 70.0, "y": -10.0, "rot": 0.0,
              "side": "bottom"},
             {"ref": "C3", "package": "x", "x": 80.0, "y": -10.0, "rot": 90.0,
              "side": "bottom"},
             {"ref": "Q1", "package": "SOT-223", "x": 1.0, "y": 1.0, "rot": 90.0,
              "side": "bottom"},
             {"ref": "Q2", "package": "SOT-223", "x": 1.0, "y": 1.0, "rot": 90.0,
              "side": "top"}]
    derive = bom_cpl.model_deriver(pcb, {r: {"lcsc": c} for r, c in LCSC.items()},
                                   cache, False)
    rules = [(__import__("re").compile("SOT-223"), 180.0)]
    rows, audit = bom_cpl.build_cpl(parts, rules, derive)
    by = {r["Designator"]: r for r in rows}
    assert float(by["U3"]["Rotation"]) == 180.0
    assert float(by["C3"]["Rotation"]) == 90.0
    assert float(by["Q1"]["Rotation"]) == (180 - 90 + 180) % 360
    assert float(by["Q2"]["Rotation"]) == (90 + 180) % 360
    aud = {a["ref"]: a for a in audit}
    assert aud["U3"]["correction"] == 0.0 and aud["U3"]["source"] == "lcsc_model"
    assert aud["Q1"]["correction"] == 180.0 and aud["Q1"]["source"] == "table"
    v, _ = _verdicts(pcb, cache, {r: {"x": float(by[r]["Mid X"]),
                                      "y": -float(by[r]["Mid Y"]),
                                      "rot": float(by[r]["Rotation"]),
                                      "layer": "Bottom"} for r in ("U3", "C3")})
    assert v["U3"]["verdict"] == v["C3"]["verdict"] == "ok"
    assert v["U3"]["offset_mm"] < 0.5


def test_dfm_bottom_verdicts_and_preview_warning(ws, tmp_path):
    pcb, cache = ws
    cpl = tmp_path / "CPL.csv"
    _cpl_csv(cpl, [("U3", 70, 180), ("U1", 10, 90)])
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U3": {"lcsc": "C116592"}, "U1": {"lcsc": "C116592"}}))
    vios: list = []
    dfm_check.check_placement(pcb, cpl, pj, None, cache, False, vios)
    prev = [v for v in vios if v["kind"] == "cpl_bottom_preview"]
    assert len(prev) == 1 and prev[0]["severity"] == "warning"
    assert prev[0]["refs"] == ["U3"] and "order preview" in prev[0]["msg"]
    assert not [v for v in vios if v["severity"] == "error"]

    _cpl_csv(cpl, [("U3", 70, 0)])
    vios = []
    dfm_check.check_placement(pcb, cpl, pj, None, cache, False, vios)
    assert [(v["kind"], v["severity"]) for v in _no_mark_warning_aside(vios)] == [
        ("cpl_rotation", "error")]


def test_render_draws_a_bottom_part_mirrored(ws, tmp_path):
    pcb, cache = ws
    fp = cv.board_footprints(pcb)["U3"]
    model = easyeda.get("C116592", cache, fetch=False)
    pad1 = next(p for p in cpl_render.board_shapes(fp)["pads"]
                if p["number"] == "1")["center"]
    right = cpl_render.model_shapes(model, _cpl(U3=180)["U3"], "bottom")
    wrong = cpl_render.model_shapes(model, _cpl(U3=0)["U3"], "bottom")
    assert math.hypot(right["pin1"][0] - pad1[0], right["pin1"][1] - pad1[1]) < 0.05
    assert math.hypot(wrong["pin1"][0] - pad1[0], wrong["pin1"][1] - pad1[1]) > 1.0
    # every model pad on its own number, not just pin 1
    board = {p["number"]: p["center"] for p in cpl_render.board_shapes(fp)["pads"]}
    for p in right["pads"]:
        assert math.hypot(p["center"][0] - board[p["number"]][0],
                          p["center"][1] - board[p["number"]][1]) < 0.3

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    seen = {}
    real_close = plt.close

    def close(fig=None):
        for ax in fig.axes:
            if ax.get_title():
                seen[ax.get_title().split()[0]] = (ax.get_title(), ax.get_xlim())
        real_close(fig)
    plt.close = close
    try:
        idx = cpl_render.render(pcb, _cpl(U3=180, U1=90), LCSC, cache,
                                tmp_path / "r", fetch=False)
    finally:
        plt.close = real_close
    assert idx["parts"]["U3"]["side"] == "bottom"
    assert idx["parts"]["U1"]["side"] == "top"
    assert "BOTTOM, from below" in seen["U3"][0]
    assert "BOTTOM" not in seen["U1"][0]
    assert seen["U3"][1][0] > seen["U3"][1][1]   # x runs right to left
    assert seen["U1"][1][0] < seen["U1"][1][1]


def test_cache_is_read_without_network(ws, monkeypatch):
    pcb, cache = ws

    def boom(_):
        raise AssertionError("network touched")
    monkeypatch.setattr(easyeda, "_fetch_raw", boom)
    assert easyeda.get("C116592", cache, fetch=True).package.startswith(
        "SOT-23-6")
    assert easyeda.get("C999999", cache, fetch=False) is None


def test_fetch_caches_trimmed_record(tmp_path):
    raw = {"title": "X", "dataStr": {"shape": LED_MODEL["pins"]},
           "packageDetail": {"title": "LED0603", "dataStr": {
               "head": {"x": 4000, "y": 3000},
               "shape": LED_MODEL["pads"] + ["SVGNODE~huge"]}}}
    m = easyeda.get("c900001", tmp_path, fetcher=lambda _: raw)
    assert m.pin_names == {"1": "A", "2": "K"}
    cached = json.loads((tmp_path / "C900001.json").read_text())
    assert "SVGNODE~huge" not in json.dumps(cached)
    # A part EasyEDA does not know is not cached, so a later run asks again.
    assert easyeda.get("C1", tmp_path, fetcher=lambda _: {}) is None
    assert not (tmp_path / "C1.json").exists()


def test_bom_cpl_uses_the_model_and_falls_back_to_the_table(ws):
    pcb, cache = ws
    fps = cv.board_footprints(pcb)
    rules = bom_cpl.load_rotations(bom_cpl.REF_ROTATIONS)
    parts = [{"ref": r, "x": fps[r].x, "y": -fps[r].y, "rot": fps[r].rot,
              "side": "top", "package": fps[r].name}
             for r in ("U1", "U2", "X1")]
    derive = bom_cpl.model_deriver(
        pcb, {r: {"lcsc": c} for r, c in LCSC.items()}, cache, fetch=False)
    cpl, audit = bom_cpl.build_cpl(parts, rules, derive)
    got = {a["ref"]: a for a in audit}
    assert got["U1"]["source"] == "lcsc_model" and got["U1"]["final_rot"] == 90
    assert got["U2"]["source"] == "lcsc_model" and got["U2"]["final_rot"] == 180
    # No model: the table decides (an imported footprint name -> 0).
    assert got["X1"]["source"] == "table" and got["X1"]["final_rot"] == 0


def test_table_no_longer_turns_imported_sot23_5_and_6():
    rules = bom_cpl.load_rotations(bom_cpl.REF_ROTATIONS)
    for name in ("SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BL",
                 "SOT-23-5_L2.9-W1.6-P0.95-LS2.8-BL",
                 "LQFP-48_L7.0-W7.0-P0.50-LS9.0-BL"):
        assert bom_cpl.correct_rotation(name, 90.0, rules)[0] == 90.0, name
    assert bom_cpl.correct_rotation("SOT-23", 0.0, rules)[0] == 180.0


def test_merge_visual_verdicts():
    parts = [{"ref": "U1", "verdict": "ok"}, {"ref": "U2", "verdict": "ok"},
             {"ref": "D1", "verdict": "wrong_polarity"},
             {"ref": "C1", "verdict": "ok"}, {"ref": "C2", "verdict": "ok"}]
    vis = {"model": "claude-sonnet-5-5", "parts": {
        "U1": {"pin1": "match", "polarity": "n/a"},
        "U2": {"pin1": "mismatch", "polarity": "n/a"},
        "D1": {"pin1": "match", "polarity": "mismatch"},
        "C1": {"pin1": "unclear", "polarity": "n/a"}}}
    got = {r["ref"]: r["agreement"] for r in cv.merge_visual(parts, vis)}
    assert got == {"U1": "agree", "U2": "disagree", "D1": "agree",
                   "C1": "unclear", "C2": "missing"}
    assert {r["agreement"] for r in cv.merge_visual(parts, None)} == {
        "not_run"}


def test_dfm_placement_errors_and_visual_disagreement(ws, tmp_path):
    pcb, cache = ws
    cpl = tmp_path / "CPL.csv"
    with cpl.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writerow(["U1", 10, -10, "Top", 270])   # the owner's 180-off part
        w.writerow(["U2", 20, -10, "Top", 180])   # right
    vis = tmp_path / "cpl_visual.json"
    vis.write_text(json.dumps({"parts": {
        "U1": {"pin1": "mismatch", "polarity": "n/a"},
        "U2": {"pin1": "mismatch", "polarity": "n/a"}}}))
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U1": {"lcsc": "C116592"},
                              "U2": {"lcsc": "C116592"}}))
    vios: list = []
    facts = dfm_check.check_placement(pcb, cpl, pj, vis, cache, False, vios)
    vios = _no_mark_warning_aside(vios)
    kinds = sorted((v["kind"], v["refs"][0]) for v in vios)
    assert kinds == [("cpl_rotation", "U1"), ("cpl_visual_disagree", "U2")]
    assert all(v["severity"] == "error" for v in vios)
    assert facts["failed"] == ["U1"]
    # Both layers say U1 is wrong: that is agreement, not a second error.
    assert {p["ref"]: p["agreement"] for p in facts["parts"]} == {
        "U1": "agree", "U2": "disagree"}


def test_render_puts_pin1_dot_on_pad1_only_when_right(ws, tmp_path):
    pcb, cache = ws
    fp = cv.board_footprints(pcb)["U1"]
    model = easyeda.get("C116592", cache, fetch=False)
    pad1 = next(p["center"] for p in cpl_render.board_shapes(fp)["pads"]
                if p["number"] == "1")
    right = cpl_render.model_shapes(model, _cpl(U1=90)["U1"])["pin1"]
    wrong = cpl_render.model_shapes(model, _cpl(U1=270)["U1"])["pin1"]
    assert abs(right[0] - pad1[0]) < 0.05 and abs(right[1] - pad1[1]) < 0.05
    assert abs(wrong[0] - pad1[0]) > 1.0 or abs(wrong[1] - pad1[1]) > 1.0

    idx = cpl_render.render(pcb, _cpl(U1=90, U2=180, D1=0, X1=0), LCSC,
                            cache, tmp_path / "r", fetch=False, per_image=3)
    assert [i["refs"] for i in idx["images"]] == [["D1", "U1", "U2"], ["X1"]]
    for i in idx["images"]:
        png = tmp_path / "r" / i["file"]
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert idx["parts"]["X1"]["has_model"] is False
    assert idx["parts"]["D1"]["polar"] is True


# ---------------------------------------------- cache resolution, fetch errors

def test_cache_order_env_then_board_dir_then_shared(tmp_path, monkeypatch):
    home = tmp_path / "home"
    board = tmp_path / "board"
    board.mkdir()
    parts = board / "parts.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HWDE_EASYEDA_CACHE", raising=False)
    monkeypatch.delenv("AIEE_EASYEDA_CACHE", raising=False)
    shared = home / ".cache" / "hwde" / "easyeda"
    # A board's own dir is its cache, before its first fetch makes it too:
    # models are kept in the board, not only in one agent's HOME.
    assert cv.default_cache(parts) == board / "easyeda"
    assert cv.default_cache(None) == shared
    # The env var wins over both.
    monkeypatch.setenv("HWDE_EASYEDA_CACHE", str(tmp_path / "env"))
    assert cv.default_cache(parts) == tmp_path / "env"
    assert cv.default_cache(None) == tmp_path / "env"


def test_failed_fetch_is_fetch_failed_not_no_model(ws, tmp_path, monkeypatch):
    """A fetch that raises must not read as a missing model (and so not as a
    pin-1 result); an API with no data for the part stays no_model."""
    pcb, _ = ws
    monkeypatch.setattr(easyeda.time, "sleep", lambda s: None)

    def boom(lcsc):
        raise OSError("HTTP Error 403: rate limited")
    monkeypatch.setattr(easyeda, "_fetch_raw", boom)
    rep = cv.verify(pcb, _cpl(U1=90), LCSC, tmp_path / "empty", fetch=True)
    row = rep["parts"][0]
    assert row["verdict"] == "fetch_failed"
    assert "403" in row["why"] and "pin-1" in row["why"]
    assert rep["failed"] == ["U1"] and rep["status"] == "violations"
    assert "fetch_failed" in cv.FAIL_VERDICTS

    # The 403 latched the rate limit for this process; a fresh run starts
    # without it.
    easyeda.reset_rate_limit()
    monkeypatch.setattr(easyeda, "_fetch_raw", lambda lcsc: {})
    rep = cv.verify(pcb, _cpl(U1=90), LCSC, tmp_path / "empty2", fetch=True)
    assert rep["parts"][0]["verdict"] == "no_model"


# ------------------------------------- rate limit: 403/429 stop the fetching

def _http(code):
    import urllib.error

    def fetch(lcsc):
        raise urllib.error.HTTPError(easyeda.API.format(lcsc=lcsc), code,
                                     "Forbidden", None, None)
    return fetch


class _Calls:
    """A fetcher that records each LCSC number it is asked for."""

    def __init__(self, fetch):
        self.fetch, self.asked = fetch, []

    def __call__(self, lcsc):
        self.asked.append(lcsc)
        return self.fetch(lcsc)


@pytest.mark.parametrize("code", [403, 429])
def test_rate_limit_latches_and_later_parts_are_not_fetched(tmp_path, code,
                                                             capsys):
    calls = _Calls(_http(code))
    errs: dict = {}
    assert easyeda.get("C1", tmp_path, fetcher=calls, errors=errs) is None
    assert errs["C1"] == f"HTTP {code} from EasyEDA (rate limited)"
    assert easyeda.rate_limited() == errs["C1"]
    # Every later uncached part is skipped, not asked again.
    for c in ("C2", "C3"):
        assert easyeda.get(c, tmp_path, fetcher=calls, errors=errs) is None
        assert errs[c].startswith("not fetched: HTTP") and "rate" in errs[c]
    assert calls.asked == ["C1"]
    # Said once on stderr, not per part.
    assert capsys.readouterr().err.count("rate limited") == 1
    # A cached part is still read.
    shutil.copy(FIX / "C106245.json", tmp_path / "C106245.json")
    assert easyeda.get("C106245", tmp_path, fetcher=calls) is not None


def test_404_or_empty_is_no_model_and_network_error_is_not(tmp_path):
    import urllib.error
    errs: dict = {}
    assert easyeda.get("C1", tmp_path, fetcher=_http(404), errors=errs) is None
    assert easyeda.get("C2", tmp_path, fetcher=lambda _: {},
                       errors=errs) is None
    assert errs == {} and easyeda.rate_limited() is None

    def down(lcsc):
        raise urllib.error.URLError("Temporary failure in name resolution")
    calls = _Calls(down)
    for c in ("C3", "C4"):
        assert easyeda.get(c, tmp_path, fetcher=calls, errors=errs) is None
    assert errs["C3"].startswith("network error: URLError")
    # A network error is not a rate limit: the next part is still asked.
    assert calls.asked == ["C3", "C4"] and easyeda.rate_limited() is None
    assert easyeda.get("C5", tmp_path, fetcher=_http(500), errors=errs) is None
    assert errs["C5"] == "HTTP 500 from EasyEDA"


def test_no_model_answer_is_kept_and_read_offline(tmp_path):
    """404 / empty / padless: EasyEDA answered, kept as <LCSC>.none, read
    offline as "no model" (no error); a part never asked about is a miss
    with the reason; a later model replaces the marker."""
    board, shared = tmp_path / "board", easyeda.shared_cache()
    assert easyeda.get("C1", board, fetcher=_http(404)) is None
    assert easyeda.get("C2", board, fetcher=lambda _: {}) is None
    for c in ("C1", "C2"):
        assert easyeda.none_known(c, board) and easyeda.none_known(c, shared)
        errs: dict = {}
        assert easyeda.get(c, board, fetch=False, errors=errs) is None
        assert errs == {}, c
    errs = {}
    assert easyeda.get("C3", board, fetch=False, errors=errs) is None
    assert "offline" in errs["C3"] and "not in the cache" in errs["C3"]
    # a marker only in the shared cache is read, and nothing is written
    # into the dir an offline run checks (a pinned fixture stays frozen)
    other = tmp_path / "other"
    assert easyeda.get("C1", other, fetch=False, errors=errs) is None
    assert "C1" not in errs and not other.exists()
    # a network error is not an answer: no marker
    import urllib.error

    def down(_):
        raise urllib.error.URLError("x")
    assert easyeda.get("C4", board, fetcher=down, errors={}) is None
    assert not easyeda.none_known("C4", board)
    # a model now: the marker goes
    raw = json.loads((FIX / "C106245.json").read_text())
    assert easyeda.get("C1", board, fetcher=lambda _: _raw(raw)) is not None
    assert not easyeda.none_known("C1", board)
    assert not easyeda.none_known("C1", shared)


def test_offline_cold_cache_names_the_fetch_and_the_way_to_fill_it(
        ws, tmp_path):
    pcb, _ = ws
    cpl = tmp_path / "CPL.csv"
    cpl.write_text("Designator,Mid X,Mid Y,Layer,Rotation\nU1,10,-10,Top,90\n")
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U1": {"lcsc": "C116592"}}))
    vios: list = []
    dfm_check.check_placement(pcb, cpl, pj, None, tmp_path / "empty",
                              False, vios)
    v = [v for v in vios if v["kind"] == "cpl_fetch_failed"][0]
    assert "EasyEDA footprint model" in v["msg"] and "CPL pin-1" in v["msg"]
    assert "offline" in v["reason"] and "HWDE_EASYEDA_OFFLINE" in v["msg"]
    assert "rerun once EasyEDA answers" not in v["msg"]


def test_cpl_verify_403_fails_first_part_and_skips_the_rest(ws, tmp_path,
                                                            monkeypatch):
    pcb, _ = ws
    monkeypatch.setattr(easyeda.time, "sleep", lambda s: None)
    calls = _Calls(_http(403))
    monkeypatch.setattr(easyeda, "_fetch_raw", calls)
    lcsc = {"U1": "C116592", "C1": "C106245", "C2": "C106675"}
    rep = cv.verify(pcb, _cpl(U1=90, C1=0, C2=0), lcsc, tmp_path / "empty",
                    fetch=True)
    rows = {r["ref"]: r for r in rep["parts"]}
    assert {r["verdict"] for r in rows.values()} == {"fetch_failed"}
    assert calls.asked == ["C106245"]   # first in natural order, then none
    assert "HTTP 403" in rows["C1"]["fetch_error"]
    assert rows["U1"]["fetch_error"].startswith("not fetched")
    assert rep["rate_limited"] == "HTTP 403 from EasyEDA (rate limited)"
    assert rep["fetch_failed"] == ["C1", "C2", "U1"]
    assert rep["status"] == "violations"


def test_dfm_reports_a_refused_fetch_once_never_as_no_model(ws, tmp_path,
                                                            monkeypatch):
    pcb, _ = ws
    monkeypatch.setattr(easyeda.time, "sleep", lambda s: None)
    monkeypatch.setattr(easyeda, "_fetch_raw", _http(403))
    cpl = tmp_path / "CPL.csv"
    with cpl.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writerow(["U1", 10, -10, "Top", 90])
        w.writerow(["C1", 40, -10, "Top", 0])
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U1": {"lcsc": "C116592"},
                              "C1": {"lcsc": "C106245"}}))
    vios: list = []
    facts = dfm_check.check_placement(pcb, cpl, pj, None, tmp_path / "empty",
                                      True, vios)
    vios = _no_mark_warning_aside(vios)
    assert [v["kind"] for v in vios] == ["cpl_fetch_failed"]
    v = vios[0]
    assert v["severity"] == "error" and sorted(v["refs"]) == ["C1", "U1"]
    assert "HTTP 403" in v["msg"] and "rate limited" in v["msg"]
    assert "rerun" in v["msg"] and v["rate_limited"] is True
    assert facts["fetch_failed"] == ["C1", "U1"]
    assert "cpl_fetch_failed" in dfm_check.DFM_FAMILIES["placement"]


def test_bom_cpl_says_fetch_failed_not_none_and_fails_the_run(ws, tmp_path,
                                                              monkeypatch):
    pcb, _ = ws
    monkeypatch.setattr(easyeda.time, "sleep", lambda s: None)
    calls = _Calls(_http(403))
    monkeypatch.setattr(easyeda, "_fetch_raw", calls)
    fps = cv.board_footprints(pcb)
    pos = tmp_path / "pos.csv"
    lines = ["Ref,Val,Package,PosX,PosY,Rot,Side"]
    for r in ("U1", "X1", "C1"):
        f = fps[r]
        lines.append(f"{r},v,{f.name},{f.x},{-f.y},{f.rot},top")
    pos.write_text("\n".join(lines) + "\n")
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U1": {"lcsc": "C116592"},
                              "X1": {"lcsc": "C116593"},
                              "C1": {"lcsc": "C106245"}}))
    rep = bom_cpl.run(pcb, tmp_path / "fab", pos=pos, parts_json=pj,
                      easyeda_cache=tmp_path / "empty", fetch_models=True)
    got = {a["ref"]: a for a in rep["rotation_audit"]}
    assert {a["source"] for a in got.values()} == {"fetch_failed"}
    assert "HTTP 403" in got["C1"]["fetch_error"]
    assert len(calls.asked) == 1
    assert rep["model_fetch_failed"] == ["C1", "U1", "X1"]
    assert rep["easyeda_rate_limited"].startswith("HTTP 403")
    assert rep["status"] == "violations"
    vio = [v for v in rep["violations"]
           if v["kind"] == "cpl_model_fetch_failed"]
    assert len(vio) == 1 and "rate limited" in vio[0]["message"]


def test_render_records_fetch_error_apart_from_no_model(ws, tmp_path,
                                                        monkeypatch):
    pcb, _ = ws
    monkeypatch.setattr(easyeda.time, "sleep", lambda s: None)
    easyeda.reset_rate_limit()

    def forbidden(lcsc):
        raise OSError("HTTP Error 403: rate limited")
    monkeypatch.setattr(easyeda, "_fetch_raw", forbidden)
    idx = cpl_render.render(pcb, _cpl(U1=90), LCSC, tmp_path / "empty",
                            tmp_path / "r", fetch=True)
    part = idx["parts"]["U1"]
    assert part["has_model"] is False and "403" in part["fetch_error"]
    assert idx["fetch_failed"] == ["U1"] and idx["rate_limited"]
    assert "rerun" in idx["note"]
    easyeda.reset_rate_limit()

    monkeypatch.setattr(easyeda, "_fetch_raw", lambda lcsc: {})
    idx = cpl_render.render(pcb, _cpl(U1=90), LCSC, tmp_path / "empty2",
                            tmp_path / "r2", fetch=True)
    assert idx["parts"]["U1"]["has_model"] is False
    assert "fetch_error" not in idx["parts"]["U1"]
    assert idx["fetch_failed"] == [] and "note" not in idx
    easyeda.reset_rate_limit()


# ------------------------- models kept in the board; a refused fetch is unknown

def _raw(model):
    return {"title": model["title"], "dataStr": {"shape": model["pins"]},
            "packageDetail": {"title": model["package"], "dataStr": {
                "head": {"x": model["origin"][0], "y": model["origin"][1]},
                "shape": model["pads"]}}}


def test_board_cache_hit_never_asks_easyeda(ws, tmp_path):
    """A board that carries its models (a fresh checkout or a scratch copy of
    it) verifies with EasyEDA refusing every request."""
    pcb, cache = ws
    calls = _Calls(_http(403))
    errs: dict = {}
    assert easyeda.get("C116592", cache, fetcher=calls, errors=errs)
    assert calls.asked == [] and errs == {} and easyeda.rate_limited() is None


def test_fetch_keeps_the_model_in_the_board_and_the_shared_cache(tmp_path,
                                                                 monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setenv("HWDE_EASYEDA_CACHE", str(shared))
    monkeypatch.setattr(easyeda, "shared_cache", lambda: shared)
    board = tmp_path / "board" / "parts" / "easyeda"   # not there yet
    m = easyeda.get("C900001", board, fetcher=lambda _: _raw(LED_MODEL))
    assert m is not None and m.pin_names == {"1": "A", "2": "K"}
    assert (board / "C900001.json").is_file()
    assert (shared / "C900001.json").is_file()
    # The next run of the board reads it there, with EasyEDA refusing.
    calls = _Calls(_http(403))
    assert easyeda.get("C900001", board, fetcher=calls) is not None
    assert calls.asked == []


def test_shared_cache_fills_the_board_cache(tmp_path, monkeypatch):
    """A model this machine fetched for another board is read, and copied
    into this board's cache so the board carries it from then on."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shutil.copy(FIX / "C116592.json", shared / "C116592.json")
    monkeypatch.setattr(easyeda, "shared_cache", lambda: shared)
    board = tmp_path / "board" / "easyeda"
    calls = _Calls(_http(403))
    assert easyeda.get("C116592", board, fetch=False) is not None
    assert (board / "C116592.json").is_file()
    assert easyeda.get("C116592", board, fetcher=calls) is not None
    assert calls.asked == []


def _cpl_csv(path, rows):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for ref, x, rot in rows:
            w.writerow([ref, x, -10, "Top", rot])
    return path


def test_dfm_403_with_no_cache_is_unverified_not_failed(ws, tmp_path,
                                                        monkeypatch):
    pcb, _ = ws
    monkeypatch.setattr(easyeda.time, "sleep", lambda s: None)
    monkeypatch.setattr(easyeda, "_fetch_raw", _http(403))
    cpl = _cpl_csv(tmp_path / "CPL.csv", [("U1", 10, 90), ("C1", 40, 0)])
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U1": {"lcsc": "C116592"},
                              "C1": {"lcsc": "C106245"}}))
    vios: list = []
    dfm_check.check_placement(pcb, cpl, pj, None, tmp_path / "empty", True,
                              vios)
    g = dfm_check.grade(vios)
    assert g["status"] == "error"          # exit 2, never 1
    assert sorted(g["unverified"]["refs"]) == ["C1", "U1"]
    assert "HTTP 403" in g["unverified"]["reason"]
    assert g["error"].startswith("placement unverified, not failed")
    assert checklib.emit({**g}, str(tmp_path / "r.json")) == 2
    # A real finding beside it still fails the board.
    vios.append({"severity": "error", "kind": "dfm_trace_width", "refs": []})
    assert dfm_check.grade(vios) == {"status": "violations"}
    # Warnings alone pass.
    assert dfm_check.grade([{"severity": "warning", "kind": "cpl_offset"}]) \
        == {"status": "pass"}


def test_dfm_rotation_mismatch_from_the_cache_still_fails(ws, tmp_path,
                                                          monkeypatch):
    pcb, cache = ws
    monkeypatch.setattr(easyeda, "_fetch_raw", _http(403))
    cpl = _cpl_csv(tmp_path / "CPL.csv", [("U1", 10, 270)])
    pj = tmp_path / "parts.json"
    pj.write_text(json.dumps({"U1": {"lcsc": "C116592"}}))
    vios: list = []
    dfm_check.check_placement(pcb, cpl, pj, None, cache, True, vios)
    assert [v["kind"] for v in _no_mark_warning_aside(vios)] == ["cpl_rotation"]
    assert dfm_check.grade(vios) == {"status": "violations"}
    assert easyeda.rate_limited() is None   # never asked


# ------- imported (Eagle) libraries: hw-lab eda-crossover -------

def _model(pads, names=None, package="X"):
    from types import SimpleNamespace as NS
    return NS(lcsc="C1", package=package, pin_names=names or {},
              pads=[NS(number=n, x=x, y=y) for n, x, y in pads])


def _kfp(ref, pads, rot=0.0, name="SparkFun:PART"):
    return cv.Footprint(ref, name, 10.0, 10.0, rot, "top",
                        [cv.KPad(n, x, y, 0.6, 0.6) for n, x, y in pads])


def test_eagle_pads_a_c_are_anode_cathode():
    """A board pad named C beside one named A is a cathode: the LED that the
    package table would have placed backwards (K on A) gets 180."""
    fp = _kfp("LED1", [("A", -0.88, 0), ("C", 0.88, 0)], name="SparkFun:LED-0603")
    model = _model([("1", 0.8, 0), ("2", -0.8, 0)], {"1": "A", "2": "K"}, "LED0603")
    a = cv.analyse(fp, model)
    assert a["verdict"] == "ok" and "role" in a["basis"]
    assert a["corrections"] == [180.0]
    fp = _kfp("LED1", [("A", 0.88, 0), ("C", -0.88, 0)], name="SparkFun:LED-0603")
    assert cv.analyse(fp, model)["corrections"] == [0.0]
    assert cv.board_roles(fp, model) == {"A": "A", "C": "K"}


def test_eagle_p1_pads_match_model_numbers():
    pads = [("1", -0.85, 0.7), ("2", 0.85, 0.7), ("3", 0.85, -0.7), ("4", -0.85, -0.7)]
    fp = _kfp("Y1", [("P" + n, x, y) for n, x, y in pads])
    assert cv.analyse(fp, _model(pads))["verdict"] == "ok"


def test_switch_pads_fit_by_geometry_not_numbers():
    """LCSC's KMR2 model numbers its pins across the short side; SparkFun's
    footprint along the long one. A switch only has to land on its pads;
    anything else with that mismatch stays no_fit."""
    model = _model([("1", 2.05, -0.8), ("2", 2.05, 0.8), ("3", -2.05, 0.8), ("4", -2.05, -0.8)])
    board = [("1", -2.05, 0.8), ("2", 2.05, 0.8), ("3", 2.05, -0.8), ("4", -2.05, -0.8)]
    a = cv.analyse(_kfp("S1", board), model)
    assert a["verdict"] == "ok" and "geometry" in a["basis"] and set(a["corrections"]) == {0.0, 180.0}
    # the turn with pin 1 on pad 1 first: model 1 is (2.05, -0.8), board 1 (-2.05, 0.8)
    assert a["corrections"][0] == 180.0
    # not a switch: the pads land, but its pin map is for a person (#1024)
    assert cv.analyse(_kfp("U1", board), model)["verdict"] == cv.PIN_MAP
    wider = [("1", -3.0, 0.8), ("2", 3.0, 0.8), ("3", 3.0, -0.8), ("4", -3.0, -0.8)]
    assert cv.analyse(_kfp("S1", wider), model)["verdict"] == "no_fit"


def test_placement_offset_ignores_a_thermal_pad_split_differently():
    """ESP32-S3-WROOM-1: pad 41 is 21 entries on the board and 9 in the LCSC
    model, not all centred. The perimeter pads coincide, so there is no offset;
    a plain mean called it 0.6 mm and the CPL put the module off its pads."""
    ring = [("1", -4.0, -4.0), ("2", 4.0, -4.0), ("3", 4.0, 4.0), ("4", -4.0, 4.0)]
    model = _model(ring + [("41", x, 0.0) for x in (-1.0, 0.0, 1.0)])
    pads = ring + [("41", x * 0.5 + 1.0, y * 0.5 + 1.0) for x in (-1, 0, 1) for y in (-1, 0, 1)]
    fp = _kfp("U1", pads)
    assert cv.placement_offset(fp, model, 0.0) is None
    row = cv.check_part(fp, model, {"rot": 0.0, "x": 10.0, "y": 10.0})
    assert row["offset_mm"] == 0.0


def test_placement_offset_when_origin_isnt_the_part_centre():
    """A connector whose footprint origin is off its pads' centre: the CPL has
    to carry the difference (SparkFun's Qwiic 1.87 mm), or JLC places it off
    its pads. A small one is left alone."""
    model = _model([("1", -1.5, -1.0), ("2", 1.5, -1.0), ("3", -1.5, 1.0), ("4", 1.5, 1.0)])
    pads = [(n, x, y + 1.87) for n, x, y in
            [("1", -1.5, -1.0), ("2", 1.5, -1.0), ("3", -1.5, 1.0), ("4", 1.5, 1.0)]]
    dx, dy = cv.placement_offset(_kfp("J2", pads), model, 0.0)
    assert abs(dx) < 1e-6 and abs(dy - 1.87) < 1e-6
    near = [(n, x, y - 1.87 + 0.1) for n, x, y in pads]
    assert cv.placement_offset(_kfp("J2", near), model, 0.0) is None


# ------------------------------------------- Standard PCBA for parts on both sides

def _pcba_board(tmp_path, w, h, fiducials=0, margin=8.0):
    """A w x h board with two pads `margin` in from the edges, and fiducials
    in the margin."""
    edges = "".join(
        f'(gr_line (start {a} {b}) (end {c} {d}) (layer "Edge.Cuts") '
        '(stroke (width 0.1) (type solid)))'
        for a, b, c, d in ((0, 0, w, 0), (w, 0, w, h), (w, h, 0, h), (0, h, 0, 0)))
    pad = ('(footprint "t:R" (layer "F.Cu") (at {x} {y}) (property "Reference" '
           '"{r}" (at 0 0) (layer "F.SilkS")) (pad "1" smd rect (at 0 0) '
           '(size 1 1) (layers "F.Cu")))')
    body = pad.format(x=margin, y=margin, r="R1") \
        + pad.format(x=w - margin, y=h - margin, r="R2")
    for i in range(fiducials):
        body += ('(footprint "Fiducial:Fiducial_1mm" (layer "F.Cu") '
                 f'(at {2 + i} 2) (property "Reference" "FID{i}" (at 0 0) '
                 '(layer "F.SilkS")) (pad "" smd circle (at 0 0) (size 1 1) '
                 '(layers "F.Cu")))')
    f = tmp_path / "b.kicad_pcb"
    f.write_text('(kicad_pcb (version 20240108) (generator "t")'
                 '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                 '(44 "Edge.Cuts" user))' + edges + body + ")")
    import geom
    return geom.load_board(f)


def _layers_csv(tmp_path, *layers):
    cpl = tmp_path / "CPL.csv"
    with cpl.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for i, lay in enumerate(layers):
            w.writerow([f"R{i}", 1, -1, lay, 0])
    return cpl


def _pcba_kinds(bg, cpl):
    vios: list = []
    dfm_check._standard_pcba(bg, cpl, vios)
    return vios


def test_both_sides_small_board_warns_with_what_it_lacks(tmp_path):
    """twin-side's R2: 70 x 58, no fiducials, no rails. Warns, never fails."""
    bg = _pcba_board(tmp_path, 70, 58, margin=2.0)
    v = _pcba_kinds(bg, _layers_csv(tmp_path, "Top", "Bottom"))
    assert [(x["kind"], x["severity"]) for x in v] == [
        ("both_sides_needs_panel", "warning")]
    for word in ("70.0 x 58.0", "rails", "0 fiducials", "Standard"):
        assert word in v[0]["msg"], word


def test_one_sided_or_no_cpl_says_nothing(tmp_path):
    bg = _pcba_board(tmp_path, 40, 40)
    assert _pcba_kinds(bg, _layers_csv(tmp_path, "Top", "Top")) == []
    assert _pcba_kinds(bg, _layers_csv(tmp_path, "Bottom")) == []
    assert _pcba_kinds(bg, tmp_path / "missing.csv") == []
    assert _pcba_kinds(bg, None) == []


def test_both_sides_board_that_is_its_own_panel_passes(tmp_path):
    bg = _pcba_board(tmp_path, 80, 70, fiducials=3)
    assert _pcba_kinds(bg, _layers_csv(tmp_path, "Top", "Bottom")) == []
    # each thing on its own is still missed
    for kw, word in (({"fiducials": 2}, "2 fiducials"),
                     ({"fiducials": 3, "margin": 2.0}, "rails")):
        bg = _pcba_board(tmp_path, 80, 70, **kw)
        v = _pcba_kinds(bg, _layers_csv(tmp_path, "Top", "Bottom"))
        assert len(v) == 1 and word in v[0]["msg"]
    bg = _pcba_board(tmp_path, 80, 60, fiducials=3)
    v = _pcba_kinds(bg, _layers_csv(tmp_path, "Top", "Bottom"))
    assert len(v) == 1 and "under 70 mm" in v[0]["msg"]


@pytest.mark.parametrize("sub", ["panel", "."])
def test_a_declared_panel_is_accepted(tmp_path, sub):
    """panel.json as eda-crossover's panel generator writes it, under fab/panel
    or beside the CPL, stands in for the small board."""
    bg = _pcba_board(tmp_path, 70, 58, margin=2.0)
    cpl = _layers_csv(tmp_path, "Top", "Bottom")
    d = tmp_path / sub
    d.mkdir(exist_ok=True)
    good = {"width_mm": 70.0, "height_mm": 128.0,
            "rail_mm": {"sides": 5.0, "top_bottom": 5.0},
            "fiducials": [[10, 3.85], [60, 3.85], [15, 124.15]]}
    (d / "panel.json").write_text(json.dumps(good))
    assert _pcba_kinds(bg, cpl) == []
    (d / "panel.json").write_text(json.dumps({**good, "height_mm": 60.0}))
    v = _pcba_kinds(bg, cpl)
    assert len(v) == 1 and "panel.json" in v[0]["msg"] and "under 70" in v[0]["msg"]
    (d / "panel.json").write_text(json.dumps({**good, "rail_mm": 0}))
    assert "rails" in _pcba_kinds(bg, cpl)[0]["msg"]
    (d / "panel.json").write_text(json.dumps({**good, "fiducials": []}))
    assert "0 fiducials" in _pcba_kinds(bg, cpl)[0]["msg"]


# ---------------------------------------- an LED* footprint, K on pad 1 (C2286)

# boards/twin-side/parts/easyeda/C2286.json on hw-lab h2/twin-side-route:
# KT-0603R, pad 2 = K west, pad 1 = A east, as the LCSC model has it.
C2286 = {"lcsc": "C2286", "title": "KT-0603R",
         "package": "LED-SMD_L1.6-W0.8-R-RD", "origin": [4000.8828, 3003.1398],
         "pads": ["PAD~RECT~3997.93~3003.14~3.1496~3.1496~1~~2~0~~0~gge5",
                  "PAD~RECT~4003.836~3003.14~3.1496~3.1496~1~~1~0~~0~gge15"],
         "silk": [],
         "pins": ["P~show~0~2~-20~-5~180~gge28~0^^-20~-5^^M -20 -5 h 15~#800"
                  "^^0~-1~-5~0~K~start~~~#800^^0~-9~-9~0~2~end~~~#800",
                  "P~show~0~1~20~-5~0~gge37~0^^20~-5^^M 20 -5 h -15~#800"
                  "^^0~1~-5~0~A~end~~~#800^^0~9~-9~0~1~start~~~#800"]}
PASSIVES_LED = [("1", -0.7875, 0.0), ("2", 0.7875, 0.0)]


def test_led_footprint_is_k_on_pad_1_and_the_model_turns_to_match():
    """Device:LED on passives:LED0603, no pinfunctions: pad 1 = K on the west,
    as the model's K is. Correction 0. The number fit (180) put it on backwards
    with verdict ok."""
    model = easyeda.parse(C2286)
    fp = _kfp("D102", PASSIVES_LED, name="passives:LED0603")
    a = cv.analyse(fp, model)
    assert a["verdict"] == "ok" and a["corrections"] == [0.0]
    assert a["basis"].startswith("pin role")
    assert a["number_corrections"] == [180.0]
    assert cv.derive_rotation(fp, model) == 0.0
    row = cv.check_part(fp, model, {"x": 10.0, "y": 10.0, "rot": 180.0})
    assert row["verdict"] == "wrong_polarity"
    assert cv.check_part(fp, model, {"x": 10.0, "y": 10.0, "rot": 0.0})["verdict"] == "ok"


def test_led_footprint_on_the_bottom_follows_the_bottom_rule():
    model = easyeda.parse(C2286)
    fp = _kfp("D301", [(n, x, -y) for n, x, y in PASSIVES_LED], rot=-90.0,
              name="passives:LED0603")
    fp.side = "bottom"
    assert cv.analyse(fp, model)["corrections"] == [0.0]
    assert cv.derive_rotation(fp, model) == 270.0     # 180 + 90 + 0


def test_imported_led_footprint_keeps_its_models_numbering():
    """The LED rule mustn't reach a footprint pulled from the part itself."""
    model = easyeda.parse(C2286)
    pads = [("2", -0.75, 0.0), ("1", 0.75, 0.0)]
    fp = _kfp("D1", pads, name="mcu:LED-SMD_L1.6-W0.8-R-RD")
    assert cv.board_roles(fp, model) == {"2": "K", "1": "A"}
    a = cv.analyse(fp, model)
    assert a["corrections"] == [0.0] and "number_corrections" not in a


@pytest.mark.parametrize("ref,pads,name", [
    ("U1", PASSIVES_LED, "passives:LED0603"),                  # not a D/LED ref
    ("D1", PASSIVES_LED + [("3", 0.0, 1.0)], "x:LED-RGB"),     # not two pads
    ("D1", PASSIVES_LED, "passives:D0603"),                    # not LED*
])
def test_led_rule_stays_narrow(ref, pads, name):
    assert cv.board_roles(_kfp(ref, pads, name=name), None) == {}


# ------------------------------------- offset limit scales with pad pitch (#683)

def _grid(pitch, n=3, first="A1"):
    """An n x n ball grid at `pitch`, centred, balls named A1.."""
    c = (n - 1) / 2
    return [(f"{chr(65 + r)}{k + 1}", (k - c) * pitch, (r - c) * pitch)
            for r in range(n) for k in range(n)]


def _row_at(pads, off, ref="U1"):
    """check_part with the CPL `off` mm (in x) from the footprint origin; the
    model's pads are the board's, so only the offset can fail."""
    fp = cv.Footprint(ref, "X:PART", 10.0, 10.0, 0.0, "top",
                      [cv.KPad(n, x, y, 0.3, 0.3) for n, x, y in pads])
    return cv.check_part(fp, _model(pads), {"rot": 0.0, "x": 10.0 + off, "y": 10.0})


def test_offset_limit_scales_with_pad_pitch():
    """A 0.5 mm BGA 0.24 mm off its balls fails; a 1206 0.24 mm off may pass."""
    assert cv.offset_limit_mm(_kfp("U1", _grid(0.5))) == pytest.approx(0.2)
    assert cv.offset_limit_mm(_kfp("U1", _grid(0.8))) == pytest.approx(0.32)
    assert cv.offset_limit_mm(_kfp("R1", [("1", -1.5, 0), ("2", 1.5, 0)])) == cv.OFFSET_WARN_MM
    bga = _row_at(_grid(0.5), 0.24)
    assert bga["offset_mm"] == pytest.approx(0.24, abs=1e-3)
    assert bga["verdict"] == "wrong_offset" and bga["verdict"] in cv.FAIL_VERDICTS
    assert _row_at(_grid(0.5), 0.15)["verdict"] == "ok"
    r1206 = _row_at([("1", -1.5, 0), ("2", 1.5, 0)], 0.24, "R1")
    assert r1206["verdict"] == "ok" and r1206["offset_mm"] == pytest.approx(0.24, abs=1e-3)
    # past the flat limit a coarse part is still only a warning
    assert _row_at([("1", -1.5, 0), ("2", 1.5, 0)], 0.6, "R1")["verdict"] == "ok"


def test_fine_pitch_row_carries_its_limit():
    row = _row_at(_grid(0.5), 0.24)
    assert row["offset_limit_mm"] == pytest.approx(0.2)


# ------------------------------- render: BGA pin-1 marker, no invented K (#683, #673)

def test_bga_pin1_is_a1_else_lowest_row_then_column():
    mk = lambda pads: cv.Footprint("U1", "X:BGA", 0, 0, 0, "top",
                                   [cv.KPad(n, x, y, 0.3, 0.3) for n, x, y in pads])
    assert cpl_render.bga_pin1(mk(_grid(0.5))).number == "A1"
    no_a1 = [p for p in _grid(0.5) if p[0] != "A1"]
    assert cpl_render.bga_pin1(mk(no_a1)).number == "A2"
    no_row_a = [p for p in _grid(0.5, 4) if p[0][0] != "A"]
    assert cpl_render.bga_pin1(mk(no_row_a)).number == "B1"
    assert cpl_render.bga_pin1(mk([("1", 0, 0), ("2", 1, 0)])) is None


def test_polar_without_roles_is_unknown_not_k(ws, tmp_path):
    pcb, cache = ws
    idx = cpl_render.render(pcb, _cpl(U1=90, U2=180, D1=0), LCSC, cache,
                            tmp_path / "r", fetch=False)

    fp = cv.board_footprints(pcb)["U1"]
    assert cpl_render.polar_label({}, fp) == ("1", "?")


def test_render_marks_pin1_on_a_bga(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + _fp(
        "U9", "Package_BGA:BGA-9", 10, 10, 0, _grid(0.5), size=(0.3, 0.3)) + ")")
    idx = cpl_render.render(pcb, {"U9": {"x": 10, "y": 10, "rot": 0.0, "layer": "Top"}},
                            {}, tmp_path / "cache", tmp_path / "r", fetch=False)
    assert idx["parts"]["U9"]["bga_pin1"] == "A1"


# ------------------------- a ball grid with no model: basis footprint (#698)

# The real hdi-bga packages: WLCSP-49, UFBGA-64, TFBGA-54, FBGA-153.
HDI = json.loads((REPO / "tests/fixtures/hdi_bga_balls.json").read_text())
HDI_REFS = ("U401", "U402", "U403", "U404")


def _hdi_pcb(tmp_path, rots=None, layer="F.Cu"):
    """A board with the four hdi-bga packages and an empty parts map."""
    rots = rots or {}
    body = "".join(
        _fp(r, HDI[r]["footprint"], 20.0 * i, 20.0, rots.get(r, 0),
            [tuple(p) for p in HDI[r]["pads"]],
            size=(HDI[r]["size"], HDI[r]["size"]), layer=layer)
        for i, r in enumerate(HDI_REFS))
    pcb = tmp_path / "hdi.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + body + ")")
    return pcb


def _hdi_cpl(rots=None, layer="Top"):
    rots = rots or {}
    return {r: {"x": 20.0 * i, "y": 20.0, "rot": float(rots.get(r, 0)), "layer": layer}
            for i, r in enumerate(HDI_REFS)}


def _ball_fp(c=0.0, n=4, m=3, side="top", ref="U1", board_rot=0.0):
    """A standard grid (A1 upper left, columns to the right, rows downward)
    turned by `c` on the footprint, m rows of n columns. On the bottom its
    local pads are mirrored in y, as a flipped KiCad footprint's are."""
    pads = []
    for r in range(m):
        for k in range(n):
            x, y = cv.rot((k - (n - 1) / 2) * 0.8, (r - (m - 1) / 2) * 0.8, c)
            pads.append(cv.KPad(f"{chr(65 + r)}{k + 1}", x,
                                y if side == "top" else -y, 0.3, 0.3))
    return cv.Footprint(ref, "X:BGA", 10.0, 10.0, board_rot, side, pads)


def _footprint_row(fp, rot):
    return cv.verify(Path("nowhere.kicad_pcb"),
                     {fp.ref: {"x": fp.x, "y": fp.y, "rot": float(rot), "layer": "Top"}},
                     {}, Path("/nonexistent"), fetch=False, fps={fp.ref: fp})


@pytest.mark.parametrize("ref", HDI_REFS)
def test_hdi_bga_gets_a_rotation_verdict_not_no_model(ref, tmp_path):
    """Known answer (#698): every package has A1 upper left, so CPL 0."""
    rep = cv.verify(_hdi_pcb(tmp_path), _hdi_cpl(), {}, tmp_path / "c", fetch=False)
    row = {r["ref"]: r for r in rep["parts"]}[ref]
    assert row["verdict"] == cv.FOOTPRINT_MATCH
    assert row["basis"] == "footprint" and row["expected_rot"] == [0.0]
    assert row["verdict"] not in cv.FAIL_VERDICTS and row["verdict"] != "ok"
    assert ref in rep["footprint_basis"]["refs"]
    assert "tape" in rep["footprint_basis"]["note"]


def test_turned_u404_row_fails(tmp_path):
    """Known answer (#698): a deliberately turned CPL row fails, alone."""
    rep = cv.verify(_hdi_pcb(tmp_path), _hdi_cpl({"U404": 90}), {},
                    tmp_path / "c", fetch=False)
    row = {r["ref"]: r for r in rep["parts"]}["U404"]
    assert row["verdict"] == "wrong_rotation" and row["basis"] == "footprint"
    assert row["expected_rot"] == [0.0] and "A1" in row["why"]
    assert rep["failed"] == ["U404"] and rep["status"] == "violations"
    assert "U404" not in rep["footprint_basis"]["refs"]


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_every_wrong_quarter_turn_of_u404_fails(turn, tmp_path):
    rep = cv.verify(_hdi_pcb(tmp_path), _hdi_cpl({"U404": turn}), {},
                    tmp_path / "c", fetch=False)
    assert rep["failed"] == ["U404"]


@pytest.mark.parametrize("c", [0.0, 90.0, 180.0, 270.0])
@pytest.mark.parametrize("side", ["top", "bottom"])
def test_footprint_correction_equals_the_model_fit(c, side):
    """An independent answer: a real model of the standard part, fitted by
    pad number, must give the correction read from the footprint alone."""
    fp = _ball_fp(c, side=side)
    std = _model([(p.number, *cv.rot(p.x, p.y if side == "top" else -p.y, -c))
                  for p in fp.pads])
    fitted = cv.analyse(fp, std)["corrections"]
    assert fitted == pytest.approx([c], abs=1e-6)
    assert cv.footprint_a1(fp)["correction"] == pytest.approx(c, abs=1e-6)


@pytest.mark.parametrize("side,board_rot,want", [
    ("top", 0.0, 0.0), ("top", 90.0, 90.0), ("top", 270.0, 270.0),
    ("bottom", 0.0, 180.0), ("bottom", 90.0, 90.0), ("bottom", 180.0, 0.0)])
def test_expected_rotation_follows_the_side_rule(side, board_rot, want):
    """Top: board + c. Bottom: 180 - board + c, read from below."""
    fp = _ball_fp(side=side, board_rot=board_rot)
    row = _footprint_row(fp, want)["parts"][0]
    assert row["verdict"] == cv.FOOTPRINT_MATCH and row["expected_rot"] == [want]
    assert _footprint_row(fp, want + 90)["parts"][0]["verdict"] == "wrong_rotation"


def test_bottom_footprint_basis_also_asks_for_the_bottom_preview():
    rep = _footprint_row(_ball_fp(side="bottom"), 180.0)
    assert rep["bottom_preview"]["refs"] == ["U1"]


def test_a1_turned_in_the_footprint_moves_the_expected_rotation():
    """A library footprint with A1 lower right needs CPL 180 at board rot 0."""
    fp = _ball_fp(180.0)
    assert _footprint_row(fp, 180.0)["parts"][0]["verdict"] == cv.FOOTPRINT_MATCH
    assert _footprint_row(fp, 0.0)["parts"][0]["verdict"] == "wrong_rotation"


def test_depopulated_grid_reads_the_axes_not_the_a1_ball():
    """No ball A1, and a centre-only row A (FBGA-153 style)."""
    fp = _ball_fp(90.0, n=6, m=5)
    fp = replace(fp, pads=[p for p in fp.pads
                           if p.number not in ("A1", "A2", "A5", "A6")])
    got = cv.footprint_a1(fp)
    assert got["correction"] == pytest.approx(90.0) and got["pin"] == "A3"


def test_mirrored_land_pattern_stays_no_model():
    """Drawn in bottom view: no turn puts a standard part on it, so it blocks."""
    fp = _ball_fp()
    fp = replace(fp, pads=[replace(p, x=-p.x) for p in fp.pads])
    assert "mirror" in cv.footprint_a1(fp)["why"]
    row = _footprint_row(fp, 0.0)["parts"][0]
    assert row["verdict"] == "no_model" and "mirror" in row["why"]
    assert row["verdict"] in cv.FAIL_VERDICTS


@pytest.mark.parametrize("pads", [
    [("1", -1.5, 0), ("2", 1.5, 0)],                               # a resistor
    [(f"{a}{k}", (k - 6) * 0.5, y) for a, y in (("A", -1), ("B", 1))
     for k in range(1, 13)],                                       # a USB-C
    [(f"{a}{k}", (k - 16) * 2.54, y) for a, y in (("A", -2.54), ("B", 0), ("C", 2.54))
     for k in range(1, 33)],                                       # a DIN 41612
])
def test_not_a_ball_grid_stays_no_model(pads):
    fp = cv.Footprint("J1", "X:J", 0, 0, 0, "top",
                      [cv.KPad(n, x, y, 0.3, 0.3) for n, x, y in pads])
    assert cv.footprint_a1(fp) is None
    assert _footprint_row(fp, 0.0)["parts"][0]["verdict"] == "no_model"


def test_sliver_lands_named_like_balls_are_not_a_ball_grid():
    """A fine-pitch connector's long leads aren't balls."""
    fp = cv.Footprint("J1", "X:J", 0, 0, 0, "top",
                      [cv.KPad(f"{a}{k}", k * 0.5, y, 0.25, 1.2)
                       for a, y in (("A", -2), ("B", 0), ("C", 2))
                       for k in range(1, 9)])
    assert cv.footprint_a1(fp) is None


def test_fetch_failure_on_a_bga_stays_fetch_failed(tmp_path, monkeypatch):
    """A failed fetch says nothing, and the real model may yet answer."""
    def boom(lcsc, cache_dir=None, fetch=True, pace_s=0.0, fetcher=None, errors=None):
        errors[lcsc] = "HTTP 403"
        return None
    monkeypatch.setattr(easyeda, "get", boom)
    fp = _ball_fp()
    rep = cv.verify(Path("x.kicad_pcb"), {"U1": {"x": 10.0, "y": 10.0, "rot": 0.0}},
                    {"U1": "C1"}, tmp_path, fps={"U1": fp})
    assert rep["parts"][0]["verdict"] == "fetch_failed"
    assert rep["footprint_basis"] is None


def test_offline_bga_with_an_uncached_lcsc_number_is_fetch_failed(tmp_path):
    """One fetch away from the real model: a convention must not stand in,
    and a cold cache offline is a fetch not made, not "no model"."""
    fp = _ball_fp()
    rep = cv.verify(Path("x.kicad_pcb"), {"U1": {"x": 10.0, "y": 10.0, "rot": 0.0}},
                    {"U1": "C999999"}, tmp_path, fetch=False, fps={"U1": fp})
    row = rep["parts"][0]
    assert row["verdict"] == "fetch_failed" and "basis" not in row
    assert "offline" in row["fetch_error"] and "C999999" in row["fetch_error"]


def test_offline_bga_easyeda_had_no_model_for_gets_footprint_basis(tmp_path):
    """A kept "no model" answer (<LCSC>.none) judges offline as online."""
    (tmp_path / "C999999.none").write_text('{"lcsc": "C999999", "none": "x"}')
    fp = _ball_fp()
    rep = cv.verify(Path("x.kicad_pcb"), {"U1": {"x": 10.0, "y": 10.0, "rot": 0.0}},
                    {"U1": "C999999"}, tmp_path, fetch=False, fps={"U1": fp})
    assert rep["parts"][0]["verdict"] == cv.FOOTPRINT_MATCH


def test_bga_the_api_has_no_model_for_gets_footprint_basis(tmp_path, monkeypatch):
    monkeypatch.setattr(easyeda, "get", lambda *a, **k: None)
    fp = _ball_fp()
    rep = cv.verify(Path("x.kicad_pcb"), {"U1": {"x": 10.0, "y": 10.0, "rot": 0.0}},
                    {"U1": "C999999"}, tmp_path, fetch=True, fps={"U1": fp})
    assert rep["parts"][0]["verdict"] == cv.FOOTPRINT_MATCH


def test_model_still_wins_over_the_footprint(tmp_path):
    """A model that says otherwise decides: the convention is only a fallback."""
    fp = _ball_fp(0.0, n=4, m=3)
    turned = _model([(p.number, *cv.rot(p.x, p.y, -90.0)) for p in fp.pads])
    row = cv.check_part(fp, turned, {"x": 10.0, "y": 10.0, "rot": 90.0})
    assert row["verdict"] == "ok" and row["basis"] != "footprint"


def test_derive_rotation_does_not_use_the_footprint_convention():
    """bom_cpl writes a CPL from a real model or the table, never this."""
    assert cv.derive_rotation(_ball_fp(180.0), None) is None
    assert cv.analyse(_ball_fp(180.0), None)["verdict"] == "no_model"


@pytest.mark.parametrize("rot,want", [(0.0, "unknown"), (90.0, "unknown")])
def test_footprint_basis_never_verifies_the_ledger_fact(rot, want, tmp_path):
    """`verified` means the pads landed on the LCSC model. No model, no claim,
    whether the row matched (0) or failed (90)."""
    ws = tmp_path / "w"
    (ws / "kicad").mkdir(parents=True)
    (ws / "state.json").write_text("{}")
    pcb = ws / "kicad" / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + _fp(
        "U1", "X:BGA", 10, 10, 0, _grid(0.5), size=(0.3, 0.3)) + ")")
    import ledgerlib
    seen = {}
    real_put = ledgerlib.put
    orig = (ledgerlib.writable, ledgerlib.expected)
    ledgerlib.writable = lambda w: True
    ledgerlib.expected = lambda w: {ledgerlib.key("U1", "footprint")}
    ledgerlib.put = lambda w, facts, who: seen.update(f=facts) or None
    try:
        cv.verify(pcb, {"U1": {"x": 10, "y": 10, "rot": rot, "layer": "Top"}},
                  {}, tmp_path / "c", fetch=False)
    finally:
        ledgerlib.writable, ledgerlib.expected = orig
        ledgerlib.put = real_put
    assert seen["f"][0]["status"] == want and seen["f"][0]["source"] == ""


def test_image_match_agrees_with_a_footprint_match():
    vis = {"parts": {"U1": {"pin1": "match", "polarity": "n/a"}}}
    row = {"ref": "U1", "verdict": cv.FOOTPRINT_MATCH}
    assert cv.merge_visual([row], vis)[0]["agreement"] == "agree"
    vis["parts"]["U1"]["pin1"] = "mismatch"
    assert cv.merge_visual([row], vis)[0]["agreement"] == "disagree"


def test_dfm_warns_on_a_footprint_match_and_errors_on_a_turned_row(tmp_path):
    fab = tmp_path / "CPL.csv"
    rows = ["Designator,Mid X,Mid Y,Layer,Rotation"]
    for r, c in _hdi_cpl().items():
        rows.append(f"{r},{c['x']},{-c['y']},Top,{90 if r == 'U404' else 0}")
    fab.write_text("\n".join(rows) + "\n")
    vios: list = []
    dfm_check.check_placement(_hdi_pcb(tmp_path), fab, None, None, tmp_path / "c",
                              False, vios)
    by = {v["kind"]: v for v in vios}
    assert by["cpl_footprint_basis"]["severity"] == "warning"
    assert by["cpl_footprint_basis"]["refs"] == ["U401", "U402", "U403"]
    assert by["cpl_rotation"]["severity"] == "error"
    assert by["cpl_rotation"]["refs"] == ["U404"]
    assert not [v for v in vios if v["kind"] == "cpl_no_model"]


def _index_for(tmp_path, rot, c_rot, layer):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + _fp(
        "U9", "X:BGA", 10, 10, rot, _grid(0.5, 4), size=(0.3, 0.3), layer=layer) + ")")
    return pcb, cpl_render.render(
        pcb, {"U9": {"x": 10, "y": 10, "rot": c_rot, "layer": "Top"}},
        {}, tmp_path / "cache", tmp_path / "r", fetch=False)


@pytest.mark.parametrize("layer,rot,c_rot", [
    ("F.Cu", 0, 0.0), ("F.Cu", 90, 90.0), ("B.Cu", 0, 180.0), ("B.Cu", 90, 90.0)])
def test_render_draws_the_expected_dot_on_the_boards_a1(layer, rot, c_rot, tmp_path):
    pcb, idx = _index_for(tmp_path, rot, c_rot, layer)
    part = idx["parts"]["U9"]
    assert part["expected_pin1"]["basis"] == "footprint"
    assert part["expected_pin1"]["pin"] == "A1"
    a1 = next(p for p in cpl_render.board_shapes(cv.board_footprints(pcb)["U9"])["pads"]
              if p["number"] == "A1")
    assert part["expected_pin1"]["at"] == pytest.approx(list(a1["center"]), abs=1e-6)


def test_render_dot_leaves_the_boards_a1_when_the_row_is_turned(tmp_path):
    pcb, idx = _index_for(tmp_path, 0, 90.0, "F.Cu")
    a1 = next(p for p in cpl_render.board_shapes(cv.board_footprints(pcb)["U9"])["pads"]
              if p["number"] == "A1")
    at = idx["parts"]["U9"]["expected_pin1"]["at"]
    assert math.hypot(at[0] - a1["center"][0], at[1] - a1["center"][1]) > 0.5


def test_render_draws_no_expected_dot_for_a_plain_part_or_with_a_model(ws, tmp_path):
    pcb, cache = ws
    idx = cpl_render.render(pcb, _cpl(X1=0, U1=90), LCSC, cache, tmp_path / "r",
                            fetch=False)
    assert "expected_pin1" not in idx["parts"]["X1"]
    assert "expected_pin1" not in idx["parts"]["U1"]


# KiCad's SOT-89-3: the collector tab is a custom pad whose anchor sits in the
# pin row (-1.8625) while its copper reaches x = 2.0 across the body.
SOT89_KICAD = """(kicad_pcb (version 20240108)
(footprint "Package_TO_SOT_SMD:SOT-89-3" (layer "F.Cu") (at 10 10 0)
 (property "Reference" "Q1")
 (pad "1" smd roundrect (at -1.95 -1.5) (size 1.3 0.9) (layers "F.Cu"))
 (pad "2" smd custom (at -1.8625 0) (size 1.475 0.9) (layers "F.Cu")
  (options (clearance outline) (anchor rect))
  (primitives (gr_poly (pts (xy 3.8625 0.8665) (xy 0.7375 0.8665)
   (xy 0.7375 -0.8665) (xy 3.8625 -0.8665)) (width 0) (fill yes))))
 (pad "3" smd roundrect (at -1.95 1.5) (size 1.3 0.9) (layers "F.Cu"))))
"""


def test_custom_pad_lands_by_its_copper_not_its_anchor(tmp_path):
    """bms-4s Q301: LCSC's BCX56 SOT-89 (C123402, the tab an EasyEDA POLYGON)
    on KiCad's SOT-89-3 was no_fit - both sides placed the tab by a
    reference point 1.75 mm from where the other side's tab is."""
    pcb = tmp_path / "q.kicad_pcb"
    pcb.write_text(SOT89_KICAD)
    fp = cv.board_footprints(pcb)["Q1"]
    tab = next(p for p in fp.pads if p.number == "2")
    assert abs(tab.x - (-0.015)) < 0.02 and abs(tab.y) < 1e-6
    assert tab.w == pytest.approx(4.6) and tab.h == pytest.approx(1.733)
    model = easyeda.get("C123402", FIX, fetch=False)
    mtab = next(p for p in model.pads if p.number == "2")
    # its PAD~ x is -1.357; the polygon's area centroid is 0.39 mm nearer the pins
    assert mtab.x == pytest.approx(-0.966, abs=0.01) and abs(mtab.y) < 1e-3
    out = cv.analyse(fp, model)
    assert out["verdict"] == "ok" and out["corrections"] == [180.0]


# ------------------------------------------------- the pin-1 silk mark (#787)
# Real marks from hw-lab boards, local mm. U401 is hdi-bga's WLCSP-49: its A1 is
# upper left, and both its dots are there.
U401_DOTS = ((-2.03, -1.52, 0.10), (-1.5, -1.6, 0.03))
U401_OUTLINE = "".join(
    f'(fp_line (start {a} {b}) (end {c} {d}) (layer "F.SilkS"))'
    for a, b, c, d in ((-1.5, -1.6, 1.5, -1.6), (1.5, -1.6, 1.5, 1.6),
                       (1.5, 1.6, -1.5, 1.6), (-1.5, 1.6, -1.5, -1.6)))


def _dot(x, y, r=0.1, layer="F.SilkS"):
    return f'(fp_circle (center {x} {y}) (end {x + r} {y}) (layer "{layer}"))'


def _u401_silk(sx=1, sy=1, layer="F.SilkS"):
    """U401's real silk, its dots moved to the corner (sx, sy) from A1's."""
    return U401_OUTLINE + "".join(_dot(sx * x, sy * y, r, layer)
                                  for x, y, r in U401_DOTS)


def _u401_board(tmp_path, silk, layer="F.Cu", r=0, x=20.0):
    pads = [tuple(p) for p in HDI["U401"]["pads"]]
    body = _fp("U401", HDI["U401"]["footprint"], x, 20.0, r, pads,
               size=(0.25, 0.25), layer=layer, silk=silk)
    pcb = tmp_path / "u401.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + body + ")")
    return pcb


def _mark_row(pcb, ref="U401", rot=0.0, layer="Top"):
    rep = cv.verify(pcb, {ref: {"x": 20.0, "y": 20.0, "rot": rot, "layer": layer}},
                    {}, pcb.parent / "c", fetch=False)
    return {p["ref"]: p for p in rep["parts"]}[ref], rep


def test_u401_with_its_dot_near_a1_passes(tmp_path):
    """Known answer (#787): the real footprint's dots are at A1."""
    row, rep = _mark_row(_u401_board(tmp_path, _u401_silk()))
    assert row["silk_mark"] == "ok" and row["silk_pin"] == "A1"
    assert rep["silk_mark_wrong"] == [] and rep["silk_mark_missing"] is None
    assert rep["status"] == "pass"


@pytest.mark.parametrize("sx,sy", [(-1, -1), (1, -1), (-1, 1)])
def test_u401_with_its_dot_moved_to_another_corner_fails(sx, sy, tmp_path):
    """Known answer (#787): JLC would place by this dot, so a wrong part."""
    row, rep = _mark_row(_u401_board(tmp_path, _u401_silk(sx, sy)))
    assert row["silk_mark"] == "wrong"
    assert rep["silk_mark_wrong"] == ["U401"] and rep["status"] == "violations"
    # The CPL itself is still right: this isn't a rotation failure.
    assert row["verdict"] == cv.FOOTPRINT_MATCH and rep["failed"] == []


def test_u401_with_its_dot_removed_warns(tmp_path):
    """Known answer (#787): no mark is JLC's email, not a wrong part."""
    row, rep = _mark_row(_u401_board(tmp_path, U401_OUTLINE))
    assert row["silk_mark"] == "none"
    assert rep["silk_mark_wrong"] == [] and rep["status"] == "pass"
    assert rep["silk_mark_missing"]["refs"] == ["U401"]
    assert "silkscreen" in rep["silk_mark_missing"]["note"]


def _dfm_kinds(pcb, tmp_path, ref="U401", rot=0):
    cpl = tmp_path / "CPL.csv"
    _cpl_csv(cpl, [(ref, 20, rot)])
    vios: list = []
    facts = dfm_check.check_placement(pcb, cpl, None, None, tmp_path / "c",
                                      False, vios)
    marks = [v for v in vios if v["kind"].startswith(("cpl_silk_mark",
                                                      "cpl_no_silk_mark"))]
    return marks, facts


def test_planted_mutant_dot_moved_reaches_the_dfm_gate(tmp_path):
    """Plant the mutant in a good board's text: the same gate that was silent
    must raise an error naming the part, and only the dots may differ."""
    good = _u401_board(tmp_path, _u401_silk())
    marks, facts = _dfm_kinds(good, tmp_path)
    assert marks == []
    assert {p["ref"]: p["silk_mark"] for p in facts["parts"]} == {"U401": "ok"}

    text = good.read_text()
    bad_text = text
    for x, y, r in U401_DOTS:
        bad_text = bad_text.replace(_dot(x, y, r), _dot(-x, -y, r))
    assert bad_text != text
    good.write_text(bad_text)
    marks, _ = _dfm_kinds(good, tmp_path)
    assert [(m["kind"], m["severity"], m["refs"]) for m in marks] == [
        ("cpl_silk_mark", "error", ["U401"])]
    assert "silkscreen" in marks[0]["msg"] and "A1" in marks[0]["msg"]


def test_missing_mark_is_one_warning_for_the_run(tmp_path):
    marks, _ = _dfm_kinds(_u401_board(tmp_path, U401_OUTLINE), tmp_path)
    assert [(m["kind"], m["severity"], m["refs"]) for m in marks] == [
        ("cpl_no_silk_mark", "warning", ["U401"])]


def test_new_kinds_are_in_the_placement_check_list():
    """check_placement's kinds count only if the report's table names them."""
    src = (REPO / "scripts/dfm_check.py").read_text()
    table = src[src.index('"placement": ("cpl_rotation"'):]
    table = table[:table.index(")")]
    assert '"cpl_silk_mark"' in table and '"cpl_no_silk_mark"' in table


@pytest.mark.parametrize("r", [0, 90, 180, 270])
def test_the_mark_is_read_in_the_footprints_own_frame(r, tmp_path):
    """Turning the whole footprint turns the dot with it: still A1."""
    row, _ = _mark_row(_u401_board(tmp_path, _u401_silk(), r=r), rot=float(r))
    assert row["silk_mark"] == "ok"


def _flipped(sx=1, sy=1):
    # KiCad's flip stores silk mirrored in y, on the back layer, like the pads.
    return "".join(_dot(sx * x, -sy * y, r, "B.SilkS") for x, y, r in U401_DOTS)


def test_bottom_part_mark_is_read_on_its_own_side(tmp_path):
    row, _ = _mark_row(_u401_board(tmp_path, _flipped(), layer="B.Cu"),
                       rot=180.0, layer="Bottom")
    assert row["silk_mark"] == "ok"
    row, rep = _mark_row(_u401_board(tmp_path, _flipped(-1, -1), layer="B.Cu"),
                         rot=180.0, layer="Bottom")
    assert row["silk_mark"] == "wrong" and rep["silk_mark_wrong"] == ["U401"]


def test_a_dot_on_the_far_sides_silk_is_not_a_mark(tmp_path):
    """Top part, dot only on B.SilkS: nothing on the side JLC looks at."""
    row, _ = _mark_row(_u401_board(tmp_path, _u401_silk(layer="B.SilkS")))
    assert row["silk_mark"] == "none"


# Each form as hw-lab boards draw it, with pin 1 where that board has it.
QUAD = [("1", -1.0, 1.3), ("2", -1.0, -1.3), ("3", 1.0, -1.3), ("4", 1.0, 1.3)]
MARK_FORMS = {
    # SOT-25 U402: an arc whose ends meet, as drawn: they miss by 0.002 mm
    "closed_arc": '(fp_arc (start -1.78245 1.519975) (mid -1.778775 1.280006) '
                  '(end -1.78 1.52) (layer "F.SilkS"))',
    # TSOT-23-5 U104: a filled triangle, turned to this pin 1's corner
    "triangle": '(fp_poly (pts (xy -1.45 1.51) (xy -1.69 1.84) (xy -1.21 1.84)) '
                '(layer "F.SilkS") (fill solid))',
    # MSOP-10 U203: a poly of two points drawn out and back
    "out_and_back_poly": '(fp_poly (pts (xy -1.24 1.0) (xy -0.76 1.0) (xy -1.24 1.0)) '
                         '(layer "F.SilkS"))',
    # QFN-16 U701: a chamfer line across the corner
    "chamfer_line": '(fp_line (start -1.08 1.58) (end -1.58 1.08) (layer "F.SilkS"))',
    "filled_circle": '(fp_circle (center -0.95 0.45) (end -0.8 0.45) (layer "F.SilkS") '
                     '(fill solid))',
}


def _quad_row(tmp_path, silk, pads=None, ref="U1", fpid="X:IC"):
    body = _fp(ref, fpid, 20.0, 20.0, 0, pads or QUAD, silk=silk)
    pcb = tmp_path / "q.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + body + ")")
    return _mark_row(pcb, ref)[0]


@pytest.mark.parametrize("form", MARK_FORMS)
def test_each_mark_form_is_seen_at_pin_1(form, tmp_path):
    assert _quad_row(tmp_path, MARK_FORMS[form])["silk_mark"] == "ok"


@pytest.mark.parametrize("form", MARK_FORMS)
def test_each_mark_form_is_seen_at_the_wrong_corner(form, tmp_path):
    """The same marks with pin 1 at the opposite corner of the pad field."""
    far = [("1", 1.0, -1.3), ("2", 1.0, 1.3), ("3", -1.0, 1.3), ("4", -1.0, -1.3)]
    assert _quad_row(tmp_path, MARK_FORMS[form], far)["silk_mark"] == "wrong"


@pytest.mark.parametrize("silk", [
    # MSOP-10 U203's side notch marks an end, not a corner
    '(fp_arc (start -1.52 -0.55) (mid -0.97 -0.01) (end -1.5 0.55) (layer "F.SilkS"))',
    # an outline: square, long, or a big circle round the body
    '(fp_line (start -1 -1.6) (end 1 -1.6) (layer "F.SilkS"))',
    '(fp_circle (center -0.5 0.5) (end 1.5 0.5) (layer "F.SilkS"))',
    # a dead-centre dot points at no corner
    '(fp_circle (center 0 0) (end 0.1 0) (layer "F.SilkS"))',
    # a diagonal running out from the centre doesn't cut a corner
    '(fp_line (start -0.5 0.65) (end -1 1.3) (layer "F.SilkS"))',
], ids=["notch", "outline", "big_circle", "centre_dot", "radial_line"])
def test_shapes_that_arent_a_mark(silk, tmp_path):
    assert _quad_row(tmp_path, silk)["silk_mark"] == "none"


def test_a_second_dot_off_pin_1_is_wrong_and_named(tmp_path):
    """One dot right and one wrong leaves JLC two to choose from."""
    row = _quad_row(tmp_path, _dot(-0.95, 0.45) + _dot(0.95, -0.45))
    assert row["silk_mark"] == "wrong" and row["silk_off"] == [(0.95, -0.45)]


def test_a_usb_c_named_like_balls_is_left_alone(tmp_path):
    """eda-crossover J5: pads in rows A, B and C, but slots and not balls."""
    pads = [f'(pad "{r}{k}" smd rect (at {k * 0.5 - 2} {i * 2.0}) (size 0.3 1.1) '
            f'(layers "F.Cu"))' for i, r in enumerate("ABC") for k in range(1, 7)]
    pcb = tmp_path / "j.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (footprint "X:USB-C" (layer "F.Cu") '
                   '(at 20 20 0) (property "Reference" "J5") ' + "".join(pads) + "))")
    assert _mark_row(pcb, "J5")[0]["silk_mark"] is None


def test_pin_1_mid_side_accepts_either_corner_of_that_side(tmp_path):
    """A PLCC's pin 1 sits mid-side, so it has no one corner: either end of
    its side is that pin's, and the far side is still wrong."""
    plcc = [("1", 0.0, 2.0), ("2", -2.0, 0.0), ("3", 0.0, -2.0), ("4", 2.0, 0.0)]
    assert _quad_row(tmp_path, _dot(-1.5, 1.8), plcc)["silk_mark"] == "ok"
    assert _quad_row(tmp_path, _dot(1.5, 1.8), plcc)["silk_mark"] == "ok"
    assert _quad_row(tmp_path, _dot(1.5, -1.8), plcc)["silk_mark"] == "wrong"


@pytest.mark.parametrize("ref,fpid,pads", [
    ("R1", "Resistor_SMD:R_0603", [("1", -0.8, 0), ("2", 0.8, 0)]),
    ("C1", "Capacitor_SMD:C_0603", [("1", -0.8, 0), ("2", 0.8, 0)]),
    # no polarity here and no pin-1 convention we read (#800 reads D and Q)
    ("J1", "Conn:Header_1x04", [(str(i + 1), i * 1.0, 0) for i in range(4)]),
    # an IC ref, but 2-pad or with no pad 1 to compare
    ("U9", "X:TWO", [("1", -0.8, 0), ("2", 0.8, 0)]),
    ("U9", "X:NAMED", [("VIN", -1, 1), ("GND", 1, 1), ("SW", 0, -1)]),
])
@pytest.mark.parametrize("silk", ["", _dot(1.5, 1.5)], ids=["no_dot", "any_dot"])
def test_parts_outside_the_rule_are_left_alone(ref, fpid, pads, silk, tmp_path):
    body = _fp(ref, fpid, 20.0, 20.0, 0, pads, silk=silk)
    pcb = tmp_path / "p.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + body + ")")
    row, rep = _mark_row(pcb, ref)
    assert row["silk_mark"] is None
    assert rep["silk_mark_wrong"] == [] and rep["silk_mark_missing"] is None


def test_a_ball_grid_is_judged_whatever_its_ref(tmp_path):
    pads = [tuple(p) for p in HDI["U401"]["pads"]]
    body = _fp("M1", "X:BGA", 20.0, 20.0, 0, pads, size=(0.25, 0.25),
               silk=_u401_silk(-1, -1))
    pcb = tmp_path / "m.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + body + ")")
    assert _mark_row(pcb, "M1")[0]["silk_mark"] == "wrong"


# ------- polarity marks on two-pad parts and SOT-23 transistors (#800) -------
# Silk copied from KiCad's own library footprints, so the shapes are real.

def _lines(segs, layer="F.SilkS", sx=1, sy=1):
    return "".join(f'(fp_line (start {sx * a} {sy * b}) (end {sx * c} {sy * d}) '
                   f'(layer "{layer}"))' for a, b, c, d in segs)


SOD123 = [("1", -1.65, 0), ("2", 1.65, 0)]           # pad 1 is K
SOD123_SILK = [(-2.36, -1, -2.36, 1), (-2.36, -1, 1.65, -1), (-2.36, 1, 1.65, 1)]
SOD123_BOX = [(-2.36, -1, -2.36, 1), (2.36, -1, 2.36, 1),    # no bar: a
              (-2.36, -1, 2.36, -1), (-2.36, 1, 2.36, 1)]    # centred box
CPELEC = [("1", -1.8, 0), ("2", 1.8, 0)]             # pad 1 is +
CPELEC_PLUS = [(-3, -1.56, -2.5, -1.56), (-2.75, -1.81, -2.75, -1.31)]
CPELEC_BODY = [(-2.26, -1.195563, -2.26, -1.06), (-2.26, -1.195563, -1.195563, -2.26),
               (-2.26, 1.195563, -2.26, 1.06), (-2.26, 1.195563, -1.195563, 2.26),
               (-1.195563, -2.26, 2.26, -2.26), (-1.195563, 2.26, 2.26, 2.26),
               (2.26, -2.26, 2.26, -1.06), (2.26, 2.26, 2.26, 1.06)]
KICAD_SOT23 = [("1", -0.9375, -0.95), ("2", -0.9375, 0.95), ("3", 0.9375, 0)]
SOT23_BODY = [(-0.76, -1.56, 0.76, -1.56), (-0.76, -1.51, -0.76, -1.56),
              (-0.76, 0.39, -0.76, -0.39), (-0.76, 1.56, -0.76, 1.51),
              (0.76, -1.56, 0.76, -0.56), (0.76, 0.56, 0.76, 1.56),
              (0.76, 1.56, -0.76, 1.56)]
SOT23_ARROW = ('(fp_poly (pts (xy -1.3 -0.38) (xy -1.06 -0.05) (xy -1.54 -0.05)) '
               '(layer "F.SilkS") (fill solid))')


def _part_board(tmp_path, ref, fpid, pads, silk, r=0, layer="F.Cu",
                size=(0.9, 1.2)):
    body = _fp(ref, fpid, 20.0, 20.0, r, pads, size=size, layer=layer, silk=silk)
    pcb = tmp_path / f"{ref}.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108) " + body + ")")
    return pcb


def _sod(tmp_path, segs, **kw):
    return _part_board(tmp_path, "D1", "Diode_SMD:D_SOD-123", SOD123,
                       _lines(segs, **{k: v for k, v in kw.items()
                                       if k in ("sx", "layer")}),
                       **{k: v for k, v in kw.items() if k in ("r",)})


def test_a_diode_bar_at_the_cathode_passes(tmp_path):
    row, rep = _mark_row(_sod(tmp_path, SOD123_SILK), "D1")
    assert row["silk_mark"] == "ok" and row["silk_pin"] == "1"
    assert rep["silk_mark_wrong"] == [] and rep["silk_mark_missing"] is None


def test_a_diode_bar_at_the_anode_is_wrong_and_says_why(tmp_path):
    """The same silk mirrored: JLC turns the diode round to match it."""
    row, rep = _mark_row(_sod(tmp_path, SOD123_SILK, sx=-1), "D1")
    assert row["silk_mark"] == "wrong" and rep["silk_mark_wrong"] == ["D1"]
    assert "toward pad 2" in row["silk_why"]


def test_a_diode_drawn_as_a_plain_box_warns(tmp_path):
    row, rep = _mark_row(_sod(tmp_path, SOD123_BOX), "D1")
    assert row["silk_mark"] == "none" and rep["silk_mark_wrong"] == []
    assert rep["silk_mark_missing"]["refs"] == ["D1"]


@pytest.mark.parametrize("r", [0, 90, 180, 270])
def test_a_turned_diode_keeps_its_bar(r, tmp_path):
    row, _ = _mark_row(_part_board(tmp_path, "D1", "Diode_SMD:D_SOD-123", SOD123,
                                   _lines(SOD123_SILK), r=r), "D1", rot=float(r))
    assert row["silk_mark"] == "ok"


def test_a_bottom_diode_reads_its_own_sides_bar(tmp_path):
    # KiCad's flip: silk mirrored in y on B.SilkS; a bar is symmetric in y.
    def board(sx):
        return _part_board(tmp_path, "D1", "Diode_SMD:D_SOD-123", SOD123,
                           _lines(SOD123_SILK, "B.SilkS", sx=sx, sy=-1),
                           layer="B.Cu")
    assert _mark_row(board(1), "D1", 180.0, "Bottom")[0]["silk_mark"] == "ok"
    assert _mark_row(board(-1), "D1", 180.0, "Bottom")[0]["silk_mark"] == "wrong"


def test_an_electrolytic_plus_beside_its_plus_pad_passes(tmp_path):
    pcb = _part_board(tmp_path, "C1", "Capacitor_SMD:CP_Elec_4x5.4", CPELEC,
                      _lines(CPELEC_PLUS + CPELEC_BODY), size=(2.6, 1.6))
    assert _mark_row(pcb, "C1")[0]["silk_mark"] == "ok"


def test_an_electrolytic_plus_beside_its_minus_pad_is_wrong(tmp_path):
    plus = [(-a, b, -c, d) for a, b, c, d in CPELEC_PLUS]
    pcb = _part_board(tmp_path, "C1", "Capacitor_SMD:CP_Elec_4x5.4", CPELEC,
                      _lines(plus + CPELEC_BODY), size=(2.6, 1.6))
    row, rep = _mark_row(pcb, "C1")
    assert row["silk_mark"] == "wrong" and rep["silk_mark_wrong"] == ["C1"]


def test_an_electrolytic_with_no_plus_is_not_guessed_from_its_body(tmp_path):
    """A can's chamfered body leans to + on some libraries and to - on
    others, so without a + there's nothing safe to read."""
    pcb = _part_board(tmp_path, "C1", "Capacitor_SMD:CP_Elec_4x5.4", CPELEC,
                      _lines(CPELEC_BODY), size=(2.6, 1.6))
    row, rep = _mark_row(pcb, "C1")
    assert row["silk_mark"] is None and rep["silk_mark_missing"] is None


def test_a_sot23_transistor_with_its_arrow_at_pin_1_passes(tmp_path):
    pcb = _part_board(tmp_path, "Q1", "Package_TO_SOT_SMD:SOT-23", KICAD_SOT23,
                      _lines(SOT23_BODY) + SOT23_ARROW, size=(1.475, 0.6))
    assert _mark_row(pcb, "Q1")[0]["silk_mark"] == "ok"


def test_a_sot23_transistor_with_no_mark_is_left_alone(tmp_path):
    """Its three pads already fix the way round, so no mark isn't a fault."""
    pcb = _part_board(tmp_path, "Q1", "Package_TO_SOT_SMD:SOT-23", KICAD_SOT23,
                      _lines(SOT23_BODY), size=(1.475, 0.6))
    row, rep = _mark_row(pcb, "Q1")
    assert row["silk_mark"] is None and rep["silk_mark_missing"] is None


def test_a_sot23_transistor_with_a_dot_by_pin_3_is_wrong(tmp_path):
    pcb = _part_board(tmp_path, "Q1", "Package_TO_SOT_SMD:SOT-23", KICAD_SOT23,
                      _lines(SOT23_BODY) + _dot(1.3, 1.4), size=(1.475, 0.6))
    row, rep = _mark_row(pcb, "Q1")
    assert row["silk_mark"] == "wrong" and rep["silk_mark_wrong"] == ["Q1"]


def test_a_battery_holder_is_not_read_as_a_polar_two_pad(tmp_path):
    pcb = _part_board(tmp_path, "BT1", "Battery:BatteryHolder_CR2032", SOD123,
                      _lines(SOD123_SILK))
    assert _mark_row(pcb, "BT1")[0]["silk_mark"] is None


def test_planted_mutant_diode_bar_moved_reaches_the_dfm_gate(tmp_path):
    good = _sod(tmp_path, SOD123_SILK)
    marks, facts = _dfm_kinds(good, tmp_path, "D1")
    assert marks == []
    assert {p["ref"]: p["silk_mark"] for p in facts["parts"]} == {"D1": "ok"}
    text = good.read_text()
    bad = text.replace(_lines(SOD123_SILK), _lines(SOD123_SILK, sx=-1))
    assert bad != text
    good.write_text(bad)
    marks, _ = _dfm_kinds(good, tmp_path, "D1")
    assert [(m["kind"], m["severity"], m["refs"]) for m in marks] == [
        ("cpl_silk_mark", "error", ["D1"])]


# ------- a foreign board's shipped CPL (oasis v0.2, #880) -------

def test_a_keyed_two_pad_housing_goes_on_one_way_only():
    """oasis v0.2's mister1: a JST PH whose LCSC model numbers its pads the
    other way to KiCad's. JLC placed it 180 off; a plain two-pad part (a
    resistor) may sit either way round, a keyed housing may not."""
    model = _model([("1", 1.0, 0.0), ("2", -1.0, 0.0)], package="CONN-SMD_2P")
    board = [("1", -1.0, 0.0), ("2", 1.0, 0.0)]
    jst = cv.analyse(_kfp("mister1", board,
                          name="Connector_JST:JST_PH_S2B-PH-SM4-TB_1x02"), model)
    assert jst["verdict"] == "ok" and jst["corrections"] == [180.0]
    assert cv.analyse(_kfp("BT1", board, name="aiee:CR2032"),
                      _model(model.pads and [("1", 1.0, 0.0), ("2", -1.0, 0.0)])
                      )["corrections"] == [180.0]
    r = cv.analyse(_kfp("R1", board, name="Resistor_SMD:R_0603"),
                   _model([("1", 1.0, 0.0), ("2", -1.0, 0.0)], package="R0603"))
    assert r["corrections"] == [0.0, 180.0] and "either way" in r["basis"]


def test_an_unkeyed_single_row_header_sits_either_way_round():
    pads = [(str(i), 0.0, (i - 2.5) * 2.54) for i in range(1, 5)]
    model = _model([(n, x, -y) for n, x, y in pads], package="HDR-TH_4P")
    a = cv.analyse(_kfp("J3", pads,
                        name="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"),
                   model)
    assert a["verdict"] == "ok" and a["corrections"] == [0.0, 180.0]
    # the same pads as a keyed JST housing: one way only
    b = cv.analyse(_kfp("J3", pads, name="Connector_JST:JST_XH_B4B-XH-A_1x04"),
                   model)
    assert b["corrections"] == [180.0]


def test_a_lone_pin_named_a_is_no_polarity():
    """LCSC's TS-1187A switch (C318884) names pin 1 A: that is not an anode
    without a K beside it, so the part is not held to one way round."""
    fp = _kfp("SW1", [("1", -1.0, 0.0), ("2", 1.0, 0.0)], name="aiee:SW-SMD")
    assert not cv.is_polar(fp, _model([("1", -1, 0), ("2", 1, 0)], {"1": "A", "2": "B"}))
    assert cv.is_polar(fp, _model([("1", -1, 0), ("2", 1, 0)], {"1": "A", "2": "K"}))
    assert cv.is_polar(fp, _model([("1", -1, 0), ("2", 1, 0)], {"1": "+", "2": "-"}))


def test_a_switch_with_shared_pad_numbers_fits_by_geometry():
    """oasis's TS-1187A: the board numbers its four pads 1,1,2,2, the model
    1-4. A number fit lands, but only geometry says the part sits."""
    model = _model([("1", -3.0, -1.85), ("2", 3.0, -1.85), ("3", -3.0, 1.85),
                    ("4", 3.0, 1.85)], {"1": "A", "2": "A", "3": "B", "4": "B"},
                   package="SW-SMD_4P")
    board = [("1", -3.0, -1.85), ("1", 3.0, -1.85), ("2", -3.0, 1.85),
             ("2", 3.0, 1.85)]
    a = cv.analyse(_kfp("SW2", board, name="Button_Switch_SMD:SW_SPST_TL3342"),
                   model)
    assert a["verdict"] == "ok" and "geometry" in a["basis"]
    assert 0.0 in a["corrections"] and 180.0 in a["corrections"]


def test_an_exposed_pad_split_differently_is_fitted_on_the_rest():
    """oasis U1 (ESP32-C3-WROOM-02): the model's thermal pad is three entries
    round the centre, the board's one off in a corner. Its centre is dragged
    off; the perimeter pads, alike on both sides, still fit."""
    ring = [("1", -4.0, -4.0), ("2", 4.0, -4.0), ("3", 4.0, 4.0), ("4", -4.0, 4.0)]
    model = _model(ring + [("5", x, 0.0) for x in (-1.0, 0.0, 1.0)])
    board = ring + [("5", 1.5, 1.5)]
    a = cv.analyse(_kfp("U1", board), model)
    assert a["verdict"] == "ok" and a["corrections"] == [0.0]
    # turned, the perimeter fit still finds the turn
    turned = [(n, -y, x) for n, x, y in ring] + [("5", -1.5, 1.5)]
    assert cv.analyse(_kfp("U1", turned), model)["corrections"] == [90.0] or \
        cv.analyse(_kfp("U1", turned), model)["corrections"] == [270.0]


def test_pads_a_micron_apart_are_one_row_not_a_two_micron_pitch():
    fp = _kfp("U7", [("1", -1.174999, 0.0), ("2", -1.175001, 0.5),
                     ("3", 1.175, 0.0), ("4", 1.175, 0.5)])
    assert cv.offset_limit_mm(fp) > 0.01


def test_read_cpl_reads_past_a_byte_order_mark(tmp_path):
    p = tmp_path / "positions.csv"
    p.write_bytes("﻿Designator,Mid X,Mid Y,Layer,Rotation\n"
                  "MISTER1,10,-10,top,90\n".encode("utf-8"))
    assert cv.read_cpl(p)["MISTER1"]["rot"] == 90.0


_POS = ("Ref,Val,Package,PosX,PosY,Rot,Side\n"
        '"U1","X","SOT-23-6",10.000000,-10.000000,{u1},top\n'
        '"U3","X","SOT-23-6",70.000000,-10.000000,0.000000,bottom\n'
        '"C3","100nF","C0603",80.000000,-10.000000,90.000000,bottom\n')


def _run_main(ws, tmp_path, cpl):
    pcb, cache = ws
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps(LCSC), encoding="utf-8")
    out = tmp_path / "rep.json"
    rc = cv.main(["--pcb", str(pcb), "--cpl", str(cpl), "--parts", str(parts),
                  "--cache-dir", str(cache), "--offline", "--out", str(out)])
    return rc, json.loads(out.read_text(encoding="utf-8"))


def test_kicad_placement_file_is_read_as_the_cpl_it_stands_for(ws, tmp_path):
    """kicad-cli's own pos CSV raised KeyError 'Mid X' (rc 2). Its PosX/PosY
    are the CPL's Mid X/Mid Y and its Rot is the board's rotation: U3 at
    KiCad 0 on the bottom stands for CPL 180, which fits its model."""
    pos = tmp_path / "board-pos.csv"
    pos.write_text(_POS.format(u1="90.000000"), encoding="utf-8")
    fmt, rows = cv.read_placement(pos)
    assert fmt == "kicad_pos"
    assert rows["U3"] == {"x": 70.0, "y": 10.0, "rot": 180.0,
                          "layer": "Bottom"}
    assert rows["U1"]["rot"] == 90.0 and rows["U1"]["layer"] == "Top"
    rc, rep = _run_main(ws, tmp_path, pos)
    v = {p["ref"]: p for p in rep["parts"]}
    assert rep["status"] != "error", rep
    assert [v[r]["verdict"] for r in ("U1", "U3", "C3")] == ["ok"] * 3
    assert rep["cpl_format"] == "kicad_pos"
    assert "no correction" in rep["cpl_note"]


def test_kicad_placement_file_with_a_wrong_rotation_fails(ws, tmp_path):
    pos = tmp_path / "board-pos.csv"
    pos.write_text(_POS.format(u1="270.000000"), encoding="utf-8")
    rc, rep = _run_main(ws, tmp_path, pos)
    v = {p["ref"]: p for p in rep["parts"]}
    assert rc == 1 and v["U1"]["verdict"] == "wrong_rotation"


def test_kicad_placement_file_with_cpl_column_names_is_kicad_pos(ws, tmp_path):
    """KiCad's file with PosX/PosY/Rot renamed Mid X/Mid Y/Rotation, Ref and
    Side kept (one-rom fire-28's fab/*-top-pos.csv) raised 'not a CPL' (rc
    2): it reads as KiCad's own file, rotation and all."""
    pos = tmp_path / "board-top-pos.csv"
    pos.write_text(_POS.format(u1="90.000000").replace(
        "PosX,PosY,Rot", "Mid X,Mid Y,Rotation"), encoding="utf-8")
    fmt, rows = cv.read_placement(pos)
    assert fmt == "kicad_pos"
    assert rows["U3"] == {"x": 70.0, "y": 10.0, "rot": 180.0,
                          "layer": "Bottom"}
    rc, rep = _run_main(ws, tmp_path, pos)
    v = {p["ref"]: p for p in rep["parts"]}
    assert rep["status"] != "error", rep
    assert [v[r]["verdict"] for r in ("U1", "U3", "C3")] == ["ok"] * 3
    assert rep["cpl_format"] == "kicad_pos"


def test_a_cpl_header_is_matched_whatever_its_case(tmp_path):
    p = tmp_path / "cpl.csv"
    p.write_text(" designator ,MID X,mid y,LAYER,rotation\nU1,10,-10,Top,450\n",
                 encoding="utf-8")
    fmt, rows = cv.read_placement(p)
    assert fmt == "cpl"
    assert rows["U1"] == {"x": 10.0, "y": 10.0, "rot": 90.0, "layer": "Top"}


def test_a_file_that_is_neither_names_both_column_sets(ws, tmp_path):
    p = tmp_path / "other.csv"
    p.write_text("Part,X,Y\nU1,1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Mid X.*PosX.*part, x, y"):
        cv.read_cpl(p)
    rc, rep = _run_main(ws, tmp_path, p)
    assert rc == 2 and "PosX" in rep["error"] and "KeyError" not in rep["error"]


def test_designators_match_the_board_whatever_their_case(ws):
    """Fabrication Toolkit upper-cases designators (mister1 ships MISTER1) and
    JLC matches them so: the row is the board's part, not 'not on the board'."""
    pcb, cache = ws
    cpl = {k.lower(): v for k, v in _cpl(U1=90, C1=0).items()}
    v, _ = _verdicts(pcb, cache, cpl)
    assert v["U1"]["verdict"] == "ok" and v["C1"]["verdict"] == "ok"


def test_a_row_with_no_lcsc_number_is_not_placed_on_a_foreign_cpl(ws):
    """oasis's D1-D5 are in its CPL with no LCSC part: JLC does not place
    them. hwde's own CPL holds only placed parts, so there it still fails."""
    pcb, cache = ws
    cpl = _cpl(X1=0, U1=90)
    rep = cv.verify(pcb, cpl, LCSC, cache, fetch=False, unsourced_placed=False)
    v = {p["ref"]: p for p in rep["parts"]}
    assert v["X1"]["verdict"] == "not_placed" and v["U1"]["verdict"] == "ok"
    own = {p["ref"]: p for p in cv.verify(pcb, cpl, LCSC, cache,
                                          fetch=False)["parts"]}
    assert own["X1"]["verdict"] != "not_placed"


def test_dfm_checks_a_shipped_cpl_without_parts_json(ws, tmp_path,
                                                     monkeypatch):
    """A foreign board ships fab/CPL.csv and no parts.json: placement reads
    the LCSC numbers off the board, the models kept beside the CPL in
    fab/easyeda, and skips a row the board gives no LCSC number."""
    pcb, cache = ws
    monkeypatch.delenv("HWDE_EASYEDA_CACHE", raising=False)
    monkeypatch.delenv("AIEE_EASYEDA_CACHE", raising=False)
    text = pcb.read_text()
    for ref, code in {"U1": "C116592", "C1": "C106245"}.items():
        text = text.replace(f'(property "Reference" "{ref}")',
                            f'(property "Reference" "{ref}") '
                            f'(property "LCSC" "{code}")')
    pcb.write_text(text)
    fab = tmp_path / "fab"
    shutil.copytree(cache, fab / "easyeda")
    cpl = _cpl_csv(fab / "CPL.csv", [("U1", 10, 270), ("C1", 40, 0), ("X1", 60, 0)])
    assert cv.lcsc_map(pcb, None).get("U1") == "C116592"
    assert dfm_check._placement_cache(cpl, None, True) == fab / "easyeda"
    vios: list = []
    facts = dfm_check.check_placement(pcb, cpl, None, None, None, False, vios,
                                      shipped=True)
    assert [(v["kind"], v["refs"]) for v in _no_mark_warning_aside(vios)] == [
        ("cpl_rotation", ["U1"])]
    assert not any("X1" in (v.get("refs") or []) for v in vios)
    assert facts


def test_a_shipped_package_without_its_models_is_not_written_into(
        tmp_path, monkeypatch):
    """A shipped CPL with no fab/easyeda beside it (our own fab package, a
    bench fixture) reads the shared cache: a dfm run never adds a dir to the
    package it checks."""
    monkeypatch.delenv("HWDE_EASYEDA_CACHE", raising=False)
    monkeypatch.delenv("AIEE_EASYEDA_CACHE", raising=False)
    cpl = tmp_path / "fab" / "CPL.csv"
    assert dfm_check._placement_cache(cpl, None, True) == (
        cv.default_cache(None))
    parts = tmp_path / "board" / "parts.json"
    assert dfm_check._placement_cache(cpl, parts, False) == (
        tmp_path / "board" / "easyeda")


# --------------------------------- two-pad part with no model (oasis v0.2 L3)

def _two_pad(ref="L3", fpid="Inductor_SMD:L_Vishay_IHLP-2020", rot=180.0,
             side="top", g=4.852):
    pads = [cv.KPad("1", -g / 2, 0, 1.5, 2.0), cv.KPad("2", g / 2, 0, 1.5, 2.0)]
    return cv.Footprint(ref, fpid, 10.0, 10.0, rot, side, pads)


def test_two_pad_part_with_no_model_is_checked_on_its_axis():
    """oasis v0.2's L3: an IHLP inductor EasyEDA has no model for, CPL at
    the board's 180. It was a false `no_model` failure; either way round is
    right, as for a modelled 0603."""
    fp = _two_pad()
    for rot in (180.0, 0.0):
        row = _footprint_row(fp, rot)["parts"][0]
        assert row["verdict"] == "ok", (rot, row)
        assert row["basis"] == cv.TWO_PAD_BASIS
    assert cv.two_pad_stand_in(fp) is not None


@pytest.mark.parametrize("turn", [90.0, 270.0])
def test_two_pad_part_with_no_model_still_fails_turned_across(turn):
    """The real fault the stand-in must still catch: laid across its pads."""
    rep = _footprint_row(_two_pad(), 180.0 + turn)
    row = rep["parts"][0]
    assert row["verdict"] == "wrong_rotation" and rep["failed"] == ["L3"]
    assert "no LCSC model" in row["why"]


def test_two_pad_part_with_no_model_still_fails_off_its_pads():
    fp = _two_pad("C9", "Capacitor_SMD:C_0402_1005Metric", 0.0, g=1.0)
    rep = cv.verify(Path("x.kicad_pcb"),
                    {"C9": {"x": 11.0, "y": 10.0, "rot": 0.0, "layer": "Top"}},
                    {}, Path("/nonexistent"), fetch=False, fps={"C9": fp})
    assert rep["parts"][0]["verdict"] == "wrong_offset"


@pytest.mark.parametrize("ref,fpid", [
    ("D5", "Diode_SMD:D_SOD-123"),           # polar: the cathode is not in the shape
    ("D6", "LED_SMD:LED_0603_1608Metric"),
    ("J7", "Connector:Conn_01x02"),          # keyed by its ref
])
def test_polar_or_keyed_two_pad_part_with_no_model_stays_no_model(ref, fpid):
    fp = _two_pad(ref, fpid, 0.0)
    assert cv.two_pad_stand_in(fp) is None
    assert _footprint_row(fp, 0.0)["parts"][0]["verdict"] == "no_model"


def test_offline_two_pad_part_with_an_uncached_lcsc_number_is_fetch_failed(tmp_path):
    """One fetch away from the real model: the stand-in waits, as a BGA
    does, and says the fetch wasn't made (offline), not "no model"."""
    fp = _two_pad()
    rep = cv.verify(Path("x.kicad_pcb"), {"L3": {"x": 10.0, "y": 10.0, "rot": 180.0}},
                    {"L3": "C999999"}, tmp_path, fetch=False, fps={"L3": fp})
    row = rep["parts"][0]
    assert row["verdict"] == "fetch_failed" and "offline" in row["fetch_error"]
    assert rep["fetch_failed"] == ["L3"]


def test_offline_two_pad_part_easyeda_had_no_model_for_gets_the_stand_in(tmp_path):
    """oasis v0.2's L3 in a score run: EasyEDA answered C844983 with no
    footprint once (kept as C844983.none), so offline it is judged by the
    stand-in as online, not left `not judged (fetch failed)`."""
    (tmp_path / "C844983.none").write_text('{"lcsc": "C844983", "none": "x"}')
    fp = _two_pad()
    rep = cv.verify(Path("x.kicad_pcb"), {"L3": {"x": 10.0, "y": 10.0, "rot": 180.0}},
                    {"L3": "C844983"}, tmp_path, fetch=False, fps={"L3": fp})
    row = rep["parts"][0]
    assert row["verdict"] == "ok" and row["basis"] == cv.TWO_PAD_BASIS
    assert rep["fetch_failed"] == [] and rep["failed"] == []
    # the planted fault still fails offline: laid across its pads
    rep = cv.verify(Path("x.kicad_pcb"), {"L3": {"x": 10.0, "y": 10.0, "rot": 90.0}},
                    {"L3": "C844983"}, tmp_path, fetch=False, fps={"L3": fp})
    assert rep["parts"][0]["verdict"] == "wrong_rotation"


def test_pinned_oasis_keeps_easyedas_no_model_answer_for_l3():
    """The score reads oasis offline: its L3 answer must be in the pin."""
    pin = Path(__file__).resolve().parent.parent / "pinned" / "oasis-v0-2"
    if not pin.is_dir():
        pytest.skip("no pinned oasis")
    assert easyeda.none_known("C844983", pin / "fab" / "easyeda")


def test_two_pad_part_the_api_has_no_model_for_gets_the_stand_in(tmp_path, monkeypatch):
    monkeypatch.setattr(easyeda, "get", lambda *a, **k: None)
    fp = _two_pad()
    rep = cv.verify(Path("x.kicad_pcb"), {"L3": {"x": 10.0, "y": 10.0, "rot": 180.0}},
                    {"L3": "C844983"}, tmp_path, fetch=True, fps={"L3": fp})
    row = rep["parts"][0]
    assert row["verdict"] == "ok" and row["lcsc"] == "C844983"
    assert rep["failed"] == []


# ------------------------- bottom preview only where the reading matters (C4)

def test_bottom_part_right_either_way_round_needs_no_preview(ws):
    """oasis v0.2's C4: an unpolarised 0805 on the bottom is right under both
    bottom-side readings (they differ by 180), so no preview ask. A bottom IC
    beside it still gets one."""
    pcb, cache = ws
    v, rep = _verdicts(pcb, cache, _cpl(C3=90, U3=180))
    assert v["C3"]["verdict"] == "ok" and v["U3"]["verdict"] == "ok"
    assert rep["bottom_preview"]["refs"] == ["U3"]
    v, rep = _verdicts(pcb, cache, _cpl(C3=90, C4=135, C5=90))
    assert rep["bottom_preview"] is None


def test_bottom_polar_two_pad_part_still_asks_for_the_preview(ws):
    """The LED's two readings put its cathode on opposite pads: it asks."""
    pcb, cache = ws
    v, rep = _verdicts(pcb, cache, _cpl(D2=0))
    assert v["D2"]["verdict"] == "ok"
    assert rep["bottom_preview"]["refs"] == ["D2"]


# ------------------------------------- pad numbers in another order (#1024)
SOT23 = [("1", -0.95, 1.1), ("2", 0.95, 1.1), ("3", 0.0, -1.1)]
# oasis v0.3 Q1/Q3, lib:SOT-23-3-pinswapped-mosfet: numbered to its symbol,
# pad 2 sits alone where the part's pin 3 (drain) is
SOT23_SWAPPED = [("1", -0.95, 1.1), ("3", 0.95, 1.1), ("2", 0.0, -1.1)]


def test_pads_that_land_only_by_geometry_are_a_pin_map_warning_not_no_fit():
    """Matched by number nothing fits, but the model lands on the pads by
    geometry. JLC places by geometry, so this is no placement fault: it is
    named as a pin map to check, with the geometric rotation."""
    fp = _kfp("Q1", SOT23_SWAPPED, rot=270.0,
              name="lib:SOT-23-3-pinswapped-mosfet")
    model = _model(SOT23, package="SOT-23")
    a = cv.analyse(fp, model)
    assert a["verdict"] == cv.PIN_MAP and "geometry" in a["basis"]
    assert a["corrections"] == [0.0]
    assert cv.PIN_MAP not in cv.FAIL_VERDICTS
    row = cv.check_part(fp, model, {"x": 10.0, "y": 10.0, "rot": 270.0})
    assert row["verdict"] == cv.PIN_MAP and "pin map" in row["why"]
    assert row["expected_rot"] == [270.0]
    # a CPL off every geometric fit lands no pad, and still fails
    bad = cv.check_part(fp, model, {"x": 10.0, "y": 10.0, "rot": 0.0})
    assert bad["verdict"] == "wrong_rotation" and "pin map" in bad["why"]
    # derive_rotation stays a number-fit answer only
    assert cv.derive_rotation(fp, model) is None


def test_pin_map_warning_needs_the_same_pin_numbers():
    """A footprint whose numbers are not the model's (pads missing or
    renamed) is no permutation of it: no fit by number stays no_fit."""
    model = _model(SOT23, package="SOT-23")
    renamed = [("1", -2.0, 1.1), ("4", 2.0, 1.1), ("5", 0.0, -1.1)]
    assert cv.analyse(_kfp("Q1", renamed), model)["verdict"] == "no_fit"
    # the same numbers, but a row of three, which no turn lands it on
    wide = [("1", -2.5, 0.0), ("3", 0.0, 0.0), ("2", 2.5, 0.0)]
    assert cv.analyse(_kfp("Q1", wide), model)["verdict"] == "no_fit"
