"""T2 regression tests for check_current gate blind spots.

Derived from LEARNINGS.md:
 - 2026-07-28 [routing][check_current]: via-count rule is net-wide; overrides
   fed only track widths (a 1 A fuse tap on a 5 A net needed 10 vias).
 - 2026-07-29 [check_current][gates]: plane-fed rail = every via is a 1-via
   leaf cluster (27 unsatisfiable clusters on lumina-carrier +3V3).
 - 2026-07-29 [check_current][gates]: no bridge awareness - undersized_track
   could not say whether a parallel same-net path exists.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import _boards  # noqa: E402
import check_current  # noqa: E402
import geom  # noqa: E402


# ---- synthetic boards (helper copied from tests/test_checks.py) ------------

def _board(tmp_path_factory, name: str, body: str) -> geom.BoardGeom:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 20 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


def _kinds(vs, kind):
    return [v for v in vs if v["kind"] == kind]


# ---- fixture A: plane-fed rail (LEARNINGS 2026-07-29 shape) ----------------
# Full B.Cu plane on +3V3; three 1-via leaf taps >2 mm apart, each with a
# short 0.3 mm F.Cu escape. At 1.0 A / 0.5 A-per-via every cluster needs 2.

PLANE_FED_BODY = """  (zone (net "+3V3") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10))))
  (via (at 3 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+3V3"))
  (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+3V3"))
  (via (at 17 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+3V3"))
  (segment (start 3 5) (end 3.8 5) (width 0.3) (layer "F.Cu") (net "+3V3"))
  (segment (start 10 5) (end 10.8 5) (width 0.3) (layer "F.Cu") (net "+3V3"))
  (segment (start 17 5) (end 17.8 5) (width 0.3) (layer "F.Cu") (net "+3V3"))
"""

ENTRY_A = {"net": "+3V3", "current_a": 1.0, "via_amps": 0.5}


@pytest.fixture(scope="module")
def plane_fed_bg(tmp_path_factory):
    return _board(tmp_path_factory, "planefed", PLANE_FED_BODY)


def test_plane_fed_absent_keeps_old_errors(plane_fed_bg):
    """Control: without plane_fed the three 1-via taps are hard errors."""
    vs, facts = check_current.check_net(plane_fed_bg, dict(ENTRY_A))
    weak = _kinds(vs, "insufficient_transition_vias")
    assert len(weak) == 3
    assert all(v["severity"] == "error" for v in weak)
    assert all(v["required"] == 2 and v["vias"] == 1 for v in weak)
    assert {tuple(v["pos"]) for v in weak} == {(3.0, 5.0), (10.0, 5.0),
                                               (17.0, 5.0)}
    assert "advisory" not in weak[0]
    assert "plane_fed" not in facts


def test_plane_fed_downgrades_to_advisory(plane_fed_bg):
    vs, facts = check_current.check_net(
        plane_fed_bg, dict(ENTRY_A, plane_fed=True))
    weak = _kinds(vs, "insufficient_transition_vias")
    assert len(weak) == 3
    assert all(v["severity"] == "warning" for v in weak)
    assert all(v["advisory"] is True for v in weak)
    assert {tuple(v["pos"]) for v in weak} == {(3.0, 5.0), (10.0, 5.0),
                                               (17.0, 5.0)}
    # 0.3 mm escapes at the 1.0 A full-budget screen are advisory too
    thin = _kinds(vs, "undersized_track")
    assert thin and all(v["severity"] == "warning" and v["advisory"] is True
                        for v in thin)
    # violations reported, but nothing at error severity
    assert vs and not any(v["severity"] == "error" for v in vs)
    assert facts["plane_fed"] is True
    assert facts["advisory_violations"] == len(vs) == 6


def test_plane_fed_override_region_stays_error(plane_fed_bg):
    """The regulator-feed tap declared via an override stays enforceable."""
    entry = dict(ENTRY_A, plane_fed=True,
                 overrides=[{"near": [3, 5], "radius_mm": 1.0,
                             "current_a": 1.0}])
    vs, _ = check_current.check_net(plane_fed_bg, entry)
    weak = _kinds(vs, "insufficient_transition_vias")
    assert len(weak) == 3
    by_pos = {tuple(v["pos"]): v for v in weak}
    tap1 = by_pos[(3.0, 5.0)]
    assert tap1["severity"] == "error" and tap1["required"] == 2
    assert "advisory" not in tap1
    for pos in [(10.0, 5.0), (17.0, 5.0)]:
        assert by_pos[pos]["severity"] == "warning"
        assert by_pos[pos]["advisory"] is True


def test_plane_fed_clean_rail_passes(tmp_path_factory):
    """A plane-fed rail with adequate copper stays status pass."""
    body = """  (zone (net "+3V3") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10))))
  (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+3V3"))
  (segment (start 10 5) (end 12 5) (width 0.5) (layer "F.Cu") (net "+3V3"))
"""
    bg = _board(tmp_path_factory, "planeok", body)
    entry = {"net": "+3V3", "current_a": 0.4, "via_amps": 0.5,
             "plane_fed": True}
    vs, facts = check_current.check_net(bg, entry)
    assert vs == []
    assert facts["plane_fed"] is True
    assert facts["advisory_violations"] == 0
    cons = bg.path.parent / "c.json"
    cons.write_text(json.dumps({"power": [entry]}), encoding="utf-8")
    payload, out = check_current.run(
        ["--pcb", str(bg.path), "--constraints", str(cons)])
    assert payload["status"] == "pass" and out is None


# ---- fixture B: declared plane-fed without a plane -------------------------

# ---- #828: plane-fed stubs carry their own loads, not the rail's budget
# B.Cu plane fed by U1 through one via, with loads U3 and U4 on it; U2 hangs
# off the plane on a single via and a 0.15 mm F.Cu stub. At 1.0 A / 0.5 A per via both screen out.

STUB_BODY = PLANE_FED_BODY.split("  (via")[0] + """  (via (at 2 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+3V3"))
  (footprint "t:U" (at 2 5)
    (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "+3V3")))
  (via (at 12 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+3V3"))
  (segment (start 12 5) (end 16 5) (width 0.15) (layer "F.Cu") (net "+3V3"))
  (footprint "t:U" (at 16.5 5)
    (layer "F.Cu")
    (property "Reference" "U2" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "+3V3")))
""" + "".join(f"""  (footprint "t:U" (at 7 {y})
    (layer "B.Cu")
    (property "Reference" "U{i}" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (net "+3V3")))
""" for i, y in ((3, 2), (4, 8)))
STUB_ENTRY = dict(ENTRY_A, plane_fed=True, source="U1")


@pytest.fixture(scope="module")
def stub_bg(tmp_path_factory):
    return _board(tmp_path_factory, "planestub", STUB_BODY)


def test_plane_fed_stub_of_unknown_load_is_unjudged(stub_bg):
    """U2's draw is unknown: its stub is not charged the rail's 1.0 A. U1's
    via holds the named source, whose output the entry gives (1.0 A): one
    via is short for it, so that stays an error (#845)."""
    vs, facts = check_current.check_net(stub_bg, STUB_ENTRY)
    assert [(v["kind"], v["pos"], v["severity"]) for v in vs] == [
        ("insufficient_transition_vias", [2.0, 5.0], "error")]
    assert vs[0]["source_stub"] is True and "source stub" in vs[0]["msg"]
    assert {(u["kind"], tuple(u["refs"])) for u in facts["unjudged"]} == {
        ("undersized_track", ("U2",)),
        ("insufficient_transition_vias", ("U2",))}


def test_source_stub_with_enough_vias_passes(stub_bg):
    """The same source stub at 0.5 A needs one via and has it."""
    vs, _ = check_current.check_net(stub_bg, dict(STUB_ENTRY, current_a=0.5))
    assert [v for v in vs if v.get("source_stub")] == []


def test_unnamed_source_stub_stays_advisory(stub_bg):
    """No named source: the output current is not known, so the full-budget
    screen stays a warning."""
    entry = {k: v for k, v in STUB_ENTRY.items() if k != "source"}
    vs, _ = check_current.check_net(stub_bg, entry)
    assert [v for v in vs if v.get("source_stub")] == []
    assert all(v["severity"] == "warning" for v in vs)


def test_plane_fed_stub_judged_at_its_load(stub_bg):
    vs, facts = check_current.check_net(
        stub_bg, dict(STUB_ENTRY, loads={"U2": 0.1}))
    assert [v["pos"] for v in vs] == [[2.0, 5.0]]
    assert {b["kind"] for b in facts["leaf_branches"]} == {
        "undersized_track", "insufficient_transition_vias"}


def test_plane_fed_stub_undersized_for_its_load_is_error(stub_bg):
    """Keep it loud: 0.9 A through one via and a 0.15 mm track is an error,
    not an advisory warning."""
    vs, _ = check_current.check_net(
        stub_bg, dict(STUB_ENTRY, loads={"U2": 0.9}))
    bad = [v for v in vs if v["severity"] == "error"
           and not v.get("source_stub")]
    assert {v["kind"] for v in bad} == {"undersized_track",
                                        "insufficient_transition_vias"}
    assert all(v["leaf_loads"] == ["U2"] and v["current_a"] == 0.9
               and "advisory" not in v for v in bad)
    via = _kinds(bad, "insufficient_transition_vias")[0]
    assert via["required"] == 2 and "plane-fed stub" in via["msg"]


def test_plane_missing_is_error(tmp_path_factory):
    body = ('  (segment (start 1 5) (end 5 5) (width 1) (layer "F.Cu") '
            '(net "+3V3"))\n')
    bg = _board(tmp_path_factory, "noplane", body)
    vs, facts = check_current.check_net(
        bg, {"net": "+3V3", "current_a": 0.5, "plane_fed": True})
    assert [v["kind"] for v in vs] == ["plane_missing"]
    v = vs[0]
    assert v["severity"] == "error" and v["net"] == "+3V3"
    assert v["pos"] is not None and len(v["pos"]) == 2  # net-copper point
    assert facts["plane_fed"] is True
    assert facts["advisory_violations"] == 0


# ---- fixture C: overrides reach via clusters (LEARNINGS 2026-07-28) --------

def test_override_reaches_via_cluster(tmp_path_factory):
    """A 0.4 A branch tap on a 5 A net no longer needs 10 vias."""
    body = (
        '  (via (at 4 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net "VBUS"))\n'
        '  (via (at 16 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net "VBUS"))\n')
    bg = _board(tmp_path_factory, "viaover", body)
    entry = {"net": "VBUS", "current_a": 5.0, "via_amps": 0.5,
             "overrides": [{"near": [4, 5], "radius_mm": 1.0,
                            "current_a": 0.4}]}
    vs, _ = check_current.check_net(bg, entry)
    weak = _kinds(vs, "insufficient_transition_vias")
    assert len(weak) == 1          # tap cluster passes at 0.4 A (need 1 via)
    assert weak[0]["pos"] == [16.0, 5.0]
    assert weak[0]["severity"] == "error" and weak[0]["required"] == 10
    # control: without the override both clusters demand 10 vias (old rule)
    vs2, _ = check_current.check_net(
        bg, {"net": "VBUS", "current_a": 5.0, "via_amps": 0.5})
    assert len(_kinds(vs2, "insufficient_transition_vias")) == 2


# dumbbell pour (copied from tests/test_checks.py): two 5x5 lobes joined by
# a 0.2 mm strip; the neck fails a 0.5 A budget (req 0.25 mm)
DUMBBELL = """  (zone (net "PWR") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 15 0) (xy 15 5) (xy 0 5)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 5 0) (xy 5 2.4) (xy 10 2.4) (xy 10 0) (xy 15 0)
           (xy 15 5) (xy 10 5) (xy 10 2.6) (xy 5 2.6) (xy 5 5) (xy 0 5))))
  (via (at 2 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
  (via (at 13 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
"""


def test_override_drops_pour_neck(tmp_path_factory):
    """A failing neck whose reported pos sits in an override region is
    re-tested at the override requirement and dropped when it passes."""
    bg = _board(tmp_path_factory, "neckover", DUMBBELL)
    base = {"net": "PWR", "current_a": 0.5}
    vs, _ = check_current.check_net(bg, dict(base))
    assert len(_kinds(vs, "pour_neckdown")) == 1     # control
    # region covers the whole pour (neck pos is a split-component sample)
    entry = dict(base, overrides=[{"near": [7.5, 2.5], "radius_mm": 10.0,
                                   "current_a": 0.05}])
    vs2, _ = check_current.check_net(bg, entry)
    assert _kinds(vs2, "pour_neckdown") == []


# ---- fixture D: bridge labeling (LEARNINGS 2026-07-29) ---------------------

_PADS_PWR = """  (footprint "t:U" (at 2 5)
    (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "PWR")))
  (footprint "t:U" (at 14 5)
    (layer "F.Cu")
    (property "Reference" "U2" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "PWR")))
"""

_DIRECT = '  (segment (start 2 5) (end 14 5) (width 0.2) (layer "F.Cu") (net "PWR"))\n'
_DETOUR = (
    '  (segment (start 2 5) (end 2 8) (width 0.2) (layer "F.Cu") (net "PWR"))\n'
    '  (segment (start 2 8) (end 14 8) (width 0.2) (layer "F.Cu") (net "PWR"))\n'
    '  (segment (start 14 8) (end 14 5) (width 0.2) (layer "F.Cu") (net "PWR"))\n')


def test_parallel_paths_labeled_not_bridge(tmp_path_factory):
    bg = _board(tmp_path_factory, "loop", _PADS_PWR + _DIRECT + _DETOUR)
    vs, facts = check_current.check_net(bg, {"net": "PWR", "current_a": 2.0})
    thin = _kinds(vs, "undersized_track")
    assert len(thin) == 4
    assert all(v["bridge"] is False for v in thin)
    assert all(v["severity"] == "error" for v in thin)  # label, not a waiver
    assert facts["bridge_labeled"] is True


def test_sole_path_labeled_bridge(tmp_path_factory):
    bg = _board(tmp_path_factory, "solepath", _PADS_PWR + _DIRECT)
    vs, facts = check_current.check_net(bg, {"net": "PWR", "current_a": 2.0})
    thin = _kinds(vs, "undersized_track")
    assert len(thin) == 1
    assert thin[0]["bridge"] is True
    assert facts["bridge_labeled"] is True


def test_zone_parallel_path_not_bridge(tmp_path_factory):
    """A segment paralleled by the net's own pour (via through-vias at both
    ends) is not a bridge when the graph includes zone fills, and since both
    ends sit on the pour it is dropped as a pour tap, not reported."""
    body = _DIRECT + (
        '  (via (at 2 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net "PWR"))\n'
        '  (via (at 14 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net "PWR"))\n'
        """  (zone (net "PWR") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10))))
""")
    bg = _board(tmp_path_factory, "zonepar", body)
    vs, facts = check_current.check_net(bg, {"net": "PWR", "current_a": 2.0})
    assert _kinds(vs, "undersized_track") == []
    assert facts["pour_taps"] == [[8.0, 5.0]]
    assert facts["bridge_labeled"] is True


# ---- pour taps (PCB-0018-A PHASE/LS_SRC class) -----------------------------
# A 10 A pour on F.Cu holds the FET pad Q1.1; a 0.25 mm signal tap leaves it.

_TAP_POUR = """  (zone (net "PH") (layer "F.Cu")
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))
  (footprint "t:Q" (at 5 5)
    (layer "F.Cu")
    (property "Reference" "Q1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") (net "PH")))
  (footprint "t:R" (at 15 5)
    (layer "F.Cu")
    (property "Reference" "R1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net "PH")))
"""

ENTRY_PH = {"net": "PH", "current_a": 10.0}


def test_tap_inside_own_pour_dropped(tmp_path_factory):
    """A thin track whose both ends sit in the pour that carries the current
    (pad to pad across it) is shunted by it: no finding."""
    body = _TAP_POUR + (
        '  (segment (start 5 5) (end 5 8) (width 0.25) (layer "F.Cu") '
        '(net "PH"))\n')
    bg = _board(tmp_path_factory, "pourtap", body)
    vs, facts = check_current.check_net(bg, ENTRY_PH)
    assert _kinds(vs, "undersized_track") == []
    assert len(facts["pour_taps"]) == 1


def test_stub_inside_pad_dropped(tmp_path_factory):
    """A non-bridge stub drawn wholly over its pad adds no section."""
    body = _TAP_POUR + (
        '  (segment (start 15 5) (end 18 5) (width 0.25) (layer "F.Cu") '
        '(net "PH"))\n'
        '  (segment (start 15 5) (end 18 5) (width 0.25) (layer "B.Cu") '
        '(net "PH"))\n'
        '  (via (at 18 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net "PH"))\n'
        '  (segment (start 15 5) (end 15.2 5) (width 0.25) (layer "F.Cu") '
        '(net "PH"))\n')
    bg = _board(tmp_path_factory, "padstub", body)
    vs, facts = check_current.check_net(bg, ENTRY_PH)
    thin = _kinds(vs, "undersized_track")
    # the two 3 mm legs stay: they leave the pad copper and no pour shunts them
    assert sorted(v["layer"] for v in thin) == ["B.Cu", "F.Cu"]
    assert all(v["pos"] == [16.5, 5.0] for v in thin)
    assert facts["pour_taps"] == [[15.1, 5.0]]


def test_bridge_wholly_inside_own_pour_dropped(tmp_path_factory):
    """A track that is a graph bridge (the graph has no zone copper) but lies
    wholly in its own net's fill carries nothing of its own: dropped (#844).
    Cut the pour away from half of it and the half outside is judged again."""
    seg = ('  (segment (start 5 5) (end 5 8) (width 0.25) (layer "F.Cu") '
           '(net "PH"))\n'
           '  (via (at 5 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
           '(net "PH"))\n'
           '  (segment (start 5 8) (end 12 8) (width 0.25) (layer "B.Cu") '
           '(net "PH"))\n')
    bg = _board(tmp_path_factory, "bridgeinpour", _TAP_POUR + seg)
    vs, facts = check_current.check_net(bg, ENTRY_PH)
    assert [v for v in _kinds(vs, "undersized_track")
            if v["layer"] == "F.Cu"] == []
    cut = _TAP_POUR.replace("(xy 10 10) (xy 0 10)", "(xy 10 6) (xy 0 6)")
    bg = _board(tmp_path_factory, "bridgecut", cut + seg)
    vs, _f = check_current.check_net(bg, ENTRY_PH)
    assert [v for v in _kinds(vs, "undersized_track") if v["layer"] == "F.Cu"]


def test_sole_path_tap_out_of_pour_still_fails(tmp_path_factory):
    """The same tap leaving the pour to reach R1 is the only path there: a
    genuine bridge keeps its error, whatever it feeds."""
    body = _TAP_POUR + (
        '  (segment (start 5 5) (end 15 5) (width 0.25) (layer "F.Cu") '
        '(net "PH"))\n')
    bg = _board(tmp_path_factory, "bridgetap", body)
    vs, facts = check_current.check_net(bg, ENTRY_PH)
    [v] = _kinds(vs, "undersized_track")
    assert v["bridge"] is True and v["severity"] == "error"
    assert "pour_taps" not in facts


def test_no_undersized_no_bridge_fact(tmp_path_factory):
    """bridge_labeled only appears when labeling actually ran."""
    body = _PADS_PWR + \
        '  (segment (start 2 5) (end 14 5) (width 1.2) (layer "F.Cu") (net "PWR"))\n'
    bg = _board(tmp_path_factory, "wideok", body)
    vs, facts = check_current.check_net(bg, {"net": "PWR", "current_a": 2.0})
    assert _kinds(vs, "undersized_track") == []
    assert "bridge_labeled" not in facts


# ---- T6 (P8B-3): derived return-net coverage -------------------------------
# The pd-trigger 5A GND choke class: no board declares its return net, so
# return-path ampacity was unchecked. A >=3A rail synthesizes ONE plane-fed
# entry for the return net; ALL derived findings are advisory warnings.

_GND_RETURN_BODY = """  (segment (start 1 1) (end 19 1) (width 2.0) (layer "F.Cu") (net "VBUS"))
  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 15 0) (xy 15 5) (xy 0 5)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 5 0) (xy 5 2.4) (xy 10 2.4) (xy 10 0) (xy 15 0)
           (xy 15 5) (xy 10 5) (xy 10 2.6) (xy 5 2.6) (xy 5 5) (xy 0 5))))
  (via (at 2 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
  (via (at 13 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
  (segment (start 2 2) (end 2.8 2) (width 0.3) (layer "F.Cu") (net "GND"))
"""


def _run_cli(bg, power):
    cons = bg.path.parent / "cons.json"
    cons.write_text(json.dumps({"power": power}), encoding="utf-8")
    return check_current.run(["--pcb", str(bg.path),
                              "--constraints", str(cons)])[0]


def test_derived_return_fires_at_5a(tmp_path_factory):
    bg = _board(tmp_path_factory, "gndret", _GND_RETURN_BODY)
    payload = _run_cli(bg, [{"net": "VBUS", "current_a": 5.0}])
    derived = [e for e in payload["checked"] if e.get("derived")]
    assert len(derived) == 1 and derived[0]["net"] == "GND"
    gnd_vs = [v for v in payload["violations"] if v["net"] == "GND"]
    assert gnd_vs, "derived GND coverage produced no findings"
    # the choke class is visible: pour neck between the via attachments
    necks = [v for v in gnd_vs if v["kind"] == "pour_neckdown"]
    assert len(necks) == 1
    # ...and EVERY derived finding is an advisory warning, never an error
    assert all(v["severity"] == "warning" for v in gnd_vs)
    assert all(v["derived"] is True for v in gnd_vs)
    assert necks[0]["advisory"] is True
    assert "derived return-net coverage" in necks[0]["msg"]


def test_derived_return_below_threshold_skipped(tmp_path_factory):
    bg = _board(tmp_path_factory, "gndlow", _GND_RETURN_BODY)
    payload = _run_cli(bg, [{"net": "VBUS", "current_a": 2.0}])
    assert not any(e.get("derived") for e in payload["checked"])
    assert not any(v["net"] == "GND" for v in payload["violations"])


def test_derived_return_skipped_when_declared(tmp_path_factory):
    """An explicitly declared return net is the owner's judgment - no
    synthesis on top of it."""
    bg = _board(tmp_path_factory, "gnddecl", _GND_RETURN_BODY)
    payload = _run_cli(bg, [{"net": "VBUS", "current_a": 5.0},
                            {"net": "GND", "current_a": 5.0,
                             "plane_fed": True}])
    assert not any(e.get("derived") for e in payload["checked"])
    # the declared entry still runs (pour neck at ERROR: declared plane_fed)
    necks = [v for v in payload["violations"] if v["kind"] == "pour_neckdown"]
    assert necks and all(v["severity"] == "error" for v in necks)


def test_derived_return_needs_a_zone(tmp_path_factory):
    """Routed-only return (no zone fill) is not judgeable at plane-fed
    semantics - no synthesis, no noise."""
    body = ('  (segment (start 1 1) (end 19 1) (width 2.0) (layer "F.Cu") '
            '(net "VBUS"))\n'
            '  (segment (start 1 5) (end 19 5) (width 0.3) (layer "F.Cu") '
            '(net "GND"))\n')
    bg = _board(tmp_path_factory, "gndtrk", body)
    payload = _run_cli(bg, [{"net": "VBUS", "current_a": 5.0}])
    assert not any(e.get("derived") for e in payload["checked"])
    assert not any(v["net"] == "GND" for v in payload["violations"])


def test_derived_return_net_field_overrides_default(tmp_path_factory):
    body = _GND_RETURN_BODY.replace('"GND"', '"AGND"')
    bg = _board(tmp_path_factory, "agnd", body)
    payload = _run_cli(bg, [{"net": "VBUS", "current_a": 5.0,
                             "return_net": "AGND"}])
    derived = [e for e in payload["checked"] if e.get("derived")]
    assert len(derived) == 1 and derived[0]["net"] == "AGND"


# ---- T6 (P8A-4): plane_fed_candidate hint (facts only) ---------------------

def test_plane_fed_candidate_hint(plane_fed_bg):
    """The plane-fed shape without the key gets the facts hint; severities
    are untouched (still the old errors - the hint is machine-visible only)."""
    vs, facts = check_current.check_net(plane_fed_bg, dict(ENTRY_A))
    assert facts["plane_fed_candidate"] is True
    assert any(v["severity"] == "error" for v in vs)  # unchanged behavior


def test_no_hint_when_plane_fed_declared(plane_fed_bg):
    _, facts = check_current.check_net(
        plane_fed_bg, dict(ENTRY_A, plane_fed=True))
    assert "plane_fed_candidate" not in facts


def test_no_hint_without_zone(tmp_path_factory):
    body = _PADS_PWR + _DIRECT + (
        '  (via (at 2 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net "PWR"))\n')
    bg = _board(tmp_path_factory, "nohint", body)
    _, facts = check_current.check_net(bg, {"net": "PWR", "current_a": 2.0})
    assert "plane_fed_candidate" not in facts


# ---- exit codes / run() CLI contract ---------------------------------------

def test_cli_exit_codes(tmp_path_factory, plane_fed_bg):
    script = SCRIPTS / "check_current.py"
    # violations (advisory-only still exits 1: status is "violations")
    cons = plane_fed_bg.path.parent / "pf.json"
    cons.write_text(json.dumps(
        {"power": [dict(ENTRY_A, plane_fed=True)]}), encoding="utf-8")
    proc = subprocess.run(
        [PYTHON, str(script), "--pcb", str(plane_fed_bg.path),
         "--constraints", str(cons)], capture_output=True, text=True)
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["status"] == "violations"
    assert payload["counts"]["by_severity"] == {"warning": 6}
    assert payload["checked"][0]["plane_fed"] is True
    # error: net not on board -> exit 2
    cons2 = plane_fed_bg.path.parent / "ghost.json"
    cons2.write_text(json.dumps(
        {"power": [{"net": "/GHOST", "current_a": 1.0}]}), encoding="utf-8")
    proc2 = subprocess.run(
        [PYTHON, str(script), "--pcb", str(plane_fed_bg.path),
         "--constraints", str(cons2)], capture_output=True, text=True)
    assert proc2.returncode == 2
    assert json.loads(proc2.stdout)["status"] == "error"


# ---- pad-exit necks (PCB-0019-A SW_L1/SW_L2 class) -------------------------
# A 0.5 mm-pitch pin row: 0.85 x 0.24 mm pads at y 4.5 / 5.0 / 5.5, the middle
# one on SW. At 1.0 A the requirement is 0.5 mm, which cannot fit between the
# neighbours; the track leaves the pad narrower and widens after a via.

def _pin_row(mid_size="0.85 0.24"):
    return f"""  (footprint "t:QFN" (at 5 5)
    (layer "F.Cu")
    (property "Reference" "U3" (at 0 0 0))
    (pad "1" smd rect (at 0 -0.5) (size 0.85 0.24) (layers "F.Cu") (net "A"))
    (pad "2" smd rect (at 0 0) (size {mid_size}) (layers "F.Cu") (net "SW"))
    (pad "3" smd rect (at 0 0.5) (size 0.85 0.24) (layers "F.Cu") (net "B")))
"""


def _exit(neck_end_x: float, neck_w: float = 0.3) -> str:
    """Neck from the pad centre to x=neck_end_x, a via there, then a full
    0.5 mm track on B.Cu to x=1."""
    return (
        f'  (segment (start 5 5) (end {neck_end_x} 5) (width {neck_w}) '
        '(layer "F.Cu") (net "SW"))\n'
        f'  (via (at {neck_end_x} 5) (size 0.6) (drill 0.3) '
        '(layers "F.Cu" "B.Cu") (net "SW"))\n'
        f'  (segment (start {neck_end_x} 5) (end 1 5) (width 0.5) '
        '(layer "B.Cu") (net "SW"))\n')


ENTRY_SW = {"net": "SW", "current_a": 1.0}


def test_pad_exit_neck_accepted(tmp_path_factory):
    """0.30 mm off a 0.24 mm pad, ~0.3 mm clear of the pad before the via:
    the neck the pin pitch forces is not a finding (PCB-0019-A SW_L1)."""
    bg = _board(tmp_path_factory, "padexit", _pin_row() + _exit(4.0))
    vs, facts = check_current.check_net(bg, ENTRY_SW)
    assert _kinds(vs, "undersized_track") == []
    [info] = facts["pad_exit_necks"]
    assert info["pad"] == "U3.2" and info["pad_width_mm"] == 0.24
    assert info["length_mm"] <= check_current.PAD_EXIT_MAX_MM


def test_long_pad_exit_neck_still_fails(tmp_path_factory):
    """The same neck run 2.5 mm past the pad is a routed trace, not an
    escape: it must meet the full width."""
    bg = _board(tmp_path_factory, "padexitlong", _pin_row() + _exit(2.0))
    vs, facts = check_current.check_net(bg, ENTRY_SW)
    assert len(_kinds(vs, "undersized_track")) == 1
    assert "pad_exit_necks" not in facts


def test_neck_narrower_than_pad_still_fails(tmp_path_factory):
    """0.20 mm off a 0.24 mm pad adds a tighter section than the pad."""
    bg = _board(tmp_path_factory, "padexitthin",
                _pin_row() + _exit(4.0, neck_w=0.2))
    vs, _ = check_current.check_net(bg, ENTRY_SW)
    assert len(_kinds(vs, "undersized_track")) == 1


def test_neck_off_wide_pad_still_fails(tmp_path_factory):
    """A pad as wide as the requirement forces nothing: a short 0.3 mm stub
    off a 1 x 1 mm pad is still undersized."""
    bg = _board(tmp_path_factory, "padexitwide",
                _pin_row(mid_size="1 1") + _exit(4.0))
    vs, _ = check_current.check_net(bg, ENTRY_SW)
    assert len(_kinds(vs, "undersized_track")) == 1


# ---- via-stitched pours (PCB-0017-B GND class) -----------------------------
# F.Cu GND split into two islands with a via each; whether that is a neck
# depends on the B.Cu GND fill the vias land in. 2 A needs 1.1 mm.

_SPLIT_FCU = """  (zone (net "GND") (layer "F.Cu")
    (polygon (pts (xy 0 0) (xy 15 0) (xy 15 5) (xy 0 5)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 0 0) (xy 6 0) (xy 6 5) (xy 0 5)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 9 0) (xy 15 0) (xy 15 5) (xy 9 5))))
  (via (at 3 2.5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
  (via (at 12 2.5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
"""

_BCU_SOLID = """  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 15 0) (xy 15 5) (xy 0 5)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 15 0) (xy 15 5) (xy 0 5))))
"""

# B.Cu dumbbell: the stitched path itself chokes through a 0.2 mm strip
_BCU_NECKED = """  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 15 0) (xy 15 5) (xy 0 5)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 5 0) (xy 5 2.4) (xy 10 2.4) (xy 10 0) (xy 15 0)
           (xy 15 5) (xy 10 5) (xy 10 2.6) (xy 5 2.6) (xy 5 5) (xy 0 5))))
"""

ENTRY_GND = {"net": "GND", "current_a": 2.0}


def test_split_fcu_stitched_to_solid_bcu_passes(tmp_path_factory):
    """Each F.Cu island reaches the one B.Cu pour through its via: no neck
    (used to report ~0.00 mm on the F.Cu fill)."""
    bg = _board(tmp_path_factory, "stitched", _SPLIT_FCU + _BCU_SOLID)
    vs, _ = check_current.check_net(bg, ENTRY_GND)
    assert _kinds(vs, "pour_neckdown") == []


def test_split_fcu_without_other_layer_fails(tmp_path_factory):
    """No other fill to stitch through: the split is a real break."""
    bg = _board(tmp_path_factory, "splitonly", _SPLIT_FCU)
    vs, _ = check_current.check_net(bg, ENTRY_GND)
    [neck] = _kinds(vs, "pour_neckdown")
    assert neck["layer"] == "F.Cu"


def test_split_fcu_stitched_to_necked_bcu_fails(tmp_path_factory):
    """Stitching only helps when the other layer carries the width: a B.Cu
    path that necks to 0.2 mm leaves both fills flagged."""
    bg = _board(tmp_path_factory, "stitchneck", _SPLIT_FCU + _BCU_NECKED)
    vs, _ = check_current.check_net(bg, ENTRY_GND)
    necks = _kinds(vs, "pour_neckdown")
    assert {v["layer"] for v in necks} == {"F.Cu", "B.Cu"}
    assert all(v["severity"] == "error" for v in necks)


# ---- stitch vias are not layer transitions (PCB-0023-A /SW, 2026-10-06) ----
# /SW was one pour on all four layers of a GaN inverter, stitched by ~120
# vias >2 mm apart. Every stitch via was its own 1-via "layer transition"
# needing 24 vias at 12 A: 112 errors, all model artefacts. A via that only
# joins same-net pours is now judged with every other via joining the same
# two pours, and not at all when one pour is a dead end; a via a track or a
# loose pad feeds still counts on its own.

def _board4(tmp_path_factory, name: str, body: str) -> geom.BoardGeom:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)
    (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 20 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


def _pour(net: str, layer: str, x1: float = 12) -> str:
    return (f'  (zone (net "{net}") (layer "{layer}")\n'
            f'    (polygon (pts (xy 0 0) (xy {x1} 0) (xy {x1} 10) (xy 0 10)))\n'
            f'    (filled_polygon (layer "{layer}")\n'
            f'      (pts (xy 0 0) (xy {x1} 0) (xy {x1} 10) (xy 0 10))))\n')


def _via(x: float, y: float, net: str = "/SW") -> str:
    return (f'  (via (at {x} {y}) (size 0.6) (drill 0.3) '
            f'(layers "F.Cu" "B.Cu") (net "{net}"))\n')


def _smd(ref: str, x: float, y: float, layer: str, net: str = "/SW") -> str:
    return (f'  (footprint "t:Q" (at {x} {y}) (layer "{layer}")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}") '
            f'(net "{net}")))\n')


_ALL4 = "".join(_pour("/SW", l) for l in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
# eight 1-via stitch clusters, rows 6 mm apart, 2.5 mm pitch
_ROW = [(x, y) for y in (2, 8) for x in (1, 3.5, 6, 8.5)]
_STITCH8 = "".join(_via(x, y) for x, y in _ROW)
_STITCH4 = "".join(_via(x, y) for x, y in _ROW[:4])
# a /SW track that really hops F.Cu -> B.Cu through one via, off the pours
_HOP = ('  (segment (start 14 5) (end 16 5) (width 0.5) (layer "F.Cu") '
        '(net "/SW"))\n' + _via(16, 5) +
        '  (segment (start 16 5) (end 19 5) (width 0.5) (layer "B.Cu") '
        '(net "/SW"))\n')
SW_12A = {"net": "/SW", "current_a": 12.0, "via_amps": 0.5}


def test_stitch_vias_on_four_layers_are_not_transitions(tmp_path_factory):
    """Both terminals on the F.Cu pour; In1/In2/B.Cu pours only parallel
    it through the stitch array: zero transition findings at 12 A."""
    bg = _board4(tmp_path_factory, "stitch4",
                 _ALL4 + _STITCH8 + _smd("Q1", 11, 5, "F.Cu")
                 + _smd("L1", 5, 5, "F.Cu"))
    vs, facts = check_current.check_net(bg, dict(SW_12A))
    assert _kinds(vs, "insufficient_transition_vias") == []
    assert facts["stitch_vias"] == 8 and facts["via_clusters"] == 8


def test_track_layer_hop_still_flagged_beside_stitch_array(tmp_path_factory):
    """Same /SW net: the stitch array stays silent, the via a track uses to
    change layers is still a 1-via transition needing 24 vias."""
    bg = _board4(tmp_path_factory, "stitchhop",
                 _ALL4 + _STITCH8 + _smd("Q1", 11, 5, "F.Cu")
                 + _smd("L1", 5, 5, "F.Cu") + _HOP)
    vs, _ = check_current.check_net(bg, dict(SW_12A))
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["pos"] == [16.0, 5.0] and hop["severity"] == "error"
    assert hop["vias"] == 1 and hop["required"] == 24
    assert "stitch" not in hop


def test_pour_to_pour_hop_counts_the_whole_stitch_array(tmp_path_factory):
    """Terminals on F.Cu and B.Cu: the load current crosses the array, so
    every via joining those two pours is counted together (threshold
    unchanged: 1 via per 0.5 A)."""
    body = (_ALL4 + _STITCH8 + _smd("Q1", 11, 5, "F.Cu")
            + _smd("L1", 5, 5, "B.Cu"))
    bg = _board4(tmp_path_factory, "stitchfb", body)
    vs, _ = check_current.check_net(
        bg, {"net": "/SW", "current_a": 3.0, "via_amps": 0.5})
    assert _kinds(vs, "insufficient_transition_vias") == []   # 8 >= 6
    vs, _ = check_current.check_net(bg, dict(SW_12A))
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["stitch"] is True and hop["severity"] == "error"
    assert hop["vias"] == 8 and hop["required"] == 24


def test_plated_holes_count_toward_a_pour_to_pour_hop(tmp_path_factory):
    """Four stitch vias at 3 A (need 6) fail; two plated through-holes
    joining the same two pours make up the count."""
    base = (_ALL4 + _STITCH4 + _smd("Q1", 11, 5, "F.Cu")
            + _smd("L1", 5, 5, "B.Cu"))
    entry = {"net": "/SW", "current_a": 3.0, "via_amps": 0.5}
    vs, _ = check_current.check_net(_board4(tmp_path_factory, "pth0", base),
                                    entry)
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["stitch"] is True and hop["vias"] == 4
    pth = ''.join(
        f'  (footprint "t:J" (at {x} 8) (layer "F.Cu")\n'
        f'    (property "Reference" "J{i}" (at 0 0 0))\n'
        f'    (pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6)'
        f' (layers "*.Cu") (net "/SW")))\n' for i, x in ((1, 2), (2, 7)))
    vs, _ = check_current.check_net(
        _board4(tmp_path_factory, "pth2", base + pth), entry)
    assert _kinds(vs, "insufficient_transition_vias") == []


def test_via_in_a_loose_pad_is_a_transition(tmp_path_factory):
    """A pad with no pour around it on its own layer feeds its via: that via
    is a layer transition, not a stitch. The same pad sitting in an F.Cu
    pour turns the via into a stitch to dead-end pours."""
    body = (_pour("/SW", "In1.Cu") + _pour("/SW", "B.Cu")
            + _smd("Q1", 5, 5, "F.Cu") + _via(5, 5))
    entry = {"net": "/SW", "current_a": 1.0, "via_amps": 0.5}
    vs, facts = check_current.check_net(
        _board4(tmp_path_factory, "loosepad", body), entry)
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["pos"] == [5.0, 5.0] and "stitch" not in hop
    assert "stitch_vias" not in facts
    vs, facts = check_current.check_net(
        _board4(tmp_path_factory, "pourpad", body + _pour("/SW", "F.Cu")),
        entry)
    assert _kinds(vs, "insufficient_transition_vias") == []
    assert facts["stitch_vias"] == 1


# F.Cu /SW pour with a thermal relief round Q1's pad (hole 4..6, one spoke
# on +x that stops 0.1 mm short of the via): the pad reaches the pour, the
# via inside it touches no F.Cu fill.
_RELIEF_FCU = """  (zone (net "/SW") (layer "F.Cu")
    (polygon (pts (xy 0 0) (xy 12 0) (xy 12 10) (xy 0 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 0 0) (xy 12 0) (xy 12 10) (xy 0 10) (xy 0 5.1) (xy 4 5.1)
           (xy 4 6) (xy 6 6) (xy 6 4) (xy 4 4) (xy 4 4.9) (xy 0 4.9)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 5.4 4.85) (xy 6.05 4.85) (xy 6.05 5.15) (xy 5.4 5.15))))
"""


def test_via_in_a_thermal_relief_pad_is_a_transition(tmp_path_factory):
    """Review of the stitch rule: Q1's pad sits in the F.Cu pour through a
    spoke, but its via touches no F.Cu fill, so all 12 A from Q1 to L1 on
    B.Cu cross that one via. It is a transition, not a stitch, and fails."""
    body = (_RELIEF_FCU + "".join(_pour("/SW", l)
                                  for l in ("In1.Cu", "In2.Cu", "B.Cu"))
            + _smd("Q1", 5, 5, "F.Cu") + _via(5, 5)
            + _smd("L1", 10, 5, "B.Cu"))
    vs, facts = check_current.check_net(
        _board4(tmp_path_factory, "reliefvia", body), dict(SW_12A))
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["pos"] == [5.0, 5.0] and "stitch" not in hop
    assert hop["severity"] == "error" and hop["required"] == 24
    assert "stitch_vias" not in facts


def test_via_straddling_a_relief_pad_edge_is_a_transition(tmp_path_factory):
    """Review of the relief rule (PCB-0023-A D201): Q1's only via sits on
    its pad's edge, so part of its ring reaches the F.Cu pour past the
    relief gap. The pad itself is ~3% covered by fill (one spoke), not in
    the pour, so its 12 A to L1 on B.Cu still cross that one via. Before,
    the via counted as a stitch and the 24-via array beside it made the
    F.Cu-B.Cu hop pass at 12 A."""
    array = "".join(_via(x, y) for y in (1, 2.5, 7.5, 9)
                    for x in (1, 2.5, 4, 5.5, 7, 8.5))
    body = (_RELIEF_FCU + "".join(_pour("/SW", l)
                                  for l in ("In1.Cu", "In2.Cu", "B.Cu"))
            + _smd("Q1", 5, 5, "F.Cu") + _via(5.75, 5) + array
            + _smd("L1", 10, 5, "B.Cu"))
    bg = _board4(tmp_path_factory, "reliefedge", body)
    [via] = [v for v in bg.vias_of("/SW") if v.poly.intersects(Point(5.75, 5))]
    [pad] = bg.pads_of("/SW", "F.Cu")
    fcu = bg.zone_fill("/SW", "F.Cu")
    assert via.poly.intersects(pad.poly) and via.poly.intersects(fcu)
    assert fcu.intersection(pad.poly).area < 0.1 * pad.poly.area
    vs, facts = check_current.check_net(bg, dict(SW_12A))
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["pos"] == [5.75, 5.0] and "stitch" not in hop
    assert hop["severity"] == "error" and hop["required"] == 24
    assert facts["stitch_vias"] == 24


def test_copper_graph_cuts_a_skipped_plated_pad(tmp_path_factory):
    """A stitch bundle can hold plated pads, and the leaf bound cuts every
    barrel in it: a skipped plated pad no longer joins F.Cu to B.Cu."""
    pth = ('  (footprint "t:J" (at 5 5) (layer "F.Cu")\n'
           '    (property "Reference" "J1" (at 0 0 0))\n'
           '    (pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6)'
           ' (layers "*.Cu") (net "/SW")))\n')
    bg = _board4(tmp_path_factory, "skippad",
                 _pour("/SW", "F.Cu") + _pour("/SW", "B.Cu") + pth)
    [pad] = [p for p in bg.pads_of("/SW") if p.drill is not None]
    for skip, joined in ((frozenset(), True), (frozenset({id(pad)}), False)):
        nodes, find, _ = check_current._copper_graph(bg, "/SW", skip=skip)
        roots = {l: {find(i) for i, (nl, _) in enumerate(nodes) if nl == l}
                 for l in ("F.Cu", "B.Cu")}
        assert (roots["F.Cu"] == roots["B.Cu"]) is joined


# ---- plated pads join fills through their relief (PCB-0021-A GND) ----------
# F.Cu GND F1 | F2 and B.Cu GND B1 | B2, one via in each pair: F1-B1 and
# F2-B2. J1's plated pad sits in F2's and B1's thermal relief (0.6 mm gap,
# one spoke each), so its centre is in neither fill: it alone joins the two.

_RELIEF_FILLS = """  (zone (net "GND") (layer "F.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 0 0) (xy 8 0) (xy 8 10) (xy 0 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 12.6 0) (xy 20 0) (xy 20 10) (xy 12.6 10) (xy 12.6 5.2)
           (xy 11.9 5.2) (xy 11.9 4.8) (xy 12.6 4.8))))
  (zone (net "GND") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 10.4 0) (xy 10.4 4.8) (xy 11.1 4.8) (xy 11.1 5.2)
           (xy 10.4 5.2) (xy 10.4 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 14 0) (xy 20 0) (xy 20 10) (xy 14 10))))
  (via (at 4 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
  (via (at 17 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
"""

_RELIEF_PAD = """  (footprint "t:J" (at 11.5 5)
    (layer "F.Cu")
    (property "Reference" "J1" (at 0 0 0))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.6)
      (layers "*.Cu" "*.Mask") (net "GND")))
"""


def test_plated_pad_in_relief_joins_layers(tmp_path_factory):
    """A plated pad whose fill stops at its thermal relief still joins the
    fills it sits in on each layer (used to need the pad centre in fill)."""
    bg = _board(tmp_path_factory, "relief", _RELIEF_FILLS + _RELIEF_PAD)
    vs, _ = check_current.check_net(bg, ENTRY_GND)
    assert _kinds(vs, "pour_neckdown") == []


def test_relief_fills_without_pad_fail(tmp_path_factory):
    """Control: without J1 the two halves meet nowhere."""
    bg = _board(tmp_path_factory, "reliefnopad", _RELIEF_FILLS)
    vs, _ = check_current.check_net(bg, ENTRY_GND)
    assert {v["layer"] for v in _kinds(vs, "pour_neckdown")} == \
        {"F.Cu", "B.Cu"}


# ---- leaf branches (PCB-0021-A +SYS) ---------------------------------------
# PWR F.Cu pour: a main block holding U1 and a 0.8 mm leg holding R1 pad 1,
# cut apart on F.Cu and bridged by a 0.6 mm B.Cu strip and two single vias
# 4 mm apart. At 2 A the leg, the strip and both 1-via transitions fail;
# the leg only feeds R1 (470R 0603: at most sqrt(0.25/470) = 23 mA).

_LEAF = """  (zone (net "PWR") (layer "F.Cu")
    (polygon (pts (xy 0 0) (xy 16 0) (xy 16 10) (xy 0 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 13 4.6) (xy 16 4.6) (xy 16 5.4) (xy 13 5.4))))
  (zone (net "PWR") (layer "B.Cu")
    (polygon (pts (xy 9 4.7) (xy 14 4.7) (xy 14 5.3) (xy 9 5.3)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 9 4.7) (xy 14 4.7) (xy 14 5.3) (xy 9 5.3))))
  (via (at 9.5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
  (via (at 13.5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
  (footprint "t:U" (at 2 5)
    (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "PWR")))
"""


def _leaf_part(ref="R1", value="470R", lib="t:R0603"):
    return f"""  (footprint "{lib}" (at 15.9 5)
    (layer "F.Cu")
    (property "Reference" "{ref}" (at 0 0 0))
    (property "Value" "{value}" (at 0 0 0))
    (pad "1" smd rect (at -0.4 0) (size 0.5 0.6) (layers "F.Cu") (net "PWR"))
    (pad "2" smd rect (at 0.4 0) (size 0.5 0.6) (layers "F.Cu") (net "LED")))
"""


ENTRY_PWR = {"net": "PWR", "current_a": 2.0}


def test_leaf_branch_judged_at_its_load(tmp_path_factory):
    """The leg's neck and both transitions carry only R1's current: no
    finding, each listed in facts["leaf_branches"] at R1's bound."""
    bg = _board(tmp_path_factory, "leaf", _LEAF + _leaf_part())
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "pour_neckdown") == []
    assert _kinds(vs, "insufficient_transition_vias") == []
    leaves = facts["leaf_branches"]
    assert {b["kind"] for b in leaves} == {"pour_neckdown",
                                           "insufficient_transition_vias"}
    assert all(b["loads"] == ["R1"] for b in leaves)
    assert all(b["current_a"] == pytest.approx(0.0231, abs=1e-3)
               for b in leaves)


def test_leaf_with_unbounded_part_keeps_budget(tmp_path_factory):
    """A capacitor on the leg has no bound: the leg is judged at 2 A."""
    bg = _board(tmp_path_factory, "leafcap",
                _LEAF + _leaf_part("C1", "10uF", "t:C0603"))
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "pour_neckdown")
    assert len(_kinds(vs, "insufficient_transition_vias")) == 2
    assert all(v["current_a"] == 2.0 for v in _kinds(vs, "pour_neckdown"))
    assert "leaf_branches" not in facts


def test_leaf_with_second_path_keeps_budget(tmp_path_factory):
    """A track from the leg back to the main block makes the leg a possible
    bypass of the neck: no bound, the findings stay at 2 A."""
    track = (
        '  (segment (start 9.9 8) (end 14.5 8) (width 0.3) (layer "F.Cu") (net "PWR"))\n'
        '  (segment (start 14.5 8) (end 14.5 5.2) (width 0.3) (layer "F.Cu") (net "PWR"))\n')
    bg = _board(tmp_path_factory, "leafloop", _LEAF + _leaf_part() + track)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "pour_neckdown")
    assert len(_kinds(vs, "insufficient_transition_vias")) == 2
    assert "leaf_branches" not in facts


def test_leaf_reaching_real_load_still_fails(tmp_path_factory):
    """Negative: the leg feeds R1 and also U2, a part with no bound, so it
    is on the real power path and the neck and transitions fail at 2 A."""
    u2 = """  (footprint "t:SOT23" (at 14.5 5)
    (layer "F.Cu")
    (property "Reference" "U2" (at 0 0 0))
    (property "Value" "LDO" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 0.5 0.6) (layers "F.Cu") (net "PWR")))
"""
    bg = _board(tmp_path_factory, "leafreal", _LEAF + _leaf_part() + u2)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    necks = _kinds(vs, "pour_neckdown")
    assert necks and all(v["current_a"] == 2.0 and v["severity"] == "error"
                         for v in necks)
    assert len(_kinds(vs, "insufficient_transition_vias")) == 2
    assert "leaf_branches" not in facts


def test_leaf_reaching_no_pad_keeps_budget(tmp_path_factory):
    """Copper that reaches no pad is not known to be a leaf (a synthetic
    or unfinished board): no bound, the findings stay at 2 A. The hop into
    the far F.Cu piece is a stitch to a dead-end pour (no pad or track on
    it), so the stitch rule skips it; the hop off the main pour stays."""
    bg = _board(tmp_path_factory, "leafbare", _LEAF)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "pour_neckdown")
    [hop] = _kinds(vs, "insufficient_transition_vias")
    assert hop["pos"] == [9.5, 5.0] and hop["required"] == 4   # 2 A
    assert "leaf_branches" not in facts


@pytest.mark.parametrize("value,ohms", [
    ("470R", 470.0), ("4k7", 4700.0), ("10k", 1e4), ("2.2K", 2200.0),
    ("1M", 1e6), ("100", 100.0), ("4R7", 4.7), ("10kOhm 1%", 1e4),
    ("0R", 0.0), ("R100", None), ("", None), ("DNP", None)])
def test_parse_ohms(value, ohms):
    assert check_current.parse_ohms(value) == ohms


# ---- leaf tracks (hifi-receiver-2 core +3V3: one 0.3 mm track off a plane
# via to a single pull-up, judged at the rail's 0.8 A) ---------------------
# B.Cu PWR plane; a via at (3, 5) up to F.Cu and a 0.2 mm track to pad 1 of
# a two-pad part at (8, 5). At 2 A the track fails; R1 (10k 0402) draws at
# most sqrt(0.2/10k) = 4.5 mA.

_TAP = """  (zone (net "PWR") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10))))
  (via (at 3 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
  (segment (start 3 5) (end 7.6 5) (width 0.2) (layer "F.Cu") (net "PWR"))
"""


def _tap_part(ref="R1", value="10k", lib="t:R0402"):
    return f"""  (footprint "{lib}" (at 8 5)
    (layer "F.Cu")
    (property "Reference" "{ref}" (at 0 0 0))
    (property "Value" "{value}" (at 0 0 0))
    (pad "1" smd rect (at -0.4 0) (size 0.5 0.6) (layers "F.Cu") (net "PWR"))
    (pad "2" smd rect (at 0.4 0) (size 0.5 0.6) (layers "F.Cu") (net "SIG")))
"""


def test_leaf_track_judged_at_its_load(tmp_path_factory):
    """The tap only feeds R1: no undersized_track, listed in
    facts["leaf_branches"] at R1's bound."""
    bg = _board(tmp_path_factory, "tap", _TAP + _tap_part())
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "undersized_track") == []
    [leaf] = [b for b in facts["leaf_branches"]
              if b["kind"] == "undersized_track"]
    assert leaf["loads"] == ["R1"] and leaf["layer"] == "F.Cu"
    assert leaf["current_a"] == pytest.approx(0.00447, abs=1e-4)


def test_leaf_track_to_unbounded_part_keeps_budget(tmp_path_factory):
    """A capacitor or an IC past the tap has no bound: judged at 2 A."""
    bg = _board(tmp_path_factory, "tapcap",
                _TAP + _tap_part("C1", "100nF", "t:C0402"))
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    [v] = _kinds(vs, "undersized_track")
    assert v["current_a"] == 2.0 and v["severity"] == "error"
    assert "leaf_loads" not in v


def test_leaf_track_with_second_feed_keeps_budget(tmp_path_factory):
    """R1's pad also reaches the plane through its own via: the track is
    not the only path, so R1 does not bound it."""
    via = ('  (via (at 7.6 5.5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
           ' (net "PWR"))\n')
    bg = _board(tmp_path_factory, "tap2", _TAP + _tap_part() + via)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    [v] = _kinds(vs, "undersized_track")
    assert v["current_a"] == 2.0
    assert not [b for b in facts.get("leaf_branches", [])
                if b["kind"] == "undersized_track"]


# a router's staircase: steps shorter than the track is wide, whose end caps
# meet the next-but-one step's, so netconn reads the one line as a loop
_STAIR = """  (zone (net "PWR") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10))))
  (via (at 3 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
  (segment (start 3 5) (end 5 5) (width 0.2) (layer "F.Cu") (net "PWR"))
  (segment (start 5 5) (end 5.05 5.05) (width 0.2) (layer "F.Cu") (net "PWR"))
  (segment (start 5.05 5.05) (end 5.1 5.05) (width 0.2) (layer "F.Cu") (net "PWR"))
  (segment (start 5.1 5.05) (end 5.15 5) (width 0.2) (layer "F.Cu") (net "PWR"))
  (segment (start 5.15 5) (end 7.6 5) (width 0.2) (layer "F.Cu") (net "PWR"))
"""


def test_leaf_track_through_a_staircase_judged_at_its_load(tmp_path_factory):
    """#1042: the steps are no bridges (their caps form a loop), but the
    loop holds only track ends and two bridges lead out: one line, a leaf
    at R1's bound, not a 2 A finding."""
    bg = _board(tmp_path_factory, "stair", _STAIR + _tap_part())
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "undersized_track") == []
    assert {tuple(b["loads"]) for b in facts["leaf_branches"]
            if b["kind"] == "undersized_track"} == {("R1",)}


def test_staircase_branching_to_a_cap_keeps_budget(tmp_path_factory):
    """Negative: a third track leaves the staircase for C2, a capacitor with
    no bound. Three ways out: no bridge's sides are the cluster's, so the
    steps are judged at 2 A, not at R1's bound."""
    br = ('  (segment (start 5.05 5.05) (end 5.05 7.6) (width 0.2) '
          '(layer "F.Cu") (net "PWR"))\n'
          '  (footprint "t:C0402" (at 5.45 8) (layer "F.Cu")\n'
          '    (property "Reference" "C2" (at 0 0 0))\n'
          '    (property "Value" "100nF" (at 0 0 0))\n'
          '    (pad "1" smd rect (at -0.4 0) (size 0.5 0.6) (layers "F.Cu") '
          '(net "PWR"))\n'
          '    (pad "2" smd rect (at 0.4 0) (size 0.5 0.6) (layers "F.Cu") '
          '(net "GND")))\n')
    bg = _board(tmp_path_factory, "stair3", _STAIR + _tap_part() + br)
    vs, _ = check_current.check_net(bg, ENTRY_PWR)
    assert {v["current_a"] for v in _kinds(vs, "undersized_track")} == {2.0}


def test_declared_sense_pad_bounds_its_part(tmp_path_factory):
    """#1042: R1's pad 1 is no SENSE pin by name, but the entry declares it
    a sense pad: R1 draws at most SENSE_BOUND_A."""
    bg = _board(tmp_path_factory, "tapsns",
                _TAP + _tap_part("R1", "4R7", "t:R1206"))
    vs, facts = check_current.check_net(
        bg, dict(ENTRY_PWR, sense_pads={"R1.1": "C9.1"}))
    assert _kinds(vs, "undersized_track") == []
    [leaf] = [b for b in facts["leaf_branches"]
              if b["kind"] == "undersized_track"]
    assert leaf["current_a"] == check_current.SENSE_BOUND_A


def test_leaf_track_still_too_thin_names_its_load(tmp_path_factory):
    """A 4R7 1206 can pass 0.33 A, more than a 0.2 mm track carries at a
    tight 1 C rise: the finding stays, re-judged at that bound."""
    bg = _board(tmp_path_factory, "tapbig",
                _TAP + _tap_part("R1", "4R7", "t:R1206"))
    vs, _ = check_current.check_net(bg, {**ENTRY_PWR, "dt_c": 1})
    [v] = _kinds(vs, "undersized_track")
    assert v["leaf_loads"] == ["R1"]
    assert v["current_a"] == pytest.approx((0.5 / 4.7) ** 0.5, abs=1e-3)
    assert "leaf branch: R1" in v["msg"]


# ---- inferred switch nodes (errata corpus digits-1) ------------------------
# An LM2596 whose OUT pin (pinfunction "OUT_2") sits on an auto-named net no
# constraints entry names, routed to the inductor at `width`.

def _buck(width: float, value: str = "LM2596S-5", fn: str = "OUT_2") -> str:
    return f"""  (footprint "t:TO-263-5" (at 4 5)
    (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0))
    (property "Value" "{value}" (at 0 0 0))
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net "+12V")
      (pinfunction "VIN_1"))
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "Net-(D1-K)")
      (pinfunction "{fn}")))
  (footprint "t:L" (at 14 5)
    (layer "F.Cu")
    (property "Reference" "L1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "Net-(D1-K)")))
  (segment (start 4 5) (end 14 5) (width {width}) (layer "F.Cu")
    (net "Net-(D1-K)"))
"""


def test_switch_node_inferred_at_rated_current(tmp_path_factory):
    bg = _board(tmp_path_factory, "buck", _buck(0.25))
    inf, unrated = check_current.inferred_switch_entries(
        [], bg, check_current.switch_rows())
    assert unrated == []
    assert [(e["net"], e["current_a"], e["from_part"]) for e in inf] == [
        ("Net-(D1-K)", 3.0, "U1")]
    vs, _ = check_current.check_net(bg, inf[0])
    assert _kinds(vs, "undersized_track")


def test_switch_node_designer_fix_passes_screen(tmp_path_factory):
    # digits v2: the same node at 0.75 mm passes the dT=60C screen
    bg = _board(tmp_path_factory, "buckfix", _buck(0.75))
    inf, _ = check_current.inferred_switch_entries(
        [], bg, check_current.switch_rows())
    vs, _ = check_current.check_net(bg, inf[0])
    assert _kinds(vs, "undersized_track") == []


def test_switch_node_named_in_constraints_wins(tmp_path_factory):
    bg = _board(tmp_path_factory, "bucknamed", _buck(0.25))
    inf, _ = check_current.inferred_switch_entries(
        [{"net": "Net-(D1-K)", "current_a": 0.2}], bg,
        check_current.switch_rows())
    assert inf == []


def test_unrated_switch_pin_is_a_fact(tmp_path_factory):
    bg = _board(tmp_path_factory, "buckunk", _buck(0.25, "XYZ123", "SW"))
    inf, unrated = check_current.inferred_switch_entries(
        [], bg, check_current.switch_rows())
    assert inf == []
    assert unrated == [{"ref": "U1", "value": "XYZ123", "pin": "SW",
                        "net": "Net-(D1-K)"}]


def test_switch_node_cli_warns_without_constraints(tmp_path_factory):
    bg = _board(tmp_path_factory, "buckcli", _buck(0.25))
    cons = bg.path.parent / "empty.json"
    cons.write_text("{}", encoding="utf-8")
    proc = subprocess.run(
        [PYTHON, str(SCRIPTS / "check_current.py"), "--pcb", str(bg.path),
         "--constraints", str(cons)], capture_output=True, text=True)
    payload = json.loads(proc.stdout)
    assert payload["counts"]["by_severity"] == {"warning": 1}
    v = payload["violations"][0]
    assert v["inferred"] is True and v["from_part"] == "U1"
    assert "U1" in v["refs"] and "constraints.json" in v["msg"]



def test_switch_node_judged_with_no_constraints_file(tmp_path_factory):
    # #876: a board with no constraints.json at all (digits v1) still gets
    # its switch node judged, and verify_all runs the check on it.
    bg = _board(tmp_path_factory, "bucknofile", _buck(0.25))
    payload = check_current.run(["--pcb", str(bg.path)])[0]
    assert payload["counts"]["by_severity"] == {"warning": 1}
    assert payload["violations"][0]["from_part"] == "U1"
    import verify_all
    row = next(c for c in verify_all.BUILTIN_CHECKS
               if c["name"] == "check_current")
    assert row["needs"] == [] and row["args"]({"pcb": "x"}) == []
    assert row["args"]({"constraints": "c.json"}) == ["--constraints", "c.json"]

# ---- per-load current: branch vias of a star (#676, #650) -------------------
# U1 feeds a 2 mm F.Cu trunk (x=10, y 2..8); three single vias 3 mm apart each
# drop to B.Cu and a 0.8 mm strip to one load. 3 A rail, three 1 A loads.

def _star(sense_fn: str = "") -> str:
    out = """  (segment (start 2 5) (end 10 5) (width 2.0) (layer "F.Cu") (net "PWR"))
  (segment (start 10 2) (end 10 8) (width 2.0) (layer "F.Cu") (net "PWR"))
  (footprint "t:U" (at 2 5)
    (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "PWR")))
"""
    for i, y in enumerate((2, 5, 8), start=2):
        fn = f'(pinfunction "{sense_fn}")' if sense_fn and i == 3 else ""
        out += f"""  (via (at 10 {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))
  (segment (start 10 {y}) (end 14 {y}) (width 0.8) (layer "B.Cu") (net "PWR"))
  (footprint "t:U" (at 14.5 {y})
    (layer "B.Cu")
    (property "Reference" "U{i}" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (net "PWR") {fn}))
"""
    return out


STAR = {"net": "PWR", "current_a": 3.0, "via_amps": 1.0}


def test_star_branch_vias_carry_their_own_load(tmp_path_factory):
    bg = _board(tmp_path_factory, "star", _star())
    vs, _ = check_current.check_net(bg, STAR)
    assert len(_kinds(vs, "insufficient_transition_vias")) == 3   # 3 A, no loads
    loads = dict(STAR, loads={"U2": 1.0, "U3": 1.0, "U4": 1.0})
    vs, facts = check_current.check_net(bg, loads)
    assert _kinds(vs, "insufficient_transition_vias") == []
    assert [b["current_a"] for b in facts["leaf_branches"]
            if b["kind"] == "insufficient_transition_vias"] == [1.0] * 3
    # 1 A through each via: 0.5 A/via needs two, so they still fail at 1 A
    vs, _ = check_current.check_net(bg, dict(loads, via_amps=0.5))
    assert [v["required"] for v in _kinds(vs, "insufficient_transition_vias")] \
        == [2, 2, 2]


def test_kelvin_sense_tap_via_carries_nothing(tmp_path_factory):
    bg = _board(tmp_path_factory, "kelvin", _star("SENSE+"))
    vs, _ = check_current.check_net(bg, dict(STAR, loads={"U2": 1.0, "U4": 1.0}))
    assert _kinds(vs, "insufficient_transition_vias") == []
    vs, _ = check_current.check_net(bg, STAR)    # U2/U4 undeclared: only U3 free
    assert len(_kinds(vs, "insufficient_transition_vias")) == 2


def test_declared_load_does_not_hide_the_real_path(tmp_path_factory):
    """Negative: the source's own side stays judged at the budget."""
    bg = _board(tmp_path_factory, "starneg", _star())
    vs, _ = check_current.check_net(
        bg, dict(STAR, loads={"U2": 3.0, "U3": 1.0, "U4": 1.0}))
    assert len(_kinds(vs, "insufficient_transition_vias")) == 1


# ---- source inference: branches of an undeclared rail (#699) ----------------
# The star again, fed from a connector: three branch vias, one undeclared load
# each. Each via carries its own load, not the 3 A rail, and nothing says how
# much that is: unjudged, in the facts, not a failure at the budget.

def _part(ref: str, x: float, y: float, layer: str = "F.Cu",
          fns: tuple = ("",), net: str = "PWR") -> str:
    pads = ""
    for i, fn in enumerate(fns, start=1):
        pf = f'(pinfunction "{fn}")' if fn else ""
        pads += (f'    (pad "{i}" smd rect (at {(i - 1) * 0.6} 0) (size 0.5 1) '
                 f'(layers "{layer}") (net "{net}") {pf})\n')
    return (f'  (footprint "t:{ref[0]}" (at {x} {y})\n    (layer "{layer}")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n{pads}  )\n')


def _fed_star() -> str:
    return _star().replace('"t:U" (at 2 5)', '"t:J" (at 2 5)', 1) \
        .replace('"Reference" "U1"', '"Reference" "J1"', 1)


def _unjudged(facts, kind):
    return [u for u in facts.get("unjudged", []) if u["kind"] == kind]


def test_branch_via_past_the_source_is_unjudged(tmp_path_factory):
    bg = _board(tmp_path_factory, "fedstar", _fed_star())
    vs, facts = check_current.check_net(bg, STAR)
    assert _kinds(vs, "insufficient_transition_vias") == []
    taps = _unjudged(facts, "insufficient_transition_vias")
    assert [u["refs"] for u in taps] == [["U2"], ["U3"], ["U4"]]
    assert [u["pos"] for u in taps] == [[10.0, 2.0], [10.0, 5.0], [10.0, 8.0]]


def test_entry_source_names_the_source(tmp_path_factory):
    """U1 is a one-pad part power_parts would not pick: the entry names it,
    as a ref or a list of refs."""
    bg = _board(tmp_path_factory, "namedstar", _star())
    for source in ("U1", ["U1"]):
        vs, facts = check_current.check_net(bg, dict(STAR, source=source))
        assert _kinds(vs, "insufficient_transition_vias") == []
        assert len(_unjudged(facts, "insufficient_transition_vias")) == 3


def test_entry_source_not_on_the_net_is_an_error(tmp_path_factory):
    bg = _board(tmp_path_factory, "badsource", _star())
    with pytest.raises(check_current.CheckError, match="U99"):
        check_current.check_net(bg, dict(STAR, source="U99"))


def test_no_source_found_keeps_the_budget(tmp_path_factory):
    """Nothing on the star looks like a source: every via stays judged."""
    bg = _board(tmp_path_factory, "nosource", _star())
    vs, facts = check_current.check_net(bg, STAR)
    assert len(_kinds(vs, "insufficient_transition_vias")) == 3
    assert "unjudged" not in facts


def test_source_past_the_via_keeps_its_budget(tmp_path_factory):
    """The source hangs off U2's via: that via carries the whole rail to U1,
    U3 and U4, while U3's and U4's stay branches."""
    bg = _board(tmp_path_factory, "srcbranch", _star())
    vs, facts = check_current.check_net(bg, dict(STAR, source="U2"))
    [v] = _kinds(vs, "insufficient_transition_vias")
    assert v["pos"] == [10.0, 2.0] and v["required"] == 3
    assert [u["refs"] for u in _unjudged(
        facts, "insufficient_transition_vias")] == [["U3"], ["U4"]]


def test_undersized_trunk_via_between_source_and_loads_warns(tmp_path_factory):
    """Known answer, the escape to guard: one via between the connector and
    every load on the rail is the trunk, not a branch."""
    body = ('  (segment (start 2 5) (end 10 5) (width 2.0) (layer "F.Cu") (net "PWR"))\n'
            '  (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))\n'
            '  (segment (start 10 5) (end 16 5) (width 2.0) (layer "B.Cu") (net "PWR"))\n'
            '  (segment (start 16 2) (end 16 8) (width 2.0) (layer "B.Cu") (net "PWR"))\n'
            + _part("J1", 2, 5))
    for ref, y in (("U2", 2), ("U3", 5), ("U4", 8)):
        body += _part(ref, 16, y, "B.Cu")
    bg = _board(tmp_path_factory, "trunkvia", body)
    vs, facts = check_current.check_net(bg, STAR)
    [v] = _kinds(vs, "insufficient_transition_vias")
    assert v["required"] == 3 and v["severity"] == "error"
    assert "unjudged" not in facts


def test_undersized_trunk_track_between_source_and_loads_warns(tmp_path_factory):
    """Same for a thin track, plane-fed or not: it stays at the budget."""
    body = ('  (segment (start 2 5) (end 10 5) (width 0.2) (layer "F.Cu") (net "PWR"))\n'
            '  (segment (start 10 2) (end 10 8) (width 2.0) (layer "F.Cu") (net "PWR"))\n'
            + _part("J1", 2, 5))
    for ref, y in (("U2", 2), ("U3", 5), ("U4", 8)):
        body += _part(ref, 10, y)
    bg = _board(tmp_path_factory, "trunktrack", body)
    vs, facts = check_current.check_net(bg, STAR)
    [v] = _kinds(vs, "undersized_track")
    assert v["current_a"] == 3.0 and v["bridge"] is True
    assert "unjudged" not in facts


def test_two_loads_each_side_of_a_split_stay_judged(tmp_path_factory):
    """A branch holding half the rail's loads may carry most of its current:
    only a minority side leaves the budget."""
    body = ('  (segment (start 2 5) (end 10 5) (width 2.0) (layer "F.Cu") (net "PWR"))\n'
            '  (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))\n'
            '  (segment (start 10 5) (end 14 5) (width 2.0) (layer "B.Cu") (net "PWR"))\n'
            + _part("J1", 2, 5) + _part("U2", 6, 5) + _part("U3", 14, 5, "B.Cu"))
    bg = _board(tmp_path_factory, "halfsplit", body)
    vs, facts = check_current.check_net(bg, STAR)
    assert len(_kinds(vs, "insufficient_transition_vias")) == 1
    assert "unjudged" not in facts


def test_branch_to_a_multipad_ic_stays_judged(tmp_path_factory):
    """power_parts picks an IC with two pads on the rail as a power part (an
    amplifier's PVDD, a regulator's VIN): not a tap to wave through."""
    amp = _part("U2", 14.5, 2, "B.Cu", ("", ""))
    body = re.sub(r'\(footprint "t:U" \(at 14\.5 2\).*?\(net "PWR"\) \)\)\n',
                  lambda _m: amp, _fed_star(), count=1, flags=re.S)
    assert '"2" smd' in body
    bg = _board(tmp_path_factory, "amptap", body)
    vs, facts = check_current.check_net(bg, STAR)
    [v] = _kinds(vs, "insufficient_transition_vias")
    assert v["pos"] == [10.0, 2.0]
    assert [u["refs"] for u in _unjudged(
        facts, "insufficient_transition_vias")] == [["U3"], ["U4"]]


def _tap_with_second_pad(net: str, fn: str = "") -> str:
    """The first tap, U2, with one more pad on another net."""
    pf = f'(pinfunction "{fn}")' if fn else ""
    return _fed_star().replace(
        '"Reference" "U2" (at 0 0 0))\n',
        '"Reference" "U2" (at 0 0 0))\n'
        f'    (pad "2" smd rect (at 0 2) (size 1 1) (layers "B.Cu") '
        f'(net "{net}") {pf})\n', 1)


def test_rectifier_past_a_via_stays_judged(tmp_path_factory):
    """Lumina's V48_RTN: power_parts picks U1, but bridge D2 is what feeds the
    rail, so the via to it carries the rail and is no branch. Nor is a
    transformer winding."""
    for ref in ("D2", "T2"):
        body = _fed_star().replace('"Reference" "U2"', f'"Reference" "{ref}"', 1)
        bg = _board(tmp_path_factory, f"tap{ref}", body)
        vs, facts = check_current.check_net(bg, STAR)
        [v] = _kinds(vs, "insufficient_transition_vias")
        assert v["pos"] == [10.0, 2.0]
        assert [u["refs"] for u in _unjudged(
            facts, "insufficient_transition_vias")] == [["U3"], ["U4"]]


def test_switcher_input_past_a_via_stays_judged(tmp_path_factory):
    """A buck's VIN is the rail's power going on (lumina V48_RAW to U20, whose
    pins have no names): known by its switch pin, or by an inductor on its
    switch net."""
    choke = _part("L9", 16, 8, "B.Cu", net="/rails/BUCK5_LX")
    for name, body in (("lxnet", _tap_with_second_pad("/rails/BUCK5_LX") + choke),
                       ("swpin", _tap_with_second_pad("N1", "SW"))):
        bg = _board(tmp_path_factory, name, body)
        vs, facts = check_current.check_net(bg, STAR)
        [v] = _kinds(vs, "insufficient_transition_vias")
        assert v["pos"] == [10.0, 2.0], name
        assert [u["refs"] for u in _unjudged(
            facts, "insufficient_transition_vias")] == [["U3"], ["U4"]], name


def test_bulk_cap_tap_is_judged_by_ripple_not_budget(tmp_path_factory):
    """A capacitor's tap is a branch on any rail (#798, once #699 and PR 793's
    10 uF rule): no converter on the rail, it stays unjudged, plane or not."""
    def positions(vs):
        return [v["pos"] for v in _kinds(vs, "insufficient_transition_vias")]
    for value in ("22uF 25V X5R 1206", "10uF", "100nF 50V X7R"):
        bg = _board(tmp_path_factory, "captap", _cap_tap(value))
        vs, facts = check_current.check_net(bg, STAR)
        assert [10.0, 2.0] not in positions(vs), value
        assert ["C2"] in [u["refs"] for u in facts["unjudged"]], value
    bg = _board(tmp_path_factory, "bulkplane", _cap_tap("22uF") + """  (zone (net "PWR") (layer "B.Cu")
    (filled_polygon (layer "B.Cu")
      (pts (xy 40 40) (xy 50 40) (xy 50 50) (xy 40 50))))
""")
    vs, facts = check_current.check_net(bg, dict(STAR, plane_fed=True))
    assert [10.0, 2.0] not in positions(vs)
    assert ["C2"] in [u["refs"] for u in facts["unjudged"]]


def test_button_net_named_sw_is_still_a_branch(tmp_path_factory):
    """Core's GPIO expander U17 has a pad on /ENC_SW: an encoder's switch, no
    inductor, no switcher. The tap stays a branch."""
    bg = _board(tmp_path_factory, "buttontap", _tap_with_second_pad("/ENC_SW"))
    vs, facts = check_current.check_net(bg, STAR)
    assert _kinds(vs, "insufficient_transition_vias") == []
    assert len(_unjudged(facts, "insufficient_transition_vias")) == 3


def test_regulator_output_pin_counts_as_a_source(tmp_path_factory):
    """An LDO's single VOUT pad past the via, a two-VDD MCU on the near side:
    the via is the trunk even though only the MCU is in power_parts."""
    body = ('  (segment (start 2 5) (end 10 5) (width 2.0) (layer "F.Cu") (net "PWR"))\n'
            '  (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))\n'
            '  (segment (start 10 5) (end 14 5) (width 2.0) (layer "B.Cu") (net "PWR"))\n'
            + _part("U1", 2, 5, "F.Cu", ("VDD", "VDD"))
            + _part("U2", 4, 3, "F.Cu") + _part("U3", 6, 3, "F.Cu")
            + _part("U4", 8, 3, "F.Cu")
            + _part("U5", 14, 5, "B.Cu", ("VOUT",)))
    body = body.replace('(at 4 3)', '(at 4 5)').replace('(at 6 3)', '(at 6 5)') \
        .replace('(at 8 3)', '(at 8 5)')
    bg = _board(tmp_path_factory, "ldopin", body)
    vs, facts = check_current.check_net(bg, STAR)
    assert len(_kinds(vs, "insufficient_transition_vias")) == 1
    assert "unjudged" not in facts


def test_declared_load_past_the_via_keeps_its_verdict(tmp_path_factory):
    """A declared load is known: the via fails at it, it is not unjudged."""
    bg = _board(tmp_path_factory, "fedload", _fed_star())
    vs, facts = check_current.check_net(
        bg, dict(STAR, loads={"U2": 3.0}))
    [v] = _kinds(vs, "insufficient_transition_vias")
    assert v["pos"] == [10.0, 2.0] and v["required"] == 3
    assert [u["refs"] for u in _unjudged(
        facts, "insufficient_transition_vias")] == [["U3"], ["U4"]]


def test_override_region_stays_judged(tmp_path_factory):
    """A declared per-region current is a declaration: it is enforced."""
    bg = _board(tmp_path_factory, "fedov", _fed_star())
    vs, facts = check_current.check_net(bg, dict(STAR, overrides=[
        {"near": [10, 2], "radius_mm": 0.5, "current_a": 2.0}]))
    [v] = _kinds(vs, "insufficient_transition_vias")
    assert v["pos"] == [10.0, 2.0] and v["required"] == 2
    assert len(_unjudged(facts, "insufficient_transition_vias")) == 2


def test_derived_entry_is_not_inferred(tmp_path_factory):
    """The return screen has no source: it stays as it was."""
    bg = _board(tmp_path_factory, "fedret", _fed_star())
    vs, facts = check_current.check_net(bg, dict(STAR, derived=True))
    assert len(_kinds(vs, "insufficient_transition_vias")) == 3
    assert "unjudged" not in facts


def test_via_with_a_parallel_path_stays_judged(tmp_path_factory):
    """A second via joins the same two strips: cutting one leaves the other,
    so neither is a branch's only way in."""
    body = _fed_star() + (
        '  (via (at 13 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "PWR"))\n'
        '  (segment (start 10 2) (end 13 2) (width 2.0) (layer "F.Cu") (net "PWR"))\n')
    bg = _board(tmp_path_factory, "fedloop", body)
    vs, facts = check_current.check_net(bg, STAR)
    assert {tuple(v["pos"]) for v in _kinds(vs, "insufficient_transition_vias")} \
        == {(10.0, 2.0), (13.0, 2.0)}
    assert [u["refs"] for u in _unjudged(
        facts, "insufficient_transition_vias")] == [["U3"], ["U4"]]


def test_thin_branch_track_off_a_plane_is_unjudged(tmp_path_factory):
    """The hifi core shape: a plane, a via up, a thin track to one part."""
    body = _TAP + _tap_part("U2", "MCU", "t:QFN")
    for ref, x in (("J1", 15), ("U3", 12), ("U4", 18)):
        body += _part(ref, x, 2, "B.Cu")
    bg = _board(tmp_path_factory, "plane", body)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "undersized_track") == []
    [tap] = _unjudged(facts, "undersized_track")
    assert tap["refs"] == ["U2"] and tap["layer"] == "F.Cu"
    assert _kinds(vs, "insufficient_transition_vias") == []
    assert [u["refs"] for u in _unjudged(
        facts, "insufficient_transition_vias")] == [["U2"]]


