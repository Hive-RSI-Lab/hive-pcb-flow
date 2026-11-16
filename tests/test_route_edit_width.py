"""route_edit holds a track to the board's .kicad_dru width rules (#1153).

rules_gen holds a power net to its current's width (aiee_pwr_width_*), with
a lower minimum inside some parts' courtyards (aiee_pwr_neck_*). route_edit
took any width and only the next DRC showed it. Now a track the ops add,
set_width or renet under its net's floor, outside the net's neck zones,
refuses the run; each planted case has a quiet twin that passes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import geom  # noqa: E402
import route_edit  # noqa: E402
from checklib import CheckError  # noqa: E402
from test_route_finish import (  # noqa: E402
    NECK_DRU, _board, _fine_pitch, _pad, _seg)

WIDE = "00000000-0000-0000-0000-00000000aa01"     # a +3V3 track at 1 mm
OTHER = "00000000-0000-0000-0000-00000000aa02"    # a GND track at 0.3 mm


@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def _pcb(tmp_path):
    body = (_fine_pitch("U1", 10, 15) + _pad("U2", 10, 5, "+3V3", size=1.5)
            + _seg(14, 5, 14, 10, 1.0, "F.Cu", "+3V3", WIDE)
            + _seg(24, 5, 24, 10, 0.3, "F.Cu", "GND", OTHER))
    return _board(tmp_path, body, dru=NECK_DRU)


def _add(x0, y0, x1, y1, w):
    return json.dumps({"op": "add_track", "start": [x0, y0], "end": [x1, y1],
                       "width": w, "layer": "F.Cu", "net": "+3V3"})


def _run(pcb, *ops, extra=()):
    args = ["--pcb", str(pcb)]
    for op in ops:
        args += ["--op", op]
    return route_edit.run(args + list(extra))[0]


def _refused(pcb, *ops, match):
    before = pcb.read_bytes()
    with pytest.raises(CheckError, match=match):
        _run(pcb, *ops)
    assert pcb.read_bytes() == before        # rolled back


def test_an_add_under_the_net_width_rule_is_refused(kicad, tmp_path):
    pcb = _pcb(tmp_path)
    # the planted case: a 0.4 mm +3V3 track against the 1 mm rule
    _refused(pcb, _add(20, 5, 20, 10, 0.4),
             match=r"\+3V3 track .* is 0.4 mm, under the .kicad_dru width "
                   r"rule's 1 mm")
    # the quiet twin: the same track at the rule's width
    payload = _run(pcb, _add(20, 5, 20, 10, 1.0))
    assert payload["status"] == "pass" and "under_width_rule" not in payload
    assert any(abs(t.width - 1.0) < 1e-6 and t.shape.coords[0][0] == 20
               for t in geom.BoardGeom.from_file(pcb).tracks_of(net="+3V3"))


def test_a_neck_inside_its_zone_passes(kicad, tmp_path):
    pcb = _pcb(tmp_path)
    payload = _run(pcb, _add(10, 15, 10, 15.6, 0.25))
    assert payload["status"] == "pass" and "under_width_rule" not in payload
    # under the neck's own width it is held, zone or not
    _refused(_pcb(_sub(tmp_path, "n")), _add(10, 15, 10, 15.6, 0.2),
             match=r"is 0.2 mm, under")


def test_a_track_on_the_courtyard_off_the_zone_is_listed_not_refused(
        kicad, tmp_path):
    """Past the pads' reach but on U1's courtyard: DRC's intersectsCourtyard
    may hold it to the 0.25 mm neck, so it is named, not refused."""
    pcb = _pcb(tmp_path)
    payload = _run(pcb, _add(11.3, 13.9, 11.3, 14.3, 0.25))
    (row,) = payload["under_width_rule"]
    assert row["net"] == "+3V3" and row["width"] == 0.25 \
        and row["rule_mm"] == 1.0
    assert row["courtyard_rule"] == {"ref": "U1", "min_mm": 0.25}
    # the same track off the courtyard is refused
    _refused(_pcb(_sub(tmp_path, "off")), _add(13.3, 13.9, 13.3, 14.3, 0.25),
             match=r"is 0.25 mm, under")


def test_set_width_and_renet_are_checked(kicad, tmp_path):
    pcb = _pcb(tmp_path)
    # set_width keeps the uuid: the check takes the tracks it changed
    _refused(pcb, json.dumps({"op": "set_width", "uuid_prefix": WIDE[:-2],
                              "width": 0.4}), match=r"is 0.4 mm, under")
    # renet puts a 0.3 mm GND track on +3V3
    _refused(pcb, json.dumps({"op": "renet", "uuid_prefix": OTHER,
                              "to": "+3V3"}), match=r"is 0.3 mm, under")
    # the quiet twins: widened, or renetted at the rule's width
    assert _run(pcb, json.dumps({"op": "set_width", "uuid_prefix": WIDE,
                                 "width": 1.2}))["status"] == "pass"
    assert _run(pcb, json.dumps({"op": "set_width", "uuid_prefix": OTHER,
                                 "width": 1.0}),
                json.dumps({"op": "renet", "uuid_prefix": OTHER,
                            "to": "+3V3"}))["status"] == "pass"


def test_allow_under_rule_and_importers_list_it(kicad, tmp_path):
    pcb = _pcb(tmp_path)
    payload = _run(pcb, _add(20, 5, 20, 10, 0.4),
                   extra=["--allow-under-rule"])
    (row,) = payload["under_width_rule"]
    assert (row["net"], row["width"], row["rule_mm"]) == ("+3V3", 0.4, 1.0)
    assert "courtyard_rule" not in row
    # an importer (generators) gets the rows in its report, not a refusal
    rep: dict = {}
    route_edit.apply_ops(pcb, [json.loads(_add(21, 5, 21, 10, 0.4))],
                         report=rep)
    assert [r["width"] for r in rep["under_width_rule"]] == [0.4]
    with pytest.raises(CheckError, match="width_rules must be"):
        route_edit.apply_ops(pcb, [], width_rules="strict")


def test_refill_reports_the_drc_it_ran(kicad, tmp_path):
    """--refill ran a full DRC and dropped the report: its track_width
    violations now come back in the result."""
    pcb = _pcb(tmp_path)
    payload = _run(pcb, _add(20, 5, 20, 10, 0.4),
                   extra=["--allow-under-rule", "--refill"])
    (uuid,) = [r["uuid"] for r in payload["under_width_rule"]]
    drc = payload["drc"]
    assert drc["counts"]["total"] >= 1
    assert uuid in {it["uuid"] for v in drc["track_width"]
                    for it in v["items"]}
    # the quiet twin: at the rule's width DRC names no track of it
    payload = _run(_pcb(_sub(tmp_path, "q")), _add(20, 5, 20, 10, 1.0), extra=["--refill"])
    assert not [v for v in payload["drc"]["track_width"]
                if v["net"] == "+3V3"
                and any(it["pos"] and it["pos"][0] == 20 for it in v["items"])]
