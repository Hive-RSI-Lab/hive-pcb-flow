"""#655: the maze finisher must not rip copper placed before routing.

route_auto's finisher ripped every track and via DRC named in a copper
error, the designer's own vias included (bms-4s lost 36 of its 51 BQ25798
array vias), and reported a better count. route_auto now passes the board's
pre-routing copper, matched by geometry, as uuids the rip must leave; the
rip lists what it took and what it kept by net and position; and the run
ends with an audit that names any pre-routing via or track gone from the
routed board. Pure tests cover the matching, the audit and the rip filter;
the smoke ones run KiCad DRC and the finisher on the routed pd_trigger
fixture with a /HV_B track laid across two of its GND vias.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import geom  # noqa: E402
import route_auto  # noqa: E402
import route_finish as rf  # noqa: E402

ROUTE = REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"

HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
"""


def _via(x, y, net, uuid):
    return (f'  (via (at {x} {y}) (size 0.6) (drill 0.3)'
            f' (layers "F.Cu" "B.Cu") (net "{net}") (uuid "{uuid}"))\n')


def _seg(x1, y1, x2, y2, net, uuid, layer="F.Cu"):
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2)'
            f' (layer "{layer}") (net "{net}") (uuid "{uuid}"))\n')


def _bg(tmp_path, body, name="b"):
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(HDR + body + ")\n", encoding="utf-8")
    return p, geom.BoardGeom.from_file(p)


PRE = (_via(10, 10, "GND", "v-pre") + _via(12, 10, "GND", "v-pre2")
       + _seg(2, 5, 8, 5, "SIG", "t-pre"))


# ------------------------------------------------------------------ pure

def test_protect_matches_by_geometry_through_new_uuids(tmp_path):
    _, before = _bg(tmp_path, PRE, "before")
    pre = route_auto._preexisting(before)
    # the SES round trip: same copper under new uuids, the track split in
    # two at a hair off, and the router's own via and track beside it
    _, after = _bg(tmp_path, (
        _via(10.001, 10, "GND", "v-new") + _via(12, 10, "GND", "v-new2")
        + _seg(2, 5, 5, 5.001, "SIG", "t-a") + _seg(5, 5.001, 8, 5, "SIG", "t-b")
        + _via(20, 20, "GND", "v-router") + _seg(2, 5, 2, 9, "SIG", "t-router")
        + _via(14, 10, "SIG", "v-othernet")), "after")
    assert route_auto._protect_uuids(pre, after) == {
        "v-new", "v-new2", "t-a", "t-b"}
    assert route_auto._lost_preexisting(pre, after) == []


def test_audit_names_each_lost_via_and_track(tmp_path):
    _, before = _bg(tmp_path, PRE, "before")
    pre = route_auto._preexisting(before)
    # one via gone, the other's place taken by another net's via; the track
    # cut short on its layer and a copy on the wrong layer
    _, after = _bg(tmp_path, (
        _via(12, 10, "SIG", "v-sig") + _seg(2, 5, 6, 5, "SIG", "t-short")
        + _seg(6, 5, 8, 5, "SIG", "t-back", layer="B.Cu")), "after")
    assert route_auto._lost_preexisting(pre, after) == [
        {"kind": "via", "net": "GND", "at": [10.0, 10.0]},
        {"kind": "via", "net": "GND", "at": [12.0, 10.0]},
        {"kind": "track", "net": "SIG", "layer": "F.Cu",
         "start": [2.0, 5.0], "end": [8.0, 5.0]}]


def _err(*uuids_msgs, check="hole_to_hole"):
    return {"severity": "error", "check": check,
            "items": [{"uuid": u, "msg": m} for u, m in uuids_msgs]}


