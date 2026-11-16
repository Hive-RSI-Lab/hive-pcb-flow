"""padfanout / route_escape --pads (#929) on the golden QFN board
(tests/fixtures/padfanout, rebuilt by make_fixture.py).

The board crowds U1.2's (VIN) outward side with a C and a B via, so the
straight spot is taken and the fanout must find one off to the side. U1.11
(GND) already has a via 0.8 mm away. Pure tests check the chosen vias and
stubs with geometry written here, not the module's own checks; the live test
applies the ops with KiCad and wants no new DRC finding.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import padfanout  # noqa: E402
import route_escape  # noqa: E402
import route_finish as rf  # noqa: E402
from checklib import CheckError  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "padfanout"
PCB = FIX / "qfn.kicad_pcb"
PADS = ["U1.2", "U1.1", "U1.3", "U1.11", "U1.5"]


def _bg():
    return geom.BoardGeom.from_file(PCB)


def _board(tmp_path):
    for src in FIX.glob("qfn.*"):
        shutil.copy2(src, tmp_path / src.name)
    return tmp_path / "qfn.kicad_pcb"


@pytest.fixture(scope="module")
def fanned():
    bg = _bg()
    fo = padfanout.Fanout(bg)
    ops, rows = fo.fan(PADS)
    return bg, fo.rules, ops, {r["pad"]: r for r in rows}


def _pad(bg, spec):
    ref, num = spec.split(".")
    return next(p for p in bg.pads_of(ref=ref) if p.number == num)


def test_report_rows(fanned):
    _, _, ops, rows = fanned
    assert [rows[p]["result"] for p in PADS] == \
        ["via", "via", "via", "has via", "via"]
    assert len(ops) == 2 * 4                      # a stub and a via per pad


def test_crowded_pad_goes_to_the_side(fanned):
    """U1.2's straight-out spot (y = 10) is taken by C's via: the via sits
    off the pad's axis, outward of the pad (x < pad edge), never under U1."""
    bg, _, _, rows = fanned
    pad = _pad(bg, "U1.2")
    x, y = rows["U1.2"]["at"]
    assert abs(y - pad.center[1]) > 0.1
    assert x < pad.poly.bounds[0]


def test_never_inward():
    """Each pad's search directions exclude the one toward U1's centre."""
    bg = _bg()
    centre = bg.footprints["U1"]["at"]
    for p in bg.pads_of(ref="U1"):
        dirs = padfanout.Fanout.directions(p, centre)
        inward = (centre[0] - p.center[0], centre[1] - p.center[1])
        # the pad's long axis points at the centre: that direction is gone
        ax = (0 if abs(inward[0]) < 1e-6 else math.copysign(1, inward[0]),
              0 if abs(inward[1]) < 1e-6 else math.copysign(1, inward[1]))
        main = (int(ax[0]), 0) if abs(inward[0]) > abs(inward[1]) \
            else (0, int(ax[1]))
        assert main not in dirs and (-main[0], -main[1]) == dirs[0]


def test_vias_clear_every_other_net(fanned):
    """Independent check: each new via against every pad, via, track and
    other new via at the board's clearance, hole to hole and hole
    clearance."""
    bg, rules, ops, _ = fanned
    copper = [(p.net, p.poly, None) for p in bg.pads_of()]
    copper += [(v.net, v.poly, (v.at, v.drill)) for v in bg.vias_of()]
    copper += [(t.net, t.poly, None) for t in bg.tracks_of()]
    new = [o for o in ops if o["op"] == "add_via"]
    for o in ops:
        if o["op"] == "add_track":
            copper.append((o["net"], LineString([o["start"], o["end"]])
                           .buffer(o["width"] / 2), None))
        else:
            copper.append((o["net"], Point(o["at"]).buffer(o["size"] / 2),
                           (tuple(o["at"]), o["drill"])))
    for v in new:
        at = Point(v["at"])
        ring = at.buffer(v["size"] / 2)
        hole = at.buffer(v["drill"] / 2)
        for net, poly, drilled in copper:
            if drilled and tuple(drilled[0]) == tuple(v["at"]):
                continue                           # itself
            if drilled:
                gap = at.distance(Point(drilled[0])) \
                    - v["drill"] / 2 - drilled[1] / 2
                assert gap >= rules.hole_to_hole - 1e-6, (v, net)
            if net == v["net"]:
                continue
            assert ring.distance(poly) >= rules.clearance - 1e-6, (v, net)
            assert hole.distance(poly) >= rules.hole_clearance - 1e-6, (v, net)


