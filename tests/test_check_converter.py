"""check_converter (#355): a buck's or boost's inductor and switch against the
IC's current limits and minimum on-time, on synthetic boards.

Expected figures are worked by hand from the formulas the check cites:
TI SLVA477C eq. 1, 3, 4 (buck), TI SLVA372D eq. 2, 3, 5 (boost)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import geom  # noqa: E402
import check_converter as cv  # noqa: E402


def _board(tmp_path_factory, name: str, body: str) -> geom.BoardGeom:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


def _rpart(ref, x, pads, value=""):
    val = f'    (property "Value" "{value}" (at 0 0 0))\n' if value else ""
    body = "".join(
        f'    (pad "{i}" smd rect (at {i - 1} 0) (size 0.5 1) (layers "F.Cu") '
        f'(net "{net}")' + (f' (pinfunction "{fn}")' if fn else "") + ")\n"
        for i, (net, fn) in enumerate(pads, 1))
    return (f'  (footprint "t:{ref[0]}" (at {x} 8)\n    (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n{val}{body}  )\n')


def _buck(tmp_path_factory, name, ic="AP63203WU", henries="4.7uH"):
    """U1 bucks IN to OUT through L1 on /SW."""
    return _board(tmp_path_factory, name,
                  _rpart("U1", 10, [("IN", "VIN"), ("/SW", "SW"), ("GND", "GND"),
                                    ("OUT", "FB")], ic)
                  + _rpart("L1", 20, [("/SW", ""), ("OUT", "")], henries))


def _cons(vin=5, vout=3.3, iout=2.0, topology="buck", **op):
    out = "OUT"
    return {"voltages": [{"net": "IN", "voltage": vin},
                         {"net": out, "voltage": vout},
                         {"net": "GND", "voltage": 0}],
            "power": [{"net": out, "current_a": iout}] if iout is not None else [],
            "blocks": [{"topology": topology, "block": "B1",
                        "operating_point": dict(vout_v=vout, **op)}]}


def _kinds(vs, kind):
    return [v for v in vs if v["kind"] == kind]


TABLE = cv.load_table()


def test_buck_isat_below_peak_is_an_error(tmp_path_factory):
    # 5 V to 3.3 V, 4.7 uH, 1.1 MHz: dI = 1.7 * 3.3 / (5 * 1.1e6 * 4.7e-6)
    # = 0.217 A; Ipeak = 2 + 0.109 = 2.109 A, above a 2.0 A Isat.
    bg = _buck(tmp_path_factory, "isatlow")
    viols, recs, nj = cv.judge(bg, _cons(inductor_isat_a=2.0, vin_max_v=5.5),
                               TABLE)
    hit, = _kinds(viols, "converter_isat_low")
    assert hit["severity"] == "error" and hit["refs"] == ["L1", "U1"]
    assert hit["peak_a"] == pytest.approx(2.1085, abs=1e-3)
    assert recs[0]["ripple_a"] == pytest.approx(0.217, abs=1e-3)
    # 2.0 A is also under the AP63203's 2.8 A typical peak limit: a warning.
    assert _kinds(viols, "converter_isat_below_limit")[0]["severity"] == "warning"


def test_buck_quiet_twin(tmp_path_factory):
    bg = _buck(tmp_path_factory, "quiet")
    viols, recs, nj = cv.judge(bg, _cons(inductor_isat_a=3.5, vin_max_v=5.5),
                               TABLE)
    assert viols == [] and nj == []
    rec, = recs
    assert rec["status"] == "checked" and rec["limits_row"].startswith("AP6320")
    # Max load: peak limit 2.5 A min - dI/2 = 2.39 A (valley 2.5 + 0.11 is more).
    assert rec["iout_max_a"] == pytest.approx(2.3915, abs=1e-3)
    # 0.217 / 2 A rated = 11 %, under the datasheet's 30-50 %: noted, no finding.
    assert rec["ripple_in_band"] is False and rec["ripple_ratio"] == pytest.approx(0.109, abs=1e-3)


def test_buck_overload(tmp_path_factory):
    bg = _buck(tmp_path_factory, "overload")
    viols, _, _ = cv.judge(bg, _cons(iout=2.5, inductor_isat_a=4.0,
                                     vin_max_v=5.5), TABLE)
    hit, = _kinds(viols, "converter_overload")
    assert hit["severity"] == "error" and hit["iout_max_a"] < 2.5


def test_buck_min_on_time_at_vin_max(tmp_path_factory):
    # 12 V to 1.2 V at 1.1 MHz: 91 ns nominal passes 80 ns; at 15 V in it
    # is 73 ns.
    bg = _buck(tmp_path_factory, "ton")
    viols, recs, nj = cv.judge(bg, _cons(vin=12, vout=1.2, iout=1.0,
                                         inductor_isat_a=4.0, vin_max_v=15),
                               TABLE)
    hit, = _kinds(viols, "converter_min_on_time")
    assert hit["severity"] == "warning" and hit["ton_ns"] == pytest.approx(72.7, abs=0.1)
    assert "typical" in hit["msg"]
    viols, _, nj = cv.judge(bg, _cons(vin=12, vout=1.2, iout=1.0,
                                      inductor_isat_a=4.0), TABLE)
    assert not _kinds(viols, "converter_min_on_time")
    assert any("vin_max_v" in n for n in nj)


def test_missing_inputs_are_named(tmp_path_factory):
    bg = _buck(tmp_path_factory, "missing")
    viols, recs, nj = cv.judge(bg, _cons(iout=None), TABLE)
    assert viols == []
    assert any("no power entry current_a" in n for n in nj)
    viols, recs, nj = cv.judge(bg, {}, TABLE)
    assert recs == [] and nj == ["L1 on /SW: OUT has no declared voltage"]


def test_unlisted_ic_names_the_table(tmp_path_factory):
    bg = _buck(tmp_path_factory, "unlisted", ic="XYZ1234")
    viols, recs, nj = cv.judge(bg, _cons(inductor_isat_a=1.0, vin_max_v=5.5,
                                         fsw_khz=1000), TABLE)
    # Isat against the peak needs no row; the switch limits do.
    assert _kinds(viols, "converter_isat_low")
    assert any("no row in reference/converter_limits.yaml" in n for n in nj)


def test_lmr33630_isat_floor_quotes_the_datasheet(tmp_path_factory):
    # LMR33630B, 1.4 MHz: Isat 2.5 A clears a 1 A load's peak but is under the
    # 2.9 A low-side limit p. 22 says it must not be below.
    bg = _buck(tmp_path_factory, "lmr", ic="LMR33630BDDA", henries="4.7uH")
    viols, _, _ = cv.judge(bg, _cons(vin=12, vout=5, iout=1.0,
                                     inductor_isat_a=2.5, vin_max_v=13),
                           TABLE)
    assert not _kinds(viols, "converter_isat_low")
    hit, = _kinds(viols, "converter_isat_below_limit")
    assert hit["limit_a"] == 2.9 and "must not be less than" in hit["msg"]


def test_unverified_row_is_not_judged(tmp_path_factory, tmp_path):
    rows = [dict(r, verified=False) for r in TABLE]
    bg = _buck(tmp_path_factory, "unverified")
    viols, recs, nj = cv.judge(bg, _cons(iout=2.5, inductor_isat_a=4.0,
                                         vin_max_v=5.5), rows)
    assert viols == [] and recs[0]["status"] == "not_judged"
    assert any("[unverified:" in n for n in nj)


def _boost(tmp_path_factory, name):
    """U9 boosts IN (5 V) to OUT (12 V) through L9 (10 uH) and D9."""
    return _board(tmp_path_factory, name,
                  _rpart("U9", 10, [("IN", "VIN"), ("/SW", "SW")])
                  + _rpart("L9", 20, [("IN", ""), ("/SW", "")], "10uH")
                  + _rpart("D9", 30, [("/SW", ""), ("OUT", "")]))


def test_boost_peak_and_rhp_zero(tmp_path_factory):
    # D = 1 - 5/12 = 0.583; dI = 5 * 0.583 / (5e5 * 10e-6) = 0.583 A;
    # Ipeak = 0.292 + 0.5 / 0.417 = 1.492 A; RHP zero = 12 * 0.417^2 /
    # (2 pi * 10e-6 * 0.5) = 66.3 kHz.
    bg = _boost(tmp_path_factory, "boost")
    cons = _cons(vin=5, vout=12, iout=0.5, topology="boost", fsw_khz=500,
                 inductor_isat_a=1.2)
    viols, recs, nj = cv.judge(bg, cons, TABLE)
    hit, = _kinds(viols, "converter_isat_low")
    assert hit["peak_a"] == pytest.approx(1.4917, abs=1e-3)
    assert recs[0]["topology"] == "boost"
    assert recs[0]["rhp_zero_hz"] == pytest.approx(66315, rel=1e-3)
    cons["blocks"][0]["operating_point"]["inductor_isat_a"] = 2.0
    viols, _, _ = cv.judge(bg, cons, TABLE)
    assert viols == []


def test_cli_exit_codes(tmp_path_factory, tmp_path):
    bg = _buck(tmp_path_factory, "cli")
    c = tmp_path / "c.json"
    c.write_text(json.dumps(_cons(inductor_isat_a=2.0, vin_max_v=5.5)))
    out = tmp_path / "r.json"
    assert cv.main(["--pcb", str(bg.path), "--constraints", str(c),
                    "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert rep["converters"][0]["inductor"] == "L1"
    c.write_text(json.dumps(_cons(inductor_isat_a=3.5, vin_max_v=5.5)))
    assert cv.main(["--pcb", str(bg.path), "--constraints", str(c),
                    "--out", str(out)]) == 0
    assert cv.main(["--pcb", str(tmp_path / "none.kicad_pcb")]) == 2


def test_golden_quiet_twin():
    """The planted mutant's board and constraints with L1's Isat at 1.5 A, a
    fit part for a 0.509 A peak but still under the AP63203's 2.8 A typical
    peak limit: only that part-dependent warning remains."""
    d = ROOT / "tests" / "golden" / "mutants" / "buck-inductor-isat-low"
    cons = json.loads((d / "constraints.json").read_text())
    cons["blocks"][0]["operating_point"]["inductor_isat_a"] = 1.5
    bg = geom.load_board(d / "usbbuck4.kicad_pcb")
    viols, recs, nj = cv.judge(bg, cons, TABLE)
    assert [v["kind"] for v in viols] == ["converter_isat_below_limit"]
    assert recs[0]["peak_a"] == pytest.approx(0.5085, abs=1e-3) and nj == []
    cons["blocks"][0]["operating_point"]["inductor_isat_a"] = 3.0
    assert cv.judge(bg, cons, TABLE)[0] == []