def test_neck_to_a_branch_is_unjudged(tmp_path_factory):
    """A pour neck with only an undeclared minority load past it."""
    body = (_LEAF + _leaf_part("U5", "MCU", "t:QFN")
            + _part("J1", 2, 2) + _part("U2", 2, 8) + _part("U3", 5, 2))
    bg = _board(tmp_path_factory, "fedneck", body)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "pour_neckdown") == []
    assert _unjudged(facts, "pour_neckdown")
    assert all(u["refs"] == ["U5"] for u in _unjudged(facts, "pour_neckdown"))


def test_neck_to_every_load_keeps_the_budget(tmp_path_factory):
    """The neck between the source and the only load is the trunk."""
    body = (_LEAF + _leaf_part("U5", "MCU", "t:QFN") + _part("J1", 2, 2))
    bg = _board(tmp_path_factory, "trunkneck", body)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "pour_neckdown")
    assert "unjudged" not in facts


def test_thin_loop_past_a_branch_via_is_unjudged(tmp_path_factory):
    """Two thin tracks in parallel past a tap via: not bridges, but they carry
    no more than the branch does."""
    body = _TAP + _tap_part("U2", "MCU", "t:QFN") + (
        '  (segment (start 3 5) (end 5 7) (width 0.2) (layer "F.Cu") (net "PWR"))\n'
        '  (segment (start 5 7) (end 7.6 5) (width 0.2) (layer "F.Cu") (net "PWR"))\n')
    for ref, x in (("J1", 15), ("U3", 12), ("U4", 18)):
        body += _part(ref, x, 2, "B.Cu")
    bg = _board(tmp_path_factory, "planeloop", body)
    vs, facts = check_current.check_net(bg, ENTRY_PWR)
    assert _kinds(vs, "undersized_track") == []
    assert [u["refs"] for u in _unjudged(facts, "undersized_track")] \
        == [["U2"]] * 3


