"""route_edit takes a ripped track's teardrops with it (#1144).

KiCad 10 keeps a teardrop as a zone with no uuid, rebuilt only by a refill,
so a `remove`/`rip` used to leave the track's drop on the board as copper of
its net. Fixture: tests/fixtures/teardrops/led_twin.kicad_pcb, the dropped
LED board (led_dropped) with a second F.Cu track into via B, 13.2,21.74 ->
14.0,21.74, refilled (KiCad 10.0.6): via B has two drops, one per track.
  planted  rip the track going up from via B: its drop goes, the one on the
           track going right stays (and a refill agrees: same drops)
  twin     rip the stub wholly inside via A (no drop is its): nothing else
           goes; rip a track and lay it again on the same line: its drop
           still answers a track and stays
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import sexpdata
from shapely.geometry import LineString

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
STUB = "0d74d163-1c4b-4968-b546-e898017536d9"    # inside via A's copper
UP_TIP = [13.3, 21.142567]
RIGHT_TIP = [13.797433, 21.84]


def _tracks(pcb: Path) -> dict:
    return {t.uuid: t for t in geom.load_board(pcb).tracks_of()}


def _left(pcb: Path, gone: list[str], add: list | None = None) -> list:
    ts = _tracks(pcb)
    kept = [t for u, t in ts.items() if u not in gone] + (add or [])
    return tdl.left_by(pcb, [ts[u] for u in gone], kept)


def _drops(pcb: Path) -> list:
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    return sorted((n, ly, tuple(xy[0])) for n, ly, xy, _ in
                  tdl.teardrop_zones(root))


def _copy(tmp: Path) -> Path:
    pcb = tmp / "led.kicad_pcb"
    shutil.copy2(TWIN, pcb)
    for ext in (".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIX / f"led{ext}", tmp / f"led{ext}")
    return pcb


def test_fixture_via_b_has_a_drop_per_track():
    by = {it.name: it for it in tdl.read(TWIN).items}
    via_b = by["via@13.200,21.740"]
    assert via_b.drops == {"F.Cu": 2} and via_b.missing() == []
    tips = sorted((round(p.x, 6), round(p.y, 6)) for p in via_b.tips["F.Cu"])
    assert tips == sorted([tuple(UP_TIP), tuple(RIGHT_TIP)])
    assert len(_drops(TWIN)) == 4


def test_planted_rip_names_its_own_drop_only():
    assert _left(TWIN, [UP]) == [{"net": "GND", "layer": "F.Cu",
                                  "at": UP_TIP}]
    assert _left(TWIN, [RIGHT]) == [{"net": "GND", "layer": "F.Cu",
                                     "at": RIGHT_TIP}]
    both = _left(TWIN, [UP, RIGHT])
    assert sorted(d["at"] for d in both) == sorted([UP_TIP, RIGHT_TIP])


def test_twin_stub_and_relaid_track_leave_no_drop():
    assert _left(TWIN, [STUB]) == []
    up = _tracks(TWIN)[UP]
    again = geom.Track("GND", "F.Cu", up.width,
                       LineString(list(up.shape.coords)))
    assert _left(TWIN, [UP], add=[again]) == []


def test_no_teardrop_zones_no_read(monkeypatch):
    """A board with no teardrops never pays for teardroplib.read."""
    raw = FIX / "led.kicad_pcb"
    bg = geom.load_board(raw)
    monkeypatch.setattr(tdl, "read", lambda *a, **k: pytest.fail("read"))
    uid = bg.tracks_of()[0].uuid
    assert route_edit._drops_left(raw, bg, [{"op": "remove",
                                             "uuid": uid}]) == []


def test_verify_catches_a_drop_left_behind():
    """Mutant: a worker that skips remove_teardrop is caught by the
    verify, so the edit rolls back instead of leaving the copper."""
    op = {"op": "remove_teardrop", "net": "GND", "layer": "F.Cu",
          "at": UP_TIP}
    probs = route_edit._verify(TWIN, [op])
    assert len(probs) == 1 and "still present" in probs[0]
    other = dict(op, at=[1.0, 1.0])
    assert route_edit._verify(TWIN, [other]) == []


def test_live_rip_takes_its_drop_and_reruns_quiet(tmp_path):
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    pcb = _copy(tmp_path)
    res = route_edit.apply_ops(pcb, [{"op": "remove", "uuid": UP}])
    assert res == [{"op": "remove", "status": "removed",
                    "teardrops_removed": 1}]
    left = _drops(pcb)
    assert len(left) == 3
    by = {it.name: it for it in tdl.read(pcb).items}
    tips = [[round(p.x, 6), round(p.y, 6)]
            for p in by["via@13.200,21.740"].tips["F.Cu"]]
    assert tips == [RIGHT_TIP]
    # a re-run finds the track gone and takes nothing more (the worker's
    # save may write the zones in another order: compare the drops)
    again = route_edit.apply_ops(pcb, [{"op": "remove", "uuid": UP}])
    assert again == [{"op": "remove", "status": "absent"}]
    assert _drops(pcb) == left
    # quiet twin: the stub owns no drop, so a rip of it takes none
    res = route_edit.apply_ops(pcb, [{"op": "rip", "uuid_prefix": STUB[:8]}])
    assert res[0]["status"] == "removed" and "teardrops_removed" not in res[0]
    assert _drops(pcb) == left


def test_live_rip_matches_what_a_refill_leaves(tmp_path):
    """The drops route_edit leaves after a rip are the ones KiCad's own
    refill builds for the tracks that are left."""
    import env
    if env.find_kicad_cli() is None:
        pytest.skip("kicad-cli not found")
    pcb = _copy(tmp_path)
    route_edit.apply_ops(pcb, [{"op": "rip", "uuid_prefix": UP[:8]}])
    ours = _drops(pcb)
    import kc
    kc.run_drc(env.find_kicad_cli(), pcb, refill=True, save_board=True)
    assert len(ours) == 3 and _drops(pcb) == ours
