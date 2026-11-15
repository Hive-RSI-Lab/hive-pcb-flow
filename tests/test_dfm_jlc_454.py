"""#454 (rest of #326): JLC's hole-to-track floors, routed slot sizes, and the
mask dam by solder-mask colour, from JLC's capabilities page read
2026-11-15 (https://jlcpcb.com/capabilities/pcb-capabilities):

- 'Via hole to Track 0.2mm', 'PTH to Track 0.28mm 0.35mm is recommended,
  minimum 0.28mm', 'NPTH to Track 0.2mm', 'Copper clearance from routed
  slots: >=0.2 mm';
- 'Min. Plated Slots Width 2-layer: 0.5mm Multi-layer: 0.35mm', 'The length
  of the slot should be at least 2 times of the width', 'Min. Non-Plated
  Slots 1.0mm';
- 'Min. pad spacing: 0.10 mm (green, red, yellow, blue, purple) Min. pad
  spacing: 0.13 mm (black, white) 2oz: Min. pad spacing: 0.20 mm (any
  color)'.

Each rule has a fault and a quiet twin.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from shapely.geometry import LineString, Point, box

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "bin"), str(REPO / "scripts"), str(REPO / "scripts" / "lib")]

import dfm_check  # noqa: E402
import gerblib  # noqa: E402

CAPS = yaml.safe_load((REPO / "reference" / "jlc_capabilities.yaml").read_text())
ROWS = CAPS["design_rules"]
R2 = ROWS["2layer_1oz"]
R4 = ROWS["4layer_1oz"]
HOLE_D = 0.3


def _hole(plated=True, x=10.0):
    return SimpleNamespace(x=x, y=10.0, diameter=HOLE_D, plated=plated)


def _pad(h, ring=0.15):
    return Point(h.x, h.y).buffer(h.diameter / 2 + ring, 64)


def _track(h, gap):
    """A track whose edge is `gap` from the hole wall, right of the hole."""
    x0 = h.x + h.diameter / 2 + gap
    return box(x0, 0, x0 + 0.2, 20)


def _fab(copper=(), holes=(), slots=(), masks=None):
    def layer(comps):
        return SimpleNamespace(components=lambda: list(comps))

    return SimpleNamespace(
        copper_layer_names=lambda: ["F.Cu", "B.Cu"],
        copper=lambda name: layer(copper if name == "F.Cu" else []),
        mask=lambda side: layer((masks or {}).get(side, [])),
        holes=list(holes), slots=list(slots))


def _track_vios(fab, rules=R2, is_via=lambda h: False):
    vios: list = []
    dfm_check.check_hole_to_track(fab, rules, vios, is_via)
    return vios


# ---------------------------------------------------------- hole to track

def test_the_floors_are_on_every_fr4_row_and_cited():
    for key in ("2layer_1oz", "2layer_2oz", "4layer_1oz", "4layer_2oz",
                "6layer_1oz", "8layer_2oz"):
        row = ROWS[key]
        assert row["abs_min_pth_to_track_mm"] == 0.28
        assert row["min_pth_to_track_mm"] == 0.35
        assert row["min_via_to_track_mm"] == 0.2
        assert row["min_npth_to_track_mm"] == 0.2
    assert ROWS["2layer_1oz"]["min_plated_slot_width_mm"] == 0.5
    assert ROWS["4layer_1oz"]["min_plated_slot_width_mm"] == 0.35
    text = (REPO / "reference" / "jlc_capabilities.yaml").read_text()
    assert "PTH to Track 0.28mm 0.35mm is recommended' (2026-11-15, #454)" in text


def test_a_track_under_jlcs_pth_floor_fails():
    h = _hole()
    vios = _track_vios(_fab([_pad(h), _track(h, 0.25)], [h]))
    assert [(v["kind"], v["severity"]) for v in vios] == [
        ("dfm_hole_to_track", "error")]
    v = vios[0]
    assert v["distance_mm"] == pytest.approx(0.25, abs=1e-3)
    assert v["min_mm"] == 0.28 and v["layer"] == "F.Cu"
    assert v["hole"] == "plated hole"
    assert "dfm_hole_to_track" in dfm_check.DFM_FAMILIES["drill"]


def test_a_track_jlc_builds_but_under_the_recommended_figure_only_warns():
    h = _hole()
    vios = _track_vios(_fab([_pad(h), _track(h, 0.30)], [h]))
    assert [(v["kind"], v["severity"]) for v in vios] == [
        ("dfm_hole_to_track_preferred", "warning")]


@pytest.mark.parametrize("gap, plated, is_via", [
    (0.40, True, lambda h: False),      # PTH clear of the recommended figure
    (0.25, True, lambda h: True),       # a via: its figure is 0.2
    (0.25, True, None),                 # no board: a plated hole may be a via
    (0.25, False, lambda h: False),     # NPTH clear of its 0.2
])
def test_a_track_clear_of_its_holes_figure_is_quiet(gap, plated, is_via):
    h = _hole(plated)
    assert _track_vios(_fab([_pad(h), _track(h, gap)], [h]), is_via=is_via) == []


def test_a_track_near_a_non_plated_hole_fails_at_its_own_figure():
    h = _hole(plated=False)
    vios = _track_vios(_fab([_track(h, 0.15)], [h]))
    assert [(v["kind"], v["min_mm"], v["hole"]) for v in vios] == [
        ("dfm_hole_to_track", 0.2, "non-plated hole")]


def test_a_pour_held_at_the_npth_figure_reads_inside_it_by_arc_error_and_is_quiet():
    """minuet's USB-C pegs: a GND pour 0.2 mm off, drawn by KiCad as a
    polygon inside the circle, reads 0.1948 mm in the gerber."""
    h = _hole(plated=False)
    assert _track_vios(_fab([_track(h, 0.1948)], [h])) == []


def test_a_pour_past_the_arc_error_still_fails():
    h = _hole(plated=False)
    assert [v["distance_mm"] for v in _track_vios(_fab([_track(h, 0.19)], [h]))] \
        == [pytest.approx(0.19, abs=2e-3)]


def _board(*pads):
    """A stand-in board: pads as (kind, x, y, drill). geom keeps every
    np_thru_hole pad in npth, copper or not; the rest are pads_of() only."""
    ps = [SimpleNamespace(kind=k, center=(x, y), drill=(d, d))
          for k, x, y, d in pads]
    return SimpleNamespace(pads_of=lambda: ps,
                           npth=[p for p in ps if p.kind == "np_thru_hole"])


def test_a_non_plated_pad_the_drill_file_reads_as_plated_gets_npth_figure():
    """KiCad's merged drill file puts a non-plated hole the same size as a
    pad hole in the plated tool, so the file alone reads it as plated."""
    h = _hole(plated=True)
    fab = _fab([_track(h, 0.22)], [h])
    is_npth = dfm_check._npth_matcher(_board(("np_thru_hole", h.x, h.y, HOLE_D)))
    vios: list = []
    dfm_check.check_hole_to_track(fab, R2, vios, lambda h: False, (), is_npth)
    assert vios == []   # 0.22 clears NPTH's 0.2; as a pad hole it failed 0.28
    assert [v["hole"] for v in _track_vios(fab)] == ["plated hole"]


def test_a_plated_pad_hole_beside_a_non_plated_pad_stays_plated():
    h = _hole(plated=True)
    fab = _fab([_track(h, 0.22)], [h])
    is_npth = dfm_check._npth_matcher(_board(
        ("thru_hole", h.x, h.y, HOLE_D),
        ("np_thru_hole", h.x + 3, h.y, HOLE_D),        # elsewhere
        ("np_thru_hole", h.x, h.y, HOLE_D + 0.2)))     # other size
    vios: list = []
    dfm_check.check_hole_to_track(fab, R2, vios, lambda h: False, (), is_npth)
    assert [(v["hole"], v["min_mm"]) for v in vios] == [("plated hole", 0.28)]


def test_the_holes_own_pad_and_the_track_joined_to_it_are_not_measured():
    h = _hole()
    joined = _pad(h).union(box(h.x, 9.9, h.x + 5, 10.1))
    assert _track_vios(_fab([joined], [h])) == []


def test_copper_near_a_routed_slot_is_measured_too():
    sl = gerblib.Slot(line=LineString([(10, 10), (11, 10)]), diameter=1.0,
                      plated=False)
    near = box(11.5 + 0.1, 0, 12, 20)        # 0.1 mm from the slot's end
    vios = _track_vios(_fab([near], slots=[sl]))
    assert [(v["kind"], v["hole"]) for v in vios] == [
        ("dfm_hole_to_track", "non-plated slot")]
    clear = box(11.5 + 0.25, 0, 12, 20)
    assert _track_vios(_fab([clear], slots=[sl])) == []


def test_a_row_without_the_keys_is_silent():
    h = _hole()
    hf = ROWS["2layer_1oz_ro4350b"]
    assert "min_pth_to_track_mm" not in hf
    assert _track_vios(_fab([_pad(h), _track(h, 0.05)], [h]), rules=hf) == []


# ------------------------------------------------------------------ slots

def _slot(w, run, plated=True):
    return gerblib.Slot(line=LineString([(5, 5), (5 + run, 5)]), diameter=w,
                        plated=plated)


def _slot_vios(slots, rules=R2):
    vios: list = []
    dfm_check.check_slots(_fab(slots=slots), rules, vios)
    return vios


def test_a_plated_slot_under_the_two_layer_width_fails():
    vios = _slot_vios([_slot(0.4, 1.0)])
    assert [(v["kind"], v["severity"], v["min_mm"]) for v in vios] == [
        ("dfm_slot_size", "error", 0.5)]
    assert "dfm_slot_size" in dfm_check.DFM_FAMILIES["drill"]


def test_the_same_slot_on_a_multilayer_board_is_quiet():
    assert _slot_vios([_slot(0.4, 1.0)], rules=R4) == []


def test_a_non_plated_slot_under_1mm_fails_and_one_at_1mm_is_quiet():
    vios = _slot_vios([_slot(0.8, 1.0, plated=False)])
    assert [(v["kind"], v["min_mm"], v["plated"]) for v in vios] == [
        ("dfm_slot_size", 1.0, False)]
    assert _slot_vios([_slot(1.0, 1.5, plated=False)]) == []


def test_a_stubby_plated_slot_warns_and_a_long_one_is_quiet():
    vios = _slot_vios([_slot(0.6, 0.2)])      # 0.8 long, 0.6 wide
    assert [(v["kind"], v["severity"], v["count"]) for v in vios] == [
        ("dfm_slot_size_preferred", "warning", 1)]
    assert _slot_vios([_slot(0.6, 0.6)]) == []   # 1.2 long: 2x


def test_a_round_hole_written_as_a_route_is_not_a_slot():
    assert _slot_vios([_slot(0.4, 0.0)]) == []


# ------------------------------------------------------- mask dam by colour

BY = CAPS["board"]["solder_mask_dam_by_color_mm"]


def _dam_vios(color, gap=0.11, rules=R2):
    a = box(0, 0, 1, 1)
    b = box(1 + gap, 0, 2 + gap, 1)
    vios: list = []
    dfm_check.check_mask_dam(_fab(masks={"F": [a, b]}), rules, vios, BY, color)
    return vios


def test_a_dam_too_thin_for_black_mask_warns():
    vios = _dam_vios("Black")
    assert [(v["kind"], v["min_mm"], v["mask_color"]) for v in vios] == [
        ("dfm_mask_dam", 0.13, "black")]


@pytest.mark.parametrize("color", ["Green", None, "Purple", "Custom"])
def test_the_same_dam_on_green_or_unknown_mask_is_quiet(color):
    assert _dam_vios(color) == []


def test_two_oz_copper_holds_every_colour_to_its_own_figure():
    r = ROWS["2layer_2oz"]
    assert r["min_solder_mask_dam_mm"] == 0.2
    assert dfm_check.mask_dam_floor(r, BY, "Black") == 0.2
    assert [v["min_mm"] for v in _dam_vios("Green", 0.15, r)] == [0.2]
    assert _dam_vios("Green", 0.15, R2) == []


# ------------------------------------------------- rotated square pads

ROT_GERBER = """%FSLAX46Y46*%
%MOMM*%
%AMRotRect*
21,1,$1,$2,0,0,$3*%
%ADD10RotRect,1.700000X1.700000X{rot}*%
D10*
X10000000Y-10000000D03*
M02*
"""


@pytest.mark.parametrize("rot", [0.0, 90.0, 45.0, 315.0, 30.0])
def test_a_rotated_square_pad_keeps_its_area_at_every_angle(tmp_path, rot):
    """gerbonara 1.6.3 turns a Rectangle at 45 or 315 degrees into a point;
    the pad vanished and the track into it read as 0.015 mm from its drill
    (minuet v4 J5). 0 and 90 degrees are the twins that always worked."""
    g = tmp_path / "F_Cu.gtl"
    g.write_text(ROT_GERBER.format(rot=f"{rot:.6f}"))
    comps = gerblib.read_gerber(g, "F.Cu").components()
    assert len(comps) == 1
    assert comps[0].area == pytest.approx(1.7 * 1.7, rel=1e-3)
    assert comps[0].contains(Point(10.0, 10.0).buffer(0.5))


def test_a_mask_only_non_plated_pad_on_a_parsed_board_gets_npth_figure(tmp_path):
    """megabuzz's 0.4 mm pegs: np_thru_hole with layers "*.Mask" only, no
    copper, so they are not in pads_of(); the matcher reads geom's npth."""
    import geom
    pad = ('(footprint "t:P" (at 10 10) (layer "F.Cu") (property "Reference" "P1") '
           f'(pad "" np_thru_hole circle (at 0 0) (size 0.42 0.42) (drill {HOLE_D}) '
           '(layers "*.Mask")))\n')
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb (version 20260206) (generator "test")\n'
                 '(general (thickness 1.6))\n'
                 '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
                 f'(setup)\n(net 0 "")\n{pad})\n', encoding="utf-8")
    bg = geom.load_board(p)
    assert not [q for q in bg.pads_of() if q.kind == "np_thru_hole"]
    is_npth = dfm_check._npth_matcher(bg)
    assert is_npth(SimpleNamespace(x=10.0, y=10.0, diameter=HOLE_D))
    assert not is_npth(SimpleNamespace(x=12.0, y=10.0, diameter=HOLE_D))
