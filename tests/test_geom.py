"""S3 acceptance tests: geometry library (lib/geom.py).

Plan S3 accept criteria:
  - round-trip sanity on golden boards (copper area per net within tolerance of
    KiCad's report)   -> test_area_roundtrip vs the pcbnew SHAPE_POLY_SET oracle
  - documented API used by S4/S5                        -> test_api_surface_*
  - performance < 5 s parse on the largest golden board -> test_parse_performance

Pure tests (no marker) exercise the s-expr parser, pad geometry, stackup, via
span, caching and freshness on a hand-written synthetic board - they run under
`pytest -m "not smoke"` with no toolchain. `smoke`-marked tests drive KiCad's
bundled python (the area oracle) and kicad-cli (the refill freshness path).
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"
ORACLE = GOLDEN / "generators" / "area_oracle.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import geom  # noqa: E402

BOARDS = ["blinky2", "usbbuck4", "rf4"]


def board_path(name: str) -> Path:
    return GOLDEN / name / f"{name}.kicad_pcb"


# A hand-written 4-layer board covering every parser path: segments (nets by
# name AND by numeric table reference), through via, rotated SMD roundrect pad,
# *.Cu thru-hole pad, a baked-flipped back-side footprint, a filled zone, an
# UNFILLED zone, a keepout rule area, and a gr_rect outline. Coordinates chosen
# so areas are analytic.
SYN4 = """
(kicad_pcb
  (version 20260206)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)
    (25 "Edge.Cuts" user))
  (setup)
  (net 0 "")
  (net 1 "GND")
  (gr_rect (start 0 0) (end 10 10) (stroke (width 0.1)) (fill no) (layer "Edge.Cuts"))
  (segment (start 1 1) (end 5 1) (width 0.5) (layer "F.Cu") (net "SIG"))
  (segment (start 1 2) (end 2 2) (width 0.3) (layer "F.Cu") (net 1))
  (via (at 3 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
  (footprint "t:R"
    (at 5 5 90)
    (layer "F.Cu")
    (property "Reference" "R1" (at 0 0 0))
    (pad "1" smd roundrect (at -0.95 0 90) (size 1 1.45)
      (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25) (net "SIG"))
    (pad "2" thru_hole circle (at 0.95 0 90) (size 0.9 0.9) (drill 0.4)
      (layers "*.Cu" "*.Mask") (net "GND")))
  (footprint "t:R2"
    (layer "B.Cu")
    (at 8 8 -90)
    (property "Reference" "R2" (at 0 0 0))
    (pad "1" smd rect (at -0.5 0.2 270) (size 0.6 0.6)
      (layers "B.Cu" "B.Mask") (net "SIG")))
  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 1 1) (xy 9 1) (xy 9 9) (xy 1 9))))
  (zone (net "GND") (layer "In1.Cu")
    (polygon (pts (xy 0 0) (xy 2 0) (xy 2 2) (xy 0 2))))
  (zone (layer "In2.Cu")
    (name "ka")
    (keepout (tracks allowed) (copperpour not_allowed))
    (polygon (pts (xy 0 0) (xy 2 0) (xy 2 2) (xy 0 2))))
)
"""


@pytest.fixture(scope="module")
def syn(tmp_path_factory) -> geom.BoardGeom:
    p = tmp_path_factory.mktemp("syn") / "syn4.kicad_pcb"
    p.write_text(SYN4, encoding="utf-8")
    return geom.BoardGeom.from_file(p)


# ============================================================ pure: parsing

def test_layers_and_thickness(syn):
    assert syn.copper_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert syn.thickness == pytest.approx(1.6)


def test_counts(syn):
    assert len(syn.tracks_of()) == 2
    assert len(syn.vias_of()) == 1
    assert len(syn.pads_of()) == 3
    assert len(syn.zones_of()) == 2  # the keepout is NOT a zone
    assert syn.nets == {"SIG", "GND"}


def test_net_resolution_name_form(syn):
    # copper items reference nets by bare name; resolver returns that name
    assert syn.tracks_of(net="SIG")[0].net == "SIG"
    assert syn.vias_of(net="GND")[0].net == "GND"


def test_net_resolution_numeric_form(syn):
    # (net 1) numeric reference resolves through the root net table -> "GND"
    gnd_tracks = syn.tracks_of(net="GND", layer="F.Cu")
    assert len(gnd_tracks) == 1
    assert gnd_tracks[0].width == pytest.approx(0.3)


def test_rule_area_excluded_from_zones_and_freshness(syn):
    # keepouts never fill; they must not count as zones or as "unfilled"
    assert len(syn.rule_areas) == 1
    ra = syn.rule_areas[0]
    assert ra["name"] == "ka"
    assert ra["layers"] == ("In2.Cu",)
    assert ra["outline"].area == pytest.approx(4.0, abs=1e-9)
    assert [z.zone_id for z in syn.unfilled_zones()] == [1]  # only the In1 zone


def test_flipped_footprint_pads_are_literal(syn):
    # pcbnew bakes the flip into the file: same transform as front side,
    # pad layers taken literally. fp (8,8,-90), local (-0.5,0.2):
    # center = fp + R(+90).(-0.5,0.2) = (8-0.2, 8-0.5)
    pad = syn.pads_of(ref="R2")[0]
    assert pad.center == pytest.approx((7.8, 7.5), abs=1e-9)
    assert pad.layers == ("B.Cu",)
    assert pad.net == "SIG"


def test_via_span_is_inclusive_range(syn):
    # (layers "F.Cu" "B.Cu") is a from/to span -> copper on ALL four layers
    v = syn.vias_of()[0]
    assert v.layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    for layer in syn.copper_layers:
        assert syn.vias_of(net="GND", layer=layer), layer


def test_thruhole_pad_on_all_copper(syn):
    pad2 = syn.pads_of(ref="R1", net="GND")[0]
    assert pad2.layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def test_smd_pad_single_layer(syn):
    pad1 = [p for p in syn.pads_of(ref="R1") if p.number == "1"][0]
    assert pad1.layers == ("F.Cu",)


# ============================================================ pure: geometry

def test_pad_rotation_center(syn):
    # fp at (5,5,90deg), pad1 local (-0.95,0) -> center = fp + R(-90).(-0.95,0)
    pad1 = [p for p in syn.pads_of(ref="R1") if p.number == "1"][0]
    assert pad1.center == pytest.approx((5.0, 5.95), abs=1e-6)
    pad2 = [p for p in syn.pads_of(ref="R1") if p.number == "2"][0]
    assert pad2.center == pytest.approx((5.0, 4.05), abs=1e-6)


@pytest.mark.parametrize("shape,w,h,rr,expected", [
    ("rect", 2.0, 1.0, 0.0, 2.0),
    ("circle", 1.0, 1.0, 0.0, math.pi * 0.25),
    ("roundrect", 1.0, 1.45, 0.25, 1.45 - (4 - math.pi) * 0.25**2),
    ("oval", 2.0, 1.0, 0.0, 1.0 + math.pi * 0.25),  # capsule
])
def test_pad_shape_area(shape, w, h, rr, expected):
    poly = geom._pad_polygon(shape, w, h, rr, (0, 0), 0)
    assert poly.area == pytest.approx(expected, abs=5e-3)


def test_rotation_preserves_area():
    # axis-symmetric shape area is rotation-invariant (any sign)
    a0 = geom._pad_polygon("roundrect", 1.0, 1.45, 0.25, (0, 0), 0).area
    a90 = geom._pad_polygon("roundrect", 1.0, 1.45, 0.25, (0, 0), 90).area
    assert a0 == pytest.approx(a90, rel=1e-6)


def test_roundrect_stadium_and_rratio_clamp():
    # rratio 0.5 (KiCad max) is a stadium: w*(h-w) + pi*(w/2)^2 for w<h
    stadium = geom._pad_polygon("roundrect", 1.0, 2.0, 0.5, (0, 0), 0)
    assert stadium.area == pytest.approx(1.0 + math.pi * 0.25, abs=2e-3)
    # out-of-range rratio (malformed footprints) clamps to 0.5, never inflates
    for rr in (0.6, 1.0, 5.0):
        p = geom._pad_polygon("roundrect", 1.0, 2.0, rr, (0, 0), 0)
        assert p.area == pytest.approx(stadium.area, rel=1e-9)
    for rr in (0.0, 0.25, 0.5, 0.9):
        assert geom._pad_polygon("roundrect", 1.0, 2.0, rr, (0, 0), 0).area <= 2.0 + 1e-9
    # square + rratio 0.5 degenerates to a circle
    circ = geom._pad_polygon("roundrect", 1.0, 1.0, 0.5, (0, 0), 0)
    assert circ.area == pytest.approx(math.pi / 4, abs=2e-3)


def test_outline_area(syn):
    assert syn.outline.area == pytest.approx(100.0, abs=1e-6)


def test_arc_points_quarter_circle():
    # start/mid/end on the unit circle -> sampled points stay on radius 1
    pts = geom._arc_points((1, 0), (math.cos(math.pi / 4), math.sin(math.pi / 4)),
                           (0, 1), n=16)
    assert len(pts) == 17
    for x, y in pts:
        assert math.hypot(x, y) == pytest.approx(1.0, abs=1e-9)
    assert pts[0] == pytest.approx((1, 0)) and pts[-1] == pytest.approx((0, 1))


@pytest.mark.parametrize("label,s,m,e", [
    # CW quarter: 0 deg -> -45 deg -> -90 deg
    ("cw", (1, 0), (math.cos(-math.pi / 4), math.sin(-math.pi / 4)), (0, -1)),
    # CW crossing the +/-pi atan2 boundary: -170 -> 180 -> 170 deg
    ("cw-cross-pi", (math.cos(math.radians(-170)), math.sin(math.radians(-170))),
     (-1, 0), (math.cos(math.radians(170)), math.sin(math.radians(170)))),
    # CCW crossing the boundary: 170 -> 180 -> -170 deg
    ("ccw-cross-pi", (math.cos(math.radians(170)), math.sin(math.radians(170))),
     (-1, 0), (math.cos(math.radians(-170)), math.sin(math.radians(-170)))),
    # CCW major arc: 0 -> 135 -> 270 deg
    ("ccw-major", (1, 0), (math.cos(math.radians(135)), math.sin(math.radians(135))),
     (0, -1)),
])
def test_arc_points_direction_and_boundary(label, s, m, e):
    pts = geom._arc_points(s, m, e, n=32)
    for x, y in pts:
        assert math.hypot(x, y) == pytest.approx(1.0, abs=1e-9), label
    assert pts[0] == pytest.approx(s, abs=1e-9)
    assert pts[-1] == pytest.approx(e, abs=1e-9)
    # the sampled polyline must pass through mid (correct sweep direction)
    dmin = min(math.hypot(x - m[0], y - m[1]) for x, y in pts)
    assert dmin < 0.05, f"{label}: arc does not pass through mid (dmin={dmin})"


def test_zone_fill_area(syn):
    # B.Cu fill is an 8x8 square = 64 mm^2
    assert syn.zone_fill("GND", "B.Cu").area == pytest.approx(64.0, abs=1e-6)


def test_net_copper_union(syn):
    # GND on B.Cu: zone(64) absorbs the via + thru-hole pad inside it
    assert syn.net_area("GND", "B.Cu") == pytest.approx(64.0, abs=0.05)
    # GND on In2.Cu (no zone): via disk + thru-hole pad disk, disjoint
    expected = math.pi * 0.3**2 + math.pi * 0.45**2
    assert syn.net_area("GND", "In2.Cu") == pytest.approx(expected, abs=0.02)


# ============================================================ pure: stackup

def test_stackup_defaults(syn):
    s = syn.stackup
    assert s.assumed is True
    assert len(s.dielectrics) == 3  # 4 copper layers -> 3 gaps
    # outer copper 0.035, inner 0.0152; dielectric budget split 0.165/0.67/0.165
    assert all(d["epsilon_r"] == geom._FR4_ER for d in s.dielectrics)
    total_di = sum(d["height"] for d in s.dielectrics)
    assert total_di == pytest.approx(1.6 - 2 * 0.035 - 2 * 0.0152, abs=1e-6)
    assert s.dielectrics[1]["height"] > s.dielectrics[0]["height"]  # core > prepreg


def test_adjacent_copper(syn):
    assert syn.adjacent_copper("F.Cu") == (None, "In1.Cu")
    assert syn.adjacent_copper("In1.Cu") == ("F.Cu", "In2.Cu")
    assert syn.adjacent_copper("B.Cu") == ("In2.Cu", None)
    assert syn.stackup.is_outer("F.Cu") and not syn.stackup.is_outer("In1.Cu")


def test_stackup_from_board_block(tmp_path):
    # An explicit (stackup) block -> parsed heights/epsilon/copper thickness,
    # assumed=False. Copper thicknesses differ from defaults to prove they
    # are read from the block, not assumed.
    stackup = """    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.07))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2) (epsilon_r 4.6))
      (layer "In1.Cu" (type "copper") (thickness 0.018))
      (layer "dielectric 2" (type "core") (thickness 1.0) (epsilon_r 4.4))
      (layer "In2.Cu" (type "copper") (thickness 0.018))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2) (epsilon_r 4.6))
      (layer "B.Cu" (type "copper") (thickness 0.07)))"""
    src = SYN4.replace("(setup)", f"(setup\n{stackup}\n  )")
    p = tmp_path / "stk.kicad_pcb"
    p.write_text(src, encoding="utf-8")
    s = geom.BoardGeom.from_file(p).stackup
    assert s.assumed is False and "stackup" in s.source
    assert [round(d["height"], 4) for d in s.dielectrics] == [0.2, 1.0, 0.2]
    assert [d["epsilon_r"] for d in s.dielectrics] == [4.6, 4.4, 4.6]
    assert s.copper_thickness["F.Cu"] == pytest.approx(0.07)
    assert s.copper_thickness["In1.Cu"] == pytest.approx(0.018)
    assert s.height_between("F.Cu", "In2.Cu") == pytest.approx(1.2, abs=1e-6)
    assert s.epsilon_between("In1.Cu", "In2.Cu") == pytest.approx(4.4, abs=1e-6)


def test_fb_cu_zone_shorthand(tmp_path):
    # (layers "F&B.Cu") = front and back, common on GUI-drawn zones/rule areas
    src = SYN4.replace('(zone (net "GND") (layer "In1.Cu")',
                       '(zone (net "GND") (layers "F&B.Cu")')
    p = tmp_path / "fb.kicad_pcb"
    p.write_text(src, encoding="utf-8")
    bg = geom.BoardGeom.from_file(p)
    z = [z for z in bg.zones_of() if not z.filled][0]
    assert z.layers == ("F.Cu", "B.Cu")


def test_stackup_2layer(tmp_path):
    two = SYN4.replace(
        '(0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)',
        '(0 "F.Cu" signal) (2 "B.Cu" signal)')
    p = tmp_path / "two.kicad_pcb"
    p.write_text(two, encoding="utf-8")
    bg = geom.BoardGeom.from_file(p)
    assert bg.copper_layers == ["F.Cu", "B.Cu"]
    assert len(bg.stackup.dielectrics) == 1
    assert bg.stackup.dielectrics[0]["height"] == pytest.approx(1.6 - 2 * 0.035, abs=1e-6)


# ============================================================ pure: freshness / cache

def test_unfilled_zone_detected(syn):
    unfilled = syn.unfilled_zones()
    assert len(unfilled) == 1
    assert unfilled[0].layers == ("In1.Cu",)
    with pytest.raises(geom.StaleFillError):
        syn.assert_fresh()


def _fresh(syn) -> geom.BoardGeom:
    return geom.BoardGeom.from_file(syn.path)  # its own refill cache


def test_zone_kicad_also_leaves_empty_is_not_stale(syn, tmp_path, monkeypatch):
    """A zone KiCad's own refill fills to nothing isn't stale (minuet v3's
    GND zone 257): an unfilled zone that a refill leaves unfilled passes."""
    import shutil

    def same(pcb):
        d = tmp_path / "refill"
        d.mkdir()
        return Path(shutil.copy(pcb, d / pcb.name))
    monkeypatch.setattr(geom, "_refill_copy", same)
    bg = _fresh(syn)
    assert bg._empty_by_refill() == {z.zone_id for z in bg.unfilled_zones()}
    bg.assert_fresh()


def test_unfilled_zone_stale_when_kicad_cant_say(syn, monkeypatch):
    def fail(pcb):
        raise geom.StaleFillError("no kicad-cli")
    monkeypatch.setattr(geom, "_refill_copy", fail)
    bg = _fresh(syn)
    assert bg._empty_by_refill() == set()
    with pytest.raises(geom.StaleFillError):
        bg.assert_fresh()


@pytest.mark.parametrize("found", ["none", "bad pin"])
def test_missing_kicad_cli_is_a_missing_tool_not_a_stale_fill(
        syn, monkeypatch, found):
    """#983: no kicad-cli (or a HWDE_KICAD_CLI pin left pointing at nothing)
    is named as such. The refill raises KicadCliMissing, not StaleFillError,
    and assert_fresh's stale-fill refusal says KiCad could not be asked."""
    def find():
        if found == "bad pin":
            raise env.EnvError("HWDE_KICAD_CLI does not exist: /gone")
        return None
    monkeypatch.setattr(env, "find_kicad_cli", find)
    with pytest.raises(geom.KicadCliMissing, match="kicad-cli not found") as e:
        geom._refill_copy(syn.path)
    assert not isinstance(e.value, geom.StaleFillError)
    bg = _fresh(syn)
    with pytest.raises(geom.StaleFillError, match="kicad-cli not found"):
        bg.assert_fresh()


