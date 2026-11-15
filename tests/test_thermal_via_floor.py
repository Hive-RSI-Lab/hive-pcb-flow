"""check_thermal: an entry's min_vias holds on every part, and on 4+ layers
a copper-model part is credited with its net's inner planes only when vias
reach them; short of that the screen also reads it on its own-side copper
and leaves it not judged when the two readings disagree (module doc).

The case: a QFN's 2 x 2 mm exposed pad on a 4-layer board whose GND fills
In1, In2 and B.Cu. Summed over the layers the GND copper saturates the
screen (51.1 C/W) whether or not a via joins the pad to it; on the pad's
own F.Cu copper alone (4 mm2) the screen reads ~138 C/W.

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
from checklib import CheckError  # noqa: E402

SQ = "(xy 0 0) (xy 40 0) (xy 40 40) (xy 0 40)"
THETA_PLANES = check_thermal.theta_ja(check_thermal.A_SAT_MM2, True)
THETA_SIDE = check_thermal.theta_ja(4.0, True)       # the pad's own 4 mm2
# a 3 x 3 grid at 0.7 mm pitch, inside the exposed pad
GRID = [(20 + dx, 20 + dy) for dy in (-0.7, 0, 0.7) for dx in (-0.7, 0, 0.7)]

PART = ('  (footprint "t:QFN" (at 20 20) (layer "F.Cu")\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (property "Value" "BUCK1" (at 0 0 0) (layer "F.Fab"))\n'
        '    (pad "33" smd rect (at 0 0) (size 2 2) (layers "F.Cu") '
        '(net "GND")))\n')


def _zone(layer):
    return (f'  (zone (net "GND") (layer "{layer}") (polygon (pts {SQ}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {SQ})))\n')


def _board_text(vias=(), layers=4):
    names = ["In1.Cu", "In2.Cu", "B.Cu"] if layers == 4 else ["B.Cu"]
    table = ('(0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) '
             '(3 "B.Cu" signal)' if layers == 4 else
             '(0 "F.Cu" signal) (2 "B.Cu" signal)')
    via_text = "".join(
        f'  (via (at {x:g} {y:g}) (size 0.6) (drill 0.3) '
        f'(layers "F.Cu" "B.Cu") (net "GND"))\n' for x, y in vias)
    return f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers {table} (25 "Edge.Cuts" user)) (setup)
  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{PART}{via_text}{"".join(_zone(n) for n in names)})
"""


def _pcb(tmp_path, vias=(), layers=4, name="b"):
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(_board_text(vias, layers), encoding="utf-8")
    return p


def _part(tmp_path, entry, vias=(), layers=4):
    bg = geom.load_board(_pcb(tmp_path, vias, layers))
    return check_thermal.check_part(
        bg, {"ref": "U1", "net": "GND", "dt_c": 40, **entry})


def _kind(vs, kind, severity=None):
    return [v for v in vs if v["kind"] == kind
            and (severity is None or v["severity"] == severity)]


def test_the_case_reads_as_documented(tmp_path):
    """The layers' copper saturates the screen with no via at all; the
    own-side reading is the pad alone."""
    vs, f = _part(tmp_path, {"power_w": 0.2})
    assert f["theta_ja"] == pytest.approx(THETA_PLANES, abs=0.01)
    assert f["theta_ja"] == pytest.approx(51.1, abs=0.1)
    assert f["vias_near_part"] == 0
    assert f["area_side_mm2"] == pytest.approx(4.0, abs=0.01)
    assert f["theta_side_cw"] == pytest.approx(THETA_SIDE, abs=0.01)
    assert f["planes_credited"] is False
    assert f["planes_credit_vias"] == check_thermal.VIA_CREDIT_MIN
    # 0.2 W: ~10 C credited, ~28 C on its own copper, both within 40 C
    assert vs == [] and "not_judged" not in f