def test_core_branch_taps_are_unjudged():
    """Known answer: of core's 26 single-via findings at the 3V3 rail's 0.8 A,
    21 are branches and leave the warnings with their refs. The other five
    are plane-fed stubs (#828): the two at J3 and one each at U8 and U13 feed
    a part of unknown draw, so they are unjudged too; the one at 51.84, 212.31
    reaches no pad and carries nothing. No finding is left on the rail."""
    kicad = REPO / "pinned" / "core" / "kicad"
    cons = json.loads((kicad / "constraints.json").read_text(encoding="utf-8"))
    entry = next(e for e in cons["power"] if e["net"] == "+3V3")
    vs, facts = check_current.check_net(
        geom.load_board(kicad / "core.kicad_pcb"), entry)
    taps = _unjudged(facts, "insufficient_transition_vias")
    assert len(taps) == 25 and all(u["refs"] for u in taps)
    stubs = {tuple(u["pos"]): u["refs"] for u in taps
             if set(u["refs"]) & {"J3", "U8", "U13"}}
    assert stubs == {(50.137, 214.107): ["J3"], (60.437, 215.107): ["J3"],
                     (85.092, 189.466): ["U8"], (135.708, 153.672): ["U13"]}
    assert "L10" not in {r for u in taps for r in u["refs"]}
    assert vs == []
    empty = [b for b in facts["leaf_branches"] if b.get("stub")
             and b["kind"] == "insufficient_transition_vias"]
    assert empty == [{"kind": "insufficient_transition_vias",
                      "pos": [51.837, 212.307], "current_a": 0.0,
                      "loads": [], "stub": True}]
    assert {u["kind"] for u in facts["unjudged"]} == {
        "insufficient_transition_vias", "undersized_track"}


