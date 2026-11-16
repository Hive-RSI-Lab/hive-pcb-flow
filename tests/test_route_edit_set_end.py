"""route_edit moves one end of a track in place: set_end (#1148).

There was no op for it: to shorten a track you ripped it and added back the
part you kept, under a new uuid (a741d, haos-voice item 8, LRCLK 7d088a19).
set_end {at, to, [net], [layer], [uuid_prefix]} resolves the straight track
ends at `at` into the worker's set_point, so the uuid stays.
Fixture: the frozen routed pd-trigger board.
  planted  /VIND c2769c7c's end at 41.9435,49.61 goes to 38,49.61: the same
           uuid, shorter, its start where it was; the /CFG3 joint's two
           ends move together; layer and uuid_prefix narrow the pick
  twin     a point mid-track, or another net's: nothing moves (absent), and
           a re-run of the planted edit is absent too
  refused  an arc, ends of two nets, a track that would shrink to nothing
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
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import geom  # noqa: E402
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

PD_DIR = REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"
PD_PCB = PD_DIR / "pd-trigger.kicad_pcb"
VIND = "c2769c7c"                   # /VIND F.Cu (32.0929,49.61)-(41.9435,49.61)
VIND_END = [41.9435, 49.61]
SHORT = [38.0, 49.61]
CFG3_JOINT = [33.2071, 30.6861]     # /CFG3: 2f15222e and 8d4e7dc2 end here
CC1_VIA = [21.9386, 39.0807]        # /CC1 via: c8dfdb8a F.Cu, 5a02b6ca B.Cu


@pytest.fixture(scope="module")
def bg():
    return geom.BoardGeom.from_file(PD_PCB)


def _v(*ops):
    return route_edit.validate_ops({"version": 1, "ops": list(ops)})


def _resolve(bg, **op):
    flat, _ = route_edit.resolve_ops(bg, _v({"op": "set_end", **op}))
    return flat


@pytest.mark.parametrize("op, msg", [
    ({"op": "set_end", "at": [1, 1]}, r"missing \['to'\]"),
    ({"op": "set_end", "at": [1, 1], "to": "x"}, "to must be"),
    ({"op": "set_end", "at": [1, 1], "to": [1, 1]}, "same point"),
    ({"op": "set_end", "at": [1, 1], "to": [2, 2], "uuid_prefix": []},
     "uuid_prefix must be"),
    ({"op": "set_end", "at": [1, 1], "to": [2, 2], "layer": ""},
     "layer must be"),
    ({"op": "set_end", "at": [1, 1], "to": [2, 2], "radius": 1},
     "unknown keys"),
])
def test_validate_refuses(op, msg):
    with pytest.raises(CheckError, match=msg):
        _v(op)


def test_planted_one_end_resolves_to_set_point(bg):
    vind = next(t for t in bg.tracks_of(net="/VIND")
                if t.uuid.startswith(VIND))
    flat = _resolve(bg, at=VIND_END, to=SHORT, uuid_prefix=VIND)
    assert flat == [{"op": "set_point", "uuid": vind.uuid, "which": "end",
                     "to": SHORT}]
    # a joint: both same-net ends move together, so it stays joined
    flat = _resolve(bg, at=CFG3_JOINT, to=[33.25, 30.7])
    assert sorted(f["uuid"][:8] for f in flat) == ["2f15222e", "8d4e7dc2"]
    assert {f["op"] for f in flat} == {"set_point"}
    flat = _resolve(bg, at=CFG3_JOINT, to=[33.25, 30.7],
                    uuid_prefix="8d4e7dc2")
    assert [(f["uuid"][:8], f["which"]) for f in flat] == [("8d4e7dc2",
                                                             "end")]
    # the via's two tracks, one per layer: layer picks one
    flat = _resolve(bg, at=CC1_VIA, to=[22.0, 39.0], layer="F.Cu")
    assert [(f["uuid"][:8], f["which"]) for f in flat] == [("c8dfdb8a",
                                                             "start")]


def test_twin_nothing_at_the_point_moves_nothing(bg):
    assert _resolve(bg, at=[36.0, 49.61], to=SHORT) == []      # mid-track
    assert _resolve(bg, at=VIND_END, to=SHORT, net="/CC1") == []
    assert _resolve(bg, at=CFG3_JOINT, to=[33.25, 30.7],
                    uuid_prefix="ffffffff") == []
    assert _resolve(bg, at=CC1_VIA, to=[22.0, 39.0], layer="In1.Cu") == []


def test_refused_shrink_arc_and_two_nets(bg):
    with pytest.raises(CheckError, match="shrink to nothing"):
        _resolve(bg, at=VIND_END, to=[32.0929, 49.61], uuid_prefix=VIND)

    def fake(*tracks):
        return SimpleNamespace(tracks_of=lambda net=None, layer=None: [
            t for t in tracks if net in (None, t.net)
            and layer in (None, t.layer)])

    arc = geom.Track("A", "F.Cu", 0.2,
                     LineString([(0, 0), (1, 0.4), (2, 0)]), uuid="arc1")
    with pytest.raises(CheckError, match="arc1 is an arc"):
        route_edit._resolve_set_end(fake(arc), {"at": [2, 0], "to": [3, 0]})
    a = geom.Track("A", "F.Cu", 0.2, LineString([(0, 0), (2, 0)]),
                   uuid="a1")
    b = geom.Track("B", "B.Cu", 0.2, LineString([(2, 0), (2, 3)]),
                   uuid="b1")
    with pytest.raises(CheckError, match=r"nets \['A', 'B'\]"):
        route_edit._resolve_set_end(fake(a, b), {"at": [2, 0], "to": [3, 0]})
    assert route_edit._resolve_set_end(
        fake(a, b), {"at": [2, 0], "to": [3, 0], "net": "B"}) \
        == [{"op": "set_point", "uuid": "b1", "which": "start",
             "to": [3, 0]}]
    # a track shorter than AT_TOL has both ends at `at`
    tiny = geom.Track("A", "F.Cu", 0.2, LineString([(0, 0), (0.01, 0)]),
                      uuid="t1")
    with pytest.raises(CheckError, match="shrink to nothing"):
        route_edit._resolve_set_end(fake(tiny), {"at": [0, 0], "to": [1, 0]})


def test_live_set_end_keeps_the_uuid_and_reruns_absent(tmp_path):
    import env
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    dst = tmp_path / "pdt"
    shutil.copytree(PD_DIR, dst)
    pcb = dst / PD_PCB.name
    before = geom.BoardGeom.from_file(pcb)
    vind = next(t for t in before.tracks_of(net="/VIND")
                if t.uuid.startswith(VIND))
    op = json.dumps({"op": "set_end", "at": VIND_END, "to": SHORT,
                     "uuid_prefix": VIND})
    payload, _ = route_edit.run(["--pcb", str(pcb), "--op", op])
    assert payload["results"] == [{"op": "set_end", "count": 1,
                                   "status": "moved",
                                   "uuids": [vind.uuid]}]
    after = geom.BoardGeom.from_file(pcb)
    moved = next(t for t in after.tracks_of() if t.uuid == vind.uuid)
    assert [tuple(round(c, 4) for c in q) for q in moved.shape.coords] \
        == [(32.0929, 49.61), (38.0, 49.61)]
    assert (moved.net, moved.layer, moved.width) \
        == (vind.net, vind.layer, vind.width)
    assert len(after.tracks_of()) == len(before.tracks_of())
    # the verify reads the end where set_point put it: the unedited board
    # fails it (a worker that skipped the move would roll back)
    flat = [{"op": "set_point", "uuid": vind.uuid, "which": "end",
             "to": SHORT}]
    assert route_edit._verify(pcb, flat) == []
    probs = route_edit._verify(PD_PCB, flat)
    assert len(probs) == 1 and "not [38.0, 49.61]" in probs[0]
    # quiet twin: a re-run finds no end at `at` and moves nothing
    payload, _ = route_edit.run(["--pcb", str(pcb), "--op", op])
    assert payload["results"] == [
        {"op": "set_end", "count": 0, "status": "absent", "uuids": []}]
