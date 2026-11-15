"""check_thermal screens a known power IC with no thermal entry (#884).

minuet v3 ships no constraints.json; its MCF8316D motor driver sat on thin
2-layer copper and reached over-temperature at 4.5 A per phase, and
check_thermal, with no entry to work from, never looked. A part
reference/thermal_parts.yaml knows is now screened on its own datasheet.
Pure tests on synthetic boards; no toolchain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_thermal  # noqa: E402
import geom  # noqa: E402
import verify_all  # noqa: E402

SQ = "(xy 0 0) (xy 40 0) (xy 40 40) (xy 0 40)"


def _driver(value="MCF8316D", ref="U5", at=(20, 20)):
    """A VQFN-40-ish motor driver: a 3.7 x 5.7 mm GND exposed pad and two
    signal pins, as on minuet."""
    return (f'  (footprint "t:{ref}" (at {at[0]} {at[1]}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            '    (pad "41" smd rect (at 0 0) (size 3.7 5.7) (layers "F.Cu") '
            '(net "GND"))\n'
            '    (pad "1" smd rect (at -2.6 -2.5) (size 0.6 0.25) '
            '(layers "F.Cu") (net "/fan/A"))\n'
            '    (pad "2" smd rect (at -2.6 -2) (size 0.6 0.25) '
            '(layers "F.Cu") (net "+12V")))\n')


def _vias(n=6, at=(20, 20)):
    out = []
    for i in range(n):
        for j in range(n):
            x = at[0] - 1.25 + 0.5 * i
            y = at[1] - 2.5 + 1.0 * j
            out.append(f'  (via (at {x:.2f} {y:.2f}) (size 0.6) (drill 0.3) '
                       '(layers "F.Cu" "B.Cu") (net "GND"))\n')
    return "".join(out)


def _pour(layer):
    return (f'  (zone (net "GND") (layer "{layer}")\n'
            f'    (polygon (pts {SQ}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {SQ})))\n')


def _board(tmp_path, body, name="b"):
    text = f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def _run(pcb, cons=None):
    argv = ["--pcb", str(pcb)]
    if cons is not None:
        c = pcb.with_name("constraints.json")
        c.write_text(json.dumps(cons), encoding="utf-8")
        argv += ["--constraints", str(c)]
    return check_thermal.run(argv)[0]


def test_the_row_reads_the_datasheet():
    row = check_thermal.thermal_row({"value": "MCF8316DVRGFR", "props": {}},
                                    check_thermal.thermal_rows())
    assert row["theta_ja_c_w"] == 25.7 and row["tj_warn_c"] == 135
    # 4 A peak per phase: 3 x (16 / 2) x 70 mOhm = 1.68 W
    assert check_thermal.loss_w(row, 4.0) == pytest.approx(1.68)
    # on its own JEDEC board the die reaches TOTW at ~6.4 A from 25 C
    assert check_thermal.warn_current_a(row, 25.7, 25.0) == pytest.approx(
        6.38, abs=0.01)
    assert check_thermal.thermal_row({"value": "DRV8316", "props": {}},
                                     check_thermal.thermal_rows()) is None
    assert check_thermal.thermal_row(
        {"value": "driver", "props": {"MPN": "MCF8316D1VRGFR"}},
        check_thermal.thermal_rows())


def test_planted_driver_on_bare_pad_is_found_without_constraints(tmp_path):
    """Planted fault: the motor driver with nothing but its own pad to shed
    heat into, and no constraints.json. Before #884 this was nothing to
    judge."""
    p = _run(_board(tmp_path, _driver()))
    vs = [v for v in p["violations"] if v["kind"] == "thermal_area"]
    assert len(vs) == 1, p
    v = vs[0]
    assert v["severity"] == "warning" and v["inferred"] is True
    assert v["refs"] == ["U5"] and v["net"] == "GND"
    assert v["theta_ratio"] > check_thermal.THETA_RATIO_MAX
    assert "thermal warning (135 C)" in v["msg"] and "A peak" in v["msg"]
    assert p["checked"][0]["warn_peak_a"] < p["checked"][0][
        "warn_peak_a_datasheet"]
    assert p.get("nothing_to_judge") is None


def test_driver_on_full_pours_with_vias_passes(tmp_path):
    p = _run(_board(tmp_path, _driver() + _pour("F.Cu") + _pour("B.Cu")
                    + _vias()))
    assert [v for v in p["violations"] if v["kind"] == "thermal_area"] == [], p
    f = p["checked"][0]
    assert f["inferred"] and f["theta_ratio"] <= check_thermal.THETA_RATIO_MAX


def test_an_entry_for_the_part_replaces_the_screen(tmp_path):
    pcb = _board(tmp_path, _driver())
    p = _run(pcb, {"thermal": [{"ref": "U5", "net": "GND", "power_w": 0.05}]})
    assert not any(v.get("inferred") for v in p["violations"])
    assert [c.get("inferred") for c in p["checked"]] == [None]


def test_an_unknown_part_is_still_nothing_to_judge(tmp_path):
    p = _run(_board(tmp_path, _driver(value="XYZ1234")))
    assert p["violations"] == [] and "thermal_parts.yaml" in p[
        "nothing_to_judge"]
    assert "candidates" not in p


def test_a_driver_family_with_no_row_is_a_candidate(tmp_path):
    """DRV8316 is a motor driver (thermal_parts.yaml families) with no row:
    not judged, and said so, rather than silently nothing to judge."""
    p = _run(_board(tmp_path, _driver(value="DRV8316")))
    assert p["violations"] == []
    [c] = p["candidates"]
    assert c["ref"] == "U5" and c["reason"] == "no_row"
    assert "candidates" in p["nothing_to_judge"]
    assert any(n.startswith("U5 (DRV8316): no_row") for n in p["not_judged"])


def test_ambient_moves_the_warning_current(tmp_path):
    pcb = _board(tmp_path, _driver())
    cool = _run(pcb)["checked"][0]["warn_peak_a"]
    hot = _run(pcb, {"ambient_c": 50})["checked"][0]["warn_peak_a"]
    assert hot < cool


def test_verify_all_runs_it_without_constraints():
    by = {c["name"]: c for c in verify_all.CHECKS}
    th = by["check_thermal"]
    assert th["needs"] == []
    assert th["args"]({"pcb": "b.kicad_pcb"}) == []
    assert th["args"]({"pcb": "b", "constraints": "c.json"}) == [
        "--constraints", "c.json"]


def test_the_heatsink_net_is_the_biggest_pad(tmp_path):
    bg = geom.load_board(_board(tmp_path, _driver()))
    assert check_thermal.heatsink_net(bg, "U5") == "GND"
