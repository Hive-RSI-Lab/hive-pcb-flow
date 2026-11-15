"""place_edit delete (#854): remove placed parts by refdes.

Replaces the pinned boards' delete_parts.py (pcbnew Remove, then
rip_dangling.py by net). The fast tests cover the op validation; the smoke
ones run the KiCad worker and DRC on copies of the routed pd_trigger fixture:
R7 (/HV_B - GND) deleted with rip_dangling leaves DRC's dangling count where
it was (quiet case); without the rip the copper stays and the run says so;
an unknown refdes, a missing --allow-routed and a rip DRC still finds
dangling are refused with the board byte-identical (fault cases).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import place_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

ROUTE = REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"


def _check(*ops):
    return place_edit.validate_ops({"version": 1, "ops": list(ops)})


# ------------------------------------------------------------------ pure

def test_ref_list_expands_to_one_op_per_part():
    ops = _check({"op": "delete", "ref": ["R7", "R8"], "rip_dangling": True})
    assert ops == [{"op": "delete", "ref": "R7", "rip_dangling": True},
                   {"op": "delete", "ref": "R8", "rip_dangling": True}]
    assert _check({"op": "delete", "ref": "R7"}) == [
        {"op": "delete", "ref": "R7"}]


@pytest.mark.parametrize("ops, msg", [
    ([{"op": "delete", "ref": []}], "ref list is empty"),
    ([{"op": "delete", "ref": ["R7", "R7"]}], "listed twice"),
    ([{"op": "delete", "ref": "R7"}, {"op": "delete", "ref": "R7"}],
     "listed twice"),
    ([{"op": "delete", "ref": [""]}], "refdes string"),
    ([{"op": "delete", "ref": [3]}], "refdes string"),
    ([{"op": "delete", "ref": "R7", "rip_dangling": "yes"}], "boolean"),
    ([{"op": "delete", "ref": "R7", "rip_dangling": True},
      {"op": "delete", "ref": "R8"}], "same on every"),
    ([{"op": "delete", "ref": "R7"},
      {"op": "move", "ref": "R8", "x": 1, "y": 2}], "cannot share"),
    ([{"op": "delete", "ref": "R7", "rip": True}], "unknown keys"),
])
def test_bad_delete_lists_are_refused(ops, msg):
    with pytest.raises(CheckError, match=msg):
        _check(*ops)


def test_delete_counts_as_a_footprint_op():
    # so a routed board needs --allow-routed, as for a move
    assert "delete" in place_edit.FOOTPRINT_OPS


# ------------------------------------------------------------------ smoke

@pytest.fixture
def board(tmp_path):
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    work = tmp_path / "route"
    shutil.copytree(ROUTE, work)
    return work / "pd-trigger.kicad_pcb"


def _copper(pcb, nets=("/HV_B", "GND")):
    bg = geom.BoardGeom.from_file(pcb)
    return (sum(1 for t in bg.tracks_of() if t.net in nets),
            sum(1 for v in bg.vias_of() if v.net in nets))


def _run(pcb, *ops, routed=True):
    argv = ["--pcb", str(pcb)] + (["--allow-routed"] if routed else [])
    for op in ops:
        argv += ["--op", __import__("json").dumps(op)]
    return place_edit.run(argv)[0]


@pytest.mark.smoke
def test_quiet_delete_with_rip_leaves_nothing_dangling(board):
    t0, v0 = _copper(board)
    out = _run(board, {"op": "delete", "ref": ["R7"], "rip_dangling": True})
    (r,) = out["results"]
    assert r["ref"] == "R7" and r["deleted"] is True
    assert r["nets"] == ["/HV_B", "GND"]
    d = r["delete"]
    assert d["drc"]["after"]["dangling"] == d["drc"]["before"]["dangling"] == 0
    assert d["drc"]["after"]["errors"] <= d["drc"]["before"]["errors"]
    assert d["ripped"]["tracks"] >= 1
    assert "R7" not in place_edit.placelib.PlaceModel(board).footprints
    t1, v1 = _copper(board)
    assert (t0 - t1, v0 - v1) == (d["ripped"]["tracks"], d["ripped"]["vias"])
    assert "notes" not in r


@pytest.mark.smoke
def test_delete_without_rip_keeps_the_copper_and_says_so(board):
    before = _copper(board)
    out = _run(board, {"op": "delete", "ref": "R7"})
    (r,) = out["results"]
    assert r["delete"]["ripped"] == {"tracks": 0, "vias": 0, "rounds": 1}
    assert _copper(board) == before
    assert r["delete"]["drc"]["after"]["dangling"] > 0
    assert "rip_dangling" in r["notes"][0]


@pytest.mark.smoke
@pytest.mark.parametrize("ops, routed, msg", [
    ([{"op": "delete", "ref": ["R7", "R99"]}], True, "refs not on board: R99"),
    ([{"op": "delete", "ref": "R7", "rip_dangling": True}], False,
     "board has routing"),
])
def test_fault_refused_board_byte_identical(board, ops, routed, msg):
    raw = board.read_bytes()
    with pytest.raises(CheckError, match=msg):
        _run(board, *ops, routed=routed)
    assert board.read_bytes() == raw


@pytest.mark.smoke
def test_fault_rip_drc_still_finds_dangling_rolls_back(board, monkeypatch):
    # with no extra rounds, R7's first-pass rip leaves the stub netconn
    # cannot see (an end inside another track's body); DRC on the staged
    # copy must catch it and keep the board as it was
    import board_update
    monkeypatch.setattr(board_update, "DANGLE_ROUNDS", 0)
    raw = board.read_bytes()
    with pytest.raises(CheckError, match=r"DRC dangling 0 -> 1 after "
                                         r"rip_dangling \(rolled back\)"):
        _run(board, {"op": "delete", "ref": "R7", "rip_dangling": True})
    assert board.read_bytes() == raw
