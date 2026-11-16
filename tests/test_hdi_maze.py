"""#477: the route_finish maze on escaped BGAs, one small board per package.

Each fixture (tests/fixtures/hdi_maze, see its README) is a BGA already through
route_escape, with a connector to reach. The answers are what the maze does on
it with the escape left in place (--no-rip-board): completion, DRC errors and
warnings, and what is still unrouted. They are measured, not goals.

Warning counts move with KiCad (conftest's RECORDED_KICAD is older than the
10.0.6 these were measured on), so they are compared only on RECORDED; the
unconnected and error counts hold on any version. holes_co_located is left out
everywhere: on the same copper KiCad counts it by the order of the vias in the
file, and route_finish does not keep that order from run to run. Shuffling one
board's vias gave 8 to 16; its unchanged order 4, another run's 6.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import env  # noqa: E402
import route_finish as rf  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "hdi_maze"
RECORDED = "10.0.6"


def _need_tools():
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("kicad-cli or KRT not installed")


def _maze(tmp_path, case, *flags):
    for f in (FIXTURES / case).iterdir():
        shutil.copy(f, tmp_path / f.name)
    payload, _ = rf.run(["--pcb", str(tmp_path / "b.kicad_pcb"), *flags,
                         "--work-dir", str(tmp_path / "work"),
                         "--out-report", str(tmp_path / "r.json")])
    return payload


def _warnings(payload):
    found = {}
    for v in payload["violations"]:
        if v.get("severity") == "warning" and v["check"] != "holes_co_located":
            found[v["check"]] = found.get(v["check"], 0) + 1
    return found


# What the maze does on each fixture with --no-rip-board: the status counts of
# each pass, then the answers. Exact, so a route lost fails as well as one
# gained; test_maze_record_fails_on_a_lost_route keeps it so.
MAZES = {
    "tfbga54_0.8": dict(
        passes=[{"routed": 54}], before=54, after=0,
        warnings={"track_dangling": 31, "lib_footprint_issues": 2,
                  "silk_over_copper": 2}),
    "wlcsp49_0.4": dict(
        passes=[{"routed": 42, "failed": 7}, {"routed": 3, "failed": 4},
                {"failed": 4}],
        before=49, after=4,
        warnings={"track_dangling": 19, "silk_over_copper": 15,
                  "lib_footprint_issues": 2}),
    "fbga153_0.5": dict(
        passes=[{"routed": 93, "failed": 60}, {"routed": 9, "failed": 51},
                {"failed": 51}],
        before=153, after=51,
        warnings={"track_dangling": 111, "lib_footprint_issues": 2,
                  "silk_over_copper": 2}),
}


def _kicad_version():
    ver = subprocess.run([str(env.find_kicad_cli()), "version"],
                         capture_output=True, text=True).stdout
    return re.match(r"\d+\.\d+\.\d+", ver).group(0)


def _answers(payload, case, kicad):
    """Assert the payload matches MAZES[case]; warnings only on RECORDED."""
    want, f = MAZES[case], payload["facts"]
    assert [p["by_status"] for p in f["passes"]] == want["passes"]
    assert f["unconnected_before"] == want["before"]
    assert f["unconnected_after"] == want["after"]
    assert f["errors_after"] == {}
    if kicad == RECORDED:
        assert _warnings(payload) == want["warnings"]


@pytest.mark.parametrize("case,nets", [
    ("wlcsp49_0.4", 49), ("fbga153_0.5", 153), ("tfbga54_0.8", 54)])
def test_live_fixture_starts_escaped_with_every_net_open(tmp_path, case, nets):
    _need_tools()
    payload = _maze(tmp_path, case, "--measure-only")
    assert payload["facts"]["unconnected_left"] == nets
    assert payload["counts"]["by_severity"].get("error", 0) == nets


def test_live_tfbga54_maze_finishes(tmp_path):
    _need_tools()
    payload = _maze(tmp_path, "tfbga54_0.8", "--no-rip-board")
    _answers(payload, "tfbga54_0.8", _kicad_version())


@pytest.mark.smoke
def test_live_wlcsp49_maze_stops_at_4_of_49(tmp_path):
    """Smoke, not test-slow: 9 min on the loaded tower, one run. 91.8 % routed; pass
    3 gains nothing, so the maze stops there. The 4 left are real work for the
    escape or the router, not a goal. It was 41/3 routed and 5 left until
    main's routing work of 2026-11 (#1403); before the maze kept its vias
    out of their own net's SMD pads, 41/1/0 and 7 left."""
    _need_tools()
    payload = _maze(tmp_path, "wlcsp49_0.4", "--no-rip-board")
    _answers(payload, "wlcsp49_0.4", _kicad_version())


