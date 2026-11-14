"""part_repair: move, rotate or re-pad parts on a routed board and mend the
copper round them (rip, place, rejoin, gate).

The fast tests run the pieces on a synthetic part U1 with four pads in a row
and the copper shapes a rip has to tell apart: a chain through a via (net A),
a junction (B), a track that leaves the window (C) and a T onto a track's
middle (D). The live tests re-pad R6 on a copy of the routed pd_trigger
fixture: a renumber that swaps its two nets (applied, then replayed from the
op files on a fresh copy), a swap_fp to a larger package, and a join that
fails (nothing written, the board untouched).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import box

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import part_repair as pr  # noqa: E402
from checklib import CheckError  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "stages" / "pd_trigger"
ROUTE = FIX / "route"
R0805 = FIX / "lib" / "aiee.pretty" / "R0805.kicad_mod"
WIN = box(-5, -3, 6, 3)


def _seg(a, b, net, uid, layer="F.Cu", width=0.25):
    return (f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width '
            f'{width}) (layer "{layer}") (net "{net}") (uuid "{uid}"))\n')


def _board(tmp_path, body: str, name="b") -> Path:
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text('(kicad_pcb (version 20240108) (generator "t") '
                 '(general (thickness 1.6))\n(layers (0 "F.Cu" signal) '
                 '(31 "B.Cu" signal) (44 "Edge.Cuts" user))\n' + body + ")\n",
                 encoding="utf-8")
    return p


U1 = ('(footprint "T:U4" (layer "F.Cu") (at 0 0) (uuid "fU1")'
      ' (property "Reference" "U1")\n'
      + "".join(f' (pad "{n}" smd rect (at {x} 0) (size 1 1) (layers "F.Cu")'
                f' (net "{net}"))\n'
                for n, x, net in (("1", -2, "A"), ("2", 0, "B"),
                                  ("3", 2, "C"), ("4", 4, "D")))
      + ")\n")
COPPER = (
    # A: pad 1 -> a0 -> a1 -> via vA -> a2 -> a3 (a3 leaves the window)
    _seg((-2, 0), (-2, -2), "A", "a0") + _seg((-2, -2), (-3, -2), "A", "a1")
    + '(via (at -3 -2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
      ' (net "A") (uuid "vA"))\n'
    + _seg((-3, -2), (-4, -2), "A", "a2", "B.Cu")
    + _seg((-4, -2), (-6, -2), "A", "a3", "B.Cu")
    # B: pad 2 -> b0, which forks into b1 and b2
    + _seg((0, 0), (0, 2), "B", "b0") + _seg((0, 2), (0.6, 2), "B", "b1")
    + _seg((0, 2), (-0.6, 2), "B", "b2")
    # C: pad 3 -> c0 -> c1, which runs out of the window
    + _seg((2, 0), (2, 2.5), "C", "c0") + _seg((2, 2.5), (2, 4), "C", "c1")
    # D: pad 4 -> d0, which ends in the middle of d1
    + _seg((4, 0), (4, -2), "D", "d0") + _seg((3, -2), (5, -2), "D", "d1"))


@pytest.fixture
def u1(tmp_path):
    pcb = _board(tmp_path, U1 + COPPER)
    return pcb, geom.BoardGeom.from_file(pcb)


def _item(bg, uid):
    return next(it for it in bg.tracks_of() + bg.vias_of() if it.uuid == uid)


def _ripped(bg, uid, keep=()):
    rip = pr.Rip(bg, WIN, keep)
    rip.add(_item(bg, uid), "test")
    return set(rip.items)


def test_rip_follows_a_chain_to_the_first_stop(u1):
    _, bg = u1
    assert _ripped(bg, "a0") == {"a0", "a1"}      # stops at the via
    assert _ripped(bg, "b0") == {"b0"}            # a junction
    assert _ripped(bg, "c0") == {"c0"}            # c1 leaves the window
    assert _ripped(bg, "d0") == {"d0"}            # a T onto d1's middle
    # a via takes its tracks in the window on both layers, and their chains
    assert _ripped(bg, "vA") == {"vA", "a1", "a0", "a2"}


def test_rip_refuses_keep_nets_and_uuidless_copper(u1, tmp_path):
    _, bg = u1
    with pytest.raises(CheckError, match="A is --keep"):
        _ripped(bg, "a0", keep={"A"})
    pcb = _board(tmp_path, U1 + '(segment (start -2 0) (end -2 -2) (width'
                 ' 0.25) (layer "F.Cu") (net "A"))\n', name="nouuid")
    bg2 = geom.BoardGeom.from_file(pcb)
    with pytest.raises(CheckError, match="has no uuid"):
        pr.Rip(bg2, WIN, ()).add(bg2.tracks_of()[0], "test")


def test_seeds_are_copper_left_on_the_wrong_pads(u1):
    _, bg = u1
    old = {"U1": bg.pads_of(ref="U1")}
    assert pr.seeds(bg, old, old) == []
    turn = {"A": "B", "B": "C", "C": "D", "D": "A"}
    new = {"U1": [dataclasses.replace(p, net=turn[p.net]) for p in old["U1"]]}
    assert {it.uuid for it in pr.seeds(bg, old, new)} \
        == {"a0", "b0", "c0", "d0"}


def test_windows_cover_old_and_new_pads():
    old = {"U1": [SimpleNamespace(poly=box(0, 0, 1, 1))], "U2": []}
    new = {"U1": [SimpleNamespace(poly=box(5, 0, 6, 1))]}
    wins = pr.windows(old, new, 2)
    assert set(wins) == {"U1"}
    assert wins["U1"].bounds == (-2, -2, 8, 3)


def _v(source, check, net=None, severity="error"):
    return {"source": source, "check": check, "severity": severity,
            "net": net}


def test_gates_name_what_got_worse():
    clr = _v("drc", "clearance")
    unc = _v("unconnected", "unconnected_items", "A")
    drc0 = {"violations": [clr, unc]}
    assert pr.gates(drc0, drc0, {"A": [1, 1]}) == []
    assert pr.gates(drc0, {"violations": []}, {"A": [2, 1]}) == []
    assert pr.gates(drc0, drc0, {"A": [1, 1], "B": [1, 2]}) \
        == ["B: 1 -> 2 islands"]
    assert pr.gates(drc0, {"violations": [clr, clr, unc]}, {}) \
        == ["DRC drc:clearance: 1 -> 2"]
    moved = _v("unconnected", "unconnected_items", "B")
    assert pr.gates(drc0, {"violations": [clr, moved]}, {}) \
        == ["unconnected B: 0 -> 1"]
    assert pr.gates(drc0, {"violations": [clr, unc, moved]}, {}) \
        == ["unconnected B: 0 -> 1", "unconnected: 1 -> 2"]


def _vat(source, check, net, *pts, severity="error"):
    items = [{"msg": f"item {i}", "pos": list(pt), "uuid": f"u{i}-{pt[0]}"}
             for i, pt in enumerate(pts)]
    return {"source": source, "check": check, "severity": severity,
            "net": net, "layer": "F.Cu", "msg": f"{check} here",
            "pos": items[0]["pos"] if items else None, "items": items}


def test_new_violations_name_what_the_gate_counts():
    old = _vat("drc", "track_dangling", "A", (1, 1))
    clr = _vat("drc", "clearance", "A", (2, 2), (2, 3))
    unc = _vat("unconnected", "unconnected_items", "A", (0, 0), (0, 1))
    drc0 = {"violations": [old, clr, unc]}
    # the same violations, one moved by less than 1e-3: none is new
    nudged = _vat("drc", "track_dangling", "A", (1.0004, 1))
    assert pr.new_violations(drc0, {"violations": [nudged, clr, unc]}) == []
    # a check whose count did not grow is not listed, though one moved
    moved = _vat("drc", "clearance", "A", (2, 2.5), (2, 3))
    assert pr.new_violations(drc0, {"violations": [old, moved, unc]}) == []
    new = _vat("drc", "track_dangling", "B", (5, 5))
    warn = _vat("drc", "silk_overlap", None, (3, 3), severity="warning")
    excl = _vat("drc", "clearance", "C", (9, 9), severity="exclusion")
    unc_b = _vat("unconnected", "unconnected_items", "B", (4, 4), (4, 5))
    drc1 = {"violations": [old, new, clr, warn, excl, unc, unc_b]}
    got = pr.new_violations(drc0, drc1)
    assert [(v["key"], v["net"]) for v in got] == [
        ("drc:track_dangling", "B"), ("drc:silk_overlap", None),
        ("unconnected", "B")]
    assert got[0]["items"] == [{"msg": "item 0", "pos": [5, 5],
                                "uuid": "u0-5"}]
    # every line the gate writes has its violations here
    assert pr.gates(drc0, drc1, {}) == [
        "DRC drc:silk_overlap: 0 -> 1", "DRC drc:track_dangling: 1 -> 2",
        "unconnected B: 0 -> 1", "unconnected: 1 -> 2"]

    # REPORT.md lists them: where, net, the items' uuids, and a cut
    p = {"status": "violations", "board": "b.kicad_pcb", "refs": ["U1"],
         "ops": 1, "applied": False, "worse": pr.gates(drc0, drc1, {}),
         "new_violations": got}
    md = pr.report_md(p)
    assert "- drc:track_dangling at (5.000, 5.000) on F.Cu, net B:" \
        " track_dangling here (items u0-5)" in md
    assert md.index("Worse than before") < md.index("New violations")
    p["new_violations"] = got[:1] * (pr.SHOWN + 2)
    md = pr.report_md(p)
    assert md.count("- drc:track_dangling at") == pr.SHOWN
    assert "- and 2 more in part_repair.json" in md


def test_dangling_prunes_only_free_copper_in_the_window(u1):
    pcb, bg = u1
    nets = {"B", "C"}
    assert set(pr._dangling(pcb, bg, nets, win=WIN)) == {"b0", "b1", "b2"}
    # with no window, c1 (outside it) may go, and c0 behind it
    assert set(pr._dangling(pcb, bg, nets)) \
        == {"b0", "b1", "b2", "c0", "c1"}
    # an anchor stays and holds what leads to it
    assert set(pr._dangling(pcb, bg, nets, win=WIN, anchors={"b2"})) == {"b1"}
    # so does copper that meets a new route
    add = {"op": "add_track", "net": "B", "layer": "F.Cu", "width": 0.25,
           "start": [-0.3, 1.5], "end": [-0.3, 2.5]}
    assert set(pr._dangling(pcb, bg, nets, win=WIN, adds=[add])) == {"b1"}
    # copper that is gone leaves what it held free
    assert set(pr._dangling(pcb, bg, nets, win=WIN, gone={"c1"})) \
        == {"b0", "b1", "b2", "c0"}


def _add(net, a, b):
    return {"op": "add_track", "net": net, "layer": "F.Cu", "width": 0.25,
            "start": list(a), "end": list(b)}


def test_dangling_prunes_the_stubs_behind_a_new_junction(u1):
    """A route that lands where b0 forks meets b1 and b2 at that end only,
    and b0 holds it there to pad 2: the two stubs go, b0 stays."""
    pcb, bg = u1
    add = _add("B", (0, 2), (0, 2.8))
    assert set(pr._dangling(pcb, bg, {"B"}, win=WIN, adds=[add])) \
        == {"b1", "b2"}


def test_dangling_keeps_the_copper_a_new_route_hangs_on(u1, tmp_path):
    """A route that lands on b1's free end alone hangs on b1, so b1 and b0
    stay. One that lands where d1 ends at a dead stub d2 hangs on d1 (d0's
    T onto it leads to pad 4): once d2 would go too, nothing at that end
    holds the route, so d1 and d0 stay (and d2 with them)."""
    pcb, bg = u1
    add = _add("B", (0.6, 2), (0.6, 2.8))
    assert set(pr._dangling(pcb, bg, {"B"}, win=WIN, adds=[add])) == {"b2"}
    pcb2 = _board(tmp_path, U1 + COPPER
                  + _seg((5, -2), (5.5, -2.5), "D", "d2"), name="d2")
    bg2 = geom.BoardGeom.from_file(pcb2)
    assert set(pr._dangling(pcb2, bg2, {"D"}, win=WIN)) \
        == {"d0", "d1", "d2"}
    add = _add("D", (5, -2), (5, -2.8))
    assert pr._dangling(pcb2, bg2, {"D"}, win=WIN, adds=[add]) == []


PATCH_A = ('(zone (net "A") (net_name "A") (layer "F.Cu") (uuid "zA")'
           ' (name "PATCH_A")\n (polygon (pts (xy -3 -1) (xy -1 -1) (xy -1 1)'
           ' (xy -3 1)))\n (filled_polygon (layer "F.Cu") (pts (xy -3 -1)'
           ' (xy -1 -1) (xy -1 1) (xy -3 1))))\n')


def test_patch_notes_name_a_patch_the_move_leaves_behind(tmp_path):
    bg = geom.BoardGeom.from_file(_board(tmp_path, U1 + PATCH_A))
    old = {"U1": bg.pads_of(ref="U1")}
    assert pr.patch_notes(bg, old, old) == []
    away = {"U1": [dataclasses.replace(p, center=(p.center[0] + 10,
                                                  p.center[1]))
                   for p in old["U1"]]}
    notes = pr.patch_notes(bg, old, away)
    assert len(notes) == 1
    assert notes[0].startswith("zone PATCH_A on F.Cu met U1.1")
    assert "zone_patch --net A --layer F.Cu" in notes[0]


def test_load_ops_takes_footprint_ops_only(tmp_path, monkeypatch):
    with pytest.raises(CheckError, match="footprint ops only"):
        pr.load_ops(None, [json.dumps({"op": "set_dnp", "ref": "R6",
                                       "dnp": True})])
    monkeypatch.chdir(tmp_path)
    ops = pr.load_ops(None, [json.dumps(
        {"op": "swap_fp", "ref": "R6", "footprint": "lib/R.kicad_mod"})])
    assert ops[0]["footprint"] == str((tmp_path / "lib" / "R.kicad_mod")
                                      .resolve())


def test_run_refuses_bad_arguments(tmp_path):
    pcb = _board(tmp_path, U1)
    op = json.dumps({"op": "rotate", "ref": "U1", "deg": 90})
    with pytest.raises(CheckError, match="--margin must be more than 0"):
        pr.run(["--pcb", str(pcb), "--op", op, "--out", str(tmp_path / "o"),
                "--margin", "0"])
    for out in (tmp_path, tmp_path.parent):
        with pytest.raises(CheckError, match="directory of its own"):
            pr.run(["--pcb", str(pcb), "--op", op, "--out", str(out)])
    with pytest.raises(CheckError, match="board not found"):
        pr.run(["--pcb", str(tmp_path / "none.kicad_pcb"), "--op", op,
                "--out", str(tmp_path / "o")])


# ------------------------------------------------------------------ live

@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


RENUMBER = {"op": "renumber", "ref": "R6",
            "pads": [{"at": [26.25, 50.3], "number": "2"},
                     {"at": [27.75, 50.3], "number": "1"}]}


def _copy(tmp_path, name) -> Path:
    work = tmp_path / name
    shutil.copytree(ROUTE, work)
    return work / "pd-trigger.kicad_pcb"


def _pads(pcb, ref="R6"):
    return sorted((p.number, p.net, round(p.center[0], 3))
                  for p in geom.BoardGeom.from_file(pcb).pads_of(ref=ref))


def _copper(pcb):
    bg = geom.BoardGeom.from_file(pcb)
    tracks = sorted((t.net, t.layer, round(t.width, 4), tuple(
        (round(x, 4), round(y, 4)) for x, y in t.shape.coords))
        for t in bg.tracks_of())
    vias = sorted((v.net, round(v.at[0], 4), round(v.at[1], 4),
                   round(v.diameter, 4)) for v in bg.vias_of())
    pads = sorted((p.ref, p.number, p.net or "", round(p.center[0], 4),
                   round(p.center[1], 4)) for p in bg.pads_of())
    return tracks, vias, pads


def test_live_renumber_r6_applies_and_replays(kicad, tmp_path):
    import place_edit
    import route_edit
    pcb = _copy(tmp_path, "kicad")
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 0
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "pass" and p["applied"]
    assert p["ripped"]["tracks"] > 0
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]
    assert ("2", "/HV_B", 26.25) in _pads(pcb)
    assert ("1", "/ZBIAS", 27.75) in _pads(pcb)
    assert p["op_files"] == [pr.RIP_FILE, pr.PLACE_FILE, pr.ROUTE_FILE]

    # the op files, replayed on a fresh copy, make the same copper
    fresh = _copy(tmp_path, "fresh")
    assert route_edit.main(["--pcb", str(fresh),
                            "--ops", str(out / pr.RIP_FILE)]) == 0
    assert place_edit.main(["--pcb", str(fresh), "--ops",
                            str(out / pr.PLACE_FILE), "--allow-routed"]) == 0
    assert route_edit.main(["--pcb", str(fresh), "--ops",
                            str(out / pr.ROUTE_FILE), "--refill"]) == 0
    assert _copper(fresh) == _copper(pcb)


def test_live_swap_fp_r6_to_0805_leaves_the_board(kicad, tmp_path):
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    op = {"op": "swap_fp", "ref": "R6", "footprint": str(R0805),
          "fpid": "aiee:R0805"}
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(op),
                    "--out", str(out)]) == 0
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "pass" and not p["applied"]
    assert pcb.read_bytes() == before
    assert _pads(out / "work" / pcb.name) \
        == [("1", "/ZBIAS", 26.0), ("2", "/HV_B", 28.0)]
    assert (out / "REPORT.md").read_text(encoding="utf-8") \
        .startswith("# part_repair: pass")
    assert all(b <= a for a, b in p["islands"].values()), p["islands"]


def test_live_a_net_that_will_not_join_writes_nothing(kicad, tmp_path,
                                                      monkeypatch):
    def no_way(router, net, *a, **k):
        raise CheckError(f"{net}: no way through (test)")
    monkeypatch.setattr(pr, "join_net", no_way)
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 1
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "violations" and not p["applied"]
    assert p["failed"] and all("no way through" in f for f in p["failed"])
    assert pcb.read_bytes() == before
    assert not any((out / f).exists()
                   for f in (pr.RIP_FILE, pr.PLACE_FILE, pr.ROUTE_FILE))
    assert "Would not join" in (out / "REPORT.md").read_text(
        encoding="utf-8")


def test_live_a_worse_board_writes_nothing_and_names_why(kicad, tmp_path,
                                                         monkeypatch):
    fake = {"source": "drc", "check": "track_dangling", "severity": "warning",
            "pos": [27.0, 50.3], "layer": "F.Cu", "net": "/HV_B",
            "msg": "Track has an unconnected end",
            "items": [{"msg": "Track [/HV_B] on F.Cu", "pos": [27.0, 50.3],
                       "uuid": "test-uuid"}]}
    real = pr.gates

    def gates(drc0, drc1, isl):  # the repaired board's DRC finds one more
        drc1["violations"].append(fake)
        return real(drc0, drc1, isl)
    monkeypatch.setattr(pr, "gates", gates)
    pcb = _copy(tmp_path, "kicad")
    before = pcb.read_bytes()
    out = tmp_path / "out"
    assert pr.main(["--pcb", str(pcb), "--op", json.dumps(RENUMBER),
                    "--out", str(out), "--apply"]) == 1
    p = json.loads((out / "part_repair.json").read_text(encoding="utf-8"))
    assert p["status"] == "violations" and not p["applied"]
    assert any(w.startswith("DRC drc:track_dangling: ") for w in p["worse"])
    assert [v["items"][0]["uuid"] for v in p["new_violations"]] \
        == ["test-uuid"]
    assert pcb.read_bytes() == before
    assert not any((out / f).exists()
                   for f in (pr.RIP_FILE, pr.PLACE_FILE, pr.ROUTE_FILE))
    md = (out / "REPORT.md").read_text(encoding="utf-8")
    assert "Worse than before" in md and "(items test-uuid)" in md