def test_stubs_on_pad_layer_and_clear(fanned):
    bg, rules, ops, _ = fanned
    others = [(p.net, p.poly) for p in bg.pads_of() if "F.Cu" in p.layers]
    others += [(v.net, v.poly) for v in bg.vias_of()]
    others += [(o["net"], Point(o["at"]).buffer(o["size"] / 2))
               for o in ops if o["op"] == "add_via"]
    for o in ops:
        if o["op"] != "add_track":
            continue
        assert o["layer"] == "F.Cu"
        stub = LineString([o["start"], o["end"]]).buffer(o["width"] / 2)
        for net, poly in others:
            if net != o["net"]:
                assert stub.distance(poly) >= rules.clearance - 1e-6, (o, net)


def test_own_net_smd_keep(fanned):
    """No via drill within route_finish's via-to-own-SMD-pad keep."""
    bg, _, ops, _ = fanned
    for v in (o for o in ops if o["op"] == "add_via"):
        for p in bg.pads_of():
            if p.net == v["net"] and p.drill is None:
                assert p.poly.distance(Point(v["at"])) - v["drill"] / 2 \
                    >= rf.VIA_PAD_KEEP_MM - 1e-6


def test_bad_specs():
    fo = padfanout.Fanout(_bg())
    with pytest.raises(CheckError, match="want REF.PAD"):
        fo.fan(["U1"])
    with pytest.raises(CheckError, match="no such pad"):
        fo.fan(["U1.99"])
    with pytest.raises(CheckError, match="must be under"):
        padfanout.Fanout(_bg(), via=(0.3, 0.3))


def test_fan_via_parse_error(tmp_path):
    pcb = _board(tmp_path)
    with pytest.raises(CheckError, match="SIZE/DRILL"):
        route_escape.run(["--pcb", str(pcb), "--pads", "U1.2",
                          "--fan-via", "0.45"])


def test_plan_leaves_board(tmp_path):
    pcb = _board(tmp_path)
    before = pcb.read_bytes()
    out = tmp_path / "ops.json"
    payload, _ = route_escape.run(["--pcb", str(pcb), "--pads", "U1.2,U1.1",
                                   "--plan", "--out-ops", str(out)])
    assert pcb.read_bytes() == before
    assert payload["facts"]["board_updated"] is False
    assert len(json.loads(out.read_text())["ops"]) == 4


def test_no_spot_is_a_warning(tmp_path, monkeypatch):
    pcb = _board(tmp_path)
    monkeypatch.setattr(padfanout.Fanout, "spot", lambda *a: None)
    monkeypatch.setattr(padfanout.Fanout, "join", lambda *a: None)
    payload, _ = route_escape.run(["--pcb", str(pcb), "--pads", "U1.2",
                                   "--plan"])
    kinds = [v.get("kind") for v in payload["violations"]]
    assert "pad_fanout_incomplete" in kinds


@pytest.fixture(scope="module")
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def _drc(cli, pcb) -> list:
    """DRC findings other than unconnected and dangling items (the fixture's
    vias and stubs lead nowhere) and its stand-in footprint library
    warnings."""
    import kc
    return [v for v in kc.run_drc(cli, pcb)["violations"]
            if v["check"] not in ("lib_footprint_issues", "unconnected_items",
                                  "via_dangling", "track_dangling")]


def test_live_fanout_drc_clean(kicad, tmp_path):
    pcb = _board(tmp_path)
    assert _drc(kicad, pcb) == []
    payload, _ = route_escape.run(["--pcb", str(pcb), "--pads",
                                   ",".join(PADS)])
    assert payload["facts"]["board_updated"] is True
    assert pcb.read_text(encoding="utf-8").count("(via") == 3 + 4
    assert _drc(kicad, pcb) == []