def test_error_rip_leaves_protected_copper_and_lists_it(tmp_path):
    pcb, _ = _bg(tmp_path, PRE + _via(10.4, 10, "SIG", "v-router"))
    drc = {"violations": [
        _err(("v-pre", "Via [GND] on F.Cu - B.Cu"),
             ("v-router", "Via [SIG] on F.Cu - B.Cu")),
        _err(("t-pre", "Track [SIG] on F.Cu"), ("v-pre", "Via [GND]"),
             check="clearance"),
        _err(("v-pre2", "Via [GND]"), check="silk_overlap")]}
    kept: list = []
    assert rf._error_copper(pcb, drc, protect={"v-pre", "t-pre"},
                            kept=kept) == ["v-router"]
    assert kept == ["v-pre", "t-pre"]
    # without protection (the #655 fault) the designer's copper goes too
    assert rf._error_copper(pcb, drc) == ["v-pre", "v-router", "t-pre"]
    bg = geom.BoardGeom.from_file(pcb)
    assert rf.describe_copper(bg, kept) == [
        {"kind": "via", "net": "GND", "at": [10.0, 10.0],
         "layers": ["F.Cu", "B.Cu"], "uuid": "v-pre"},
        {"kind": "track", "net": "SIG", "layer": "F.Cu", "start": [2.0, 5.0],
         "end": [8.0, 5.0], "uuid": "t-pre"}]


def test_route_auto_passes_its_copper_to_the_finisher():
    src = (REPO / "scripts" / "route_auto.py").read_text("utf-8")
    call = src[src.index("route_finish.finish_staged("):]
    call = call[:call.index(")\n")]
    assert "protect_uuids=_protect_uuids(pre," in call
    assert "lost = _lost_preexisting(pre, bg2)" in src


# ------------------------------------------------------------------ smoke

CROSS = ('\t(segment\n\t\t(start 12.6 45.8)\n\t\t(end 13.8 45.8)\n'
         '\t\t(width 0.2)\n\t\t(layer "B.Cu")\n\t\t(net "/HV_B")\n'
         '\t\t(uuid "0d0d0d0d-0000-4000-8000-000000000655")\n\t)\n')
GND_VIAS = [{"kind": "via", "net": "GND", "at": [13.2, 45.8]},
            {"kind": "via", "net": "GND", "at": [14.0, 45.8]}]


def _finish(tmp_path, protect: bool):
    import env
    import kc
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    work = tmp_path / "route"
    shutil.copytree(ROUTE, work)
    pcb = work / "pd-trigger.kicad_pcb"
    pre = route_auto._preexisting(geom.BoardGeom.from_file(pcb))
    # the router's stray: a /HV_B track over two of the board's GND vias
    text = pcb.read_text()
    pcb.write_text(text.replace("\t(segment\n", CROSS + "\t(segment\n", 1))
    drc = kc.run_drc(cli, pcb, all_track_errors=True)
    uuids = (route_auto._protect_uuids(pre, geom.BoardGeom.from_file(pcb))
             if protect else None)
    facts = rf.finish_staged(cli, pcb, drc, rip_errors=True, refill=True,
                             protect_uuids=uuids)
    bg = geom.BoardGeom.from_file(pcb)
    return pre, facts, bg


@pytest.mark.smoke
def test_quiet_protected_vias_stay_the_stray_goes(tmp_path):
    pre, facts, bg = _finish(tmp_path, protect=True)
    assert facts["rip_kept"] is True
    assert [(i["kind"], i["net"]) for i in facts["ripped_items"]] == [
        ("track", "/HV_B")]
    assert [{k: i[k] for k in ("kind", "net", "at")}
            for i in facts["protected_kept"]] == GND_VIAS
    assert len(bg.vias_of()) == len(pre["vias"]) == 44
    assert route_auto._lost_preexisting(pre, bg) == []
    assert sum(facts["errors_after"].values()) < sum(
        facts["errors_before"].values())


@pytest.mark.smoke
def test_fault_without_protection_the_audit_names_the_lost_vias(tmp_path):
    # the #655 behaviour, kept as the control: the rip takes the GND vias
    # with the stray, the error count still drops, and only the audit sees it
    pre, facts, bg = _finish(tmp_path, protect=False)
    assert facts["rip_kept"] is True
    assert ("via", "GND") in {(i["kind"], i["net"])
                              for i in facts["ripped_items"]}
    assert len(bg.vias_of()) == 42
    assert route_auto._lost_preexisting(pre, bg) == GND_VIAS