@pytest.mark.parametrize("layers", [4, 2])
def test_min_vias_holds_when_the_copper_meets_the_budget(tmp_path, layers):
    """A datasheet's 9-via floor with 1 via fails on 2 and 4 layers even
    though the budget (80 C at 0.2 W) needs no via array by the copper."""
    vs, f = _part(tmp_path, {"power_w": 0.2, "dt_c": 80, "min_vias": 9},
                  vias=[(20, 20)], layers=layers)
    e = _kind(vs, "thermal_vias", "error")
    assert len(e) == 1, json.dumps(vs, indent=1)
    assert e[0]["vias"] == 1 and e[0]["required"] == 9 and e[0]["explicit"]
    # the window the count read: sqrt(4 / pi) + 1.5 mm
    assert e[0]["window_mm"] == pytest.approx(2.63, abs=0.01)
    assert "within 2.6 mm" in e[0]["msg"] and "under the 9" in e[0]["msg"]
    assert f["min_vias"] == 9
    assert not _kind(vs, "thermal_area")
    assert ("planes_credited" in f) == (layers == 4)


def test_min_vias_met_passes(tmp_path):
    vs, f = _part(tmp_path, {"power_w": 0.5, "min_vias": 9}, vias=GRID)
    assert f["vias_near_part"] == 9
    assert vs == [] and f["planes_credited"] is True
    assert "not_judged" not in f


def test_a_fractional_min_vias_rounds_up(tmp_path):
    vs, _ = _part(tmp_path, {"power_w": 0.2, "min_vias": 8.2}, vias=GRID[:8])
    e = _kind(vs, "thermal_vias", "error")
    assert e and e[0]["required"] == 9 and e[0]["vias"] == 8


def test_min_vias_zero_asks_none_but_credits_no_plane(tmp_path):
    """0 asks no via; the planes still need one to be credited."""
    vs, f = _part(tmp_path, {"power_w": 0.5, "min_vias": 0})
    assert not _kind(vs, "thermal_vias")
    assert f["planes_credit_vias"] == 1 and f["planes_credited"] is False
    assert len(f["not_judged"]) == 1


def test_a_lower_min_vias_is_the_credit_floor(tmp_path):
    """A datasheet that asks 2 vias, met, credits the planes."""
    vs, f = _part(tmp_path, {"power_w": 0.5, "min_vias": 2},
                  vias=[(19.5, 20), (20.5, 20)])
    assert f["planes_credit_vias"] == 2 and f["planes_credited"] is True
    assert vs == [] and "not_judged" not in f


@pytest.mark.parametrize("bad,match", [
    (-1, "count >= 0"), ("nan", "count >= 0"), ("inf", "count >= 0"),
    ("nine", "must be a number")])
def test_a_bad_min_vias_is_refused(tmp_path, bad, match):
    with pytest.raises(CheckError, match=match):
        _part(tmp_path, {"power_w": 0.2, "min_vias": bad})


def test_four_vias_credit_the_planes(tmp_path):
    vs, f = _part(tmp_path, {"power_w": 0.5}, vias=GRID[:4])
    assert f["planes_credited"] is True
    assert vs == [] and "not_judged" not in f


def test_uncredited_and_over_on_its_own_copper_is_not_judged(tmp_path):
    """0.5 W: ~26 C with the planes, ~69 C on the pad alone, 40 C allowed:
    no finding either way, and the report says why."""
    vs, f = _part(tmp_path, {"power_w": 0.5}, vias=[(20, 20)])
    assert vs == []
    assert f["rise_c"] == pytest.approx(0.5 * THETA_PLANES, abs=0.01)
    assert f["rise_side_c"] == pytest.approx(0.5 * THETA_SIDE, abs=0.01)
    assert f["power_limit_side_w"] == pytest.approx(40 / THETA_SIDE, abs=0.01)
    (line,) = f["not_judged"]
    assert line.startswith("U1: ~26 C rise") and "~69 C" in line
    assert "1 thermal via(s)" in line and "add >= 4 vias" in line
    assert "own F.Cu copper (4.0 mm2)" in line   # a decimal under 10 mm2


def test_over_even_with_the_planes_is_an_error_that_names_both(tmp_path):
    vs, f = _part(tmp_path, {"power_w": 1.0})
    (e,) = _kind(vs, "thermal_area", "error")
    assert e["rise_c"] == pytest.approx(THETA_PLANES, abs=0.01)
    assert e["rise_side_c"] == pytest.approx(THETA_SIDE, abs=0.01)
    assert "only 0 via(s) near it reach the GND planes" in e["msg"]
    assert "own F.Cu copper (4.0 mm2) it rises ~138 C" in e["msg"]
    assert "not_judged" not in f


