"""S11 acceptance tests: plane generation (planes_gen, SPEC P7.2).

Pure tests (planning, priorities, via-grid math, EP heuristics, verification)
run on synthetic text boards with no toolchain and are unmarked; tests that
build corpus boards and drive the SWIG worker + kicad-cli refill carry
`smoke`.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import env  # noqa: E402
import geom  # noqa: E402
import planes_gen  # noqa: E402
from checklib import CheckError  # noqa: E402
from shapely.geometry import box  # noqa: E402


# ---- synthetic boards (test_place_anneal helper pattern) --------------------

def _fp(ref: str, x: float, y: float, pads: str = "") -> str:
    return (f'  (footprint "t:{ref}" (layer "F.Cu")\n    (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n{pads})\n')


def _pad(num: str, x: float, y: float, net: str | None = None,
         w: float = 0.6, h: float = 0.6, kind: str = "smd rect",
         layers: str = '"F.Cu"') -> str:
    n = f' (net "{net}")' if net else ""
    return (f'    (pad "{num}" {kind} (at {x} {y}) (size {w} {h})'
            f' (layers {layers}){n})\n')


_LAYERS = {
    2: '(0 "F.Cu" signal) (2 "B.Cu" signal)',
    4: ('(0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) '
        '(31 "B.Cu" signal)'),
}


def _pcb(tmp_path, name: str, body: str, layers: int = 2,
         w: float = 60.0, h: float = 40.0) -> Path:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers {_LAYERS[layers]} (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end {w} {h}) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _basic_body() -> str:
    body = _fp("U1", 30, 20, _pad("1", -2, 0, "GND") + _pad("2", 2, 0, "VCC"))
    body += _fp("C1", 10, 10, _pad("1", -1, 0, "VCC") + _pad("2", 1, 0, "GND"))
    body += _fp("C2", 12, 14, _pad("1", -1, 0, "VCC") + _pad("2", 1, 0, "GND"))
    body += _fp("R1", 20, 30, _pad("1", -1, 0, "SIG") + _pad("2", 1, 0, "SIG"))
    return body


def _bg(tmp_path, name="b", layers=2, body=None) -> geom.BoardGeom:
    return geom.BoardGeom.from_file(
        _pcb(tmp_path, name, body if body is not None else _basic_body(),
             layers=layers))


# ============================================================ pure: planning

def test_defaults_2layer(tmp_path):
    bg = _bg(tmp_path)
    plan, meta = planes_gen.build_plan(bg, {})
    assert meta["source"] == "defaults"
    assert [(z["net"], z["layer"]) for z in plan] == [("GND", "B.Cu")]
    # full-outline rect inset 0.5 mm from the bbox
    assert plan[0]["_rect"] == (0.5, 0.5, 59.5, 39.5)
    assert plan[0]["priority"] == 0


def test_defaults_4layer_dominant_power_from_constraints(tmp_path):
    bg = _bg(tmp_path, layers=4)
    con = {"power": [{"net": "VCC", "current_a": 1.0},
                     {"net": "SIG", "current_a": 0.1}]}
    plan, _ = planes_gen.build_plan(bg, con)
    assert [(z["net"], z["layer"]) for z in plan] == \
        [("GND", "In1.Cu"), ("VCC", "In2.Cu")]


def test_defaults_4layer_pad_count_fallback(tmp_path):
    # no constraints["power"]: dominant = most pads, GND excluded
    bg = _bg(tmp_path, layers=4)
    plan, _ = planes_gen.build_plan(bg, {})
    assert [(z["net"], z["layer"]) for z in plan] == \
        [("GND", "In1.Cu"), ("VCC", "In2.Cu")]  # VCC 3 pads > SIG 2


def test_defaults_4layer_no_power_net_second_gnd(tmp_path):
    body = _fp("C1", 10, 10, _pad("1", -1, 0, "GND") + _pad("2", 1, 0, "GND"))
    bg = _bg(tmp_path, layers=4, body=body)
    plan, meta = planes_gen.build_plan(bg, {})
    assert [(z["net"], z["layer"]) for z in plan] == \
        [("GND", "In1.Cu"), ("GND", "In2.Cu")]
    assert any("second" in n and "GND" in n for n in meta["notes"])


def test_high_speed_reference_net_gets_plane(tmp_path):
    bg = _bg(tmp_path, layers=4)
    con = {"planes": [{"net": "VCC", "layer": "In2.Cu"}],
           "high_speed": [{"net": "SIG", "reference": "GND"}]}
    plan, meta = planes_gen.build_plan(bg, con)
    assert meta["added_for_reference"] == [{"net": "GND", "layer": "In1.Cu"}]
    assert ("GND", "In1.Cu") in [(z["net"], z["layer"]) for z in plan]
    assert any("reference" in n for n in meta["notes"])
    # dict-form reference {layer: net} is understood too
    con2 = {"planes": [{"net": "VCC", "layer": "In2.Cu"}],
            "high_speed": [{"net": "SIG", "reference": {"In1.Cu": "GND"}}]}
    plan2, meta2 = planes_gen.build_plan(bg, con2)
    assert meta2["added_for_reference"] == [{"net": "GND", "layer": "In1.Cu"}]


def test_reference_already_planned_not_duplicated(tmp_path):
    bg = _bg(tmp_path)
    con = {"high_speed": [{"net": "SIG", "reference": "GND"}]}
    plan, meta = planes_gen.build_plan(bg, con)  # defaults already pour GND
    assert meta["added_for_reference"] == []
    assert len([z for z in plan if z["net"] == "GND"]) == 1


# ============================================================ pure: priorities

def test_island_priorities_auto(tmp_path):
    # usbbuck4-golden pattern: full power plane + two islands on one layer
    bg = _bg(tmp_path, layers=4)
    con = {"planes": [
        {"net": "VCC", "layer": "In2.Cu"},
        {"net": "GND", "layer": "In2.Cu", "region": [2, 2, 10, 38]},
        {"net": "SIG", "layer": "In2.Cu", "region": [40, 20, 58, 38]},
        {"net": "GND", "layer": "In1.Cu"}]}
    plan, _ = planes_gen.build_plan(bg, con)
    by = {(z["net"], z["layer"]): z["priority"] for z in plan}
    assert by[("VCC", "In2.Cu")] == 0
    assert by[("GND", "In2.Cu")] == 1      # island beats the big pour
    assert by[("SIG", "In2.Cu")] == 1      # islands do not overlap each other
    assert by[("GND", "In1.Cu")] == 0      # other layer untouched


def test_explicit_priority_honored_and_bumped(tmp_path):
    bg = _bg(tmp_path, layers=4)
    # explicit higher-than-needed priority is honored
    con = {"planes": [
        {"net": "VCC", "layer": "In2.Cu"},
        {"net": "GND", "layer": "In2.Cu", "region": [2, 2, 10, 38],
         "priority": 5}]}
    plan, _ = planes_gen.build_plan(bg, con)
    assert plan[1]["priority"] == 5
    # explicit too-low island priority gets bumped above the big pour
    con2 = {"planes": [
        {"net": "VCC", "layer": "In2.Cu", "priority": 3},
        {"net": "GND", "layer": "In2.Cu", "region": [2, 2, 10, 38],
         "priority": 0}]}
    plan2, _ = planes_gen.build_plan(bg, con2)
    assert plan2[0]["priority"] == 3
    assert plan2[1]["priority"] == 4


def test_same_net_overlap_is_one_zone(tmp_path):
    # KiCad 10 flags same-priority overlapping zones (zones_intersect) even
    # for the SAME net, and fills two of different priority apart - so
    # overlapping same-net entries become one zone over their union.
    bg = _bg(tmp_path)
    con = {"planes": [{"net": "GND", "layer": "B.Cu"},
                      {"net": "GND", "layer": "B.Cu",
                       "region": [10, 10, 30, 30]}]}
    plan, meta = planes_gen.build_plan(bg, con)
    assert len(plan) == 1
    assert plan[0]["_rect"] == (0.5, 0.5, 59.5, 39.5)
    assert any("merged 2" in n for n in meta["notes"])


def test_touching_same_net_rects_merge(tmp_path):
    """lite-main (hifi-receiver-2): PVDD strips sharing an edge were filled
    apart by KiCad (distinct priorities) and had to be joined by tracks
    across each seam. Touching same-net rects are one polygon zone now."""
    bg = _bg(tmp_path)
    con = {"planes": [
        {"net": "VCC", "layer": "F.Cu", "region": [1, 1, 10, 5],
         "connect": "solid"},
        {"net": "VCC", "layer": "F.Cu", "region": [1, 5, 14, 9],
         "connect": "solid", "priority": 2},
        {"net": "VCC", "layer": "F.Cu", "region": [40, 1, 50, 9],
         "connect": "solid"}]}
    plan, _ = planes_gen.build_plan(bg, con)
    assert len(plan) == 2
    merged = next(z for z in plan if z.get("_merged_from"))
    assert merged["_rect"] == (1.0, 1.0, 14.0, 9.0)
    assert merged["_geom"].area == pytest.approx(9 * 4 + 13 * 4)
    assert merged["priority"] == 2            # the explicit one carried
    zones = planes_gen.worker_zones(plan)
    zm = next(z for z in zones if "poly" in z)
    assert "rect" not in zm and len(zm["poly"]) == 6   # an L, not a box
    assert zm["connect"] == "solid"
    # the disjoint one keeps its rect
    assert [z["rect"] for z in zones if "rect" in z] == [[40.0, 1.0, 50.0, 9.0]]


def test_touching_rects_differing_settings_stay_apart(tmp_path):
    """Rects sharing a mere EDGE are zones_intersect errors at equal
    priority (LEARNINGS 1327b): when they can't merge (other settings),
    they still never tie, and the plan says they fill apart."""
    bg = _bg(tmp_path)
    con = {"planes": [
        {"net": "GND", "layer": "B.Cu", "region": [1, 1, 30, 39]},
        {"net": "GND", "layer": "B.Cu", "region": [30, 1, 59, 39],
         "connect": "solid"}]}
    plan, meta = planes_gen.build_plan(bg, con)
    assert len(plan) == 2
    assert plan[0]["priority"] != plan[1]["priority"]
    assert any("differ in settings" in n for n in meta["notes"])
    # other nets touching: distinct priorities, never merged
    con2 = {"planes": [
        {"net": "GND", "layer": "B.Cu", "region": [1, 1, 30, 39]},
        {"net": "VCC", "layer": "B.Cu", "region": [30, 1, 59, 39]}]}
    plan2, _ = planes_gen.build_plan(bg, con2)
    assert len(plan2) == 2 and plan2[0]["priority"] != plan2[1]["priority"]
    # disjoint rects (a real gap) still tie at 0 - no needless stacking
    con3 = {"planes": [
        {"net": "GND", "layer": "B.Cu", "region": [1, 1, 29, 39]},
        {"net": "GND", "layer": "B.Cu", "region": [31, 1, 59, 39]}]}
    plan3, _ = planes_gen.build_plan(bg, con3)
    assert plan3[0]["priority"] == plan3[1]["priority"] == 0


def test_corner_touch_stays_separate(tmp_path):
    bg = _bg(tmp_path)
    con = {"planes": [
        {"net": "GND", "layer": "B.Cu", "region": [1, 1, 10, 10]},
        {"net": "GND", "layer": "B.Cu", "region": [10, 10, 20, 20]}]}
    plan, meta = planes_gen.build_plan(bg, con)
    assert len(plan) == 2
    assert any("corner" in n for n in meta["notes"])


def test_connect_solid_key_validated_and_forwarded(tmp_path):
    """T6 (P7B-2): the sidecar 'connect' key reaches the worker zone dicts;
    bad values refuse before any toolchain runs."""
    bg = _bg(tmp_path)
    con = {"planes": [
        {"net": "GND", "layer": "B.Cu"},
        {"net": "VCC", "layer": "B.Cu", "region": [10, 10, 30, 30],
         "connect": "solid"}]}
    plan, _ = planes_gen.build_plan(bg, con)
    zones = planes_gen.worker_zones(plan)
    assert zones[0]["connect"] is None          # default stays thermal
    assert zones[1]["connect"] == "solid"
    with pytest.raises(CheckError, match="connect"):
        planes_gen.build_plan(bg, {"planes": [
            {"net": "GND", "layer": "B.Cu", "connect": "full"}]})


# ============================================================ pure: shapes

def test_worker_zone_dicts_and_layer_types(tmp_path):
    bg = _bg(tmp_path, layers=4)
    con = {"planes": [
        {"net": "GND", "layer": "In1.Cu"},
        {"net": "VCC", "layer": "In2.Cu", "clearance": 0.3,
         "min_width": 0.3, "min_island_mm2": 2.0},
        {"net": "GND", "layer": "B.Cu"}]}
    plan, _ = planes_gen.build_plan(bg, con)
    zones = planes_gen.worker_zones(plan)
    assert zones[0] == {"net": "GND", "layer": "In1.Cu",
                        "rect": [0.5, 0.5, 59.5, 39.5], "priority": 0,
                        "min_island_mm2": None, "clearance": None,
                        "min_width": planes_gen.MIN_WIDTH_MM,
                        "connect": None}
    assert zones[1]["clearance"] == 0.3
    assert zones[1]["min_width"] == 0.3
    assert zones[1]["min_island_mm2"] == 2.0
    # ONLY inner plane layers become "power"; the outer pour stays signal
    assert planes_gen.plane_layer_types(plan, bg) == (
        {"In1.Cu": "power", "In2.Cu": "power"}, [])


def _seg(net: str, layer: str, x1, y1, x2, y2) -> str:
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) '
            f'(layer "{layer}") (net "{net}"))\n')


def test_layer_type_region_pour_stays_signal(tmp_path):
    # #925: one small region pour on an inner routing layer must not turn
    # the layer into a plane (power) in the stackup
    body = _basic_body() + _seg("SIG", "In2.Cu", 5, 30, 25, 30)
    bg = _bg(tmp_path, layers=4, body=body)
    plan, _ = planes_gen.build_plan(bg, {"planes": [
        {"net": "GND", "layer": "In1.Cu"},
        {"net": "VCC", "layer": "In2.Cu", "region": [17, 5, 29, 15.5],
         "connect": "solid"}]})
    types, notes = planes_gen.plane_layer_types(plan, bg)
    assert types == {"In1.Cu": "power"}
    assert len(notes) == 1 and notes[0].startswith("In2.Cu ")
    assert "cover 5% of the board" in notes[0]
    assert "1 track(s) of other nets (SIG)" in notes[0]


def test_layer_type_full_pour_with_routing_stays_signal(tmp_path):
    # a full pour on a layer that still carries another net's tracks is a
    # mixed layer, not a plane; tracks of the poured net itself don't count
    body = (_basic_body() + _seg("SIG", "In2.Cu", 5, 30, 25, 30)
            + _seg("GND", "In1.Cu", 5, 5, 9, 5))
    bg = _bg(tmp_path, layers=4, body=body)
    plan, _ = planes_gen.build_plan(bg, {"planes": [
        {"net": "GND", "layer": "In1.Cu"},
        {"net": "VCC", "layer": "In2.Cu"}]})
    types, notes = planes_gen.plane_layer_types(plan, bg)
    assert types == {"In1.Cu": "power"}
    assert notes == ["In2.Cu layer type not set to power: it carries 1 "
                     "track(s) of other nets (SIG)"]


def test_layer_type_tiled_regions_count_as_plane(tmp_path):
    # two region pours that together cover the board make a (split) plane
    bg = _bg(tmp_path, layers=4)
    plan, _ = planes_gen.build_plan(bg, {"planes": [
        {"net": "GND", "layer": "In1.Cu"},
        {"net": "VCC", "layer": "In2.Cu", "region": [0.5, 0.5, 30, 39.5]},
        {"net": "SIG", "layer": "In2.Cu", "region": [30, 0.5, 59.5, 39.5]}]})
    assert planes_gen.plane_layer_types(plan, bg) == (
        {"In1.Cu": "power", "In2.Cu": "power"}, [])


# ============================================================ pure: thermal vias

def test_via_grid_math():
    pad = box(-2, -2, 2, 2)          # 4x4 mm EP at the origin
    pts = planes_gen.via_grid(pad)   # 0.6 via, 1.2 pitch
    assert len(pts) == 9             # 3x3: +/-1.2 offsets fit inside 4x4
    assert (0.0, 0.0) in pts
    assert all(abs(x) <= 1.2 and abs(y) <= 1.2 for x, y in pts)
    assert len(planes_gen.via_grid(box(-0.6, -0.6, 0.6, 0.6))) == 1  # center
    assert planes_gen.via_grid(box(-0.25, -0.25, 0.25, 0.25)) == []  # no room
    assert pts == sorted(pts)        # deterministic order


def test_ep_candidates(tmp_path):
    body = _fp("U1", 20, 20,                       # EP on GND, 16 mm2
               _pad("9", 0, 0, "GND", 4, 4)
               + _pad("", 0, 0, None, 5, 5)        # bigger NO-NET overlap
               + _pad("1", -3, 0, "SIG"))          # must not deter
    body += _fp("U2", 45, 20,                      # EP on VCC -> skipped
                _pad("9", 0, 0, "VCC", 4, 4) + _pad("1", -3, 0, "GND", 3, 3))
    body += _fp("C1", 10, 35, _pad("1", 0, 0, "GND", 0.5, 0.5))  # under floor
    body += _fp("J1", 50, 35,                      # THT only -> no SMD EP
                _pad("1", 0, 0, "GND", 3, 3, kind="thru_hole circle",
                     layers='"*.Cu"'))
    bg = _bg(tmp_path, body=body)
    eps = planes_gen.ep_pads(bg, {"GND"})
    assert [(p.ref, p.number, p.net) for p in eps] == [("U1", "9", "GND")]
    ops, pads = planes_gen.thermal_via_ops(bg, {"GND"})
    assert len(ops) == 9 and ops[0]["op"] == "add_via"
    assert all(o["net"] == "GND" and o["size"] == 0.6 and o["drill"] == 0.3
               for o in ops)
    assert pads == [{"ref": "U1", "pad": "9", "net": "GND", "vias": 9}]
    # lower floor pulls in C1's 0.25 mm2 pad too - no room for a via there
    eps2 = planes_gen.ep_pads(bg, {"GND"}, min_mm2=0.2)
    assert {p.ref for p in eps2} == {"U1", "C1"}
    ops2, pads2 = planes_gen.thermal_via_ops(bg, {"GND"}, ep_min_mm2=0.2)
    assert {p["ref"] for p in pads2} == {"U1"}
    assert len(ops2) == 9


_VIA = ('  (via (at {x} {y}) (size 0.6) (drill 0.3)'
        ' (layers "F.Cu" "B.Cu") (net "GND"))\n')


def test_thermal_grid_avoids_existing_drills(tmp_path):
    """T6 (P7A-5a / LEARNINGS 1327a): grid points inside the hole floor of
    drills already in the land are dropped (U22 shipped 15 vias-in-pad;
    planes_gen stacked 21 more on top -> 24 hole_to_hole)."""
    body = _fp("U1", 20, 20, _pad("9", 0, 0, "GND", 4, 4))
    for dx in (-1.2, 0.0, 1.2):
        body += _VIA.format(x=20 + dx, y=20)
    bg = _bg(tmp_path, body=body)
    ops, pads = planes_gen.thermal_via_ops(bg, {"GND"})
    assert pads == [{"ref": "U1", "pad": "9", "net": "GND", "vias": 6}]
    assert len(ops) == 6                      # 9-grid minus the 3 occupied
    occupied = {(18.8, 20.0), (20.0, 20.0), (21.2, 20.0)}
    assert not any(tuple(op["at"]) in occupied for op in ops)


def test_thermal_grid_skips_land_with_own_via_array(tmp_path):
    body = _fp("U1", 20, 20, _pad("9", 0, 0, "GND", 4, 4))
    for dx, dy in ((-1.2, 0), (0, 0), (1.2, 0), (0, 1.2)):
        body += _VIA.format(x=20 + dx, y=20 + dy)
    bg = _bg(tmp_path, body=body)
    ops, pads = planes_gen.thermal_via_ops(bg, {"GND"})
    assert ops == []
    assert pads[0]["vias"] == 0
    assert "own via array" in pads[0]["note"]


def test_thermal_grid_counts_same_footprint_tht_pads(tmp_path):
    """Footprint-shipped thru-hole vias-in-pad (the U22 eFuse pattern) are
    pads of the SAME footprint, not board vias - counted too."""
    tht = ('    (pad "{n}" thru_hole circle (at {x} {y}) (size 0.6 0.6)'
           ' (drill 0.3) (layers "*.Cu") (net "GND"))\n')
    spots = ((-1.2, 0), (1.2, 0), (0, -1.2), (0, 1.2))
    body = _fp("U1", 20, 20, _pad("9", 0, 0, "GND", 4, 4)
               + "".join(tht.format(n=10 + i, x=x, y=y)
                         for i, (x, y) in enumerate(spots)))
    bg = _bg(tmp_path, body=body)
    ops, pads = planes_gen.thermal_via_ops(bg, {"GND"})
    assert ops == []
    assert pads and pads[0]["vias"] == 0 and "own via array" in pads[0]["note"]


def test_thermal_grid_respects_via_keepouts(tmp_path):
    """#435 (hc-power): a rule area that forbids vias (a top-only heater
    coupon) gets no thermal vias; a rule area that allows them does."""
    body = _fp("U1", 20, 20, _pad("9", 0, 0, "GND", 4, 4))
    body += _fp("U2", 45, 20, _pad("9", 0, 0, "GND", 4, 4))
    ra = ('  (zone (net 0) (net_name "") (layers "In1.Cu" "B.Cu") (name "{n}")'
          ' (hatch edge 0.5) (keepout (tracks allowed) (vias {v}) (pads allowed)'
          ' (copperpour allowed) (footprints allowed))'
          ' (polygon (pts (xy {x0} 15) (xy {x1} 15) (xy {x1} 25) (xy {x0} 25))))\n')
    body += ra.format(n="coupon", v="not_allowed", x0=15, x1=25)
    body += ra.format(n="scope", v="allowed", x0=40, x1=50)
    bg = _bg(tmp_path, body=body)
    assert len(planes_gen.via_keepouts(bg)) == 1
    ops, pads = planes_gen.thermal_via_ops(bg, {"GND"})
    by = {p["ref"]: p for p in pads}
    assert by["U1"]["vias"] == 0 and "forbids vias" in by["U1"]["note"]
    assert by["U2"]["vias"] == 9
    assert all(o["at"][0] > 40 for o in ops)


def test_thermal_grid_measures_custom_pads_by_their_copper(tmp_path):
    """#435: a custom pad with a 0.005 mm anchor and ~7 mm2 of primitives
    (LMG2100R044 PGND) is an exposed pad by its polygon, not its anchor."""
    custom = ('    (pad "9" smd custom (at 0 0) (size 0.005 0.005) (layers "F.Cu")'
              ' (net "GND") (options (clearance outline) (anchor rect))'
              ' (primitives (gr_poly (pts (xy -1.5 -1.25) (xy 1.5 -1.25)'
              ' (xy 1.5 1.25) (xy -1.5 1.25)) (width 0) (fill yes))))\n')
    bg = _bg(tmp_path, body=_fp("U1", 20, 20, custom))
    eps = planes_gen.ep_pads(bg, {"GND"})
    assert [(p.ref, p.number) for p in eps] == [("U1", "9")]
    ops, _ = planes_gen.thermal_via_ops(bg, {"GND"})
    assert ops


def test_thermal_grid_needs_a_pour_on_another_layer(tmp_path):
    """#435: vias from an F.Cu pad to a net poured only on F.Cu connect
    nothing (hc-power load bank LDk)."""
    bg = _bg(tmp_path, body=_fp("U1", 20, 20, _pad("9", 0, 0, "LDK", 4, 4)))
    ops, pads = planes_gen.thermal_via_ops(
        bg, {"LDK"}, pour_layers={"LDK": {"F.Cu"}})
    assert ops == [] and "no pour on another" in pads[0]["note"]
    ops, _ = planes_gen.thermal_via_ops(
        bg, {"LDK"}, pour_layers={"LDK": {"F.Cu", "B.Cu"}})
    assert len(ops) == 9
    assert planes_gen.pour_layers(bg, [{"net": "GND", "layer": "In1.Cu"},
                                       {"net": "GND", "layer": "B.Cu"}]) \
        == {"GND": {"In1.Cu", "B.Cu"}}



def test_thermal_grid_honours_min_vias(tmp_path):
    """#967: constraints.thermal[ref].min_vias 0 must stop the grid (an
    LDO's live VOUT tab on the plane net would be drilled into GND), and a
    grid short of a positive floor says so."""
    body = _fp("U1", 20, 20, _pad("4", 0, 0, "+3V3", 4, 4))
    body += _fp("U2", 45, 20, _pad("9", 0, 0, "+3V3", 4, 4))
    bg = _bg(tmp_path, body=body)
    mv = planes_gen.thermal_min_vias(
        {"thermal": [{"ref": "U1", "power_w": 0.5, "min_vias": 0},
                     {"ref": "U2", "power_w": 1, "min_vias": 12},
                     {"ref": "U3", "power_w": 1}]})
    assert mv == {"U1": 0, "U2": 12}
    ops, pads = planes_gen.thermal_via_ops(bg, {"+3V3"}, min_vias=mv)
    by = {p["ref"]: p for p in pads}
    assert by["U1"]["vias"] == 0 and "min_vias is 0" in by["U1"]["note"]
    assert by["U2"]["vias"] == 9 and "under" in by["U2"]["note"]
    assert len(ops) == 9 and all(abs(o["at"][0] - 45) < 3 for o in ops)
    # without the constraints both grids are laid, as before
    ops, _ = planes_gen.thermal_via_ops(bg, {"+3V3"})
    assert len(ops) == 18

# ============================================================ pure: validation

def test_plan_validation_errors(tmp_path):
    bg = _bg(tmp_path)
    for con, frag in [
            ({"planes": []}, "empty"),
            ({"planes": [{"net": "NOPE", "layer": "B.Cu"}]}, "not on board"),
            ({"planes": [{"net": "GND", "layer": "In1.Cu"}]}, "copper layer"),
            ({"planes": [{"net": "GND", "layer": "B.Cu", "bogus": 1}]},
             "unknown keys"),
            ({"planes": [{"net": "GND", "layer": "B.Cu",
                          "region": [5, 5, 5, 35]}]}, "degenerate"),
            ({"planes": [{"net": "GND", "layer": "B.Cu",
                          "region": [1, 2, 3]}]}, "region"),
            ({"planes": [{"net": "GND", "layer": "B.Cu"}],
              "high_speed": [{"net": "X", "reference": "VREF"}]},
             "reference net"),
    ]:
        with pytest.raises(CheckError, match=re.escape(frag)):
            planes_gen.build_plan(bg, con)


def test_cli_missing_board_exits_2():
    assert planes_gen.main(["--pcb", "no/such/board.kicad_pcb"]) == 2


def test_cli_bad_plan_exits_2_before_toolchain(tmp_path):
    pcb = _pcb(tmp_path, "cli", _basic_body())
    con = tmp_path / "c.json"
    con.write_text(json.dumps(
        {"planes": [{"net": "NOPE", "layer": "B.Cu"}]}), encoding="utf-8")
    assert planes_gen.main(["--pcb", str(pcb), "--constraints", str(con)]) == 2


# ============================================================ pure: verification

_FILLED_ZONE = """  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0.5 0.5) (xy 59.5 0.5) (xy 59.5 39.5) (xy 0.5 39.5)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 1 1) (xy 20 1) (xy 20 20) (xy 1 20)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 40 25) (xy 55 25) (xy 55 38) (xy 40 38)))
  )
