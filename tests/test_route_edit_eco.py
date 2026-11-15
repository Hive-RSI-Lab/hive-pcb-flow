"""U-ECO tests: the copper and footprint ECOs boards used to hand-script with
pcbnew (the hi-fi boards' rip*/add_*/renet/rename_net/pad_*/set_dnp scripts)
as route_edit / place_edit ops.

Pure tests (venv only) cover validation and the selector -> uuid resolution on
the frozen routed pd-trigger fixture. Smoke tests (SWIG bundled python) apply
them end to end: what landed, the byte-identical rollback when KiCad undoes a
renet, and place_edit's refusal to re-net a pad copper touches.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import place_edit  # noqa: E402
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

PD_DIR = REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"
PD_PCB = PD_DIR / "pd-trigger.kicad_pcb"


@pytest.fixture(scope="module")
def bg():
    return geom.BoardGeom.from_file(PD_PCB)


def _v(*ops):
    return route_edit.validate_ops({"version": 1, "ops": list(ops)})


# ------------------------------------------------------------ validation

@pytest.mark.parametrize("op, msg", [
    ({"op": "rip"}, "needs at least one of"),
    ({"op": "rip", "layer": "F.Cu"}, "needs at least one of"),
    ({"op": "rip", "net": []}, "net must be"),
    ({"op": "rip", "net": "A", "kind": "zone"}, "kind must be"),
    ({"op": "rip", "net": "A", "touch": "any"}, "touch goes with at"),
    ({"op": "rip", "net": "A", "radius": 1}, "radius goes with"),
    ({"op": "rip", "region": [5, 0, 1, 1]}, "region must be"),
    ({"op": "rip", "at": [1]}, "at must be"),
    ({"op": "rip", "net": "A", "bogus": 1}, "unknown keys"),
    ({"op": "renet", "net": "A"}, "missing ['to']"),
    ({"op": "add_path", "points": [[0, 0]], "width": 0.2, "layer": "F.Cu",
      "net": "A"}, "points must be"),
    ({"op": "rename_net", "from": "A", "to": "A"}, "same net"),
])
def test_validate_refuses(op, msg):
    with pytest.raises(CheckError, match=msg.replace("[", r"\[")
                       .replace("]", r"\]")):
        _v(op)


def test_validate_accepts_selectors():
    _v({"op": "rip", "net": ["A", "B"], "layer": "F.Cu"},
       {"op": "rip", "at": [1, 2], "radius": 0.1, "touch": "any"},
       {"op": "rip", "pads": ["U1", "R1.2"], "radius": 0.5},
       {"op": "renet", "uuid_prefix": "ab", "to": "GND"},
       {"op": "add_path", "points": [[0, 0], [1, 0], [1, 1]], "width": 0.2,
        "layer": "F.Cu", "net": "A"},
       {"op": "rename_net", "from": "A", "to": "B"})


def test_load_ops_inline_and_file(tmp_path):
    f = tmp_path / "ops.json"
    f.write_text(json.dumps({"version": 1, "ops": [
        {"op": "remove", "uuid": "x"}]}))
    ops = route_edit.load_ops(str(f), ['{"op": "rip", "net": "A"}',
                                       '[{"op": "rip", "net": "B"}]'])
    assert [o["op"] for o in ops] == ["remove", "rip", "rip"]
    with pytest.raises(CheckError, match="give --ops"):
        route_edit.load_ops(None, [])
    with pytest.raises(CheckError, match="not JSON"):
        route_edit.load_ops(None, ["{nope"])


def test_place_edit_validates_pad_and_dnp():
    ok = {"version": 1, "ops": [
        {"op": "set_pad", "ref": "R1", "pad": ["1", "2"], "net": ""},
        {"op": "set_pad", "ref": "R1", "pad": "1", "size": [0.7, 0.8]},
        {"op": "set_dnp", "ref": "R1", "dnp": True}]}
    assert len(place_edit.validate_ops(ok)) == 3
    for op, msg in (({"op": "set_pad", "ref": "R1", "pad": "1"},
                     "needs net, size and/or zone_connection"),
                    ({"op": "set_pad", "ref": "R1", "pad": 1, "net": "A"},
                     "pad must be"),
                    ({"op": "set_pad", "ref": "R1", "pad": "1", "size": 0},
                     "size must be"),
                    ({"op": "set_dnp", "ref": "R1", "dnp": "yes"},
                     "dnp must be")):
        with pytest.raises(CheckError, match=msg):
            place_edit.validate_ops({"version": 1, "ops": [op]})


# ------------------------------------------------------------ selectors

def test_select_by_net_layer_kind(bg):
    every = route_edit.select_items(bg, {"net": "GND"})
    tracks = route_edit.select_items(bg, {"net": "GND", "kind": "track"})
    vias = route_edit.select_items(bg, {"net": "GND", "kind": "via"})
    assert len(tracks) == len(bg.tracks_of(net="GND")) > 0
    assert len(vias) == len(bg.vias_of(net="GND")) > 0
    assert len(every) == len(tracks) + len(vias)
    f = route_edit.select_items(bg, {"net": "GND", "layer": "F.Cu"})
    assert f and all(isinstance(t, geom.Track) and t.layer == "F.Cu"
                     for t in f)
    two = route_edit.select_items(bg, {"net": ["GND", "/VDD"]})
    assert {t.net for t in two} == {"GND", "/VDD"}


def test_select_at_point_and_touch_any(bg):
    t = bg.tracks_of(net="/VIND")[0]
    c = list(t.shape.coords)
    hit = route_edit.select_items(bg, {"at": list(c[0])})
    assert t.uuid in {x.uuid for x in hit}
    assert all(x.net == "/VIND" for x in hit)
    mid = t.shape.interpolate(0.5, normalized=True)
    if t.length > 0.2:
        assert t.uuid not in {x.uuid for x in route_edit.select_items(
            bg, {"at": [mid.x, mid.y]})}
    assert t.uuid in {x.uuid for x in route_edit.select_items(
        bg, {"at": [mid.x, mid.y], "touch": "any"})}


def test_select_region_and_uuid_prefix(bg):
    t = bg.tracks_of(net="/CFG1")[0]
    x, y = list(t.shape.coords)[0]
    hit = route_edit.select_items(
        bg, {"region": [x - 0.01, y - 0.01, x + 0.01, y + 0.01]})
    assert t.uuid in {i.uuid for i in hit}
    assert route_edit.select_items(bg, {"region": [-9, -9, -8, -8]}) == []
    got = route_edit.select_items(bg, {"uuid_prefix": t.uuid[:12]})
    assert [i.uuid for i in got] == [t.uuid]


def test_select_pads_on_own_net_only(bg):
    hit = route_edit.select_items(bg, {"pads": ["R14.2"]})
    assert hit and all(i.net == "/VIND" for i in hit)
    assert route_edit.select_items(bg, {"pads": ["R14.2"],
                                        "net": "GND"}) == []
    whole = route_edit.select_items(bg, {"pads": ["R14"]})
    assert {i.uuid for i in hit} <= {i.uuid for i in whole}
    wide = route_edit.select_items(bg, {"pads": ["R14.2"], "radius": 5})
    assert len(wide) >= len(hit)


def test_resolve_ops_expands_and_maps_back(bg):
    ops = _v({"op": "rip", "net": "/CFG1"},
             {"op": "rip", "net": "/CFG1", "region": [-9, -9, -8, -8]},
             {"op": "add_path", "points": [[0, 0], [1, 0], [1, 0], [1, 1]],
              "width": 0.2, "layer": "F.Cu", "net": "GND"},
             {"op": "remove", "uuid": "u"})
    flat, spans = route_edit.resolve_ops(bg, ops)
    n = len(bg.tracks_of(net="/CFG1")) + len(bg.vias_of(net="/CFG1"))
    assert len(spans[0]) == n and spans[1] == []
    assert [flat[j]["op"] for j in spans[2]] == ["add_track"] * 2
    assert flat[spans[3][0]] == {"op": "remove", "uuid": "u"}


# ------------------------------------------------------------ smoke (KiCad)

@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")


@pytest.fixture
def pd(tmp_path):
    dst = tmp_path / "pdt"
    shutil.copytree(PD_DIR, dst)
    return dst / PD_PCB.name


def test_route_edit_ecos_apply(kicad, pd):
    before = geom.BoardGeom.from_file(pd)
    n_cfg1 = len(before.tracks_of(net="/CFG1")) \
        + len(before.vias_of(net="/CFG1"))
    payload, _ = route_edit.run([
        "--pcb", str(pd),
        "--op", '{"op": "rip", "net": "/CFG1"}',
        "--op", json.dumps({"op": "add_path", "net": "GND", "layer": "B.Cu",
                            "width": 0.3,
                            "points": [[10, 10], [12, 10], [12, 12]]}),
        "--op", '{"op": "rename_net", "from": "/HV_OK", "to": "/HV_GOOD"}'])
    rip, path, ren = payload["results"]
    assert rip["status"] == "removed" and rip["count"] == n_cfg1
    assert path == {"op": "add_path", "count": 2, "status": "added",
                    "segments": ["added", "added"]}
    assert ren["status"] == "renamed" and ren["count"] > 0
    after = geom.BoardGeom.from_file(pd)
    assert not after.tracks_of(net="/CFG1")
    assert not after.pads_of(net="/HV_OK")
    assert len(after.pads_of(net="/HV_GOOD")) \
        == len(before.pads_of(net="/HV_OK"))
    # a re-run finds nothing left to rip and the path already there
    again, _ = route_edit.run([
        "--pcb", str(pd), "--op", '{"op": "rip", "net": "/CFG1"}',
        "--op", json.dumps({"op": "add_path", "net": "GND", "layer": "B.Cu",
                            "width": 0.3, "points": [[10, 10], [12, 10]]})])
    assert again["results"][0]["status"] == "absent"
    assert again["results"][1]["segments"] == ["exists"]


def test_renet_of_pad_tied_copper_rolls_back(kicad, pd):
    """KiCad re-derives a pad-tied track's net from its pad on save; verify
    catches it and the board stays byte-identical."""
    raw = pd.read_bytes()
    with pytest.raises(CheckError, match="re-derives"):
        route_edit.apply_ops(pd, _v({"op": "renet", "pads": ["R14.2"],
                                     "to": "GND"}))
    assert pd.read_bytes() == raw


def test_remove_takes_a_zone_uuid(kicad, pd):
    """A superseded plane (one planes_gen left behind) is removed by its uuid
    like a track or via; before, `remove` looked only at tracks and vias and
    reported the zone absent, and the dead pour stayed on the board."""
    text = pd.read_text(encoding="utf-8")
    uuid = "7a436be0-2999-4f58-bc4c-bab947d6e41b"          # the VBUS pour
    assert uuid in text
    n = len(geom.BoardGeom.from_file(pd).zones_of(net="VBUS"))
    payload, _ = route_edit.run([
        "--pcb", str(pd), "--op", json.dumps({"op": "remove", "uuid": uuid})])
    assert payload["results"][0]["status"] == "removed"
    assert len(geom.BoardGeom.from_file(pd).zones_of(net="VBUS")) == n - 1
    assert uuid not in pd.read_text(encoding="utf-8")


def test_place_edit_pad_and_dnp(kicad, pd):
    payload, _ = place_edit.run([
        "--pcb", str(pd),
        "--op", '{"op": "set_pad", "ref": "U1", "pad": "10", "net": ""}',
        "--op", '{"op": "set_pad", "ref": "J2", "pad": "2", "size": 2.5}',
        "--op", '{"op": "set_dnp", "ref": "D2", "dnp": true}'])
    assert payload["verified"]
    bg = geom.BoardGeom.from_file(pd)
    assert {p.net for p in bg.pads_of(ref="U1") if p.number == "10"} \
        in ({None}, {""})
    assert [p.size for p in bg.pads_of(ref="J2") if p.number == "2"] \
        == [(2.5, 2.5)]
    assert "D2" in place_edit._dnp_refs(pd)


def test_place_edit_refuses_routed_pad_renet(kicad, pd):
    raw = pd.read_bytes()
    with pytest.raises(CheckError, match="rip them first"):
        place_edit.run(["--pcb", str(pd), "--op",
                        '{"op": "set_pad", "ref": "R14", "pad": "2", '
                        '"net": "GND"}'])
    assert pd.read_bytes() == raw


# ------------------------------------- PR #68's ECOs: widen, move_via, join,
# copy_from (hifi-receiver-2 core scripted them as widen.py, move_via.py,
# fix_dangle.py and set5v.py)

CC1_VIA = (21.9386, 39.0807)    # a /CC1 via with two straight tracks on it
CFG3_STUB = "8d4e7dc2"          # /CFG3, (33.2929, 30.6003)-(33.2071, 30.6861)


@pytest.mark.parametrize("op, msg", [
    ({"op": "set_width", "width": 0.3}, "needs at least one of"),
    ({"op": "set_width", "net": "A", "width": 0.3, "kind": "via"},
     "selects tracks only"),
    ({"op": "set_width", "net": "A", "width": 0}, "positive number"),
    ({"op": "move_via", "at": [1, 1], "to": "x"}, "to must be"),
    ({"op": "move_via", "at": [1, 1], "to": [2, 2], "drag": 1},
     "drag must be"),
    ({"op": "join_end", "at": [1, 1], "radius": 0}, "radius must be > 0"),
    ({"op": "copy_from", "pcb": "x.kicad_pcb"}, "needs at least one of"),
])
def test_validate_refuses_eco2(op, msg):
    with pytest.raises(CheckError, match=msg):
        _v(op)


def test_resolve_set_width_and_move_via(bg):
    flat, spans = route_edit.resolve_ops(bg, _v(
        {"op": "set_width", "net": "/CC1", "width": 0.35},
        {"op": "move_via", "at": list(CC1_VIA), "to": [22.0, 39.2]},
        {"op": "move_via", "at": list(CC1_VIA), "to": [22.0, 39.2],
         "drag": False},
        {"op": "move_via", "at": [1.0, 1.0], "to": [2.0, 2.0]}))
    widen = [flat[j] for j in spans[0]]
    assert widen and {f["op"] for f in widen} == {"set_width"}
    assert len(widen) == len([t for t in bg.tracks_of(net="/CC1")
                              if abs(t.width - 0.35) > 1e-3])
    move = [flat[j] for j in spans[1]]
    assert [f["which"] for f in move][0] == "at"
    assert sorted(f["which"] for f in move[1:]) in (
        ["end", "end"], ["end", "start"], ["start", "start"])
    assert len(spans[2]) == 1 and spans[3] == []


def test_resolve_join_end_and_copy_from(bg):
    stub = next(t for t in bg.tracks_of(net="/CFG3")
                if t.uuid.startswith(CFG3_STUB))
    a, b = list(stub.shape.coords)
    # the stub's end already sits on an anchor: nothing to add
    flat, _ = route_edit.resolve_ops(bg, _v({"op": "join_end",
                                             "at": list(a)}))
    assert flat == []
    with pytest.raises(CheckError, match="no track ends"):
        route_edit.resolve_ops(bg, _v({"op": "join_end", "at": [1, 1]}))
    flat, _ = route_edit.resolve_ops(bg, _v(
        {"op": "copy_from", "pcb": str(PD_PCB), "net": "/CFG1"}))
    assert len(flat) == len(bg.tracks_of(net="/CFG1")) \
        + len(bg.vias_of(net="/CFG1"))
    assert {f["op"] for f in flat} <= {"add_track", "add_via"}


def test_route_edit_eco2_apply(kicad, pd):
    """widen + move_via (dragging its tracks) + a rip and join_end that put
    a stub back + rip/copy_from that restore a net from the saved copy."""
    before = geom.BoardGeom.from_file(PD_PCB)
    stub = next(t for t in before.tracks_of(net="/CFG3")
                if t.uuid.startswith(CFG3_STUB))
    a, b = list(stub.shape.coords)
    payload, _ = route_edit.run([
        "--pcb", str(pd),
        "--op", '{"op": "set_width", "net": "/CC1", "width": 0.35}',
        "--op", json.dumps({"op": "move_via", "at": list(CC1_VIA),
                            "to": [22.0, 39.2]}),
        "--op", json.dumps({"op": "rip", "uuid_prefix": CFG3_STUB}),
        "--op", '{"op": "rip", "net": "/CFG1"}'])
    assert [r["status"] for r in payload["results"]] \
        == ["changed", "moved", "removed", "removed"]
    mid = geom.BoardGeom.from_file(pd)
    assert {round(t.width, 3) for t in mid.tracks_of(net="/CC1")} == {0.35}
    via = [v for v in mid.vias_of(net="/CC1")
           if abs(v.at[0] - 22.0) < 1e-3 and abs(v.at[1] - 39.2) < 1e-3]
    assert len(via) == 1
    on_via = [t for t in mid.tracks_of(net="/CC1")
              if any(abs(q[0] - 22.0) < 1e-3 and abs(q[1] - 39.2) < 1e-3
                     for q in (t.shape.coords[0], t.shape.coords[-1]))]
    assert len(on_via) == 2
    assert not mid.tracks_of(net="/CFG1")

    payload, _ = route_edit.run([
        "--pcb", str(pd),
        "--op", json.dumps({"op": "join_end", "at": list(b),
                            "radius": 0.2}),
        "--op", json.dumps({"op": "copy_from", "pcb": str(PD_PCB),
                            "net": "/CFG1"})])
    assert [r["status"] for r in payload["results"]] == ["added", "added"]
    after = geom.BoardGeom.from_file(pd)
    seg = lambda t: tuple(sorted(  # noqa: E731
        tuple(round(c, 3) for c in q) for q in t.shape.coords))
    assert seg(stub) in {seg(t) for t in after.tracks_of(net="/CFG3")}
    assert sorted(seg(t) for t in after.tracks_of(net="/CFG1")) \
        == sorted(seg(t) for t in before.tracks_of(net="/CFG1"))
    assert len(after.vias_of(net="/CFG1")) == len(before.vias_of(net="/CFG1"))



# ------------------------------------- set_point, public (#1140: part_repair
# cuts a dead tail back with it, so its replay file carries it)

@pytest.mark.parametrize("op, msg", [
    ({"op": "set_point", "uuid": "u", "which": "mid", "to": [1, 1]},
     "which must be start, end or at"),
    ({"op": "set_point", "uuid": "u", "which": "end", "to": "x"},
     "to must be"),
    ({"op": "set_point", "uuid": "u", "to": [1, 1]}, r"missing \['which'\]"),
    ({"op": "set_point", "uuid": "", "which": "at", "to": [1, 1]},
     "uuid must be"),
])
def test_validate_refuses_set_point(op, msg):
    with pytest.raises(CheckError, match=msg):
        _v(op)


def test_resolve_set_point_checks_the_item(bg):
    stub = next(t for t in bg.tracks_of(net="/CFG3")
                if t.uuid.startswith(CFG3_STUB))
    via = next(v for v in bg.vias_of(net="/CC1") if v.uuid)
    ok = [{"op": "set_point", "uuid": stub.uuid, "which": "end",
           "to": [33.25, 30.6432]},
          {"op": "set_point", "uuid": via.uuid, "which": "at",
           "to": [22.0, 39.2]}]
    flat, spans = route_edit.resolve_ops(bg, _v(*ok))
    assert flat == ok and spans == [[0], [1]]
    for op, msg in (({**ok[0], "which": "at"}, "which at on a track"),
                    ({**ok[1], "which": "start"}, "which start on a via"),
                    ({**ok[0], "uuid": "nope"}, "no track or via nope")):
        with pytest.raises(CheckError, match=msg):
            route_edit.resolve_ops(bg, _v(op))
    # an arc's end moved would bend it through its old mid point
    arc = SimpleNamespace(uuid="a1",
                          shape=LineString([(0, 0), (1, 1), (2, 0)]))
    fake = SimpleNamespace(vias_of=lambda: [], tracks_of=lambda: [arc])
    with pytest.raises(CheckError, match="is an arc"):
        route_edit.resolve_ops(fake, _v({"op": "set_point", "uuid": "a1",
                                         "which": "end", "to": [2, 1]}))


def test_route_edit_set_point_applies_and_replays(kicad, pd, tmp_path):
    """A track's end moves and its uuid stays; an ops file carries the op
    (the CLI validates it as part_repair's replay does), and a re-run
    finds it done."""
    before = geom.BoardGeom.from_file(pd)
    stub = next(t for t in before.tracks_of(net="/CFG3")
                if t.uuid.startswith(CFG3_STUB))
    a, b = list(stub.shape.coords)
    mid = [round((a[0] + b[0]) / 2, 4), round((a[1] + b[1]) / 2, 4)]
    f = tmp_path / "cut.route_edit.json"
    f.write_text(json.dumps({"version": 1, "ops": [
        {"op": "set_point", "uuid": stub.uuid, "which": "end", "to": mid}]}))
    for want in ("changed", "exists"):
        payload, _ = route_edit.run(["--pcb", str(pd), "--ops", str(f)])
        assert [r["status"] for r in payload["results"]] == [want]
        got = next(t for t in geom.BoardGeom.from_file(pd).tracks_of()
                   if t.uuid == stub.uuid)
        ends = list(got.shape.coords)
        assert len(ends) == 2 and all(
            abs(p - q) < 1e-6 for e, w in zip(ends, (a, mid))
            for p, q in zip(e, w))


def test_add_on_an_item_removed_in_the_same_list(kicad, pd, tmp_path):
    """A via and a track the list removes, and adds again on the same spot
    (part_repair's push rips a via the route then lays there again): the
    adds make new items and the run verifies, whichever comes first. The
    adds used to answer "exists" off the item the removal pass then took,
    so _verify rolled the run back. A re-run finds both done."""
    before = geom.BoardGeom.from_file(pd)
    via = next(v for v in before.vias_of(net="/CC1")
               if abs(v.at[0] - CC1_VIA[0]) < 1e-3
               and abs(v.at[1] - CC1_VIA[1]) < 1e-3)
    stub = next(t for t in before.tracks_of(net="/CFG3")
                if t.uuid.startswith(CFG3_STUB))
    a, b = list(stub.shape.coords)
    ops = [{"op": "remove", "uuid": via.uuid},
           {"op": "add_via", "at": list(via.at), "size": via.diameter,
            "drill": via.drill, "net": "/CC1"},
           {"op": "add_track", "start": list(a), "end": list(b),
            "width": stub.width, "layer": stub.layer, "net": "/CFG3"},
           {"op": "remove", "uuid": stub.uuid}]
    f = tmp_path / "again.route_edit.json"
    f.write_text(json.dumps({"version": 1, "ops": ops}))
    for want in (["removed", "added", "added", "removed"],
                 ["absent", "exists", "exists", "absent"]):
        payload, _ = route_edit.run(["--pcb", str(pd), "--ops", str(f)])
        assert [r["status"] for r in payload["results"]] == want
        text = pd.read_text(encoding="utf-8")
        assert via.uuid not in text and stub.uuid not in text
        after = geom.BoardGeom.from_file(pd)
        assert len(after.vias_of(net="/CC1")) \
            == len(before.vias_of(net="/CC1"))
        assert len([v for v in after.vias_of(net="/CC1")
                    if abs(v.at[0] - via.at[0]) < 1e-3
                    and abs(v.at[1] - via.at[1]) < 1e-3]) == 1
        assert len(after.tracks_of(net="/CFG3")) \
            == len(before.tracks_of(net="/CFG3"))


def test_place_edit_silk_clear_region(kicad, pd):
    """silk_clear with a region takes only the silk lying inside it
    (silk_rm.py: one end of U16's body outline)."""
    import placelib
    fp = placelib.PlaceModel(pd).footprints["U1"]
    x, y = fp.pos

    def silk(pcb):
        """U1's own graphic silk items."""
        import sexpdata
        for node in sexpdata.loads(pcb.read_text())[1:]:
            if isinstance(node, list) and str(node[0]) == "footprint" \
                    and ["property", "Reference", "U1"] in [
                        [str(x) for x in k[:3]] for k in node
                        if isinstance(k, list)]:
                return sum(1 for k in node if isinstance(k, list)
                           and str(k[0]) in ("fp_line", "fp_arc",
                                             "fp_circle", "fp_poly")
                           and "SilkS" in sexpdata.dumps(k))
        raise AssertionError("U1 not on board")
    total = silk(pd)
    assert total > 0
    payload, _ = place_edit.run([
        "--pcb", str(pd), "--op", json.dumps(
            {"op": "silk_clear", "ref": "U1",
             "region": [x, y - 10, x + 10, y + 10]})])
    left = silk(pd)
    assert 0 < left < total


# ------------------------------------------------------------ fill_vias

@pytest.mark.parametrize("op, msg", [
    ({"op": "fill_vias"}, "needs at least one of"),
    ({"op": "fill_vias", "net": "A", "kind": "track"}, "selects vias only"),
    ({"op": "fill_vias", "net": "A", "filled": "yes"}, "filled must be"),
])
def test_validate_refuses_fill_vias(op, msg):
    with pytest.raises(CheckError, match=msg):
        _v(op)


def test_resolve_fill_vias_picks_vias_only(bg):
    flat, spans = route_edit.resolve_ops(bg, _v(
        {"op": "fill_vias", "net": "/CC1"},
        {"op": "fill_vias", "net": "/CC1", "filled": False}))
    vias = [v.uuid for v in bg.vias_of(net="/CC1")]
    assert vias
    assert [f["uuid"] for f in (flat[j] for j in spans[0])] == vias
    assert {f["filled"] for f in (flat[j] for j in spans[0])} == {True}
    assert {flat[j]["filled"] for j in spans[1]} == {False}


def test_route_edit_fill_vias_apply(kicad, pd):
    """Per-via filling + capping (IPC-4761 Type VII): check_wicking's reader
    (which fab_export.via_fill_facts reuses) sees exactly those vias filled,
    a re-run changes nothing, and filled: false clears them."""
    before = geom.BoardGeom.from_file(pd)
    want = {v.uuid for v in before.vias_of(net="/CC1")}
    others = {v.uuid for v in before.vias_of()} - want
    payload, _ = route_edit.run(["--pcb", str(pd), "--op",
                                 '{"op": "fill_vias", "net": "/CC1"}'])
    r = payload["results"][0]
    assert r["status"] == "filled" and r["count"] == len(want)
    fills = route_edit._via_fills(pd)
    assert all(fills[u] == (True, True) for u in want)
    assert all(fills[u] == (False, False) for u in others)
    again, _ = route_edit.run(["--pcb", str(pd), "--op",
                               '{"op": "fill_vias", "net": "/CC1"}'])
    assert again["results"][0]["segments"] == ["exists"] * len(want)
    route_edit.run(["--pcb", str(pd), "--op",
                    '{"op": "fill_vias", "net": "/CC1", "filled": false}'])
    assert all(v == (False, False) for v in route_edit._via_fills(pd).values())
