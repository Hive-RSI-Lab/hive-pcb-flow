"""#743, #772: a castellated pad exempts its own copper, not the whole piece.

check_copper_to_edge skipped every piece touching a castellated hole, so a pour
tied to a castellated pad could run past the edge anywhere with no finding. Then
a disk sized from the hole (#743) gave a pad wider than the disk a false finding
on its sides, and let real copper beside a narrow pad off (#772). Only the pad
grown by the edge floor is exempt now.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import Point, box
from shapely.ops import unary_union

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import dfm_check  # noqa: E402

RULES = {"min_copper_to_edge_mm": 0.3}
BAND = {"min_copper_to_edge_mm": 0.3, "abs_min_copper_to_edge_mm": 0.2}
OUTLINE = box(0, 0, 100, 50)
BAR = box(1, 10, 99, 20)
HOLE = (21, 50, 1.0)           # on the top edge, over the tab at x = 20..22


def _pad(x, y, drill, ring=0.25):
    """What castellation.edge_copper gives for castellated_fp's pad: drill + 2
    ring wide, ring past the hole outward (over the top edge), the extension
    inward."""
    half = drill / 2 + ring
    return (Point(x, y).buffer(drill / 2),
            box(x - half, y - drill / 2 - 0.8, x + half, y + half))


def _edge(*polys, rules=RULES, cast=()):
    cast = [_pad(*h) for h in cast]
    layer = SimpleNamespace(components=lambda: list(polys))
    fab = SimpleNamespace(copper_layer_names=lambda: ["F.Cu"],
                          copper=lambda name: layer, outline=OUTLINE)
    vios: list = []
    dfm_check.check_copper_to_edge(fab, rules, vios, cast)
    return vios


def _tabs(xs, top=49.9, width=2.0):
    """The bar with tabs up to `top` (the top edge is at y = 50)."""
    return unary_union([BAR] + [box(x, 20, x + width, top) for x in xs])


def test_the_repro_reports_the_far_tab():
    """The repro in #743: tabs at x = 20 and 60, the hole at the first."""
    vios = _edge(_tabs([20, 60]), cast=[HOLE])
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]
    assert vios[0]["distance_mm"] == pytest.approx(0.1)
    assert 60 <= vios[0]["pos"][0] <= 62


def test_the_far_tab_reads_as_it_does_with_no_hole():
    plain = _edge(_tabs([20, 60]))
    assert len(plain) == 2
    far = [v for v in plain if v["pos"][0] > 50]
    got = _edge(_tabs([20, 60]), cast=[HOLE])
    assert [(v["pos"], v["ends"], v["distance_mm"]) for v in got] == \
           [(v["pos"], v["ends"], v["distance_mm"]) for v in far]


def test_a_piece_close_only_at_the_hole_stays_clean():
    assert _edge(_tabs([20]), cast=[HOLE]) == []


def test_copper_all_inside_the_area_gives_nothing():
    assert _edge(box(20.8, 49.8, 21.2, 50.2), cast=[HOLE]) == []


@pytest.mark.parametrize("drill, ring", [(1.0, 0.25), (0.6, 0.25), (0.6, 0.18),
                                         (0.85, 0.2), (1.0, 0.5)])
def test_a_buildable_pad_straddling_the_edge_stays_clean(drill, ring):
    """castellated_fp's pad: drill + 2 ring wide, ring past the hole outward,
    the extension inward, with a track leaving it. The pad's sides cross the
    edge, so the exemption has to reach past them."""
    _, pad = _pad(50, 50, drill, ring)
    piece = unary_union([pad, box(49.9, 20, 50.1, 49), BAR])
    assert _edge(piece, cast=[(50, 50, drill, ring)]) == []
    # the same pad with no castellated hole is an ordinary edge error
    assert _edge(piece) != []


@pytest.mark.parametrize("ring", [0.25, 0.5, 0.55, 0.6, 0.7, 1.0])
def test_the_issues_rings_all_stay_clean(ring):
    """The table in #772: rings from 0.6 gave 2 findings at 0.0 mm, the pad's
    two sides, past a disk of hole radius + 0.6."""
    _, pad = _pad(50, 50, 1.0, ring)
    assert _edge(pad, cast=[(50, 50, 1.0, ring)]) == []