@pytest.mark.smoke
def test_live_fbga153_maze_stops_at_51_of_153(tmp_path):
    """Smoke, not test-slow: 55 min on the loaded tower, one run. 66.7 % routed;
    pass 3 gains nothing, so the maze stops there. The 51 left are real work for
    the escape or the router, not a goal. It was 85/6 routed and 62 left
    until main's routing work of 2026-11 (#1403)."""
    _need_tools()
    payload = _maze(tmp_path, "fbga153_0.5", "--no-rip-board")
    _answers(payload, "fbga153_0.5", _kicad_version())


def _recorded_payload(case):
    """A payload that is exactly MAZES[case]."""
    want = MAZES[case]
    return {"facts": {"passes": [{"by_status": dict(s)} for s in want["passes"]],
                      "unconnected_before": want["before"],
                      "unconnected_after": want["after"], "errors_after": {}},
            "violations": [{"check": c, "severity": "warning"}
                           for c, n in want["warnings"].items() for _ in range(n)]}


@pytest.mark.parametrize("case", sorted(MAZES))
def test_maze_record_fails_on_a_lost_route(case):
    """#1403: the live maze tests are smoke, so a loosened record would go
    unseen. The record itself passes; one route lost in pass 1, one more net
    left at the end, or one DRC error each fail, on any KiCad."""
    _answers(_recorded_payload(case), case, RECORDED)
    lost = _recorded_payload(case)
    first = lost["facts"]["passes"][0]["by_status"]
    first["routed"] -= 1
    first["failed"] = first.get("failed", 0) + 1
    more_left = _recorded_payload(case)
    more_left["facts"]["unconnected_after"] += 1
    error = _recorded_payload(case)
    error["facts"]["errors_after"] = {"clearance": 1}
    for bad in (lost, more_left, error):
        for kicad in (RECORDED, "9.0.0"):
            with pytest.raises(AssertionError):
                _answers(bad, case, kicad)


def _stub_board(tracks):
    """A stand-in for BoardGeom with just what stub_exits reads."""
    from shapely.geometry import LineString, box
    NS = SimpleNamespace
    return NS(
        tracks_of=lambda: [NS(net=net, layer=layer, shape=LineString(pts))
                           for net, layer, pts in tracks],
        vias_of=lambda: [NS(net="/V", at=(5.0, 1.0))],
        pads_of=lambda: [NS(net="/A", poly=box(-.1, -.1, .1, .1)),
                         NS(net="/B", poly=box(.4, -.1, .6, .1)),
                         NS(net="/V", poly=box(4.9, -.1, 5.1, .1))])


def test_stub_exits_finds_each_open_end_and_its_heading():
    """#784: stubs ending at one x,y on different layers each get their exit."""
    bg = _stub_board([("/A", "F.Cu", [(0, 0), (0, 1)]),
                      ("/B", "In1.Cu", [(.5, 0), (.5, 1)]),
                      ("/B", "In1.Cu", [(.5, 1), (1.5, 1)]),
                      ("/V", "F.Cu", [(5, 0), (5, 1)])])
    assert rf.stub_exits(bg) == {
        "/A": [("F.Cu", (0, 1), (0, 1))],
        "/B": [("In1.Cu", (1.5, 1), (1, 0))]}