"""

_EMPTY_ZONE = """  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0.5 0.5) (xy 59.5 0.5) (xy 59.5 39.5) (xy 0.5 39.5)))
  )
"""


def _plan_entry(rect=(0.5, 0.5, 59.5, 39.5)) -> dict:
    return {"net": "GND", "layer": "B.Cu", "priority": 0, "_rect": rect}


def test_verify_zones_reports_area_and_islands(tmp_path):
    bg = _bg(tmp_path, body=_basic_body() + _FILLED_ZONE)
    facts, violations = planes_gen.verify_zones(bg, [_plan_entry()],
                                                {("GND", "B.Cu"): 0})
    assert violations == []
    z = facts[0]
    assert z["islands"] == 2
    assert z["area_mm2"] == pytest.approx(19 * 19 + 15 * 13, abs=0.01)
    assert z["status"] == "added"


def test_verify_zones_zero_fill_is_violation(tmp_path):
    bg = _bg(tmp_path, body=_basic_body() + _EMPTY_ZONE)
    facts, violations = planes_gen.verify_zones(bg, [_plan_entry()],
                                                {("GND", "B.Cu"): 0})
    assert facts[0]["area_mm2"] == 0.0
    assert len(violations) == 1
    v = violations[0]
    assert v["kind"] == "zone_unfilled" and v["severity"] == "error"
    assert v["net"] == "GND" and v["layer"] == "B.Cu"


def test_verify_zones_missing_zone_is_hard_error(tmp_path):
    bg = _bg(tmp_path)  # no zones at all
    with pytest.raises(CheckError, match="not persisted"):
        planes_gen.verify_zones(bg, [_plan_entry()], {("GND", "B.Cu"): 0})


def test_existing_cover_skips_replan(tmp_path):
    bg = _bg(tmp_path, body=_basic_body() + _FILLED_ZONE)
    # partial fill (~23%) does NOT skip; a small covered region does
    plan, meta = planes_gen.build_plan(bg, {})
    assert not plan[0].get("_existing")
    con = {"planes": [{"net": "GND", "layer": "B.Cu",
                       "region": [2, 2, 18, 18]}]}   # inside the 1..20 fill
    plan2, meta2 = planes_gen.build_plan(bg, con)
    assert plan2[0]["_existing"] >= planes_gen.EXISTING_COVER
    assert any("not re-added" in n for n in meta2["notes"])


_SHORT_ZONE = """  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0.5 0.5) (xy 59.5 0.5) (xy 59.5 35.5) (xy 0.5 35.5)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0.5 0.5) (xy 59.5 0.5) (xy 59.5 35.5) (xy 0.5 35.5)))
  )