# ---- #798: a bulk or bus cap's via, at the ripple current it carries

def _cap_tap(value):
    """_fed_star with its far part U2 made a capacitor of this value."""
    return _fed_star().replace(
        '"Reference" "U2" (at 0 0 0))',
        f'"Reference" "C2" (at 0 0 0))\n    (property "Value" "{value}" '
        '(at 0 0 0))', 1)


def _at(vs, pos):
    return [v for v in _kinds(vs, "insufficient_transition_vias")
            if v["pos"] == list(pos)]


CORE_KICAD = REPO / "pinned" / "core" / "kicad"
C7_TAP = (90.987, 172.849)        # core: PVDD's one-via tap to C7, 10 uF


def _core_pvdd(pcb=CORE_KICAD / "core.kicad_pcb", current_a=None):
    """core's PVDD at the bus ripple of a class-D stage: 1.5 A at D = 0.5 is
    0.75 A RMS, so one via at 0.5 A falls short and two carry it."""
    cons = json.loads((CORE_KICAD / "constraints.json").read_text(
        encoding="utf-8"))
    cons["blocks"] = [{"topology": "class-d-amp", "block": "B1", "name": "t",
                       "operating_point": {"bus_net": "PVDD", "iout_a": 1.5,
                                           "duty": 0.5}}]
    entry = next(e for e in cons["power"] if e["net"] == "PVDD")
    if current_a is not None:
        entry = dict(entry, current_a=current_a)
    return check_current.check_net(geom.load_board(pcb), entry, cons=cons)