def test_fill_status(syn):
    status = syn.fill_status()
    assert sum(status.values()) == 1  # one filled, one not


def test_load_board_cache(tmp_path):
    p = tmp_path / "c.kicad_pcb"
    p.write_text(SYN4, encoding="utf-8")
    a = geom.load_board(p)
    b = geom.load_board(p)
    assert a is b  # same object from cache
    assert geom.load_board(p, refresh=True) is not a


def test_parse_error_on_garbage(tmp_path):
    p = tmp_path / "bad.kicad_pcb"
    p.write_text("(not_a_board (foo))", encoding="utf-8")
    with pytest.raises(geom.GeomError):
        geom.BoardGeom.from_file(p)


def test_cli_summary(tmp_path):
    p = tmp_path / "cli.kicad_pcb"
    p.write_text(SYN4, encoding="utf-8")
    out = tmp_path / "s.json"
    rc = geom.main(["--pcb", str(p), "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "pass"
    assert data["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert data["unfilled_zones"] == [1]
    assert data["rule_areas"] == [{"name": "ka", "layers": ["In2.Cu"],
                                   "area_mm2": 4.0}]
    # --check-fill: unfilled zone -> exit 1, status "violations"
    out2 = tmp_path / "s2.json"
    rc2 = geom.main(["--pcb", str(p), "--out", str(out2), "--check-fill"])
    assert rc2 == 1
    assert json.loads(out2.read_text(encoding="utf-8"))["status"] == "violations"


# ============================================================ smoke: oracle round-trip

_ORACLE_CACHE: dict[str, dict] = {}


def _oracle_areas(name: str) -> dict:
    if name in _ORACLE_CACHE:
        return _ORACLE_CACHE[name]
    cli = env.find_kicad_cli()
    if cli is None:
        pytest.skip("no kicad-cli")
    bp = env.find_kicad_python(cli)
    if bp is None:
        pytest.skip("no bundled python")
    cp = subprocess.run([str(bp), str(ORACLE), "--pcb", str(board_path(name))],
                        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert cp.returncode == 0, cp.stderr or cp.stdout
    data = json.loads(cp.stdout)
    assert data["status"] == "pass"
    _ORACLE_CACHE[name] = data
    return data


@pytest.mark.smoke
@pytest.mark.parametrize("name", BOARDS)
def test_copper_layers_match_oracle(name):
    oracle = _oracle_areas(name)
    bg = geom.BoardGeom.from_file(board_path(name))
    assert bg.copper_layers == oracle["copper_layers"]


@pytest.mark.smoke
@pytest.mark.parametrize("name", BOARDS)
def test_area_roundtrip(name):
    """Per-(net, layer) copper area from geom vs the pcbnew oracle.

    Both union the SAME KiCad primitives, so they agree tightly on real copper;
    tolerances absorb pad-corner / via-circle faceting on tiny nets.
    """
    oracle = _oracle_areas(name)["net_area_mm2"]
    bg = geom.BoardGeom.from_file(board_path(name))

    otot = gtot = 0.0
    geom_seen = {(n, l) for n in bg.nets for l in bg.copper_layers
                 if bg.net_area(n, l) > 0.05}
    oracle_seen = set()
    for net, layers in oracle.items():
        for layer, oa in layers.items():
            ga = bg.net_area(net, layer)
            otot += oa
            gtot += ga
            if oa > 0.05:
                oracle_seen.add((net, layer))
            if oa > 1.0:
                assert abs(ga - oa) / oa < 0.02, \
                    f"{name} {net}/{layer}: oracle={oa:.4f} geom={ga:.4f}"
            else:
                assert abs(ga - oa) < 0.15, \
                    f"{name} {net}/{layer}: oracle={oa:.4f} geom={ga:.4f}"

    # total board copper area matches very tightly
    assert abs(gtot - otot) / otot < 0.005, f"{name} total o={otot:.2f} g={gtot:.2f}"
    # neither side invents or drops meaningful copper (> 0.05 mm^2)
    assert not (oracle_seen - geom_seen), f"{name} geom missing: {oracle_seen - geom_seen}"
    assert not (geom_seen - oracle_seen), f"{name} geom phantom: {geom_seen - oracle_seen}"


@pytest.mark.smoke
def test_parse_performance():
    import time
    worst = 0.0
    for name in BOARDS:
        t0 = time.perf_counter()
        bg = geom.BoardGeom.from_file(board_path(name))
        for n in bg.nets:  # force every net union (checks build the same way)
            for l in bg.copper_layers:
                bg.net_area(n, l)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 5.0, f"slowest board took {worst:.2f}s (budget 5s)"


# ============================================================ smoke: API surface (S4/S5)

@pytest.mark.smoke
def test_api_surface_on_golden():
    from shapely.geometry.base import BaseGeometry
    bg = geom.BoardGeom.from_file(board_path("usbbuck4"))
    # net_copper / zone_fill / layer_copper return shapely geometry
    assert isinstance(bg.net_copper("GND", "In1.Cu"), BaseGeometry)
    assert bg.zone_fill("GND", "In1.Cu").area > 100  # In1 is a GND plane
    assert isinstance(bg.layer_copper("F.Cu"), BaseGeometry)
    # GND has a filled zone on an inner plane; a signal net does not
    assert "In1.Cu" in bg.layers_with_zone("GND")
    assert bg.layers_with_zone("/USB_DP") == []
    # exclude drops a net from a layer union
    full = bg.layer_copper("In1.Cu").area
    less = bg.layer_copper("In1.Cu", exclude="GND").area
    assert less < full


@pytest.mark.smoke
def test_goldens_are_filled():
    for name in BOARDS:
        bg = geom.BoardGeom.from_file(board_path(name))
        assert bg.unfilled_zones() == []
        bg.assert_fresh()  # fast path: no unfilled zones


@pytest.mark.smoke
def test_flip_transform_matches_pcbnew(tmp_path):
    """V10: flip two footprints (rot-90 cap + USB micro with asymmetric and
    duplicate-numbered pads) via pcbnew, then geom's parsed pad centers,
    copper layers, and nets must reproduce pcbnew's ground truth exactly."""
    cli = env.find_kicad_cli()
    if cli is None:
        pytest.skip("no kicad-cli")
    bp = env.find_kicad_python(cli)
    if bp is None:
        pytest.skip("no bundled python")
    fixture = GOLDEN / "generators" / "flip_fixture.py"
    flipped = tmp_path / "usbbuck4_flip.kicad_pcb"
    dump_p = tmp_path / "pads.json"
    cp = subprocess.run(
        [str(bp), str(fixture), "--pcb", str(board_path("usbbuck4")),
         "--refs", "C10,J1", "--out", str(flipped), "--dump", str(dump_p)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert cp.returncode == 0, cp.stderr or cp.stdout

    dump = json.loads(dump_p.read_text(encoding="utf-8"))["refs"]
    bg = geom.BoardGeom.from_file(flipped)

    def key(num, x, y, layers, net):
        return (num, round(x, 4), round(y, 4), frozenset(layers), net)

    for ref, info in dump.items():
        assert info["flipped"] is True
        truth = sorted(key(p["number"], p["at"][0], p["at"][1],
                           p["layers"], p["net"])
                       for p in info["pads"] if p["layers"])  # copper pads
        mine = sorted(key(p.number, p.center[0], p.center[1],
                          p.layers, p.net or "")
                      for p in bg.pads_of(ref=ref))
        assert mine == truth, f"{ref}: geom pads differ from pcbnew"


@pytest.mark.smoke
def test_plane_split_mutant_keepout_and_fill_drop():
    """The S4 primary fixture: its keepout rule area must not trip the
    freshness gate, and the In1 GND fill drop must match the S1-recorded
    15.4 mm^2 that check_return_path will detect."""
    mut = geom.BoardGeom.from_file(
        GOLDEN / "mutants" / "plane-split-under-clock" / "rf4.kicad_pcb")
    mut.assert_fresh()  # must NOT raise despite the keepout
    assert len(mut.rule_areas) == 1
    ra = mut.rule_areas[0]
    assert ra["name"] == "mutant-plane-split"
    assert ra["layers"] == ("In1.Cu",)
    from shapely.geometry import box as _box
    assert ra["outline"].intersects(_box(133.0, 116.5, 134.4, 127.5))  # manifest region

    gold = geom.BoardGeom.from_file(board_path("rf4"))
    assert gold.rule_areas == []
    drop = (gold.zone_fill("GND", "In1.Cu").area
            - mut.zone_fill("GND", "In1.Cu").area)
    assert drop == pytest.approx(15.4, abs=0.5)


@pytest.mark.smoke
def test_assert_fresh_refill_path():
    # blinky2 fills were saved by kicad-cli refill -> a fresh refill must match.
    bg = geom.BoardGeom.from_file(board_path("blinky2"))
    bg.assert_fresh(refill=True, area_tol=0.02)


def test_mounting_hole_circle_is_not_the_outline(tmp_path):
    """#1015, the planted fault: minuet-v3 draws its outline in lines and a
    mounting hole as an Edge.Cuts circle. The circle used to win, so every
    edge distance on the board was measured to the hole."""
    lines = "".join(
        f'  (gr_line (start {a} {b}) (end {c} {d}) (layer "Edge.Cuts"))\n'
        for a, b, c, d in ((0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30),
                           (0, 30, 0, 0)))
    hole = '  (gr_circle (center 5 5) (end 7 5) (layer "Edge.Cuts"))\n'
    window = '  (gr_rect (start 20 10) (end 24 14) (layer "Edge.Cuts"))\n'
    src = ('(kicad_pcb (version 20260206) (generator "test")\n'
           '  (layers (0 "F.Cu" signal) (25 "Edge.Cuts" user))\n'
           + hole + window + lines + ')\n')
    p = tmp_path / "hole.kicad_pcb"
    p.write_text(src, encoding="utf-8")
    bg = geom.BoardGeom.from_file(p)
    assert bg.outline.bounds == pytest.approx((0, 0, 40, 30))
    assert len(bg.outline_faces) == 3
    assert bg.outline_faces[1].area == pytest.approx(16.0)
    assert bg.outline_faces[2].area == pytest.approx(math.pi * 4, rel=0.01)
    # a board that is only a circle is still that circle
    p.write_text(src.replace(lines, "").replace(window, ""), encoding="utf-8")
    assert geom.BoardGeom.from_file(p).outline.area == pytest.approx(
        math.pi * 4, rel=0.01)


# #1165: easyeda2kicad's custom pads are `(gr_poly (pts ...) (width 0.1))` with
# no fill token, and KiCad fills them in a KiCad 5-format file. A 2 x 2 mm
# square poly on a 0.2 mm anchor, stroked 0.1 mm.
_POLY_PAD = ('(pad "1" smd custom (at 0 0) (size 0.2 0.2) (layers "F.Cu")'
             ' (net "A") (options (clearance outline) (anchor rect))'
             ' (primitives (gr_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1)'
             ' (xy -1 1)) (width 0.1){fill})))')
_FP_POLY = ('(fp_poly (pts (xy 3 -1) (xy 5 -1) (xy 5 1) (xy 3 1))'
            ' (layer "F.Cu") (width 0.1){fill})')


def _fill_board(tmp_path, head, version, fill="", extra=""):
    src = (f'({head} {version}(generator "test")\n'
           '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
           '  (footprint "t:U" (at 10 10) (layer "F.Cu")\n'
           '    (property "Reference" "U1" (at 0 0))\n'
           f'    {_POLY_PAD.format(fill=fill)}\n'
           f'    {_FP_POLY.format(fill=fill)})\n'
           f'{extra})\n')
    p = tmp_path / "fill.kicad_pcb"
    p.write_text(src, encoding="utf-8")
    return geom.BoardGeom.from_file(p)


@pytest.mark.parametrize("version", ["", "(version 20171130) ", "(version 20210108) "])
def test_legacy_fill_less_poly_pad_is_solid(tmp_path, version):
    """#1165, the planted fault: in a KiCad 5-format file (no version, or one
    up to 20210108) a stroked poly pad primitive with no fill token was read as
    a 0.1 mm ring round an empty middle; KiCad fills it, so it is solid."""
    bg = _fill_board(tmp_path, "kicad_pcb", version)
    assert bg.legacy_fill
    pad = bg.pads_of(ref="U1")[0]
    assert pad.poly.area == pytest.approx(2.1 ** 2, rel=0.01)
    assert pad.poly.contains(geom.Point(10.5, 10.5))
    fpc = [c["shape"] for c in bg.fp_copper if c["ref"] == "U1"]
    assert fpc and fpc[0].area == pytest.approx(2.1 ** 2, rel=0.01)


def test_fill_less_file_is_legacy_whatever_its_version(tmp_path):
    """A file with no fill token anywhere is read the old way too: its
    writer never says fill, so leaving one out does not mean an outline."""
    assert _fill_board(tmp_path, "kicad_pcb", "(version 20260206) ").legacy_fill
    assert geom.legacy_fill([geom.sexpdata.Symbol("module"), "x"])


@pytest.mark.parametrize("fill,extra", [
    (" (fill none)", ""),
    (" (fill no)", ""),
    # a modern file that writes fill tokens elsewhere: an absent one is no fill
    ("", '  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1)) (fill no)'
         ' (layer "Edge.Cuts"))\n'),
])
def test_modern_unfilled_poly_pad_stays_outline(tmp_path, fill, extra):
    """#1165, the quiet twin: in a file that writes fill tokens, a stroked
    poly that says no fill, or leaves it out, is still the 0.1 mm ring."""
    bg = _fill_board(tmp_path, "kicad_pcb", "(version 20260206) ", fill, extra)
    assert not bg.legacy_fill
    pad = bg.pads_of(ref="U1")[0]
    ring = 2.1 ** 2 - 1.9 ** 2 + 0.2 * 0.2  # the stroke, and the anchor inside it
    assert pad.poly.area == pytest.approx(ring, rel=0.02)
    assert not pad.poly.contains(geom.Point(10.5, 10.5))
    fpc = [c["shape"] for c in bg.fp_copper if c["ref"] == "U1"]
    assert fpc and fpc[0].area < 1.0


def test_legacy_fill_less_circle_stays_ring(tmp_path):
    """KiCad 5 drew a stroked gr_circle with no fill as a ring, so a
    legacy file keeps it one."""
    n = geom.sexpdata.loads('(gr_circle (center 0 0) (end 1 0) (width 0.1))')
    assert geom._copper_prim(n, legacy=True).area == pytest.approx(
        math.pi * (1.05 ** 2 - 0.95 ** 2), rel=0.02)
