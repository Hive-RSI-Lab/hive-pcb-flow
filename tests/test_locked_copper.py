"""#664: locked copper survives route_finish and the route_auto import.

route_finish's board rip took locked tracks like any other (they were soft,
passable copper on the rip grid), its error rip took any DRC named, and its
dangling sweep took a locked stub. On the import side, KiCad's DSN export
folds a locked track and the unlocked one chained to it into one
`(type fix)` wire; Freerouting sends no fix wire back and KiCad's SES import
deletes every unlocked track, so the unlocked one was lost. These pure tests
plant a locked track or via beside an unlocked quiet twin: the locked one is
a wall and stays, the twin still rips; route_auto reads the DSN's fix
copper and puts back what the import took, as it was drawn.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import geom  # noqa: E402
import route_auto  # noqa: E402
import route_finish as rf  # noqa: E402
import routelib  # noqa: E402

HDR = """(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "+3V3") (net 4 "W")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
"""


def _lk(locked):
    return {True: " (locked yes)", False: "", "no": " (locked no)",
            "bare": " locked"}[locked]


def _seg(x1, y1, x2, y2, net, uuid, layer="F.Cu", w=0.25, locked=False):
    head = "(segment" + (_lk(locked) if locked == "bare" else "")
    tail = "" if locked == "bare" else _lk(locked)
    return (f'  {head} (start {x1} {y1}) (end {x2} {y2}) (width {w})'
            f' (layer "{layer}") (net "{net}"){tail} (uuid "{uuid}"))\n')


def _via(x, y, net, uuid, locked=False):
    return (f'  (via (at {x} {y}) (size 0.6) (drill 0.3)'
            f' (layers "F.Cu" "B.Cu") (net "{net}"){_lk(locked)}'
            f' (uuid "{uuid}"))\n')


def _pad(ref, x, y, net, size=1.0):
    return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n'
            f'    (pad "1" smd rect (at 0 0) (size {size} {size})'
            f' (layers "F.Cu") (net "{net}")))\n')


def _board(tmp_path, body, name="t"):
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(HDR + body + ")\n", encoding="utf-8")
    return p


def _pair(net, a, b):
    return {"net": net,
            "a": {"msg": f"Pad 1 [{net}] of U1 on F.Cu", "pos": list(a)},
            "b": {"msg": f"Pad 1 [{net}] of U2 on F.Cu", "pos": list(b)}}


def _plan(pcb, pairs, **kw):
    bg = geom.BoardGeom.from_file(pcb)
    rules = rf.board_rules(pcb, bg)
    return rf.plan_routes(bg, rules, pairs, bg.copper_layers, **kw)


# ------------------------------------------------------------------ parse

def test_locked_parses_in_both_spellings_and_no_is_unlocked(tmp_path):
    pcb = _board(tmp_path, (
        _seg(1, 1, 5, 1, "SIG", "t-yes", locked=True)
        + _seg(1, 2, 5, 2, "SIG", "t-bare", locked="bare")
        + _seg(1, 3, 5, 3, "SIG", "t-no", locked="no")
        + _seg(1, 4, 5, 4, "SIG", "t-plain")
        + _via(10, 10, "GND", "v-yes", locked=True)
        + _via(12, 10, "GND", "v-plain")))
    bg = geom.BoardGeom.from_file(pcb)
    lk = {x.uuid: x.locked for x in bg.tracks_of() + bg.vias_of()}
    assert lk == {"t-yes": True, "t-bare": True, "t-no": False,
                  "t-plain": False, "v-yes": True, "v-plain": False}
    assert rf.locked_uuids(bg) == {"t-yes", "t-bare", "v-yes"}


# ------------------------------------------------------------- the rips

def _walled(locked):
    body = _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
    body += _seg(15, 0.1, 15, 29.9, "+3V3", "u-wf", locked=locked)
    body += _seg(15, 0.1, 15, 29.9, "+3V3", "u-wb", "B.Cu", locked=locked)
    return body


def test_board_rip_leaves_a_locked_wall_and_rips_its_unlocked_twin(tmp_path):
    pairs = [_pair("SIG", (5, 15), (25, 15))]
    rip: list = []
    jobs = _plan(_board(tmp_path, _walled(False), "twin"), pairs,
                 rip_board=True, ripped_out=rip)
    assert jobs[0]["status"] == "routed"          # the quiet twin rips
    assert jobs[0]["ripped_nets"] == ["+3V3"] and len(rip) == 1
    rip = []
    jobs = _plan(_board(tmp_path, _walled(True), "locked"), pairs,
                 rip_board=True, ripped_out=rip)
    assert jobs[0]["status"] == "failed"          # a wall, not a price
    assert rip == [] and "ripped_nets" not in jobs[0]


def test_a_locked_via_by_an_unlocked_wall_stays_when_the_wall_rips(
        tmp_path):
    pcb = _board(tmp_path, _pad("U1", 5, 15, "SIG") + _pad("U2", 25, 15, "SIG")
                 + _seg(15, 0.1, 15, 29.9, "W", "u-wf")
                 + _seg(15, 0.1, 15, 29.9, "W", "u-wb", "B.Cu")
                 + _via(15, 18, "W", "v-lk", locked=True))
    rip: list = []
    jobs = _plan(pcb, [_pair("SIG", (5, 15), (25, 15))], rip_board=True,
                 ripped_out=rip)
    assert jobs[0]["status"] == "routed"
    assert jobs[0]["ripped_nets"] == ["W"]
    assert "v-lk" not in rip and rip


def _err(*items, check="shorting_items"):
    return {"check": check, "severity": "error",
            "items": [{"uuid": u, "msg": m} for u, m in items]}


def test_error_rip_keeps_locked_copper_and_takes_the_twin(tmp_path):
    pcb = _board(tmp_path, _via(10, 10, "GND", "v-lk", locked=True)
                 + _via(10.4, 10, "SIG", "v-twin")
                 + _seg(2, 5, 8, 5, "SIG", "t-lk", locked=True)
                 + _seg(2, 5.2, 8, 5.2, "GND", "t-twin"))
    drc = {"violations": [
        _err(("v-lk", "Via [GND] on F.Cu - B.Cu"),
             ("v-twin", "Via [SIG] on F.Cu - B.Cu")),
        _err(("t-lk", "Track [SIG] on F.Cu"),
             ("t-twin", "Track [GND] on F.Cu"), check="clearance")]}
    kept: list = []
    assert rf._error_copper(pcb, drc, kept=kept) == ["v-twin", "t-twin"]
    assert kept == ["v-lk", "t-lk"]


def test_dangling_sweep_treats_a_locked_stub_as_an_anchor():
    """sweep_dangling (route_finish's sweep and, since #1531, route_auto's
    stub trim) clears the uuid of locked copper before
    route_cleanup.find_dangling, which only removes what has a uuid."""
    import inspect
    src = inspect.getsource(rf.sweep_dangling)
    assert "locked_uuids(bg)" in src and "uuid=None" in src
    assert "sweep_dangling(" in inspect.getsource(rf._sweep_dangling)


# ------------------------------------------------------------ the import

DSN = """(pcb "t.dsn"
  (parser (string_quote ")) (space_in_quoted_tokens on))
  (resolution um 10)
  (unit um)
  (wiring
    (wire (path F.Cu 250  2000 -5000  8000 -5000  8000 -9000)
      (net "SIG") (type fix))
    (wire (path F.Cu 250  2000 -20000  9000 -20000) (net "SIG")
      (type route))
    (via "Via[0-1]_600:300_um" 10000 -10000 (net "GND") (type fix))
  )
)
"""


def test_dsn_fixed_copper_reads_fix_wires_and_vias_in_board_mm():
    fx = routelib.dsn_fixed_copper(DSN)
    assert fx["segs"] == [("F.Cu", (2.0, 5.0), (8.0, 5.0)),
                          ("F.Cu", (8.0, 5.0), (8.0, 9.0))]
    assert fx["vias"] == [(10.0, 10.0)]
    assert routelib.dsn_fixed_copper("(pcb x)") == {"segs": [], "vias": []}


def test_restore_puts_back_the_fix_track_the_import_took(tmp_path):
    # before the import: a locked track, the unlocked one KiCad's export
    # chained to it (one fix wire), and an unlocked track not on a fix wire
    pristine = _board(tmp_path, (
        _seg(2, 5, 8, 5, "SIG", "t-lk", locked=True)
        + _seg(8, 5, 8, 9, "SIG", "t-chain")
        + _seg(2, 20, 9, 20, "SIG", "t-free")), "pre")
    # after: KiCad kept the locked one, deleted the rest; Freerouting's
    # route came back under a new uuid for the free track only
    trial = _board(tmp_path, (
        _seg(2, 5, 8, 5, "SIG", "t-lk", locked=True)
        + _seg(2, 20, 9, 20, "SIG", "t-new")), "trial")
    fixed = routelib.dsn_fixed_copper(DSN)
    back = route_auto._restore_fixed(pristine, trial, fixed)
    assert [(d["uuid"], d["start"], d["end"]) for d in back] == [
        ("t-chain", [8.0, 5.0], [8.0, 9.0])]
    bg = geom.BoardGeom.from_file(trial)
    assert sorted(t.uuid for t in bg.tracks_of()) == [
        "t-chain", "t-lk", "t-new"]
    pre = route_auto._preexisting(geom.BoardGeom.from_file(pristine))
    assert route_auto._lost_preexisting(pre, bg) == []
    # a second pass finds nothing to do
    assert route_auto._restore_fixed(pristine, trial, fixed) == []


def test_restore_is_quiet_when_the_import_kept_everything(tmp_path):
    body = (_seg(2, 5, 8, 5, "SIG", "t-lk", locked=True)
            + _seg(8, 5, 8, 9, "SIG", "t-chain"))
    pristine = _board(tmp_path, body, "pre")
    # same copper, the chained one back under a new uuid: nothing lost
    trial = _board(tmp_path, body.replace("t-chain", "t-echo"), "trial")
    before = trial.read_text(encoding="utf-8")
    assert route_auto._restore_fixed(
        pristine, trial, routelib.dsn_fixed_copper(DSN)) == []
    assert trial.read_text(encoding="utf-8") == before
    # no fix copper in the DSN: nothing is even looked at
    assert route_auto._restore_fixed(pristine, trial,
                                     {"segs": [], "vias": []}) == []


def test_item_block_is_the_whole_item():
    text = HDR + _seg(8, 5, 8, 9, "SIG", "t-chain") + _via(
        1, 1, "GND", "v-1", locked=True) + ")\n"
    assert route_auto._item_block(text, "v-1").startswith("(via (at 1 1)")
    assert route_auto._item_block(text, "v-1").endswith('(uuid "v-1"))')
    assert route_auto._item_block(text, "t-chain").startswith("(segment")
    assert route_auto._item_block(text, "nope") is None
