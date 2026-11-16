"""route_edit takes the drop a moved track end leaves behind (#1436).

set_point start|end, set_end (#1148, which resolves to set_point) and
move_via's drag move a track end in place, uuid kept. KiCad 10 keeps a
teardrop as a zone with no uuid, rebuilt only by a refill, so the drop on
the old end stayed at the via as copper of its net. _drops_left now counts
the old track gone and the moved one kept, the moved one answering a drop
only while its copper still meets the via's (a tip still on the shortened
track is not enough), and never at the old place of a via the list moves.
Fixture: tests/fixtures/teardrops/led_twin.kicad_pcb (see
test_route_edit_teardrops): via B 13.2,21.74 has two F.Cu drops, one for
UP (via B -> 13.2,20.9) and one for RIGHT (via B -> 14.0,21.74).
  planted  shorten UP off via B (set_point, set_end): its drop goes, RIGHT's
           stays, and a refill agrees; move via B with drag: both go
  twin     move UP's free end; move UP's via end but still inside via B's
           copper; a set_point on RIGHT's far end; a re-run: nothing goes
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import sexpdata

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import geom  # noqa: E402
import route_edit  # noqa: E402
import teardroplib as tdl  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "teardrops"
TWIN = FIX / "led_twin.kicad_pcb"
UP = "87af71fa-71d2-4210-b623-8e434c2ec740"      # via B -> 13.2,20.9
RIGHT = "2d06d944-a56c-4df3-ae10-330d51f1f521"   # via B -> 14.0,21.74
VIA_B = "d150125a-0f47-42a1-b4eb-36069d276772"   # 13.2,21.74
UP_TIP = [13.3, 21.142567]
RIGHT_TIP = [13.797433, 21.84]


def _drop(at: list) -> dict:
    return {"op": "remove_teardrop", "net": "GND", "layer": "F.Cu",
            "at": at}


def _left(ops: list[dict]) -> list:
    bg = geom.load_board(TWIN)
    flat, _ = route_edit.resolve_ops(
        bg, route_edit.validate_ops({"version": 1, "ops": ops}))
    return route_edit._drops_left(TWIN, bg, flat)


def _sp(which: str, to: list, uuid: str = UP) -> dict:
    return {"op": "set_point", "uuid": uuid, "which": which, "to": to}


def _drops(pcb: Path) -> list:
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    return sorted((n, ly, tuple(xy[0])) for n, ly, xy, _ in
                  tdl.teardrop_zones(root))


def _via_b_tips(pcb: Path) -> list:
    by = {it.name: it for it in tdl.read(pcb).items}
    return sorted([round(p.x, 6), round(p.y, 6)]
                  for p in by["via@13.200,21.740"].tips.get("F.Cu", []))


def _copy(tmp: Path) -> Path:
    pcb = tmp / "led.kicad_pcb"
    shutil.copy2(TWIN, pcb)
    for ext in (".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIX / f"led{ext}", tmp / f"led{ext}")
    return pcb


def test_planted_via_end_moved_off_names_its_drop():
    # well off the via, and just off it with UP's tip still on the track
    assert _left([_sp("start", [13.2, 21.0])]) == [(0, _drop(UP_TIP))]
    assert _left([_sp("start", [13.2, 21.3])]) == [(0, _drop(UP_TIP))]
    # set_end resolves to the same set_point
    assert _left([{"op": "set_end", "at": [13.2, 21.74], "to": [13.2, 21.0],
                   "uuid_prefix": UP[:8]}]) == [(0, _drop(UP_TIP))]


def test_planted_move_via_drag_names_both_drops():
    got = _left([{"op": "move_via", "at": [13.2, 21.74],
                  "to": [13.6, 22.4]}])
    # flat: 0 the via, 1 RIGHT's start, 2 UP's start
    assert sorted(got) == [(1, _drop(RIGHT_TIP)), (2, _drop(UP_TIP))]


def test_twin_moves_that_keep_the_drop():
    # UP's free end; UP's via end still in via B's copper (two ways)
    assert _left([_sp("end", [13.2, 20.5])]) == []
    assert _left([_sp("start", [13.25, 21.74])]) == []
    assert _left([_sp("start", [13.2, 21.6])]) == []
    # RIGHT's far end moved: the drop at via B is still RIGHT's
    assert _left([_sp("end", [14.2, 21.74], RIGHT)]) == []
    # a set_end that matches nothing is a no-op
    assert _left([{"op": "set_end", "at": [13.2, 21.74], "to": [13.2, 21.0],
                   "uuid_prefix": "ffffffff"}]) == []


def test_mutant_without_the_touch_rule_keeps_the_drop():
    """The tip alone is not enough: at 13.2,21.3 UP's tip still lies on
    the shortened track, so a moved track kept as a plain kept track (the
    rule a rip uses) would answer the drop and leave it."""
    bg = geom.load_board(TWIN)
    ts = {t.uuid: t for t in bg.tracks_of()}
    from shapely.geometry import LineString
    up = ts[UP]
    c = list(up.shape.coords)
    c[0] = (13.2, 21.3)
    moved = geom.Track(up.net, up.layer, up.width, LineString(c))
    kept = [t for u, t in ts.items() if u != UP]
    assert tdl.left_by(TWIN, [up], kept + [moved]) == []
    assert tdl.left_by(TWIN, [up], kept, [moved]) == [
        {"net": "GND", "layer": "F.Cu", "at": UP_TIP}]


def _live(tmp: Path, ops: list[dict]):
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    pcb = _copy(tmp)
    return pcb, route_edit.apply_ops(pcb, ops)


SHORTEN = [{"op": "set_end", "at": [13.2, 21.74], "to": [13.2, 21.3],
            "uuid_prefix": UP[:8]}]


def test_live_set_end_takes_its_drop_and_reruns_quiet(tmp_path):
    pcb, res = _live(tmp_path, SHORTEN)
    assert res == [{"op": "set_end", "count": 1, "status": "moved",
                    "uuids": [UP], "teardrops_removed": 1}]
    left = _drops(pcb)
    assert len(left) == 3 and _via_b_tips(pcb) == [RIGHT_TIP]
    # a re-run finds no end at via B and takes nothing more
    again = route_edit.apply_ops(pcb, SHORTEN)
    assert again == [{"op": "set_end", "count": 0, "status": "absent",
                      "uuids": []}]
    assert _drops(pcb) == left


def test_live_set_end_matches_what_a_refill_leaves(tmp_path):
    """Via B's drops after the move are the ones KiCad's own refill builds
    for the tracks as moved."""
    pcb, _ = _live(tmp_path, SHORTEN)
    ours = _via_b_tips(pcb)
    import env
    import kc
    kc.run_drc(env.find_kicad_cli(), pcb, refill=True, save_board=True)
    assert ours == [RIGHT_TIP] and _via_b_tips(pcb) == ours


def test_live_twin_free_end_moved_takes_no_drop(tmp_path):
    pcb, res = _live(tmp_path, [_sp("end", [13.2, 20.5])])
    assert res[0]["status"] == "changed" and "teardrops_removed" not in res[0]
    assert _drops(pcb) == _drops(TWIN)