LUMINA = REPO / "tests" / "fixtures" / "stages" / "lumina_carrier"
C52_TAPS = ((31.75, 118.0), (34.65, 118.8))


def _lumina_12v(pcb=LUMINA / "routed.kicad_pcb"):
    cons = json.loads((LUMINA / "constraints.json").read_text(encoding="utf-8"))
    entry = next(e for e in cons["power"] if e["net"] == "+12V")
    return check_current.check_net(geom.load_board(pcb), entry, cons=cons)


def test_lumina_output_caps_pass_at_their_ripple():
    """Known answer 1 (#798): lumina's vias to its buck's output caps C52/C53
    no longer fail. They carry the inductor's ripple, ~0.13 A RMS, not the
    rail's 2 A. The figure and what it comes from are in facts: 12 V from 57 V
    through 68 uH at 300 kHz."""
    vs, facts = _lumina_12v()
    for pos in C52_TAPS:
        assert not _at(vs, pos), pos
    rows = [r for r in facts["ripple_caps"]
            if r["kind"] == "insufficient_transition_vias"]
    assert sorted(r["pos"] for r in rows) == sorted(map(list, C52_TAPS))
    for row in rows:
        assert set(row["refs"]) == {"C52", "C53"}
        assert row["amps"] == pytest.approx(0.134, abs=0.001)
        term, = row["terms"]
        assert term["from"] == "U20"
        inputs = term["inputs"]
        assert (inputs["L_h"], inputs["fsw_hz"], inputs["vin_v"],
                inputs["vout_v"]) == (pytest.approx(68e-6), 300e3, 57.0, 12.0)
        assert "AN4067" in term["formula"]