# ------------------------------------------------- via in pad (#940)

VIP = FIX / "vip.kicad_pcb"


def _vip_board(tmp_path):
    for src in FIX.glob("vip.*"):
        shutil.copy2(src, tmp_path / src.name)
    return tmp_path / "vip.kicad_pcb"


def test_walled_pad_has_no_spot_without_via_in_pad():
    """The planted fault: U2.1 is walled in by net A on every side but the
    inward one, and VIN has no copper to join, so without the option the
    pad stays unfanned."""
    ops, rows = padfanout.Fanout(geom.BoardGeom.from_file(VIP)).fan(["U2.1"])
    assert ops == [] and rows[0]["result"] == "no spot"


def test_via_in_pad_at_the_pad_centre():
    bg = geom.BoardGeom.from_file(VIP)
    fo = padfanout.Fanout(bg, via_in_pad=True)
    ops, rows = fo.fan(["U2.1"])
    pad = _pad(bg, "U2.1")
    assert rows[0]["result"] == "via in pad"
    assert ops == [{"op": "add_via", "at": [8.0, 10.0], "size": fo.via[0],
                    "drill": fo.via[1], "net": "VIN"}]
    ring = Point(8.0, 10.0).buffer(fo.via[0] / 2)
    assert pad.poly.contains(ring)
    for t in bg.tracks_of(net="A"):                # clear of the wall
        assert ring.distance(t.poly) >= fo.rules.clearance - 1e-6


def test_via_in_pad_needs_a_pad_that_holds_the_via(monkeypatch):
    """U1's 0.7 x 0.25 mm pads can't hold a via: still `no spot`."""
    monkeypatch.setattr(padfanout.Fanout, "spot", lambda *a: None)
    monkeypatch.setattr(padfanout.Fanout, "join", lambda *a: None)
    ops, rows = padfanout.Fanout(_bg(), via_in_pad=True).fan(["U1.2"])
    assert ops == [] and rows[0]["result"] == "no spot"


def test_via_in_pad_refused_where_the_fab_cannot_fill(tmp_path):
    """qfn.kicad_pcb has two copper layers: the fab fills vias on 4+."""
    assert 2 not in route_escape.filled_via_layers()
    pcb = _board(tmp_path)
    before = pcb.read_bytes()
    with pytest.raises(CheckError, match="fills and caps vias only"):
        route_escape.run(["--pcb", str(pcb), "--pads", "U1.2",
                          "--via-in-pad"])
    assert pcb.read_bytes() == before


def test_via_in_pad_plan_writes_the_fill_after(tmp_path):
    pcb = _vip_board(tmp_path)
    out = tmp_path / "ops.json"
    payload, _ = route_escape.run(["--pcb", str(pcb), "--pads", "U2.1",
                                   "--via-in-pad", "--plan",
                                   "--out-ops", str(out)])
    got = json.loads(out.read_text())
    assert [o["op"] for o in got["ops"]] == ["add_via"]
    assert got["then"] == [{"op": "fill_vias", "kind": "via", "net": "VIN",
                            "at": [8.0, 10.0], "filled": True}]
    assert payload["facts"]["via_in_pad"] == 1
    assert payload["violations"] == []


def test_live_via_in_pad_filled_and_drc_clean(kicad, tmp_path):
    import fab_export
    pcb = _vip_board(tmp_path)
    assert _drc(kicad, pcb) == []
    payload, _ = route_escape.run(["--pcb", str(pcb), "--pads", "U2.1",
                                   "--via-in-pad"])
    assert payload["facts"]["board_updated"] is True
    f = fab_export.via_fill_facts(pcb)
    assert f["in_pad"] == [{"ref": "U2", "pad": "1", "at": [8.0, 10.0],
                            "filled": True}]
    assert _drc(kicad, pcb) == []


# ------------------------------------------------- plane balls (#956)