@pytest.mark.parametrize("floor", [0.3, 0.4, 0.5, 0.8])
def test_a_higher_floor_widens_the_exemption_with_it(floor):
    """The disk's margin assumed a 0.3 floor: a higher one cut it short."""
    _, pad = _pad(50, 50, 1.0, 1.0)
    rules = {"min_copper_to_edge_mm": floor}
    assert _edge(pad, rules=rules, cast=[(50, 50, 1.0, 1.0)]) == []
    assert _edge(pad, rules=rules) != []


def test_a_pad_off_its_drill_is_exempt_where_its_copper_is():
    """#772 sized the area from the pad, so an offset pad needs nothing more:
    copper past the pad's far side counts from the pad, not from the hole."""
    pad = box(47, 49, 53, 51)       # drill at 47.5: 5 mm of pad to one side
    drill = Point(47.5, 50).buffer(0.5)
    layer = SimpleNamespace(components=lambda: [pad])
    fab = SimpleNamespace(copper_layer_names=lambda: ["F.Cu"],
                          copper=lambda name: layer, outline=OUTLINE)
    vios: list = []
    dfm_check.check_copper_to_edge(fab, RULES, vios, [(drill, pad)])
    assert vios == []


def test_copper_past_the_grown_pad_is_reported():
    """The exemption's end: 0.05 inside the grown pad is exempt, 0.05 past it
    is not. Tabs 0.1 mm off the top edge, fed from below so they share the
    pad's piece."""
    half = 0.75                     # 1.0 hole, ring 0.25, 0.3 floor
    feed = box(20.8, 20, 21.2, 49.6)
    grown = 21 + half + RULES["min_copper_to_edge_mm"]
    for x, want in ((grown - 0.05 - 0.2, 0), (grown + 0.05, 1)):
        tab = box(x, 20, x + 0.2, 49.9)
        piece = unary_union([BAR, feed, tab])
        assert len(_edge(piece, cast=[HOLE])) == want, x


def test_a_real_intrusion_beside_a_narrow_pad_is_reported():
    """Inside the old disk of hole radius + 0.6 (1.1 from the hole here), and
    0.05 past the pad grown by the floor: that disk let it off."""
    feed = box(20.8, 20, 21.2, 49.6)
    tab = box(21.95, 20, 22.05, 49.9)    # 0.1 mm wide, 0.1 mm off the edge
    piece = unary_union([BAR, feed, tab])
    vios = _edge(piece, cast=[(21, 50, 1.0, 0.1)])  # pad 0.6 half width
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]
    assert vios[0]["distance_mm"] == pytest.approx(0.1)
    assert 21.9 <= vios[0]["pos"][0] <= 22.1


def test_a_run_along_the_edge_past_the_hole_is_one_place():
    """Copper 0.1 mm off the edge passing through the hole: the pad cuts it, but
    it is one run at one gap (#804), with the hole's own copper left out."""
    run = unary_union([box(10, 49.0, 32, 49.9), box(20.8, 49.9, 21.2, 50)])
    vios = _edge(run, cast=[HOLE])
    assert len(vios) == 1
    assert vios[0]["distance_mm"] == pytest.approx(0.1)
    assert not 21 - 1.0 < vios[0]["pos"][0] < 21 + 1.0


def test_each_hole_on_a_piece_exempts_its_own_copper():
    cast = [HOLE, (61, 50, 1.0)]
    assert _edge(_tabs([20, 60]), cast=cast) == []
    vios = _edge(_tabs([20, 60, 80]), cast=cast)
    assert len(vios) == 1 and 80 <= vios[0]["pos"][0] <= 82


def test_a_hole_exempts_only_the_piece_it_touches():
    """A separate piece beside the pad never touched the hole, so nothing
    exempts it, as before."""
    apart = box(21.9, 40, 22, 49.9)
    assert len(_edge(_tabs([20]), apart, cast=[HOLE])) == 1


def test_the_hole_of_another_piece_leaves_a_piece_whole():
    """A piece no hole touches is measured as it always was."""
    other = _tabs([20, 60])
    far_hole = [(5, 0, 1.0)]
    assert [v["pos"] for v in _edge(other, cast=far_hole)] == \
           [v["pos"] for v in _edge(other)]


