"""check_tombstone (DFA-11, dfm family `tombstone`, #328): a planted fault
and its quiet twin for each way an 0603-or-smaller chip's two ends come out
unequal.

Each board is one 0603 chip C1 at (10, 10): pad 1 (GND) at x = 9.25, pad 2
(SIG) at x = 10.75, both 0.8 x 0.95 mm on F.Cu, SIG on a 0.2 mm track.
Known answers, by hand from the copper (BAND_MM = 0.1):
  solid GND fill (connect_pads yes) joining pad 1 on its left, top and
    bottom: the ring's
    left side 0.95 + 2 x 0.1 corners, top and bottom 0.4 + 0.4 ...
    > 2 mm against 0.2                          -> dfm_tombstone, warning
  the same fill held 0.5 mm off with one 0.25 mm spoke: 0.25 vs 0.2 -> none
  a thermal-relief fill, four 0.5 mm spokes: 2 vs 0.2, spokes count as
    track                                      -> dfm_tombstone_traces, info
  a solid island under 5 mm^2 (a neck)        -> track-like, info at most
  a 0.3 mm GND via in pad 1 reaching a B.Cu GND fill, pad 1 also on a
    0.2 mm track: 0.2 + pi x 0.3 = 1.14 vs 0.2 -> dfm_tombstone, warning
  the same via with no B.Cu fill: 0.2 vs 0.2  -> none
  pad 1 on a 1.0 mm track: 1.0 vs 0.2         -> dfm_tombstone_traces, info
  the same with an 0805 (pads 1.9 mm apart)   -> none (out of scope)
  two chips on one solid fill                 -> ONE warning, both refs
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_tombstone as ct  # noqa: E402
import dfm_check  # noqa: E402
import geom  # noqa: E402


def _chip(ref: str, x: float, y: float, pitch: float = 1.5,
          nets=("GND", "SIG"), dnp: bool = False, zc: str = "") -> str:
    h = pitch / 2
    attr = "(attr smd dnp)" if dnp else "(attr smd)"
    pads = " ".join(
        f'(pad "{n}" smd rect (at {dx} 0) (size 0.8 0.95) (layers "F.Cu") '
        f'(net "{net}"){zc if n == "1" else ""})'
        for n, dx, net in (("1", -h, nets[0]),
                                            ("2", h, nets[1])))
    return (f'  (footprint "t:C_0603" (at {x} {y}) (layer "F.Cu") {attr}\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "100n" (at 0 0 0) (layer "F.Fab"))\n'
            f'    {pads})\n')


def _seg(x0, y0, x1, y1, net, w=0.2, layer="F.Cu") -> str:
    return (f'  (segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) '
            f'(layer "{layer}") (net "{net}"))\n')


def _fill(pts, layer="F.Cu", net="GND", extra: list | None = None,
          connect: str = "yes") -> str:
    def poly(ps):
        return " ".join(f"(xy {x} {y})" for x, y in ps)
    fills = "".join(f'    (filled_polygon (layer "{layer}") (pts {poly(p)}))\n'
                    for p in [pts] + (extra or []))
    cp = f"(connect_pads {connect} (clearance 0.2))" if connect else ""
    return (f'  (zone (net "{net}") (layer "{layer}") (hatch edge 0.5) {cp}\n'
            f'    (polygon (pts {poly(pts)}))\n{fills}  )\n')


def _pcb(tmp_path, body: str, name="t.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return p


SIG = _seg(11.15, 10, 14, 10, "SIG")
# pad 1 spans x 8.85-9.65, y 9.525-10.475: a fill from x 0 to the pad's
# right edge, y 8-12, joins it solid on three sides
SOLID_PTS = [(0, 8), (9.65, 8), (9.65, 12), (0, 12)]
SOLID = _fill(SOLID_PTS)
# the same fill stopped 0.5 mm short of the pad, one 0.25 mm spoke across
THERMAL = _fill([(0, 8), (8.35, 8), (8.35, 12), (0, 12)],
                extra=[[(8.35, 9.875), (8.85, 9.875), (8.85, 10.125),
                        (8.35, 10.125)]])
VIA = ('  (via (at 9.25 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
       '(net "GND"))\n')
GND_TRACE = _seg(8.85, 10, 6, 10, "GND")
B_PLANE = _fill([(0, 0), (30, 0), (30, 20), (0, 20)], layer="B.Cu")


def _run(tmp_path, body: str):
    vs, facts = ct.check(geom.BoardGeom.from_file(_pcb(tmp_path, body)))
    return vs, facts


def _kinds(vs):
    return [(v["kind"], v["severity"], tuple(v["refs"])) for v in vs]


def test_solid_pour_on_one_end_is_a_warning(tmp_path):
    vs, facts = _run(tmp_path, _chip("C1", 10, 10) + SIG + SOLID)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]
    v = vs[0]
    assert v["net"] == "GND" and v["layer"] == "F.Cu"
    assert v["parts"][0]["pad"] == "1" and v["parts"][0]["heavy_mm"] > 2
    assert v["parts"][0]["light_mm"] == 0.2
    assert "thermal relief" in v["msg"]
    assert facts["tombstone"] == {"small_chips": 1, "unequal": 1}


def test_thermal_relief_on_the_pour_is_quiet(tmp_path):
    vs, facts = _run(tmp_path, _chip("C1", 10, 10) + SIG + THERMAL)
    assert vs == []
    assert facts["tombstone"]["small_chips"] == 1


# a big GND fill joining pad 1 by four 0.5 mm spokes, as KiCad fills a
# thermal relief: the spokes 4 x 0.5 = 2 mm against 0.2 are unequal, but the
# relief is the cure, so they count as track -> info, never a warning
_SPOKES = [[(8.6, 9.75), (8.85, 9.75), (8.85, 10.25), (8.6, 10.25)],
           [(9.0, 9.3), (9.5, 9.3), (9.5, 9.525), (9.0, 9.525)],
           [(9.0, 10.475), (9.5, 10.475), (9.5, 10.7), (9.0, 10.7)]]
_RELIEF_RING = [(0, 8), (9.65, 8), (9.65, 9.3), (8.6, 9.3), (8.6, 10.7),
                (9.65, 10.7), (9.65, 12), (0, 12)]


def test_four_spoke_relief_is_info_not_a_warning(tmp_path):
    relief = _fill(_RELIEF_RING, extra=_SPOKES, connect="")
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + relief)
    assert _kinds(vs) == [("dfm_tombstone_traces", "info", ("C1",))]
    # the same copper with the zone set to join pads solid: a warning
    solid = _fill(_RELIEF_RING, extra=_SPOKES, connect="yes")
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + solid)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]


def test_pad_connection_overrides_the_zone(tmp_path):
    # a solid-joining zone, but the pad says thermal: no plane warning
    vs, _ = _run(tmp_path, _chip("C1", 10, 10, zc=" (zone_connect 1)")
                 + SIG + SOLID)
    assert [v["severity"] for v in vs] == ["info"]
    # a thermal zone, but the pad says solid: the warning
    vs, _ = _run(tmp_path, _chip("C1", 10, 10, zc=" (zone_connect 2)")
                 + SIG + _fill(SOLID_PTS, connect=""))
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]
    # thru_hole_only joins an SMD pad solid
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG
                 + _fill(SOLID_PTS, connect="thru_hole_only"))
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]


def test_small_fill_island_is_a_track_not_a_plane(tmp_path):
    # a 1.2 x 2 mm solid island (2.4 mm^2, a router's neck) on pad 1's
    # left: track-like -> at most info; grown to 4 x 2 mm (8 mm^2): a plane
    neck = _fill([(8.45, 9), (9.65, 9), (9.65, 11), (8.45, 11)])
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + neck)
    assert all(v["severity"] == "info" for v in vs) and vs
    big = _fill([(5.65, 9), (9.65, 9), (9.65, 11), (5.65, 11)])
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + big)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]


def test_small_island_with_a_via_to_a_plane_is_a_plane(tmp_path):
    # the same 2.4 mm^2 neck with a GND via in it beside the pad: down to a
    # B.Cu GND plane it is the pad's way into that plane - a warning; the
    # via reaching no plane leaves it a track
    neck = _fill([(8.45, 9), (9.65, 9), (9.65, 11), (8.45, 11)])
    via = ('  (via (at 9.25 9.2) (size 0.6) (drill 0.3) (layers "F.Cu" '
           '"B.Cu") (net "GND"))\n')
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + neck + via + B_PLANE)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + neck + via)
    assert all(v["severity"] == "info" for v in vs) and vs


def test_via_in_pad_to_a_plane_is_a_warning(tmp_path):
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + GND_TRACE + VIA
                 + B_PLANE)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]
    assert "via in pad 1 (0.30 mm)" in vs[0]["msg"]
    assert vs[0]["heavy_mm"] == 1.14 and vs[0]["light_mm"] == 0.2


def test_via_in_pad_to_no_plane_is_quiet(tmp_path):
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + GND_TRACE + VIA)
    assert vs == []


def test_unequal_tracks_are_info(tmp_path):
    wide = _seg(8.85, 10, 6, 10, "GND", w=1.0)
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + wide)
    assert _kinds(vs) == [("dfm_tombstone_traces", "info", ("C1",))]
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + GND_TRACE)
    assert vs == []


def test_a_plane_alone_unequal_is_charged_beside_wide_tracks(tmp_path):
    # a 1 mm GND track makes the ends unequal by itself, but the via in the
    # pad down to the B.Cu plane (pi x 0.3 = 0.94 mm against 0.2) does too:
    # the plane is a cause on its own - a warning, not a track note; with
    # no plane under the via it stays info
    wide = _seg(8.85, 10, 6, 10, "GND", w=1.0)
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + wide + VIA + B_PLANE)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1",))]
    vs, _ = _run(tmp_path, _chip("C1", 10, 10) + SIG + wide + VIA)
    assert _kinds(vs) == [("dfm_tombstone_traces", "info", ("C1",))]


def test_0805_and_dnp_are_out_of_scope(tmp_path):
    vs, facts = _run(tmp_path, _chip("C1", 10, 10, pitch=1.9) + SOLID
                     + _seg(11.3, 10, 14, 10, "SIG"))
    assert vs == [] and facts["tombstone"]["small_chips"] == 0
    vs, facts = _run(tmp_path, _chip("C1", 10, 10, dnp=True) + SIG + SOLID)
    assert vs == [] and facts["tombstone"]["small_chips"] == 0


def test_one_pour_is_one_finding_listing_its_parts(tmp_path):
    # C1 at y 10 and C2 at y 14, both GND ends in one solid fill
    fill = _fill([(0, 8), (9.65, 8), (9.65, 16), (0, 16)])
    body = (_chip("C1", 10, 10) + SIG + _chip("C2", 10, 14)
            + _seg(11.15, 14, 14, 14, "SIG") + fill)
    vs, facts = _run(tmp_path, body)
    assert _kinds(vs) == [("dfm_tombstone", "warning", ("C1", "C2"))]
    assert len(vs[0]["items"]) == 2 and facts["tombstone"]["unequal"] == 2


def test_teardrop_zone_counts_as_track(tmp_path):
    # a teardrop zone is a track's flared end, not a plane: GND's 0.2 mm
    # track widened only near the pad stays far under 3 x
    td = _fill([(8.35, 9.8), (8.85, 9.6), (8.85, 10.4), (8.35, 10.2)])
    td = td.replace("(hatch edge 0.5)", "(hatch edge 0.5) (attr (teardrop "
                    "(type padvia)))")
    bg = geom.BoardGeom.from_file(
        _pcb(tmp_path, _chip("C1", 10, 10) + SIG + GND_TRACE + td))
    assert [z.teardrop for z in bg.zones_of()] == [True]
    vs, _ = ct.check(bg)
    assert [v["kind"] for v in vs] in ([], ["dfm_tombstone_traces"])
    assert all(v["severity"] == "info" for v in vs)


def test_family_is_registered_in_dfm():
    assert dfm_check.DFM_FAMILIES["tombstone"] == (
        "dfm_tombstone", "dfm_tombstone_traces")


def test_cli_reports(tmp_path):
    pcb = _pcb(tmp_path, _chip("C1", 10, 10) + SIG + SOLID)
    payload, _ = ct.run(["--pcb", str(pcb)])
    assert payload["status"] == "violations"
    assert [v["kind"] for v in payload["violations"]] == ["dfm_tombstone"]
    empty = _pcb(tmp_path, "", name="e.kicad_pcb")
    payload, _ = ct.run(["--pcb", str(empty)])
    assert payload["status"] == "pass"
    assert "nothing_to_judge" in payload