BGA = FIX / "bga.kicad_pcb"
PLANE = ["--plane-balls", "U3", "--plane-nets", "P,GND"]
IN_PAD = {"U3.A1", "U3.A4", "U3.B4", "U3.C1", "U3.C3", "U3.D1", "U3.D2",
          "U3.D4"}


def _bga_board(tmp_path):
    for src in FIX.glob("bga.*"):
        shutil.copy2(src, tmp_path / src.name)
    return tmp_path / "bga.kicad_pcb"


@pytest.fixture(scope="module")
def balls():
    bg = geom.BoardGeom.from_file(BGA)
    fo = padfanout.Fanout(bg, via_in_pad=True)
    specs = route_escape.plane_ball_specs(bg, ["U3"], ["P", "GND"])
    ops, rows = fo.fan(specs, in_pad_only=True)
    return bg, fo, ops, {r["pad"]: r for r in rows}


def test_plane_ball_specs_name_only_the_plane_nets():
    bg = geom.BoardGeom.from_file(BGA)
    specs = route_escape.plane_ball_specs(bg, ["U3"], ["P", "GND"])
    assert set(specs) == IN_PAD | {"U3.A3", "U3.B2"}
    assert {_pad(bg, s).net for s in specs} == {"P", "GND"}


def test_plane_ball_specs_refuse_a_typo():
    bg = geom.BoardGeom.from_file(BGA)
    with pytest.raises(CheckError, match="on no ball of U3"):
        route_escape.plane_ball_specs(bg, ["U3"], ["P", "+0V75"])
    with pytest.raises(CheckError, match="no such part"):
        route_escape.plane_ball_specs(bg, ["U9"], ["P"])


def test_each_plane_ball_gets_one_via_at_its_centre(balls):
    bg, fo, ops, rows = balls
    got = {r["pad"] for r in rows.values() if r["result"] == "via in pad"}
    assert got == IN_PAD
    assert all(o["op"] == "add_via" for o in ops)  # no stubs, no joins
    assert len(ops) == len(IN_PAD)
    for o in ops:
        pad = next(p for p in bg.pads_of(ref="U3")
                   if math.dist(p.center, o["at"]) < 1e-6)
        assert o["net"] == pad.net in ("P", "GND")
        # the floor via, not the board's common 0.6/0.3, its drill inside
        # the 0.2 mm ball
        assert (o["size"], o["drill"]) == fo.rules.via_floor == (0.254, 0.127)
        assert pad.poly.contains(Point(o["at"]).buffer(o["drill"] / 2))


def test_signal_balls_are_left_alone(balls):
    bg, _, ops, _ = balls
    sig = [p for p in bg.pads_of(ref="U3") if p.net.startswith("S")]
    for o in ops:
        assert all(not p.poly.buffer(0.01).contains(Point(o["at"]))
                   for p in sig)


def test_a_ball_with_its_own_via_is_skipped_not_its_neighbour(balls):
    """A3 holds a P via. B4 is 0.71 mm from it, inside the 0.9 mm `has via`
    reach of a pad fanout: a plane ball counts only a via on its own copper,
    so B4 still gets one."""
    _, _, _, rows = balls
    assert rows["U3.A3"]["result"] == "has via"
    assert rows["U3.B4"]["result"] == "via in pad"


def test_a_ball_with_no_room_is_reported(balls):
    """The planted fault: the S4 track between rows B and C would sit
    0.037 mm from a via in B2."""
    bg, fo, ops, rows = balls
    assert rows["U3.B2"]["result"] == "no spot"
    track = bg.tracks_of(net="S4")[0]
    b2 = _pad(bg, "U3.B2")
    ring = Point(b2.center).buffer(fo.ball_via[0] / 2)
    assert ring.distance(track.poly) < fo.rules.clearance
    for o in ops:                                  # every via placed clears
        r = Point(o["at"]).buffer(o["size"] / 2)
        assert r.distance(track.poly) >= fo.rules.clearance - 1e-6


