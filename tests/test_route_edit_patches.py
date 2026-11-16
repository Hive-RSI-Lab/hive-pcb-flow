"""route_edit names (or takes) a PATCH_<net> zone whose vias it ripped (#1145).

zone_patch builds a PATCH_<net> zone around a via array; a via rip used to
leave the patch on the board with nothing to join, and nothing flagged it
(DRC, the fresh-fill check and part_repair all let an empty patch through).
Fixture: pinned core, PATCH_PVDD on F.Cu with its 11 PVDD vias.
  planted  rip all 11: the result names PATCH_PVDD ("patches_left"), and
           with "patches": true the zone goes too
  twin     rip the vias no track ends on (6 or more): the patch still holds
           a via of its net (the In4 corridor of the issue, still on a GND
           via) and is left alone; rip all 11 and add one back inside: the
           same
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point, box

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import geom  # noqa: E402
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

CORE = REPO / "pinned" / "core" / "kicad"
PCB = CORE / "core.kicad_pcb"
PVDD_BOX = box(53.6, 165.8, 59.6, 171.2)
PATCH = "0d93d1f6-8e21-4b83-8550-03803c10845a"     # PATCH_PVDD, F.Cu
NAMED = {"uuid": PATCH, "name": "PATCH_PVDD", "layer": "F.Cu"}


@pytest.fixture
def kicad():
    import env
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


@pytest.fixture(scope="module")
def bg():
    return geom.BoardGeom.from_file(PCB)


def _pvdd(bg) -> list:
    return [v for v in bg.vias_of(net="PVDD") if PVDD_BOX.contains(Point(v.at))]


def _loose(bg) -> list:
    ends = {(round(x, 3), round(y, 3)) for t in bg.tracks_of(net="PVDD")
            for x, y in (t.shape.coords[0], t.shape.coords[-1])}
    return [v for v in _pvdd(bg)
            if (round(v.at[0], 3), round(v.at[1], 3)) not in ends]


def _rm(vias) -> list[dict]:
    return [{"op": "remove", "uuid": v.uuid} for v in vias]


def _left(bg, flat) -> list:
    return [(j, z.uuid) for j, z in route_edit._patches_left(bg, flat)]


def test_planted_last_via_out_names_the_patch(bg):
    vias = _pvdd(bg)
    assert len(vias) == 11
    assert _left(bg, _rm(vias)) == [(0, PATCH)]
    # renet or move the last one out instead of removing it: the same
    rest = _rm(vias[:-1])
    last = vias[-1]
    assert _left(bg, rest + [{"op": "set_net", "uuid": last.uuid,
                              "net": "GND"}]) == [(0, PATCH)]
    assert _left(bg, rest + [{"op": "set_point", "uuid": last.uuid,
                              "which": "at", "to": [40.0, 160.0]}]) \
        == [(0, PATCH)]


def test_twin_a_via_left_or_added_keeps_the_patch(bg):
    loose = _loose(bg)
    assert 6 <= len(loose) < 11
    assert _left(bg, _rm(loose)) == []
    vias = _pvdd(bg)
    again = {"op": "add_via", "at": list(vias[0].at), "size": 0.6,
             "drill": 0.3, "net": "PVDD"}
    assert _left(bg, _rm(vias) + [again]) == []
    # a last via moved within the outline still holds it
    inside = [{"op": "set_point", "uuid": vias[-1].uuid, "which": "at",
               "to": [56.6, 168.5]}]
    assert _left(bg, _rm(vias[:-1]) + inside) == []
    # another net's via, and the patch removed by the list itself
    other = [v for v in bg.vias_of() if v.net != "PVDD"][:3]
    assert _left(bg, _rm(other)) == []
    assert _left(bg, _rm(vias) + [{"op": "remove", "uuid": PATCH}]) == []


def test_patches_must_be_a_bool():
    with pytest.raises(CheckError, match="patches must be true or false"):
        route_edit.validate_ops({"version": 1, "ops": [
            {"op": "rip", "net": "PVDD", "patches": "yes"}]})
    route_edit.validate_ops({"version": 1, "ops": [
        {"op": "remove", "uuid": "x", "patches": True}]})


def test_live_rip_names_then_takes_the_patch(kicad, tmp_path):
    work = tmp_path / "kicad"
    shutil.copytree(CORE, work)
    pcb = work / "core.kicad_pcb"
    rip = {"op": "rip", "net": "PVDD", "kind": "via",
           "region": [53.6, 165.8, 59.6, 171.2]}
    # report only: the vias go, the patch stays and is named
    res = route_edit.apply_ops(pcb, [rip])
    assert res[0]["count"] == 11 and res[0]["patches_left"] == [NAMED]
    names = {z.uuid for z in geom.BoardGeom.from_file(pcb).zones_of()}
    assert PATCH in names
    # the patch alone now: a remove of it takes it, nothing else is named
    res = route_edit.apply_ops(pcb, [{"op": "remove", "uuid": PATCH}])
    assert res == [{"op": "remove", "status": "removed"}]

    # patches: true on a fresh copy takes the zone with its last via
    shutil.rmtree(work)
    shutil.copytree(CORE, work)
    res = route_edit.apply_ops(pcb, [dict(rip, patches=True)])
    assert res[0]["patches_removed"] == [NAMED]
    assert "patches_left" not in res[0]
    after = geom.BoardGeom.from_file(pcb)
    assert PATCH not in {z.uuid for z in after.zones_of()}
    assert {z.name for z in after.zones_of() if z.name.startswith(
        "PATCH_")} == {"PATCH_VBUS", "PATCH_VBUS_IN"}
    # twin: the vias no track ends on go, the patch keeps the rest
    shutil.rmtree(work)
    shutil.copytree(CORE, work)
    bg = geom.BoardGeom.from_file(pcb)
    res = route_edit.apply_ops(pcb, [dict(r, patches=True)
                                     for r in _rm(_loose(bg))])
    assert all(set(r) == {"op", "status"} for r in res)
    assert PATCH in {z.uuid for z in geom.BoardGeom.from_file(pcb).zones_of()}