def test_lumina_load_beside_c52_keeps_the_budget(tmp_path):
    """a777e's condition on known answer 1: a far side holding any DC load is
    not a capacitor branch. A resistor to ground next to C52 brings the budget
    finding back."""
    text = (LUMINA / "routed.kicad_pcb").read_text(encoding="utf-8")
    start = text.index('(property "Reference" "C53"')
    start = text.rindex("(footprint ", 0, start)
    end = text.index("\n\t(footprint ", start + 1)
    block = re.sub(r'\(uuid "[^"]*"\)', "", text[start:end])
    pcb = tmp_path / "routed.kicad_pcb"
    pcb.write_text(text[:start] + block.replace(
        '"Reference" "C53"', '"Reference" "R953"').replace(
        '"22uF', '"1k 0805 was22uF', 1) + text[start:], encoding="utf-8")
    vs, facts = _lumina_12v(pcb)
    found = [v for pos in C52_TAPS for v in _at(vs, pos)]
    assert found and all("ripple" not in v["msg"] and "2.00 A" in v["msg"]
                         for v in found)
    assert not facts.get("ripple_caps")


def test_ripple_finding_shows_its_inputs():
    """a777e's condition 2: a reader of a finding can check the number."""
    vs, _ = _core_pvdd()
    msg = _at(vs, C7_TAP)[0]["msg"]
    assert "iout_a=1.5" in msg and "duty=0.5" in msg and "AN4067" in msg
    assert _at(vs, C7_TAP)[0]["ripple"]["terms"][0]["inputs"] == {
        "iout_a": 1.5, "duty": 0.5}


