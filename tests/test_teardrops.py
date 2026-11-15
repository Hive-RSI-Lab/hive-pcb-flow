"""Teardrops on vias and PTH pads, and the ring-breakout DFM rule (#480).

Fixture: tests/fixtures/teardrops/led.kicad_pcb (hwde-led-test, 2 GND vias,
J1 2-pin THT header, SMD LED/R/C) and led_dropped.kicad_pcb, the same board
after `route_teardrops --force` (KiCad 10.0.6 refill). Known answers, by
hand from the copper:
  via@18.831,20.753  GND: two F.Cu tracks touch it, one wholly inside the
                     via copper (a 0.29 mm stub) -> 1 join; B.Cu is GND pour
  via@13.200,21.740  GND: 1 F.Cu join; B.Cu pour
  J1.1 (+5V, PTH)    1 F.Cu join, no pour
  J1.2 (GND, PTH)    only the B.Cu pour -> nothing to drop
  SMD pads           out of scope by default
  => 3 items, 3 teardrop zones; a second run adds none.
A via's teardrop settings read back as route_edit's add_via takes them
(params_of, same_params; #1138), and read(area=) for part_repair's local
check.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import dfm_check  # noqa: E402
import route_teardrops as rtd  # noqa: E402
import teardroplib as tdl  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "teardrops"
RAW = FIX / "led.kicad_pcb"
DROPPED = FIX / "led_dropped.kicad_pcb"
VIA_A = "via@18.831,20.753"
VIA_B = "via@13.200,21.740"
UUID_A = "7a3b7045-1c37-463a-9e27-6b82ec68a88e"
UUID_B = "d150125a-0f47-42a1-b4eb-36069d276772"
CAPS = {"board": {"drill_wander_mm": 0.075, "min_ring_abs_mm": 0.05}}

FLEX_AREA = """\t(zone
\t\t(net 0)
\t\t(net_name "")
\t\t(layer "F.Cu")
\t\t(name "FLEX_TAIL")
\t\t(hatch edge 0.5)
\t\t(connect_pads (clearance 0))
\t\t(min_thickness 0.25)
\t\t(keepout (tracks allowed) (vias not_allowed) (pads allowed)
\t\t\t(copperpour allowed) (footprints allowed))
\t\t(polygon (pts (xy 0 0) (xy 1 0) (xy 1 1)))
\t)
"""


def _copy(tmp: Path, src: Path, name: str = "b.kicad_pcb") -> Path:
    out = tmp / name
    shutil.copy2(src, out)
    return out


def _edit(pcb: Path, fn) -> Path:
    pcb.write_text(fn(pcb.read_text(encoding="utf-8")), encoding="utf-8")
    return pcb


def _shrink_via_b(text: str) -> str:
    """Plant a 0.5/0.3 via (ring 0.1 < 0.05 + 0.075) at VIA_B."""
    new, n = re.subn(r"(\(at 13\.2 21\.74\)\s*\(size )0\.6", r"\g<1>0.5",
                     text)
    assert n == 1
    return new


def _drop_zones(text: str) -> list[tuple[int, int]]:
    out = []
    for m in re.finditer(r"\n\t\(zone\n", text):
        end = text.index("\n\t)\n", m.start()) + 4
        if "(teardrop" in text[m.start():end]:
            out.append((m.start(), end))
    return out


def _strip_drop_of(text: str, x: float, y: float) -> str:
    """Delete the teardrop zone anchored at (x, y)."""
    for a, b in _drop_zones(text):
        if f"(xy {x:g} {y:g}" in text[a:b] or \
                re.search(rf"\(xy {x:g}\d* {y:g}\d*\)", text[a:b]):
            return text[:a] + "\n" + text[b:]
    raise AssertionError("no teardrop zone at the point")


def _drop_type(text: str, prio: int, new: str | None) -> str:
    """Retype the teardrop zone at this priority: (type new), or no type
    at all (an older file's drop)."""
    for a, b in _drop_zones(text):
        z = text[a:b]
        if f"(priority {prio})" not in z:
            continue
        m = re.search(r"\n\t*\(type padvia\)", z)
        assert m
        rep = "" if new is None else m.group(0).replace("padvia", new)
        return text[:a] + z[:m.start()] + rep + z[m.end():] + text[b:]
    raise AssertionError("no teardrop zone at that priority")


def _add_flex(text: str) -> str:
    i = text.rstrip().rindex(")")
    return text[:i] + FLEX_AREA + text[i:]


# ------------------------------------------------------------- reading

def test_known_answer_items_joins_and_scope():
    b = tdl.read(RAW)
    by = {it.name: it for it in b.items}
    assert by[VIA_A].joins == {"F.Cu": 1}      # the inside stub is no join
    assert by[VIA_A].held == {"B.Cu"}
    assert by[VIA_B].joins == {"F.Cu": 1}
    assert by["J1.1"].joins == {"F.Cu": 1} and not by["J1.1"].held
    assert by["J1.2"].wanted() == 0
    assert by["J1.1"].ring_mm == pytest.approx(0.4, abs=1e-4)
    assert by[VIA_A].ring_mm == pytest.approx(0.15)
    todo, done = tdl.select(b, tdl.config(None))
    assert sorted(it.name for it in todo) == sorted([VIA_A, VIA_B, "J1.1"])
    assert done == [] and b.drop_count == 0
    # SMD scope on: the SMD pads join in, the pads alone
    todo_smd, _ = tdl.select(b, tdl.config({"teardrops": {"smd": True}}))
    assert {it.kind for it in todo_smd} == {"via", "pth", "smd"}
    # net filter
    only5, _ = tdl.select(b, tdl.config({"teardrops": {"nets": ["+5V"]}}))
    assert [it.name for it in only5] == ["J1.1"]


def test_dropped_board_every_item_has_its_drop_and_nothing_left_to_do():
    b = tdl.read(DROPPED)
    assert b.drop_count == 3
    by = {it.name: it for it in b.items}
    for n in (VIA_A, VIA_B, "J1.1"):
        assert by[n].enabled and by[n].drops == {"F.Cu": 1}
        assert by[n].missing() == []
    todo, done = tdl.select(b, tdl.config(None))
    assert todo == [] and len(done) == 3     # already dropped: untouched


def test_item_params_read_from_the_via_and_compared():
    """A via's (teardrops ...) block as route_edit's add_via takes it
    (#1138): led_dropped's vias are at the defaults, led's have none."""
    import sexpdata
    got = tdl.via_params(sexpdata.loads(DROPPED.read_text(encoding="utf-8")))
    assert got == {UUID_A: dict(tdl.ITEM_PARAMS, enabled=True),
                   UUID_B: dict(tdl.ITEM_PARAMS, enabled=True)}
    assert tdl.via_params(sexpdata.loads(RAW.read_text(encoding="utf-8"))) \
        == {UUID_A: None, UUID_B: None}
    a = got[UUID_A]
    assert tdl.same_params(tdl.ITEM_PARAMS, a)
    assert tdl.same_params({"length_ratio": 0.50004}, a)
    assert not tdl.same_params({"length_ratio": 0.6}, a)
    assert not tdl.same_params({"curved": True}, a)
    assert not tdl.same_params({}, None)
    assert not tdl.same_params({}, dict(a, enabled=False))
    # a key the block lacks reads as ITEM_PARAMS
    assert tdl.same_params({"two_tracks": True}, {"enabled": True})
    assert not tdl.same_params({"two_tracks": False}, {"enabled": True})


def test_worker_defaults_are_item_params():
    """teardrop_swig runs in KiCad's python and cannot import teardroplib:
    its copy of the defaults a new via takes must stay the same."""
    import teardrop_swig
    assert teardrop_swig.ITEM_DEFAULTS == tdl.ITEM_PARAMS


def test_read_an_area_keeps_its_items_and_their_drops():
    b = tdl.read(DROPPED, None, area=Point(18.8312, 20.7525).buffer(0.3))
    assert [it.name for it in b.items] == [VIA_A]
    assert b.items[0].drops == {"F.Cu": 1} and not b.items[0].missing()
    assert b.drop_count == 1


def _add_track(s, e, w, net="GND", layer="F.Cu"):
    def fn(text):
        i = text.rstrip().rindex(")")
        seg = (f"\t(segment\n\t\t(start {s[0]} {s[1]})\n\t\t(end {e[0]} "
               f"{e[1]})\n\t\t(width {w})\n\t\t(layer \"{layer}\")\n"
               f"\t\t(net \"{net}\")\n\t\t(uuid \"a3169000-0000-4000-8000-"
               f"{abs(hash((s, e))) % 10**12:012d}\")\n\t)\n")
        return text[:i] + seg + text[i:]
    return fn


def test_a_track_through_a_via_is_one_join_and_wants_its_own_drop(tmp_path):
    """Via B has one F.Cu track and its drop. A track through it, over its
    centre with no end in it, is a join of its own (KiCad 10.0.6 drops on
    it, three times, checked live): the old drop's tip lies on the old
    track, so the new one lacks a drop."""
    pcb = _edit(_copy(tmp_path, DROPPED),
                _add_track((11.2, 21.74), (15.2, 21.74), 0.25))
    by = {it.name: it for it in tdl.read(pcb).items}
    assert by[VIA_B].joins == {"F.Cu": 2} and by[VIA_B].drops == {"F.Cu": 1}
    assert by[VIA_B].deficit("F.Cu") == 1 and by[VIA_B].missing() == ["F.Cu"]
    # crossing the copper off the centre, no end in it: no join
    pcb = _edit(_copy(tmp_path, DROPPED, "off.kicad_pcb"),
                _add_track((11.2, 21.98), (15.2, 21.98), 0.25))
    by = {it.name: it for it in tdl.read(pcb).items}
    assert by[VIA_B].joins == {"F.Cu": 1} and by[VIA_B].missing() == []


def test_a_track_grazing_a_via_is_no_join(tmp_path):
    """An end inside via B with the centreline 0.285 mm off its centre: the
    centreline crosses a 0.18 mm chord of the via's 24-gon, under the
    0.25 mm width, and KiCad 10.0.6 makes no drop (checked live)."""
    pcb = _edit(_copy(tmp_path, DROPPED),
                _add_track((13.25, 22.025), (15.0, 22.025), 0.25))
    by = {it.name: it for it in tdl.read(pcb).items}
    assert by[VIA_B].joins == {"F.Cu": 1} and by[VIA_B].missing() == []
    # the same end, a track thin enough for the chord: a join, lacking
    pcb = _edit(_copy(tmp_path, DROPPED, "thin.kicad_pcb"),
                _add_track((13.25, 22.025), (15.0, 22.025), 0.15))
    by = {it.name: it for it in tdl.read(pcb).items}
    assert by[VIA_B].joins == {"F.Cu": 2} and by[VIA_B].missing() == ["F.Cu"]


def test_a_track_end_drop_is_no_items(tmp_path):
    """Via B's drop retyped track_end (KiCad's track-to-track drops) is
    still a teardrop zone but not the via's: the via lacks its drop. With
    no type (an older file) it is the via's."""
    pcb = _edit(_copy(tmp_path, DROPPED),
                lambda t: _drop_type(t, 30001, "track_end"))
    b = tdl.read(pcb)
    by = {it.name: it for it in b.items}
    assert b.drop_count == 3
    assert by[VIA_B].drops == {} and by[VIA_B].missing() == ["F.Cu"]
    pcb = _edit(_copy(tmp_path, DROPPED, "old.kicad_pcb"),
                lambda t: _drop_type(t, 30001, None))
    by = {it.name: it for it in tdl.read(pcb).items}
    assert by[VIA_B].drops == {"F.Cu": 1} and by[VIA_B].missing() == []


@pytest.mark.parametrize("r, sides", [(0.3, 24), (0.1, 16), (1.0, 32)])
def test_kicad_circle_is_the_polygon_kicad_tests_a_graze_on(r, sides):
    """TransformCircleToPolygon at 0.005 mm error: at least 16 sides, a
    multiple of 8, the corners on the circle half a step off angle 0
    (probed on pcbnew: a 0.6 mm via's first corners at +/-7.5 deg)."""
    import math
    c = tdl._kicad_circle((1.0, 2.0), r)
    pts = list(c.exterior.coords)[:-1]
    assert len(pts) == sides
    h = math.pi / sides
    assert pts[0] == pytest.approx((1.0 + r * math.cos(h),
                                    2.0 + r * math.sin(h)))
    assert all(abs(((x - 1) ** 2 + (y - 2) ** 2) ** 0.5 - r) < 1e-9
               for x, y in pts)


def test_chord_is_the_copper_the_centreline_crosses():
    import math
    c = tdl._kicad_circle((0, 0), 0.3)
    side = 0.3 * math.cos(math.pi / 24)     # the 24-gon's side on the axis
    # through the centre, side to side
    assert tdl._chord([(0, 0), (2, 0)], c, (0, 0)) == pytest.approx(2 * side)
    # 0.285 off: under the true circle's 0.187
    ch = tdl._chord([(0.05, 0.285), (2, 0.285)], c, (0.05, 0.285))
    assert 0.15 < ch < 2 * (0.3 ** 2 - 0.285 ** 2) ** 0.5
    # the inside end past the side, within TOL: still found; past TOL not
    p = (side + tdl.TOL / 2, 0)
    assert tdl._chord([p, (2, 0)], c, p) == pytest.approx(2 * side)
    p = (side + 2 * tdl.TOL, 0)
    assert tdl._chord([p, (2, 0)], c, p) == math.inf
    # an arc or a zero-length track: KiCad keeps the drop
    assert tdl._chord([(0, 0), (1, 1), (2, 0)], c, (0, 0)) == math.inf
    assert tdl._chord([(0, 0), (0, 0)], c, (0, 0)) == math.inf


def test_join_band_runs_on_from_the_outer_end():
    """A track into the via and the track going on from its outer end
    share the band (two_tracks). A stub wholly inside, a track with
    neither end inside that crosses the copper off the centre, and one
    entering on a chord narrower than itself have none, and say why; a
    track that never meets the copper is not into the via at all."""
    from types import SimpleNamespace
    from shapely.geometry import LineString

    def trk(a, b, w=0.2):
        return SimpleNamespace(shape=LineString([a, b]), width=w)

    def jb(t):
        return tdl._join_band(t, at, cu, circ, tdl._end_index([t]))
    at = (0.0, 0.0)
    cu = Point(at).buffer(0.3 + tdl.TOL)
    circ = tdl._kicad_circle(at, 0.3)
    into, on, far = trk((0, 0), (0, 1)), trk((0, 1), (1, 1)), trk((5, 5), (6, 5))
    ends = tdl._end_index([into, on, far])
    band, why = tdl._join_band(into, at, cu, circ, ends)
    assert why is None
    assert [(ln, h) for ln, h in band] == [(into.shape, 0.1 + tdl.BAND_MM),
                                          (on.shape, 0.1 + tdl.BAND_MM)]
    assert tdl._in_band(Point(0.5, 1.05), band)
    assert not tdl._in_band(Point(0.5, 0.5), band)
    assert jb(trk((0, 0), (0.1, 0))) == (None, "inside")
    assert jb(trk((-1, 0.2), (1, 0.2))) == (None, "crosses")
    assert jb(trk((-1, 0.5), (1, 0.5))) == (None, None)
    # an end in the copper 0.285 off the centre: a 0.171 mm chord
    assert jb(trk((0.05, 0.285), (2, 0.285), 0.25)) == (None, "grazes")
    band, why = jb(trk((0.05, 0.285), (2, 0.285), 0.15))
    assert band is not None and why is None
    band, why = jb(trk((-1, 0.05), (1, 0.05)))
    assert band is not None and why is None


def test_matched_pairs_each_drop_with_one_join():
    """A drop answers one join; the most joins answered, so a drop lying in
    two bands goes where it lets another drop answer too."""
    from shapely.geometry import LineString
    b0 = ((LineString([(0, 0), (0, -2)]), 0.12),)
    b1 = ((LineString([(0.2, 0), (0.2, -2)]), 0.12),)
    both, only0, only1 = Point(0.1, -1), Point(-0.05, -1), Point(0.25, -1)
    assert tdl._matched([b0, b1], []) == 0
    assert tdl._matched([b0, b1], [both]) == 1
    assert tdl._matched([b0, b1], [both, only0]) == 2   # both moves to b1
    assert tdl._matched([b0, b1], [only0, only0]) == 1
    assert tdl._matched([b0], [both, only0, only1]) == 1
    assert tdl._matched([b0, b1], [only1, both]) == 2


def test_tracks_into_an_item_answered_or_named_per_layer():
    """A drop answers one track into the item; each track no drop
    answers says why: a join with no drop, a track too wide for one, a
    graze or an off-centre crossing. deficit counts the joins alone,
    missing the unheld layers with one, undropped the unheld layers with
    a graze or crossing narrow enough for a drop."""
    from shapely.geometry import LineString

    def band(x, w):
        return ((LineString([(x, 0), (x, -2)]), w / 2 + tdl.BAND_MM),)
    lim = tdl._limits(tdl.ITEM_PARAMS)
    it = tdl.Item("via", "A", (0, 0), Point(0, 0).buffer(0.3),
                  ("F.Cu", "In1.Cu", "B.Cu"), 0.15, 0.6, True,
                  joins={"F.Cu": 2, "B.Cu": 1}, held={"B.Cu"},
                  drops={"F.Cu": 2}, lim=lim,
                  cands={"F.Cu": [(0.55, band(0.6, 0.55)),
                                  (0.2, band(0, 0.2)),
                                  (0.2, band(-0.6, 0.2))],
                         "B.Cu": [(0.2, band(0, 0.2))]},
                  grazes={"F.Cu": [(0.3, "crosses")],
                          "In1.Cu": [(0.6, "grazes")],
                          "B.Cu": [(0.25, "grazes")]},
                  tips={"F.Cu": [Point(0, -1), Point(0.6, -1)]})
    # 0.55 mm is too wide on a 0.6 mm via (filter_ratio 0.9: 0.54), 0.5
    # is not; under max_width 0 every track is (10.0.6 makes no drop)
    assert it.too_wide(0.55) and not it.too_wide(0.5)
    assert tdl.too_wide(0.5, 0.5, lim)
    assert tdl.too_wide(0.1, 0.6, (0.0, 1.0, 0.9))
    # F.Cu: the too-wide track and one join have a drop each
    assert (it.answered("F.Cu"), it.into("F.Cu")) == (2, 4)
    assert it.unanswered("F.Cu") == [(0.2, "no drop"), (0.3, "crosses")]
    assert (it.deficit("F.Cu"), it.deficit("B.Cu")) == (1, 1)
    assert (it.answered("In1.Cu"), it.into("In1.Cu")) == (0, 1)
    assert it.unanswered("In1.Cu") == [(0.6, "grazes")]
    assert it.deficit("In1.Cu") == 0
    assert it.missing() == ["F.Cu"]
    assert it.missing(in_pours=True) == ["B.Cu", "F.Cu"]
    # In1.Cu's graze is too wide for a drop, B.Cu's is held
    assert it.undropped() == ["F.Cu"]
    # one drop in a too-wide track's band and a join's: it is the join's
    two = tdl.Item("via", "A", (0, 0), Point(0, 0).buffer(0.3), ("F.Cu",),
                   0.15, 0.6, True, joins={"F.Cu": 1}, drops={"F.Cu": 1},
                   lim=lim, cands={"F.Cu": [(0.55, band(0.1, 0.55)),
                                            (0.2, band(0, 0.2))]},
                   tips={"F.Cu": [Point(0.05, -1)]})
    assert two.answered("F.Cu") == 1
    assert two.unanswered("F.Cu") == [(0.55, "too wide")]
    assert two.deficit("F.Cu") == 0 and two.missing() == []
    # an item read() did not fill compares its counts
    bare = tdl.Item("via", "A", (0, 0), Point(0, 0).buffer(0.3), ("F.Cu",),
                    0.15, 0.6, True, joins={"F.Cu": 2}, drops={"F.Cu": 1})
    assert bare.deficit("F.Cu") == 1 and bare.deficit("B.Cu") == 0
    assert (bare.answered("F.Cu"), bare.into("F.Cu")) == (1, 2)
    assert bare.undropped() == []


def test_limits_keep_a_zero_and_read_the_rest_as_the_defaults():
    assert tdl._limits({"max_width_mm": 0, "width_ratio": -1,
                        "filter_ratio": "0.5"}) == (0.0, 1.0, 0.9)
    assert tdl._limits({"max_width_mm": True, "width_ratio": float("nan"),
                        "filter_ratio": 0.0}) == (2.0, 1.0, 0.0)
    assert tdl.too_wide(0.01, 0.6, (0.0, 1.0, 0.9))
    assert not tdl.too_wide(0.53, 0.6, (2.0, 1.0, 0.9))
    assert tdl.too_wide(0.54, 0.6, (2.0, 1.0, 0.9))


def test_config_refuses_unknown_keys():
    with pytest.raises(ValueError, match="unknown keys"):
        tdl.config({"teardrops": {"enable": True}})


def test_required_on_flex_area_or_constraints(tmp_path):
    flex = _edit(_copy(tmp_path, RAW), _add_flex)
    b = tdl.read(flex)
    assert b.flex == ["FLEX_TAIL"]
    assert tdl.required(tdl.config(None), b.flex)
    assert not tdl.required(tdl.config({"teardrops": {"required": False}}),
                            b.flex)
    assert tdl.required(tdl.config({"teardrops": {"required": True}}), [])
    assert not tdl.required(tdl.config(None), [])


# ------------------------------------------------------------- ring rule

def test_standard_vias_pass_without_teardrops():
    # 0.6/0.3 (ring 0.15) and the 0.4 mm header ring clear 0.125
    assert tdl.ring_findings(tdl.read(RAW), req=False) == []


def test_planted_thin_ring_flagged_without_and_passes_with_teardrop(tmp_path):
    raw = _edit(_copy(tmp_path, RAW, "raw.kicad_pcb"), _shrink_via_b)
    f = tdl.ring_findings(tdl.read(raw), req=False)
    assert [(x["kind"], x["item"], x["severity"], x["layers"]) for x in f] \
        == [("dfm_ring_breakout", VIA_B, "warning", ["F.Cu"])]
    assert f[0]["ring_mm"] == pytest.approx(0.1)
    assert f[0]["limit_mm"] == pytest.approx(0.125)
    dropped = _edit(_copy(tmp_path, DROPPED, "dr.kicad_pcb"), _shrink_via_b)
    assert tdl.ring_findings(tdl.read(dropped), req=False) == []


def test_mutant_teardrop_deleted_is_caught(tmp_path):
    """The dropped board with VIA_B's teardrop zone cut out: the thin ring
    is a breakout again, and on a flex board the bare join is an error."""
    mut = _edit(_copy(tmp_path, DROPPED),
                lambda t: _strip_drop_of(_shrink_via_b(t), 13.2, 21.7))
    b = tdl.read(mut)
    assert b.drop_count == 2
    f = tdl.ring_findings(b, req=False)
    assert [(x["kind"], x["item"]) for x in f] == \
        [("dfm_ring_breakout", VIA_B)]
    f = tdl.ring_findings(b, req=True)
    assert [(x["kind"], x["severity"]) for x in f] == \
        [("dfm_ring_breakout", "error")]


def test_a_graze_on_a_thin_ring_is_a_breakout_that_says_so(tmp_path):
    """Via B shrunk to 0.5 mm (a 16-gon) keeps its drop; a 0.25 mm track
    with an end in its copper 0.22 mm off the centre crosses a 0.219 mm
    chord: KiCad makes it no drop, and the thin ring is a breakout that
    names the graze. A track too wide for a drop, or a graze in a pour,
    is none."""
    def board(name, w, layer="F.Cu"):
        add = _add_track((13.2, 21.96), (15.0, 21.96), w, layer=layer)
        pcb = _edit(_copy(tmp_path, DROPPED, name),
                    lambda t: add(_shrink_via_b(t)))
        return tdl.read(pcb)
    b = board("g.kicad_pcb", 0.25)
    assert {it.name: it for it in b.items}[VIA_B].grazes == \
        {"F.Cu": [(0.25, "grazes")]}
    f = tdl.ring_findings(b, req=False)
    assert [(x["kind"], x["item"], x["layers"]) for x in f] == \
        [("dfm_ring_breakout", VIA_B, ["F.Cu"])]
    assert "grazes the copper" in f[0]["msg"]
    assert tdl.ring_findings(board("w.kicad_pcb", 0.46), req=False) == []
    assert tdl.ring_findings(board("b.kicad_pcb", 0.25, "B.Cu"),
                             req=False) == []


def test_flex_board_requires_drops_on_every_join(tmp_path):
    raw = _edit(_copy(tmp_path, RAW, "raw.kicad_pcb"), _add_flex)
    vios: list = []
    facts = dfm_check.check_teardrops(raw, CAPS, vios)
    assert facts["required"] and facts["flex_areas"] == ["FLEX_TAIL"]
    assert sorted((v["kind"], v["item"], v["severity"]) for v in vios) == [
        ("dfm_teardrop_missing", "J1.1", "error"),
        ("dfm_teardrop_missing", VIA_B, "error"),
        ("dfm_teardrop_missing", VIA_A, "error")]
    dropped = _edit(_copy(tmp_path, DROPPED, "dr.kicad_pcb"), _add_flex)
    vios = []
    facts = dfm_check.check_teardrops(dropped, CAPS, vios)
    assert vios == [] and facts["drops"] == 3 \
        and facts["items_with_drops"] == 3


def test_dfm_family_and_caps_keys():
    assert dfm_check.DFM_FAMILIES["teardrop"] == (
        "dfm_ring_breakout", "dfm_teardrop_missing")
    caps = dfm_check.load_capabilities(dfm_check.CAPABILITIES)
    assert caps["board"]["drill_wander_mm"] == 0.075
    assert caps["board"]["min_ring_abs_mm"] == 0.05


# ------------------------------------------------------------- the step

def _report(argv) -> dict:
    payload, _ = rtd.run(argv)
    return payload


def test_off_unless_enabled_or_required(tmp_path):
    pcb = _copy(tmp_path, RAW)
    before = pcb.read_bytes()
    r = _report(["--pcb", str(pcb)])
    assert r["enabled_by"] is None and "skipped" in r
    assert pcb.read_bytes() == before
    r = _report(["--pcb", str(pcb), "--dry-run", "--force"])
    assert sorted(r["planned"]) == sorted([VIA_A, VIA_B, "J1.1"])
    assert pcb.read_bytes() == before
    (tmp_path / "constraints.json").write_text(
        json.dumps({"teardrops": {"enabled": True}}))
    r = _report(["--pcb", str(pcb), "--dry-run"])
    assert r["enabled_by"] == "constraints"
    flex = _edit(_copy(tmp_path, RAW, "flex.kicad_pcb"), _add_flex)
    (tmp_path / "constraints.json").unlink()
    r = _report(["--pcb", str(flex), "--dry-run"])
    assert r["enabled_by"] == "required" and r["required"]


def test_dropped_board_is_a_no_op(tmp_path):
    pcb = _copy(tmp_path, DROPPED)
    before = pcb.read_bytes()
    r = _report(["--pcb", str(pcb), "--force"])
    assert r["planned"] == [] and r["already"] == 3
    assert r["drops_after"] == 3 and pcb.read_bytes() == before


def test_drc_regression_leaves_the_board(tmp_path, monkeypatch):
    """Mutant: the refill after enabling grows a DRC type -> the staged
    board is thrown away and the run fails."""
    import kc
    import routelib
    pcb = _copy(tmp_path, RAW)
    before = pcb.read_bytes()
    calls = []

    def fake_drc(cli, staged, **kw):
        calls.append(staged)
        v = [] if len(calls) == 1 else [{"severity": "error",
                                          "source": "drc",
                                          "check": "clearance"}]
        return {"violations": v}

    monkeypatch.setattr(kc, "run_drc", fake_drc)
    monkeypatch.setattr(routelib, "run_worker",
                        lambda *a, **k: {"ok": True, "vias": 2, "pads": 1})
    import env
    monkeypatch.setattr(env, "find_kicad_cli", lambda: Path("/bin/true"))
    monkeypatch.setattr(env, "find_kicad_python", lambda cli: Path("/bin/true"))
    r = _report(["--pcb", str(pcb), "--force"])
    assert [v["kind"] for v in r["violations"]][0] == "teardrop_regression"
    assert r["violations"][0]["grown"] == {"drc:clearance": [0, 1]}
    assert pcb.read_bytes() == before and not r.get("written")


# ------------------------------------------------------------- live (slow)

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_live_teardrops_led_count_drc_idempotent(tmp_path):
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    pcb = _copy(tmp_path, RAW, "led.kicad_pcb")
    for ext in (".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIX / f"led{ext}", tmp_path / f"led{ext}")
    r = _report(["--pcb", str(pcb), "--force"])
    assert r["status"] == "pass", r["violations"]
    assert r["enabled_items"] == {"via": 2, "pth": 1, "smd": 0}
    assert r["drops_after"] == 3 == r["drops_wanted"]
    assert r["drc_after"] == r["drc_before"] == {}
    b = tdl.read(pcb)
    assert all(not it.drops for it in b.items if it.kind == "smd")
    sha = _sha(pcb)
    r2 = _report(["--pcb", str(pcb), "--force"])
    assert r2["planned"] == [] and r2["already"] == 3
    assert r2["drops_after"] == 3 and _sha(pcb) == sha
    shrunk = _edit(pcb, _shrink_via_b)
    assert tdl.ring_findings(tdl.read(shrunk), req=True) == []


def test_live_add_via_merges_teardrops_onto_a_via_already_there(tmp_path):
    """#1138: an add_via that lands on a via already there merges the keys
    given onto the via's own settings (the rest keep theirs, {} only
    enables); a new via fills the defaults for the keys left out. One worker
    run, the ops applied in order."""
    import env
    import route_edit
    import sexpdata
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    pcb = _copy(tmp_path, DROPPED, "led.kicad_pcb")
    for ext in (".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIX / f"led{ext}", tmp_path / f"led{ext}")
    b = {"op": "add_via", "at": [13.2, 21.74], "size": 0.6, "drill": 0.3,
         "net": "GND"}
    res = route_edit.apply_ops(pcb, [
        dict(b, teardrops={"filter_ratio": 0.5, "two_tracks": False}),
        dict(b, teardrops={"curved": True}),
        dict(b, teardrops={}),
        dict(b, at=[16.0, 23.5], teardrops={"max_length_mm": 0.8})])
    assert [r["status"] for r in res] == ["exists"] * 3 + ["added"]
    assert [r.get("teardrops") for r in res[:3]] == ["set", "set", None]
    got = tdl.via_params(sexpdata.loads(pcb.read_text(encoding="utf-8")))
    assert got[UUID_B] == dict(tdl.ITEM_PARAMS, enabled=True,
                               filter_ratio=0.5, two_tracks=False,
                               curved=True)
    assert got[UUID_A] == dict(tdl.ITEM_PARAMS, enabled=True)
    new = got[res[3]["uuid"]]
    assert new == dict(tdl.ITEM_PARAMS, enabled=True, max_length_mm=0.8)


def test_live_teardrops_bb_adc_count(tmp_path):
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    src = REPO / "tests" / "fixtures" / "bb_adc"
    for f in src.iterdir():
        if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copy2(f, tmp_path / f.name)
    pcb = tmp_path / "bb-adc.kicad_pcb"
    r = _report(["--pcb", str(pcb), "--force"])
    assert r["status"] == "pass", r["violations"]
    assert r["enabled_items"] == {"via": 12, "pth": 5, "smd": 0}
    assert r["drops_after"] == 19 == r["drops_wanted"]
    assert r["drc_after"] == r["drc_before"]