def test_the_exempt_copper_does_not_use_up_the_cap():
    xs = [5 + 8 * i for i in range(7)]         # 7 tabs, the hole at the first
    vios = _edge(_tabs(xs), cast=[(6, 50, 1.0)])
    assert len(vios) == dfm_check.PINCH_CAP_PER_PAIR
    assert vios[0]["pinches_not_shown"] == 1
    assert all(v["pos"][0] > 7 for v in vios)


def test_the_band_warning_leaves_out_the_hole_too():
    piece = _tabs([20, 60], top=49.75)         # 0.25: in the 0.2-0.3 band
    plain = _edge(piece, rules=BAND)
    assert plain[0]["count"] == 2
    vios = _edge(piece, rules=BAND, cast=[HOLE])
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge_preferred"]
    assert vios[0]["count"] == 1
    assert 60 <= vios[0]["items"][0]["pos"][0] <= 62
    assert _edge(_tabs([20], top=49.75), rules=BAND, cast=[HOLE]) == []


# ---- #804: a pad cut out of a run does not split it into more places

def _run(x0, x1, top=49.9):
    return box(x0, 40, x1, top)


def _row(n, pitch=5.0, x0=21.0):
    return [(x0 + pitch * i, 50, 1.0) for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
def test_one_run_with_a_row_of_pads_is_one_finding(n):
    vios = _edge(_run(10, 80), cast=_row(n))
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]
    assert vios[0]["distance_mm"] == pytest.approx(0.1)
    assert "pinches_not_shown" not in vios[0]


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
def test_the_band_counts_one_run_as_one_place(n):
    vios = _edge(_run(10, 80, top=49.75), rules=BAND, cast=_row(n))
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge_preferred"]
    assert vios[0]["count"] == 1


def test_pads_whose_areas_overlap_keep_one_run():
    vios = _edge(_run(10, 40), cast=_row(3, pitch=2.0))
    assert len(vios) == 1


def test_a_run_that_stops_short_of_the_pad_is_its_own_place():
    """The pad cuts the piece, but neither close run reaches the pad: nothing
    runs through it, so these are two places, each named at its own gap."""
    piece = unary_union([box(5, 40, 40, 49), box(5, 49, 15, 49.9),
                         box(27, 49, 37, 49.85), box(20.5, 49, 21.5, 50)])
    vios = _edge(piece, cast=[HOLE])
    assert len(vios) == 2
    by_x = sorted(vios, key=lambda v: v["pos"][0])
    assert 5 <= by_x[0]["pos"][0] <= 15
    assert by_x[0]["distance_mm"] == pytest.approx(0.1)
    assert 27 <= by_x[1]["pos"][0] <= 37
    assert by_x[1]["distance_mm"] == pytest.approx(0.15)


def test_a_run_the_pad_does_not_touch_still_fails_where_it_is():
    """Same piece, far along the edge, with the copper between backing off."""
    piece = unary_union([box(5, 40, 90, 49), box(15, 49, 28, 49.9),
                         box(60, 49, 70, 49.88)])
    vios = _edge(piece, cast=[HOLE])
    far = [v for v in vios if v["pos"][0] > 50]
    assert len(vios) == 2 and len(far) == 1
    assert 60 <= far[0]["pos"][0] <= 70
    assert far[0]["distance_mm"] == pytest.approx(0.12)
    assert far[0]["kind"] == "dfm_copper_to_edge"
    assert "0.1200 mm" in far[0]["msg"]


def test_a_separate_piece_beside_the_run_keeps_its_own_finding():
    other = box(60, 40, 70, 49.88)
    vios = _edge(_run(10, 40), other, cast=_row(2))
    assert len(vios) == 2
    assert 60 <= max(v["pos"][0] for v in vios) <= 70


def test_an_intrusion_between_two_pads_is_listed_at_its_own_place():
    """A band-level run with a deeper tab between the pads: the tab is what
    breaks the rule, at its own position and gap."""
    piece = unary_union([_run(10, 60, top=49.75), box(30, 49, 32, 49.9)])
    vios = _edge(piece, rules=BAND, cast=_row(2, pitch=20.0))
    errors = [v for v in vios if v["kind"] == "dfm_copper_to_edge"]
    assert len(errors) == 1
    assert 30 <= errors[0]["pos"][0] <= 32
    assert errors[0]["distance_mm"] == pytest.approx(0.1)