def test_core_c7_tap_fails_at_its_ripple():
    """Known answer 2 (#798), first half: hifi core's C7 on a 1-via tap, with
    a ripple figure one via can't carry, fails as an error, though PVDD is
    plane-fed."""
    vs, facts = _core_pvdd()
    found = _at(vs, C7_TAP)
    assert found and found[0]["severity"] == "error"
    assert found[0]["refs"] == ["C7"]
    assert "0.75 A" in found[0]["msg"] and "ripple" in found[0]["msg"]
    assert list(C7_TAP) not in [u["pos"] for u in facts.get("unjudged", [])]


def test_core_c7_with_vias_that_carry_it_passes(tmp_path):
    """Known answer 2 (#798), second half: the same cap and figure, with a
    second via on the tap that overlaps the first on both layers."""
    text = (CORE_KICAD / "core.kicad_pcb").read_text(encoding="utf-8")
    start = re.search(r"\(via\s+\(at 90\.987 172\.849\)", text).start()
    end = text.index("\n\t)", start) + 3
    via = text[start:end]
    second = re.sub(r'\s*\(uuid "[^"]*"\)', "", via).replace(
        "(at 90.987 172.849)", "(at 91.287 172.849)")
    pcb = tmp_path / "core.kicad_pcb"
    pcb.write_text(text[:end] + "\n\t" + second + text[end:], encoding="utf-8")
    vs, facts = _core_pvdd(pcb)
    assert not _kinds(vs, "insufficient_transition_vias")
    # at a 2 A budget the two vias fall short of it, so the ripple is what passes
    vs, facts = _core_pvdd(pcb, current_a=2.0)
    assert [r for r in facts["ripple_caps"] if r["refs"] == ["C7"]
            and r["kind"] == "insufficient_transition_vias"
            and r["amps"] == 0.75 and r["vias"] == 2]
    assert not [v for v in _kinds(vs, "insufficient_transition_vias")
                if "C7" in v.get("refs", [])]


def test_decoupling_cap_stays_unjudged(tmp_path_factory):
    """Known answer 3 (#798): a 100 nF cap's tap stays unjudged and says why,
    even on a rail whose converter can be read."""
    bg = _board(tmp_path_factory, "decoup798", _cap_tap("100nF 50V X7R"))
    cons = {"blocks": [{"topology": "class-d-amp", "operating_point": {
        "bus_net": "PWR", "iout_a": 3, "duty": 0.5}}]}
    vs, facts = check_current.check_net(bg, STAR, cons=cons)
    assert _at(vs, (10.0, 2.0)) == []
    item = next(u for u in facts["unjudged"] if u["refs"] == ["C2"])
    assert "decoupling" in item["why"]


def test_bulk_cap_with_no_converter_stays_unjudged(tmp_path_factory):
    """#798: a bulk cap with no converter to read has no figure, so it is
    unjudged and says so. It no longer takes the rail's budget."""
    for cons in (None, {"voltages": [{"net": "PWR", "voltage": 12}]}):
        bg = _board(tmp_path_factory, "bulk798", _cap_tap("22uF 25V X5R"))
        vs, facts = check_current.check_net(bg, STAR, cons=cons)
        assert _at(vs, (10.0, 2.0)) == []
        item = next(u for u in facts["unjudged"] if u["refs"] == ["C2"])
        assert "no converter" in item["why"]


def test_planted_single_via_on_buck_input_cap_fails():
    """Known answer 4 (#798): the planted single via on usbbuck4's C13 fails
    at 2 * sqrt(0.66 * 0.34) = 0.95 A; the golden at 0.4 A does not."""
    golden = REPO / "tests" / "golden"
    def run(cons_path, pcb):
        cons = json.loads(cons_path.read_text(encoding="utf-8"))
        entry = next(e for e in cons["power"] if e["net"] == "VBUS")
        return check_current.check_net(geom.load_board(pcb), entry, cons=cons)
    mutant = golden / "mutants" / "buck-input-cap-single-via"
    vs, _ = run(mutant / "constraints.json", mutant / "usbbuck4.kicad_pcb")
    found = _at(vs, (104.0, 124.4))
    assert found and found[0]["refs"] == ["C13"]
    assert "0.95 A" in found[0]["msg"] and "ripple" in found[0]["msg"]
    cons = json.loads((golden / "usbbuck4" / "constraints.json").read_text(
        encoding="utf-8"))
    cons["voltages"] = [{"net": "VBUS", "voltage": 5},
                        {"net": "+3V3", "voltage": 3.3}]
    entry = next(e for e in cons["power"] if e["net"] == "VBUS")
    vs, _ = check_current.check_net(
        geom.load_board(golden / "usbbuck4" / "usbbuck4.kicad_pcb"), entry,
        cons=cons)
    assert _at(vs, (104.0, 124.4)) == []




def test_cap_track_is_held_to_ripple_above_the_budget(tmp_path_factory):
    """#798: ripple can pass the rail's budget. A strip wide enough for a 1 A
    rail but not for the 8 A a bus cap carries fails at the ripple, with the
    figure in its message; 1 A of ripple on the same strip passes."""
    bg = _board(tmp_path_factory, "ripw", _cap_tap("22uF 25V X5R"))
    entry = {"net": "PWR", "current_a": 1.0, "via_amps": 100.0}
    def cons(iout):
        return {"blocks": [{"topology": "class-d-amp", "operating_point": {
            "bus_net": "PWR", "iout_a": iout, "duty": 0.5}}]}
    vs, _ = check_current.check_net(bg, entry, cons=cons(16))
    found = [v for v in _kinds(vs, "undersized_track")
             if v.get("refs") == ["C2"] or "ripple" in v["msg"]]
    assert found and found[0]["severity"] == "error"
    assert "ripple 8.00 A" in found[0]["msg"]
    vs, _ = check_current.check_net(bg, entry, cons=cons(2))
    assert not [v for v in _kinds(vs, "undersized_track") if "ripple" in v["msg"]]



def _two_pad(ref, x, net1, net2, fn2=""):
    pf = f'(pinfunction "{fn2}")' if fn2 else ""
    return (f'  (footprint "t:{ref[0]}" (at {x} 8)\n    (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'    (pad "1" smd rect (at 0 0) (size 0.5 1) (layers "F.Cu") (net "{net1}"))\n'
            f'    (pad "2" smd rect (at 1 0) (size 0.5 1) (layers "F.Cu") (net "{net2}") {pf})\n'
            f'  )\n')


def _buck_on_pwr(tmp_path_factory, name):
    """C2's bulk tap on PWR, and U9 switching from PWR through L9 into OUT."""
    return _board(tmp_path_factory, name, _cap_tap("22uF")
                  + _two_pad("U9", 30, "PWR", "/SW", "SW")
                  + _two_pad("L9", 34, "/SW", "OUT"))


def _buck_cons(rail_v=45.0, blocks=()):
    return {"voltages": [{"net": "PWR", "voltage": rail_v},
                         {"net": "OUT", "voltage": 5},
                         {"net": "GND", "voltage": 0}],
            "power": [{"net": "OUT", "current_a": 0.65}],
            "blocks": list(blocks)}


INVERTER = {"topology": "half-bridge-inverter", "block": "bridge",
            "operating_point": {"vin_v": 28, "iout_a": 10}}


def test_undeclared_bus_block_leaves_the_rail_unjudged(tmp_path_factory):
    """PCB-0018: an inverter block with no bus_net draws amps from caps the
    buck's input figure knows nothing of, so 0.2 A of buck ripple must not
    clear them. A rail below the block's vin_v can't be its bus."""
    bg = _buck_on_pwr(tmp_path_factory, "unlinked")
    rip = check_current.cap_ripple(bg, "PWR", ["C2"], _buck_cons(blocks=[INVERTER]))
    assert "amps" not in rip
    assert "bridge" in rip["why"] and "bus_net" in rip["why"]
    rip = check_current.cap_ripple(bg, "PWR", ["C2"], _buck_cons(
        rail_v=12.0, blocks=[INVERTER]))
    assert rip["terms"][0]["from"] == "U9"
    linked = dict(INVERTER, operating_point={
        "vin_v": 28, "iout_a": 10, "duty": 0.5, "bus_net": "PWR"})
    rip = check_current.cap_ripple(bg, "PWR", ["C2"], _buck_cons(blocks=[linked]))
    assert [t["from"] for t in rip["terms"]] == ["bridge"]


def test_buck_input_duty_uses_the_block_vin(tmp_path_factory):
    """A declared rail voltage is its limit: PCB-0018's VM is 45 V for a 28 V
    buck. The block's vin_v gives the duty when one buck block has that Vout."""
    bg = _buck_on_pwr(tmp_path_factory, "blockvin")
    buck = {"topology": "buck", "operating_point": {"vin_v": 28, "vout_v": 5}}
    rip = check_current.cap_ripple(bg, "PWR", ["C2"], _buck_cons(blocks=[buck]))
    d = 5 / 28
    assert rip["amps"] == pytest.approx(0.65 * (d * (1 - d)) ** 0.5)
    assert rip["terms"][0]["inputs"]["vin_v"] == 28
    rip = check_current.cap_ripple(bg, "PWR", ["C2"], _buck_cons())
    assert rip["terms"][0]["inputs"]["vin_v"] == 45


def test_return_net_cap_reads_no_converter(tmp_path_factory):
    """A switcher's GND pad doesn't make it a buck from 0 V: PCB-0018 said
    "U201 isn't a buck from 0 V to 5 V"."""
    bg = _board(tmp_path_factory, "retnet", _cap_tap("22uF").replace(
        '"PWR"', '"GND"') + _two_pad("U9", 30, "GND", "/SW", "SW")
        + _two_pad("L9", 34, "/SW", "OUT"))
    rip = check_current.cap_ripple(bg, "GND", ["C2"], _buck_cons())
    assert rip["why"] == "no converter to read on GND"