def test_a_filled_ball_via_may_sit_on_its_own_nets_cap_pad(balls):
    """C1's GND pad on B.Cu lies under ball D1. A plain via there is kept
    off it (paste would drain down the hole); the ball's via is filled and
    capped, so the pad is no obstacle."""
    bg, _, _, rows = balls
    fo = padfanout.Fanout(bg, via_in_pad=True)     # before D1's own via
    d1 = _pad(bg, "U3.D1")
    cap = next(p for p in bg.pads_of(ref="C1"))
    assert cap.net == d1.net == "GND"
    assert Point(d1.center).within(cap.poly)
    assert not fo.via_ok(d1.center, "GND", own=d1, via=fo.ball_via)
    assert fo.via_ok(d1.center, "GND", own=d1, via=fo.ball_via, filled=True)
    assert rows["U3.D1"]["result"] == "via in pad"


def test_plane_balls_need_the_fab_option(tmp_path):
    with pytest.raises(CheckError, match="via_in_pad"):
        padfanout.Fanout(geom.BoardGeom.from_file(BGA)).fan(
            ["U3.A1"], in_pad_only=True)
    pcb = _bga_board(tmp_path)
    before = pcb.read_bytes()
    with pytest.raises(CheckError, match="add --via-in-pad"):
        route_escape.run(["--pcb", str(pcb), *PLANE])
    with pytest.raises(CheckError, match="go together"):
        route_escape.run(["--pcb", str(pcb), "--plane-balls", "U3",
                          "--via-in-pad"])
    assert pcb.read_bytes() == before


def test_plane_balls_plan_fills_each_and_warns_on_the_rest(tmp_path):
    pcb = _bga_board(tmp_path)
    out = tmp_path / "ops.json"
    payload, _ = route_escape.run(["--pcb", str(pcb), *PLANE, "--via-in-pad",
                                   "--plan", "--out-ops", str(out)])
    got = json.loads(out.read_text())
    assert len(got["ops"]) == len(got["then"]) == len(IN_PAD)
    assert all(t["op"] == "fill_vias" and t["filled"] for t in got["then"])
    assert payload["facts"]["via_in_pad"] == len(IN_PAD)
    assert [v["kind"] for v in payload["violations"]] == \
        ["pad_fanout_incomplete"]
    assert "U3.B2" in payload["violations"][0]["msg"]


def test_live_plane_balls_filled_and_drc_clean(kicad, tmp_path):
    import fab_export
    pcb = _bga_board(tmp_path)
    assert _drc(kicad, pcb) == []
    payload, _ = route_escape.run(["--pcb", str(pcb), *PLANE, "--via-in-pad"])
    assert payload["facts"]["board_updated"] is True
    f = fab_export.via_fill_facts(pcb)
    filled = {f"{v['ref']}.{v['pad']}" for v in f["in_pad"] if v["filled"]}
    assert filled == IN_PAD
    assert _drc(kicad, pcb) == []


def test_join_reaches_a_track_of_the_net(tmp_path, monkeypatch):
    """The planted fault: VIN has a track 1 mm out from U1.2 on F.Cu and no
    via spot is free, so the pad joins the track's end (join read geom's
    tracks as if they had .start/.end and raised AttributeError). Its quiet
    twin is test_walled_pad_has_no_spot_without_via_in_pad, where VIN has no
    copper to join."""
    import route_edit
    pcb = _board(tmp_path)
    route_edit.apply_ops(pcb, [{"op": "add_track", "start": [7.5, 10.0],
                                "end": [6.5, 10.0], "width": 0.2,
                                "layer": "F.Cu", "net": "VIN"}])
    monkeypatch.setattr(padfanout.Fanout, "spot", lambda *a: None)
    ops, rows = padfanout.Fanout(geom.BoardGeom.from_file(pcb)).fan(["U1.2"])
    assert rows[0]["result"] == "join" and rows[0]["to"] == [7.5, 10.0]
    (trk,) = ops
    assert (trk["op"], trk["start"], trk["end"], trk["net"]) == \
        ("add_track", [8.5, 10.0], [7.5, 10.0], "VIN")
