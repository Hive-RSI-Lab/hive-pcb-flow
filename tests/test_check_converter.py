"""check_converter (#355): a buck's or boost's inductor and switch against the
IC's current limits and minimum on-time, on synthetic boards.

Expected figures are worked by hand from the formulas the check cites:
TI SLVA477C eq. 1 to 4 (buck), TI SLVA372D eq. 2 to 4 (boost)."""
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


def _boost(tmp_path_factory, name, ic=""):
    """U9 boosts IN to OUT through L9 (10 uH) and D9."""
    return _board(tmp_path_factory, name,
                  _rpart("U9", 10, [("IN", "VIN"), ("/SW", "SW")], ic)
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


# The rows added for switcher families the corpus had none for. Each figure
# is worked from the row and the formulas above.

NEW_ROWS = ("TLV62569DBV/TLV62569PDDC", "TLV62569DRL/TLV62569PDRL", "TPS629203",
            "TPS62A01/TPS62A01A", "SY7208C", "LM5158", "LM51581", "LMR54406",
            "TLV61070A", "LMR38010", "SY8301", "SY7200A")


def test_new_rows_are_verified_and_cite_a_hashed_datasheet():
    by = {r["mpn"]: r for r in TABLE}
    for mpn in NEW_ROWS:
        r = by[mpn]
        assert r["verified"] is True and "sha256" in r["source"], mpn
        assert r.get("peak_limit_a") or r.get("valley_limit_a"), mpn


def test_tps629203_overload_and_quiet_twin(tmp_path_factory):
    # 5 V to 3.3 V, 1 uH at 2.5 MHz (set by the MODE pin, so no fsw row and
    # the block states it): dI = 1.7 * 3.3 / (5 * 2.5e6 * 1e-6) = 0.4488 A.
    # Peak limit 0.8 A min less dI/2 = 0.5756 A (valley 0.5 + 0.2244 is more).
    bg = _buck(tmp_path_factory, "tps629203", ic="TPS629203DRL", henries="1uH")
    over = _cons(iout=0.7, inductor_isat_a=2.0, vin_max_v=5.5, fsw_khz=2500)
    viols, recs, nj = cv.judge(bg, over, TABLE)
    hit, = _kinds(viols, "converter_overload")
    assert hit["iout_max_a"] == pytest.approx(0.5756, abs=1e-3)
    assert recs[0]["limits_row"] == "TPS629203"
    quiet = _cons(iout=0.3, inductor_isat_a=2.0, vin_max_v=5.5, fsw_khz=2500)
    viols, recs, nj = cv.judge(bg, quiet, TABLE)
    assert viols == [] and nj == []
    # 240 ns on at 5.5 V in clears the 40 ns typical minimum.
    assert recs[0]["ton_s"] == pytest.approx(3.3 / (5.5 * 2.5e6), rel=1e-6)


def test_tlv62569_package_sets_the_limit(tmp_path_factory):
    # 1.5 MHz from converter_fsw.yaml; 2.2 uH: dI = 5.61 / 16.5 = 0.34 A.
    # At 2.5 A out the DBV's 3.0 A limit allows 2.83 A, the DRL's 2.5 A only
    # 2.33 A.
    cons = _cons(iout=2.5, inductor_isat_a=4.0, vin_max_v=5.5)
    dbv = _buck(tmp_path_factory, "dbv", ic="TLV62569DBVR", henries="2.2uH")
    viols, recs, _ = cv.judge(dbv, cons, TABLE)
    assert not _kinds(viols, "converter_overload")
    assert recs[0]["fsw_hz"] == 1.5e6
    assert recs[0]["iout_max_a"] == pytest.approx(2.83, abs=1e-3)
    drl = _buck(tmp_path_factory, "drl", ic="TLV62569DRLR", henries="2.2uH")
    hit, = _kinds(cv.judge(drl, cons, TABLE)[0], "converter_overload")
    assert hit["iout_max_a"] == pytest.approx(2.33, abs=1e-3)


def test_valley_only_boost_overload_and_quiet_twin(tmp_path_factory):
    # TLV61070A, 3.3 V to 5 V, 10 uH at 1 MHz: D = 0.34, dI = 3.3 * 0.34 /
    # (1e6 * 10e-6) = 0.1122 A. SLVSGK6B eq. 1: (1.80 + 0.0561) * 0.66
    # = 1.2251 A.
    bg = _boost(tmp_path_factory, "tlv61070a", ic="TLV61070ADBVR")
    cons = _cons(vin=3.3, vout=5, iout=1.5, topology="boost", fsw_khz=1000,
                 inductor_isat_a=3.0)
    viols, recs, _ = cv.judge(bg, cons, TABLE)
    hit, = viols
    assert hit["kind"] == "converter_overload"
    assert hit["iout_max_a"] == pytest.approx(1.2251, abs=1e-3)
    assert "SLVSGK6B eq. 1" in hit["msg"] and "valley limit 1.8 A" in hit["msg"]
    cons["power"][0]["current_a"] = 0.5
    viols, recs, _ = cv.judge(bg, cons, TABLE)
    assert viols == [] and recs[0]["limits_row"] == "TLV61070A"


def test_lmr54406_isat_floor_and_ripple_band(tmp_path_factory):
    # 12 V to 3.3 V, 10 uH at 1.1 MHz: dI = 8.7 * 3.3 / (12 * 1.1e6 * 10e-6)
    # = 0.2175 A, 36 % of the rated 0.6 A (inside p. 20's 20-60 %). p. 20's
    # floor is the high-side limit, 0.85 A min: Isat 1.0 A passes, 0.8 A
    # (still above the 0.609 A peak) does not.
    bg = _buck(tmp_path_factory, "lmr54406", ic="LMR54406FDBVR", henries="10uH")
    cons = _cons(vin=12, vout=3.3, iout=0.5, inductor_isat_a=1.0, vin_max_v=13)
    viols, recs, nj = cv.judge(bg, cons, TABLE)
    assert viols == [] and nj == [] and recs[0]["ripple_in_band"] is True
    cons["blocks"][0]["operating_point"]["inductor_isat_a"] = 0.8
    hit, = cv.judge(bg, cons, TABLE)[0]
    assert hit["kind"] == "converter_isat_below_limit" and hit["limit_a"] == 0.85
    assert "must be higher than peak" in hit["msg"]