def test_credited_error_has_no_own_side_clause(tmp_path):
    vs, _ = _part(tmp_path, {"power_w": 1.0}, vias=GRID)
    (e,) = _kind(vs, "thermal_area", "error")
    assert "rise_side_c" not in e and "own F.Cu" not in e["msg"]


def test_governor_judged_on_both_readings(tmp_path):
    """0.6 W at most: ~31 C with the planes needs no limit, ~83 C on the
    pad alone would; with no via the screen can't tell."""
    vs, f = _part(tmp_path, {"power_w": 0.2, "power_max_w": 0.6})
    assert vs == []
    assert f["rise_max_side_c"] == pytest.approx(0.6 * THETA_SIDE, abs=0.01)
    (line,) = f["not_judged"]
    assert "continuous maximum (0.6 W)" in line and "governor" in line


def test_governor_warning_gives_the_own_side_limit(tmp_path):
    vs, _ = _part(tmp_path, {"power_w": 0.2, "power_max_w": 1.0})
    (w,) = _kind(vs, "thermal_governor", "warning")
    assert "on its own F.Cu copper, with only 0 via(s)" in w["msg"]
    assert f"~{40 / THETA_SIDE:.1f} W" in w["msg"]


def test_a_datasheet_theta_keeps_min_vias_and_takes_no_side_reading(
        tmp_path):
    vs, f = _part(tmp_path, {
        "power_w": 0.5, "min_vias": 9, "theta_ja_c_w": 40.0,
        "theta_source": "vendor datasheet, JEDEC 2s2p"}, vias=[(20, 20)])
    assert _kind(vs, "thermal_vias", "error")
    assert "planes_credited" not in f and "not_judged" not in f


def test_the_report_lists_what_it_could_not_judge(tmp_path):
    pcb = _pcb(tmp_path, vias=[(20, 20)])
    cons = pcb.with_name("constraints.json")
    cons.write_text(json.dumps({"thermal": [
        {"ref": "U1", "net": "GND", "power_w": 0.5, "dt_c": 40}]}),
        encoding="utf-8")
    payload, _ = check_thermal.run(["--pcb", str(pcb),
                                    "--constraints", str(cons)])
    assert payload["status"] == "pass", json.dumps(payload, indent=1)
    (line,) = payload["not_judged"]
    assert line.startswith("U1:")
    assert all("not_judged" not in c for c in payload["checked"])


# #1181: past the plane credit the reading doesn't count vias, so the error
# on 4 layers names what would move it, not more vias.
@pytest.mark.parametrize("vias", [GRID[:4], GRID, ()], ids=["4", "9", "0"])
def test_four_layer_saturated_error_does_not_ask_for_vias(tmp_path, vias):
    vs, f = _part(tmp_path, {"power_w": 1.0}, vias=vias)
    (e,) = _kind(vs, "thermal_area", "error")
    assert "add thermal vias" not in e["msg"]
    assert "more vias won't change this reading" in e["msg"]
    assert "theta_ja_c_w with theta_source" in e["msg"]
    assert e["theta_ja"] == pytest.approx(THETA_PLANES, abs=0.01)


def test_four_and_nine_vias_read_the_same(tmp_path):
    """The premise: the remedy can't be vias because vias don't move it."""
    _, f4 = _part(tmp_path, {"power_w": 1.0}, vias=GRID[:4])
    _, f9 = _part(tmp_path, {"power_w": 1.0}, vias=GRID)
    assert f4["theta_ja"] == f9["theta_ja"]


def test_two_layer_saturated_error_still_names_its_levers(tmp_path):
    """The 2-layer path is untouched: it still errs, with its own remedy."""
    vs, _ = _part(tmp_path, {"power_w": 1.0}, vias=GRID, layers=2)
    (e,) = _kind(vs, "thermal_area", "error")
    assert "more vias won't change this reading" not in e["msg"]