def test_a_dip_either_side_of_a_pad_stays_two_places():
    """The run joins across the pad, but the gap opens between these two, as it
    would on an uncut run: two tabs to fix."""
    piece = unary_union([_run(5, 40, top=49.75), box(10, 49, 12, 49.9),
                         box(30, 49, 32, 49.9)])
    vios = _edge(piece, rules=BAND, cast=[HOLE])
    assert sorted(10 <= v["pos"][0] <= 12 or 30 <= v["pos"][0] <= 32
                  for v in vios) == [True, True]
    assert all(v["kind"] == "dfm_copper_to_edge" for v in vios)


def test_a_run_on_the_far_edge_of_the_same_piece_stays_its_own_place():
    piece = box(5, 0.1, 40, 49.9)
    vios = _edge(piece, cast=[HOLE])
    assert sorted(v["pos"][1] < 25 for v in vios) == [False, True]


def test_the_worst_of_a_cut_run_is_named():
    """One side closer than the other by more than the rise: the finding sits
    on the worse side, at its gap."""
    piece = unary_union([box(10, 40, 21, 49.8), box(21, 40, 32, 49.9)])
    vios = _edge(piece, cast=[HOLE])
    assert len(vios) == 1
    assert vios[0]["pos"][0] > 21
    assert vios[0]["distance_mm"] == pytest.approx(0.1)


def test_with_no_pad_on_the_piece_nothing_moves():
    """Only a cut joins anything: the same shapes with no castellated pad read
    as they did."""
    piece = unary_union([box(5, 40, 90, 49), box(15, 49, 18, 49.9),
                         box(24, 49, 28, 49.9)])
    assert len(_edge(piece)) == 2
    assert len(_edge(piece, cast=[(80, 0, 1.0)])) == 2


# ---- #822: another net's pad splits a run it does not touch

def _clear_of(run, holes, gap=0.2):
    """run with each pad of holes taken out, grown by a pour clearance."""
    return run.difference(unary_union([_pad(*h)[1].buffer(gap)
                                       for h in holes]))


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
def test_a_run_past_other_nets_pads_is_one_place(n):
    """The planted fault: a pour along the edge, kept 0.2 mm clear of a row of
    other nets' castellated pads, is one run in pieces, not n + 1 places."""
    vios = _edge(_clear_of(_run(10, 80), _row(n)), cast=_row(n))
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge"]
    assert vios[0]["distance_mm"] == pytest.approx(0.1)
    assert "pinches_not_shown" not in vios[0]


@pytest.mark.parametrize("n", [1, 3, 8])
def test_the_band_counts_a_run_past_other_nets_pads_as_one(n):
    piece = _clear_of(_run(10, 80, top=49.75), _row(n))
    vios = _edge(piece, rules=BAND, cast=_row(n))
    assert [v["kind"] for v in vios] == ["dfm_copper_to_edge_preferred"]
    assert vios[0]["count"] == 1


def test_a_half_mm_pour_clearance_still_joins():
    vios = _edge(_clear_of(_run(10, 80), _row(3), gap=0.5), cast=_row(3))
    assert len(vios) == 1


def test_two_pieces_either_side_of_a_pad_stay_two_places():
    """The quiet twin: the same shapes as two pieces of copper (a cut through
    the backing too) are two pours to fix, pad or no pad."""
    left, right = box(10, 40, 20.2, 49.9), box(21.8, 40, 32, 49.9)
    vios = _edge(left, right, cast=[HOLE])
    assert len(vios) == 2


def test_a_run_well_clear_of_the_pad_keeps_two_places():
    """The run backs off the edge 1 mm either side of the pad: nothing carries
    on past it, so the two close stretches are two places."""
    piece = unary_union([box(5, 40, 40, 49), box(5, 49, 19.0, 49.9),
                         box(23.0, 49, 37, 49.9)])
    vios = _edge(piece, cast=[HOLE])
    assert len(vios) == 2


def test_with_no_castellated_pad_the_clear_run_reads_as_before():
    piece = _clear_of(_run(10, 80), _row(3))
    assert len(_edge(piece)) == 4
    assert len(_edge(piece, cast=[(5, 0, 1.0)])) == 4