def test_exit_keepouts_hold_only_other_nets_open_ends():
    """#784: a corridor past each open tip, on the tip's own layer, that a net's
    own route ignores because it is that net's copper."""
    exits = {"/A": [("F.Cu", (0, 1), (0, 1))], "/B": [("In1.Cu", (2, 1), (1, 0))]}
    items = rf.exit_keepouts(exits, open_nets=["/A", "/B"], routing="/A",
                             length=1.0, half=lambda net: 0.05)
    assert [(i.net, set(i.layers), list(i.shape.coords), i.half) for i in items] == [
        ("/B", {"In1.Cu"}, [(2, 1), (3, 1)], 0.05)]
    assert rf.exit_keepouts(exits, open_nets=[], routing="/A",
                            length=1.0, half=lambda net: 0.05) == []


def _small_grid(walls, **kw):
    """Two layers, 5 x 3 cells. Side A is the left column of layer 0, side B
    the right column of layer 1. walls: per layer, the columns blocked."""
    nx, ny = 5, 3

    def cells(pick):
        return bytes(int(pick(c % nx)) for c in range(nx * ny))

    empty = bytes(nx * ny)
    return rf.Grid(0.0, 0.0, nx, ny, 0.1, ["F.Cu", "B.Cu"],
                   [cells(lambda i: i in w) for w in walls],
                   kw.get("vblk", empty), (0.6, 0.3),
                   [[1.0] * (nx * ny)] * 2,
                   [cells(lambda i: i == 0), empty],
                   [empty, cells(lambda i: i == nx - 1)],
                   empty, empty, kw.get("span_moves", []))


@pytest.mark.parametrize("walls,kw,refuted", [
    (({2}, set()), {}, False),                         # a via past the wall
    (({2}, set()), {"vblk": bytes([1]) * 15}, True),   # no via fits anywhere
    (({2}, {2}), {}, True),                            # both layers walled
    (({0}, set()), {}, True),                          # side A is covered
])
def test_no_path_refutes_only_what_astar_cannot_route(walls, kw, refuted):
    """#784: the check before the search. It must agree with the search, or the
    maze would give up on a route it could have found."""
    gr = _small_grid(walls, **kw)
    assert rf.no_path(gr) is refuted
    assert (rf.astar(gr, (4, 1), via_cost=2.0) is None) is refuted


def test_no_path_follows_an_hdi_span():
    """#784: a span is a way between layers where a through via has no room."""
    span = SimpleNamespace(kind="micro")
    gr = _small_grid(({2}, set()), vblk=bytes([1]) * 15, span_moves=[
        [(1, span, bytes(15), [0, 1])], [(0, span, bytes(15), [0, 1])]])
    assert rf.no_path(gr) is False
    assert rf.astar(gr, (4, 1), via_cost=2.0)


def test_too_close_via_names_a_route_crowding_its_own_vias():
    """#784: TFBGA-54's /S6_7 took a microvia and a buried via 0.14 mm apart, and
    DRC rolled the whole route back on every pass."""
    span = SimpleNamespace(kind="micro", drill=0.1)
    # no span back to layer 0 at (1, 0), so merge_close_vias can't move the run
    no_move = bytes(1 if c == 1 else 0 for c in range(15))
    gr = _small_grid((set(), set()), span_moves=[
        [(1, span, bytes(15), [0, 1])], [(0, span, no_move, [0, 1])]])
    rules = SimpleNamespace(spans=[span], span_hole_to_hole=0.24, margin=0.02,
                            via_floor=(0.45, 0.2))
    near = [(0, 0, 0), (1, 0, 0), (1, 0, 1), (2, 1, 1), (2, 1, 0), (4, 1, 0)]
    assert rf.too_close_via(gr, near, rules) == (1, 7)
    far = [(0, 0, 0), (0, 0, 1), (4, 1, 1), (4, 1, 0)]
    assert rf.too_close_via(gr, far, rules) is None
    stacked = [(0, 0, 0), (1, 0, 0), (1, 0, 1), (1, 0, 0), (4, 1, 0)]
    assert rf.too_close_via(gr, stacked, rules) is None