"""


def test_grown_board_regrows_stale_zone_outline(tmp_path):
    # the pour was drawn on a 60x36 board that board_edit grew to 60x40:
    # the fill still covers > 80%, but the OUTLINE stops at y=35.5
    con = {"planes": [{"net": "GND", "layer": "B.Cu"}]}
    bg = _bg(tmp_path, body=_basic_body() + _SHORT_ZONE)
    plan, meta = planes_gen.build_plan(bg, con)
    assert not plan[0].get("_existing")
    assert plan[0]["_regrow"] == [{"name": "", "net": "GND", "layer": "B.Cu",
                                   "inside": [0.5, 0.5, 59.5, 35.5]}]
    assert any("board grew" in n for n in meta["notes"])
    # an outline that already reaches the planned region stays "existing"
    bg2 = _bg(tmp_path, name="b2", body=_basic_body() + _SHORT_ZONE.replace(
        "35.5", "39.5"))
    plan2, _ = planes_gen.build_plan(bg2, con)
    assert plan2[0]["_existing"] and not plan2[0].get("_regrow")
    # a same-net zone that is not inside the region is never replaced
    con3 = {"planes": [{"net": "GND", "layer": "B.Cu",
                        "region": [0.5, 0.5, 40, 39.5]}]}
    plan3, _ = planes_gen.build_plan(bg, con3)
    assert not plan3[0].get("_regrow")



_SMALL_ZONE = _SHORT_ZONE.replace("59.5", "29.5").replace("35.5", "19.5")


def test_board_grown_far_past_its_pour_still_drops_the_old_zone(tmp_path):
    """#923 point 2, planted: the pour was drawn on a 30x20 board that grew
    to 60x40, so the old fill covers well under EXISTING_COVER. The old
    zone used to stay beside the new one (DRC zones_intersect); it must be
    removed and the planned zone added in its place."""
    con = {"planes": [{"net": "GND", "layer": "B.Cu"}]}
    bg = _bg(tmp_path, body=_basic_body() + _SMALL_ZONE)
    cov = planes_gen._existing_cover(bg, "GND", "B.Cu",
                                     planes_gen._shape(planes_gen.build_plan(
                                         bg, con)[0][0]))
    assert cov < planes_gen.EXISTING_COVER
    plan, meta = planes_gen.build_plan(bg, con)
    assert not plan[0].get("_existing")
    assert plan[0]["_regrow"] == [{"name": "", "net": "GND", "layer": "B.Cu",
                                   "inside": [0.5, 0.5, 29.5, 19.5]}]
    assert any("board grew" in n for n in meta["notes"])

# ============================================================ smoke fixtures

@pytest.fixture(scope="session")
def cli() -> Path:
    c = env.find_kicad_cli()
    if c is None:
        pytest.skip("kicad-cli not installed")
    return c


def _seed_board(cli, out_dir: Path, netlist: Path, name: str, layers: int,
                golden_dir: Path, schematic: Path | None = None) -> Path:
    import board_init
    import place_seed
    args = ["--netlist", str(netlist), "--name", name,
            "--out", str(out_dir / "kicad"), "--layers", str(layers)]
    if layers == 4:
        args += ["--mounting-holes", "4"]
    if schematic:
        args += ["--schematic", str(schematic)]
    assert board_init.main(args) == 0
    pcb = out_dir / "kicad" / f"{name}.kicad_pcb"
    for cname in ("constraints.json", "decoupling.json"):
        src = golden_dir / cname
        if src.is_file():
            shutil.copy2(src, pcb.parent / cname)
    payload, _ = place_seed.run(["--pcb", str(pcb),
                                 "--ops-out", str(out_dir / "seed_ops.json"),
                                 "--apply"])
    assert payload["status"] == "pass"
    return pcb


@pytest.fixture(scope="session")
def blinky2_seeded(cli, tmp_path_factory) -> Path:
    """blinky2 netlist -> board_init (2 layer) -> place_seed --apply.
    Session-scoped and PRISTINE: tests copy it before running planes_gen."""
    d = tmp_path_factory.mktemp("planes2l")
    return _seed_board(
        cli, d, REPO / "tests" / "s7_regen" / "blinky2" / "kicad" / "blinky2.net",
        "blinky2", 2, GOLDEN / "blinky2",
        schematic=GOLDEN / "blinky2" / "blinky2.kicad_sch")


@pytest.fixture(scope="session")
def usbbuck4_seeded(cli, tmp_path_factory) -> Path:
    """usbbuck4 sch -> netlist -> board_init (4 layer) -> place_seed --apply."""
    import kc
    d = tmp_path_factory.mktemp("planes4l")
    net = d / "usbbuck4.net"
    kc.export_netlist(cli, GOLDEN / "usbbuck4" / "usbbuck4.kicad_sch", net)
    return _seed_board(cli, d, net, "usbbuck4", 4, GOLDEN / "usbbuck4")


def _copy_board(pcb: Path, dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for f in pcb.parent.glob(pcb.stem + ".*"):
        shutil.copy2(f, dst / f.name)
    for cname in ("constraints.json", "decoupling.json"):
        src = pcb.parent / cname
        if src.is_file():
            shutil.copy2(src, dst / cname)
    return dst / pcb.name


def _drc_sig(report: dict) -> set:
    return {(v["check"], v["layer"], v["net"], tuple(v["pos"] or ()))
            for v in report["violations"] if v["source"] != "unconnected"}


# ============================================================ smoke: 2 layer

@pytest.mark.smoke
def test_blinky2_defaults_pour_refill_and_rerun(cli, blinky2_seeded, tmp_path):
    import kc
    pcb = _copy_board(blinky2_seeded, tmp_path / "b")
    pro_before = pcb.with_suffix(".kicad_pro").read_bytes()
    before = _drc_sig(kc.run_drc(cli, pcb))

    rep = tmp_path / "planes.json"
    assert planes_gen.main(["--pcb", str(pcb),
                            "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "pass" and r["facts"]["plan_source"] == "defaults"
    zones = r["facts"]["zones"]
    assert [(z["net"], z["layer"], z["status"]) for z in zones] == \
        [("GND", "B.Cu", "added")]
    assert zones[0]["area_mm2"] > 1000.0          # actual ~2748 mm2
    assert r["facts"]["layer_types"] == {}        # outer pour stays signal

    # geom sees a filled GND zone on B.Cu; project file untouched
    bg = geom.BoardGeom.from_file(pcb)
    zs = bg.zones_of(net="GND", layer="B.Cu")
    assert len(zs) == 1 and zs[0].filled
    assert zs[0].fill_area("B.Cu") == pytest.approx(zones[0]["area_mm2"],
                                                    rel=0.01)
    assert pcb.with_suffix(".kicad_pro").read_bytes() == pro_before

    # DRC: no NEW violations (unconnected excepted - the board is unrouted)
    after = _drc_sig(kc.run_drc(cli, pcb))
    assert after - before == set()

    # re-run = clean no-op (existing pour detected, no duplicate zone)
    assert planes_gen.main(["--pcb", str(pcb),
                            "--out-report", str(rep)]) == 0
    r2 = json.loads(rep.read_text("utf-8"))
    assert r2["facts"]["zones_added"] == 0
    assert r2["facts"]["zones"][0]["status"] == "existing"
    assert len(geom.BoardGeom.from_file(pcb).zones_of()) == 1
    assert _drc_sig(kc.run_drc(cli, pcb)) - before == set()


# ============================================================ smoke: 4 layer

@pytest.mark.smoke
def test_usbbuck4_4layer_planes_and_layer_types(cli, usbbuck4_seeded, tmp_path):
    pcb = _copy_board(usbbuck4_seeded, tmp_path / "b")
    rep = tmp_path / "planes.json"
    assert planes_gen.main(["--pcb", str(pcb),
                            "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "pass"
    zones = {(z["net"], z["layer"]): z for z in r["facts"]["zones"]}
    # defaults: GND on In1 + dominant power net (+3V3, most pads) on In2
    assert set(zones) == {("GND", "In1.Cu"), ("+3V3", "In2.Cu")}
    assert all(z["area_mm2"] > 1000.0 for z in zones.values())
    assert r["facts"]["layer_types"] == {"In1.Cu": "power",
                                         "In2.Cu": "power"}

    # geom: both planes filled
    bg = geom.BoardGeom.from_file(pcb)
    for net, layer in zones:
        zs = bg.zones_of(net=net, layer=layer)
        assert zs and any(z.filled for z in zs), (net, layer)

    # the saved (layers) block records power vs signal per layer
    text = pcb.read_text(encoding="utf-8")
    assert re.search(r'"In1\.Cu"\s+power', text)
    assert re.search(r'"In2\.Cu"\s+power', text)
    assert re.search(r'"F\.Cu"\s+signal', text)
    assert re.search(r'"B\.Cu"\s+signal', text)


# ============================================================ smoke: thermal vias

_EP_FP = '''  (footprint "aiee:EPTEST"
    (layer "F.Cu")
    (at {x} {y})
    (attr smd)
    (property "Reference" "EP1"
      (at 0 -3 0)
      (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "EPTEST"
      (at 0 3 0)
      (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "9" smd rect (at 0 0) (size 4 4) (layers "F.Cu") (net "GND"))
  )
'''


def _inject_ep(pcb: Path) -> tuple[float, float]:
    """Append a synthetic 4x4 mm GND exposed-pad footprint at a free spot
    (the corpus goldens have no EP parts - SPEC P7.2's thermal-via path needs
    a synthetic fixture)."""
    from shapely.ops import unary_union
    bg = geom.BoardGeom.from_file(pcb)
    occ = unary_union([p.poly for p in bg.pads_of()]
                      + [t.poly for t in bg.tracks_of()])
    x1, y1, x2, y2 = bg.outline.bounds
    spot = None
    for iy in range(max(1, int((y2 - y1 - 16) / 2))):
        for ix in range(max(1, int((x2 - x1 - 16) / 2))):
            x, y = round(x1 + 8 + 2 * ix, 1), round(y1 + 8 + 2 * iy, 1)
            b = box(x - 5, y - 5, x + 5, y + 5)
            if bg.outline.contains(b) and not b.intersects(occ):
                spot = (x, y)
                break
        if spot:
            break
    assert spot, "no free 10x10 mm spot for the EP fixture"
    text = pcb.read_text(encoding="utf-8")
    i = text.rstrip().rfind(")")
    pcb.write_text(text[:i] + _EP_FP.format(x=spot[0], y=spot[1]) + text[i:],
                   encoding="utf-8")
    return spot


@pytest.mark.smoke
def test_thermal_vias_under_exposed_pad_live(cli, blinky2_seeded, tmp_path):
    import kc
    pcb = _copy_board(blinky2_seeded, tmp_path / "b")
    _inject_ep(pcb)
    before = _drc_sig(kc.run_drc(cli, pcb))

    rep = tmp_path / "planes.json"
    assert planes_gen.main(["--pcb", str(pcb),
                            "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["facts"]["thermal_vias"] == 9        # 3x3 grid in the 4x4 pad
    assert r["facts"]["thermal_pads"] == [
        {"ref": "EP1", "pad": "9", "net": "GND", "vias": 9}]

    # vias landed INSIDE the EP pad, on GND, 0.6/0.3
    bg = geom.BoardGeom.from_file(pcb)
    pad = next(p for p in bg.pads_of(ref="EP1") if p.net == "GND")
    inside = [v for v in bg.vias_of(net="GND")
              if pad.poly.buffer(0.01).covers(geom.Point(*v.at))]
    assert len(inside) == 9
    assert all(v.diameter == pytest.approx(0.6)
               and v.drill == pytest.approx(0.3) for v in inside)
    # pour filled and no NEW DRC violations from pour+vias
    assert json.loads(rep.read_text("utf-8"))["status"] == "pass"
    assert _drc_sig(kc.run_drc(cli, pcb)) - before == set()


@pytest.mark.smoke
def test_connect_solid_zone_live(cli, blinky2_seeded, tmp_path):
    """T6 (P7B-2): a planes-only sidecar with "connect": "solid" produces a
    zone whose s-expr carries (connect_pads yes ...) - the scripted form of
    the pd-trigger fan-in hand patch (LEARNINGS 791)."""
    pcb = _copy_board(blinky2_seeded, tmp_path / "b")
    con = tmp_path / "planes.json"
    con.write_text(json.dumps({"planes": [
        {"net": "GND", "layer": "B.Cu", "connect": "solid"}]}),
        encoding="utf-8")
    rep = tmp_path / "r.json"
    assert planes_gen.main(["--pcb", str(pcb), "--constraints", str(con),
                            "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["status"] == "pass"
    text = pcb.read_text(encoding="utf-8")
    assert re.search(r"\(connect_pads\s+yes", text), \
        "solid pad connection missing from the saved zone"


@pytest.mark.smoke
def test_no_thermal_vias_flag(cli, blinky2_seeded, tmp_path):
    pcb = _copy_board(blinky2_seeded, tmp_path / "b")
    _inject_ep(pcb)
    rep = tmp_path / "planes.json"
    assert planes_gen.main(["--pcb", str(pcb), "--no-thermal-vias",
                            "--out-report", str(rep)]) == 0
    r = json.loads(rep.read_text("utf-8"))
    assert r["facts"]["thermal_vias"] == 0
    assert geom.BoardGeom.from_file(pcb).vias_of(net="GND") == []
