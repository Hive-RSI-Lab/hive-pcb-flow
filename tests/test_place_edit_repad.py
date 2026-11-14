"""place_edit swap_fp and renumber: re-pad a placed part.

swap_fp puts a fixed library footprint in place of a part's (the Y301 crystal
whose pad numbers were wrong in the library); renumber gives the pads at board
positions new numbers. Both carry the nets by pad number and refuse when
copper of another net touches a pad they produce. The fast tests check the
op validation and the resolve step on the pd_trigger fixture's R6 (pads 1
/ZBIAS at 26.25 and 2 /HV_B at 27.75, y 50.3); the live ones run the KiCad
worker on copies.
"""
from __future__ import annotations

import json
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
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "stages" / "pd_trigger"
R0805 = FIX / "lib" / "aiee.pretty" / "R0805.kicad_mod"
RENUMBER = {"op": "renumber", "ref": "R6",
            "pads": [{"at": [26.25, 50.3], "number": "2"},
                     {"at": [27.75, 50.3], "number": "1"}]}
SWAP = {"op": "swap_fp", "ref": "R6", "footprint": str(R0805),
        "fpid": "aiee:R0805"}


def _check(*ops):
    return place_edit.validate_ops({"version": 1, "ops": list(ops)})


@pytest.mark.parametrize("op, msg", [
    ({**RENUMBER, "pads": []}, "pads must be a list of"),
    ({**RENUMBER, "pads": [{"at": [1, 2]}]}, "pads must be a list of"),
    ({**RENUMBER, "pads": [{"at": [1, 2], "number": "1"},
                           {"at": [1.001, 2], "number": "2"}]},
     "name the same pad"),
    ({**RENUMBER, "nets": {"1": 5}}, "nets must be"),
    ({**SWAP, "footprint": "R0805.txt"}, "footprint must be the path"),
    ({**SWAP, "fpid": "R0805"}, "fpid must be"),
    ({**SWAP, "x": 3}, "give x and y together"),
])
def test_validate_repad_op(op, msg):
    with pytest.raises(CheckError, match=msg):
        _check(op)


def test_validate_repad_list():
    assert len(_check(RENUMBER, {"op": "lock", "ref": "R6",
                                 "locked": True})) == 2
    with pytest.raises(CheckError, match="already re-padded"):
        _check(RENUMBER, SWAP)
    with pytest.raises(CheckError, match="is re-padded by"):
        _check({"op": "move", "ref": "R6", "x": 1, "y": 2}, RENUMBER)
    with pytest.raises(CheckError, match="comes before the swap_fp"):
        _check({"op": "lock", "ref": "R6", "locked": True}, SWAP)
    assert len(_check(SWAP, {"op": "lock", "ref": "R6",
                             "locked": True})) == 2


def test_renumber_on_a_routed_board_refuses_the_copper(tmp_path):
    with pytest.raises(CheckError,
                       match="copper of other nets touches the pads it") as e:
        place_edit._resolve_repad(FIX / "route" / "pd-trigger.kicad_pcb",
                                  [RENUMBER], tmp_path)
    # the hint is an op route_edit takes as it stands
    hint = str(e.value).split("--op '", 1)[1].split("'", 1)[0]
    ops = route_edit.load_ops(None, [hint])
    assert ops[0]["op"] == "rip" and ops[0]["uuid_prefix"]


def test_renumber_resolves_the_nets_by_number(tmp_path):
    wops, _ = place_edit._resolve_repad(
        FIX / "route" / "pd-trigger.kicad_pcb", [RENUMBER], tmp_path,
        touch_check=False)
    assert {(p["number"], p["net"], round(p["at"][0], 3))
            for p in wops[0]["_pads"]} \
        == {("2", "/HV_B", 26.25), ("1", "/ZBIAS", 27.75)}


def test_swap_fp_resolves_the_library_part(tmp_path):
    wops, _ = place_edit._resolve_repad(
        FIX / "place" / "pd-trigger.kicad_pcb", [SWAP], tmp_path)
    op = wops[0]
    assert op["_fpid"] == "aiee:R0805"
    assert op["_nets"] == {"1": "/ZBIAS", "2": "/HV_B"}
    assert [(n, net, round(x, 3)) for n, net, x, _ in op["_expect"]] \
        == [("1", "/ZBIAS", 26.0), ("2", "/HV_B", 28.0)]


# ------------------------------------------------------------------ live

@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def _copy(tmp_path, stage: str) -> Path:
    work = tmp_path / stage
    shutil.copytree(FIX / stage, work)
    return work / "pd-trigger.kicad_pcb"


def _pads(pcb, ref="R6"):
    return sorted((p.number, p.net, round(p.center[0], 3))
                  for p in geom.BoardGeom.from_file(pcb).pads_of(ref=ref))


def test_live_renumber_swaps_the_nets(kicad, tmp_path):
    pcb = _copy(tmp_path, "route")
    place_edit.apply_ops(pcb, [dict(RENUMBER)], touch_check=False)
    assert _pads(pcb) == [("1", "/ZBIAS", 27.75), ("2", "/HV_B", 26.25)]


def test_live_swap_fp_puts_the_library_part_in(kicad, tmp_path):
    pcb = _copy(tmp_path, "place")  # carries three vias, so: --allow-routed
    payload, _ = place_edit.run(["--pcb", str(pcb), "--allow-routed", "--op",
                                 json.dumps(SWAP)])
    assert payload["verified"]
    assert _pads(pcb) == [("1", "/ZBIAS", 26.0), ("2", "/HV_B", 28.0)]
    assert '(footprint "aiee:R0805"' in pcb.read_text(encoding="utf-8")