# ---- #816: a declared bus figure, and a return-side cap at its rail's ripple

DECLARED = {"topology": "half-bridge-inverter", "block": "bridge",
            "operating_point": {"vin_v": 28, "iout_a": 10, "bus_net": "PWR",
                                "bus_irms_a": 6.5,
                                "bus_irms_source": "research/power.md:91"}}


def _declared(**changes):
    op = dict(DECLARED["operating_point"], **changes)
    return {"blocks": [dict(DECLARED, operating_point={
        k: v for k, v in op.items() if v is not None})]}


def test_declared_bus_figure_over_the_limit_fails(tmp_path_factory):
    """a777e's condition 4: 6.5 A on one via at 1 A fails, naming the via, the
    amps and the limit. The figure reads as declared, with its source, never as
    if computed."""
    bg = _board(tmp_path_factory, "decl", _cap_tap("22uF 25V X5R"))
    vs, _ = check_current.check_net(bg, STAR, cons=_declared())
    found, = _at(vs, (10.0, 2.0))
    assert found["severity"] == "error" and found["refs"] == ["C2"]
    assert found["required"] == 7
    assert "(10.00, 2.00)" in found["msg"] and "6.50 A needs 7" in found["msg"]
    assert ">= 1 via per 1.0 A" in found["msg"]
    assert "bus ripple 6.5 A RMS declared (research/power.md:91)" in found["msg"]
    assert "Iout" not in found["msg"]
    term, = found["ripple"]["terms"]
    assert term["declared"] is True and term["amps"] == 6.5
    assert term["source"] == "research/power.md:91"


def test_declared_bus_figure_under_the_limit_passes(tmp_path_factory):
    """The same via passes once one via carries 6.5 A, and the declared figure
    is still in facts for a reader."""
    bg = _board(tmp_path_factory, "declok", _cap_tap("22uF 25V X5R"))
    vs, facts = check_current.check_net(bg, dict(STAR, via_amps=7.0),
                                        cons=_declared())
    assert _at(vs, (10.0, 2.0)) == []
    row, = facts["declared_bus"]
    assert row["from"] == "bridge" and row["amps"] == 6.5


def test_declared_bus_figure_wins_over_duty(tmp_path_factory):
    """a777e's condition 2: with both, the declared value is used, and facts say
    what the duty formula would have given."""
    bg = _board(tmp_path_factory, "declduty", _cap_tap("22uF 25V X5R"))
    vs, facts = check_current.check_net(bg, STAR, cons=_declared(duty=0.5))
    term, = _at(vs, (10.0, 2.0))[0]["ripple"]["terms"]
    assert term["amps"] == 6.5
    assert term["duty_formula_a"] == pytest.approx(5.0)
    assert facts["declared_bus"][0]["duty_formula_a"] == pytest.approx(5.0)


@pytest.mark.parametrize("changes, words", [
    ({"bus_irms_a": -1.0}, "negative"),
    ({"bus_irms_a": 0}, "above 0"),
    ({"bus_irms_a": 10.5}, "above iout_a 10"),
    ({"bus_irms_a": "6.5"}, "a number"),
    ({"bus_irms_a": True}, "a number"),
    ({"iout_a": None}, "needs iout_a"),
    ({"bus_irms_source": None}, "bus_irms_source"),
    ({"bus_irms_source": " "}, "bus_irms_source"),
])
def test_declared_bus_figure_out_of_bounds_is_rejected(
        tmp_path_factory, changes, words):
    """a777e's condition 3: a figure that can't be right, or that nothing cites,
    judges nothing, and the why says so."""
    bg = _board(tmp_path_factory, "declbad", _cap_tap("22uF 25V X5R"))
    rip = check_current.cap_ripple(bg, "PWR", ["C2"], _declared(**changes))
    assert "amps" not in rip
    assert words in rip["why"] and "bridge" in rip["why"]


def _return_tap(value, rail="PWR"):
    """C2 from GND to `rail`, with its GND pad behind the one via at (10, 2)."""
    text = _cap_tap(value).replace('"PWR"', '"GND"')
    head = text.index('"Reference" "C2"')
    end = text.index("\n", text.index('(pad "1"', head)) - 1  # the footprint's ")"
    return (text[:end] + f'\n    (pad "2" smd rect (at 1 0) (size 1 1) '
            f'(layers "B.Cu") (net "{rail}"))' + text[end:])


RETURN = {"net": "GND", "current_a": 3.0, "via_amps": 1.0}


def _return_cons(*blocks, **volts):
    return {"voltages": [{"net": "GND", "voltage": 0}] + [
        {"net": n, "voltage": v} for n, v in volts.items()],
        "blocks": list(blocks)}


def test_return_cap_takes_the_ripple_of_its_rail(tmp_path_factory):
    """Known answer (b), #816: a cap's current goes in one pad and out the other,
    so its GND pad carries what its rail pad does."""
    bg = _board(tmp_path_factory, "retcap", _return_tap("22uF")
                + _two_pad("U9", 30, "PWR", "/SW", "SW")
                + _two_pad("L9", 34, "/SW", "OUT"))
    cons = _buck_cons()
    rail = check_current.cap_ripple(bg, "PWR", ["C2"], cons)
    back = check_current.cap_ripple(bg, "GND", ["C2"], cons)
    assert back["amps"] == pytest.approx(rail["amps"])
    assert [t["from"] for t in back["terms"]] == ["U9"]
    assert back["terms"][0]["rail"] == "PWR"


def test_return_cap_over_the_limit_fails_at_its_rail(tmp_path_factory):
    """The GND via of a cap on a 6.5 A bus fails like the rail's own via, as an
    error, and says which rail the figure is from."""
    bg = _board(tmp_path_factory, "retfail", _return_tap("22uF"))
    vs, _ = check_current.check_net(bg, RETURN, cons=_return_cons(DECLARED, PWR=45))
    found, = _at(vs, (10.0, 2.0))
    assert found["severity"] == "error" and found["refs"] == ["C2"]
    assert "6.50 A needs 7" in found["msg"]
    assert "return of PWR" in found["msg"]
    vs, _ = check_current.check_net(bg, dict(RETURN, via_amps=7.0),
                                    cons=_return_cons(DECLARED, PWR=45))
    assert _at(vs, (10.0, 2.0)) == []


def test_return_cap_of_an_unreadable_rail_says_which(tmp_path_factory):
    """No figure on the rail means none on its return, and the why names the
    rail rather than GND."""
    bg = _board(tmp_path_factory, "retwhy", _return_tap("22uF", rail="+3V3"))
    rip = check_current.cap_ripple(bg, "GND", ["C2"], _return_cons(**{"+3V3": 3.3}))
    assert "amps" not in rip
    assert "+3V3" in rip["why"] and "no converter" in rip["why"]


def test_return_cap_rails_add_in_rss(tmp_path_factory):
    """Caps of two rails behind one GND via: the rails add in RSS, as several
    converters do. A decoupling cap's rail adds no figure, and a cap between two
    returns has none of its own."""
    body = _return_tap("22uF") + _two_pad("C7", 30, "GND", "RAIL2") \
        + _two_pad("C8", 34, "GND", "+3V3") + _two_pad("C9", 38, "GND", "AGND")
    body = re.sub(r'("Reference" "C7" \(at 0 0 0\)\))',
                  r'\1\n    (property "Value" "47uF" (at 0 0 0))', body)
    body = re.sub(r'("Reference" "C8" \(at 0 0 0\)\))',
                  r'\1\n    (property "Value" "100nF" (at 0 0 0))', body)
    body = re.sub(r'("Reference" "C9" \(at 0 0 0\)\))',
                  r'\1\n    (property "Value" "22uF" (at 0 0 0))', body)
    bg = _board(tmp_path_factory, "retrss", body)
    two = dict(DECLARED, block="two", operating_point=dict(
        DECLARED["operating_point"], bus_net="RAIL2", bus_irms_a=2.0))
    cons = _return_cons(DECLARED, two, PWR=45, RAIL2=45, AGND=0, **{"+3V3": 3.3})
    rip = check_current.cap_ripple(bg, "GND", ["C2", "C7", "C8", "C9"], cons)
    assert rip["amps"] == pytest.approx((6.5 ** 2 + 2.0 ** 2) ** 0.5)
    assert {t["rail"] for t in rip["terms"]} == {"PWR", "RAIL2"}
    assert check_current.rail_ripple(bg, "GND", cons) == pytest.approx(6.5)
    rip = check_current.cap_ripple(bg, "GND", ["C9"], cons)
    assert rip["why"] == "no converter to read on GND"


def test_rail_ripple_of_a_return_skips_an_unreadable_rail(tmp_path_factory):
    """An LDO's cap on GND has no figure, and must not hide the bus's."""
    body = _return_tap("22uF") + _two_pad("C7", 30, "GND", "+3V3")
    body = re.sub(r'("Reference" "C7" \(at 0 0 0\)\))',
                  r'\1\n    (property "Value" "47uF" (at 0 0 0))', body)
    bg = _board(tmp_path_factory, "retskip", body)
    cons = _return_cons(DECLARED, PWR=45, **{"+3V3": 3.3})
    assert check_current.rail_ripple(bg, "GND", cons) == pytest.approx(6.5)


def test_return_net_with_no_declared_voltage_draws_nothing(tmp_path_factory):
    """Item 3, #816, as PCB-0017's B3 said it: "blocks [...] may draw from GND".
    No block draws from a return, declared 0 V or not."""
    bg = _board(tmp_path_factory, "retword", _return_tap("22uF"))
    rip = check_current.cap_ripple(bg, "GND", ["C2"], {
        "voltages": [{"net": "PWR", "voltage": 12}],
        "blocks": [dict(INVERTER, operating_point={"iout_a": 10})]})
    assert "draw from GND" not in rip["why"]
    assert "PWR" in rip["why"] and "bridge" in rip["why"]


def test_planted_inverter_bus_cap_fails(monkeypatch):
    """Known answer (c), #816: on the mutant, only the inverter's term can catch
    C13's one via. usbbuck4's own buck gives it 0.19 A, which one via carries,
    so dropping that term from the check loses the finding."""
    mutant = REPO / "tests" / "golden" / "mutants" / "inverter-bus-cap-single-via"
    cons = json.loads((mutant / "constraints.json").read_text(encoding="utf-8"))
    entry = next(e for e in cons["power"] if e["net"] == "VBUS")
    bg = geom.load_board(mutant / "usbbuck4.kicad_pcb")
    vs, _ = check_current.check_net(bg, entry, cons=cons)
    found, = _at(vs, (104.0, 124.4))
    assert found["refs"] == ["C13"] and "1.20 A" in found["msg"]
    monkeypatch.setattr(check_current, "_bus_terms", lambda net, blocks: [])
    vs, _ = check_current.check_net(bg, entry, cons=cons)
    assert _at(vs, (104.0, 124.4)) == []


def _bldc():
    """PCB-0018's VM entry and constraints with #816's inputs declared, whether
    or not the boards repo has them yet."""
    board = _boards.real_board("PCB-0018-A_bldc-motor-driver")
    cons = json.loads((board / "kicad" / "constraints.json").read_text(
        encoding="utf-8"))
    block = next(b for b in cons["blocks"] if b.get("block") == "power_stage")
    block["operating_point"].update(
        bus_net="VM", bus_irms_a=6.5, bus_irms_source="research/power.md:91")
    return (geom.load_board(next((board / "kicad").glob("*.kicad_pcb"))), cons)


def test_pcb0018_vm_caps_read_the_inverter_not_u201():
    """Known answer (a), #816: the bus caps are judged at the inverter's 6.5 A,
    the design's own figure, not at U201's input ripple."""
    bg, cons = _bldc()
    for ref in ("C401", "C410"):
        rip = check_current.cap_ripple(bg, "VM", [ref], cons)
        assert rip["amps"] == 6.5, ref
        assert [t["from"] for t in rip["terms"]] == ["power_stage"]
        assert "U201" not in str(rip["terms"])


def test_pcb0018_gnd_return_of_a_bus_cap_reads_the_inverter():
    """Known answer (b) on the real board: C401's GND side takes VM's figure."""
    bg, cons = _bldc()
    rip = check_current.cap_ripple(bg, "GND", ["C401"], cons)
    assert rip["amps"] == 6.5
    assert rip["terms"][0]["rail"] == "VM"


def _conn_board(tmp_path_factory, name, ref, ptype):
    pt = f' (pintype "{ptype}")' if ptype else ""
    body = f"""  (footprint "t:C" (at 2 5)
    (layer "F.Cu")
    (property "Reference" "{ref}" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "+5V"){pt}))
  (footprint "t:U" (at 12 5)
    (layer "F.Cu")
    (property "Reference" "U9" (at 0 0 0))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "+5V")))
"""
    return _board(tmp_path_factory, name, body)


def test_connector_source_is_decided_by_pin_type(tmp_path_factory):
    """#846: power_out makes any connector-like part a source; power_in makes
    a J header a load; a passive or untyped pin falls back to the prefix."""
    cases = [("USB1", "power_out", True), ("J1", "power_in", False),
             ("J1", "passive", True), ("J1", "", True), ("TB1", "", True),
             ("R5", "", False)]
    for i, (ref, ptype, want) in enumerate(cases):
        bg = _conn_board(tmp_path_factory, f"conn{i}", ref, ptype)
        assert check_current._is_connector(bg, ref, "+5V") is want, cases[i]
    bg = _conn_board(tmp_path_factory, "connin", "J1", "power_in")
    assert check_current._only_power_in(bg, "J1", "+5V")
    # J1 takes the rail's current: with nothing else a source, a stub that
    # feeds only J1 is not a trunk by the source rule
    assert check_current.stub_feeds(
        bg, "+5V", ["U9"], ({"J1"}, {"J1"}, set())) is True   # half the loads
    assert check_current.stub_feeds(
        bg, "+5V", ["J1"], ({"J1"}, {"J1"}, set())) is True
