"""check_thermal judges a no-pad converter from a load the design states.

On the training boards 41 no-pad bucks and 12 LDOs were candidates with
no load. Each stated-load source now has a planted fault and a quiet twin:
the output rail's summed loads (a buck and an LDO), and the buck's
datasheet current limit as a ceiling (a screen). Where an LDO's Vin or
Vout can't be worked out, the candidate says which. Pure tests on
synthetic boards; no toolchain.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_thermal_inferred import _board, _run  # noqa: E402

import check_thermal  # noqa: E402


def _part(ref, value, lib, pins, at=(20, 20)):
    """A footprint whose pads carry pin names: PINS is [(net, function)]."""
    body = "".join(
        f'    (pad "{i + 1}" smd rect (at {-1.1 if i % 2 else 1.1} '
        f'{-1.5 + 0.6 * i:.1f}) (size 0.6 0.4) (layers "F.Cu") '
        f'(net "{net}") (pinfunction "{fn}"))\n'
        for i, (net, fn) in enumerate(pins))
    return (f'  (footprint "{lib}" (at {at[0]} {at[1]}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            f'{body})\n')


def _buck(value="LMR54406DBVR", lib="Package_TO_SOT_SMD:SOT-23-6",
          extra_loads=("U3",)):
    """A buck: SW to a 2-pad inductor to +3V3, a feedback divider and an
    output cap on +3V3 (draw nothing), and a load part per EXTRA_LOADS."""
    parts = [
        _part("U2", value, lib, [("GND", "GND"), ("/sw", "SW"),
                                 ("+12V", "VIN"), ("/fb", "FB"),
                                 ("/en", "EN"), ("/cb", "CB")]),
        _part("L1", "4.7uH", "Inductor_SMD:L_1210", [("/sw", "1"),
                                                     ("+3V3", "2")],
              at=(26, 20)),
        _part("R1", "100k", "Resistor_SMD:R_0402", [("+3V3", "1"),
                                                    ("/fb", "2")],
              at=(26, 26)),
        _part("C1", "22u", "Capacitor_SMD:C_0805", [("+3V3", "1"),
                                                    ("GND", "2")],
              at=(30, 26)),
    ]
    for i, ref in enumerate(extra_loads):
        parts.append(_part(ref, "MCU", "Package_QFP:LQFP-48",
                           [("+3V3", "VDD"), ("GND", "VSS")],
                           at=(10 + 4 * i, 32)))
    return "".join(parts)


def _loads(**amps):
    return {"rail_budget": {"loads": {r: {"current_a": a}
                                      for r, a in amps.items()}}}


def _facts(p, ref="U2"):
    return next((f for f in p.get("checked", []) if f.get("ref") == ref), None)


def _cand(p, ref="U2"):
    return next((c for c in p.get("candidates", []) if c["ref"] == ref), None)


# ---- source 1: the output rail's summed stated loads -------------------

def test_planted_rail_sum_over_its_junction_fires(tmp_path):
    """Planted fault: an LMR54406 (450 mOhm high side, 173 C/W SOT-23-6)
    feeding a 1.1 A stated load: 1.1^2 x 0.45 x 1.5 = 0.82 W, Tj ~166 C
    from 25 C air, past its 150 C."""
    p = _run(_board(tmp_path, _buck()), _loads(U3=1.1))
    [v] = [v for v in p["violations"] if v["kind"] == "thermal_stated_load"]
    assert v["refs"] == ["U2"] and v["severity"] == "warning"
    assert v["tj_c"] > 150 and "+3V3" in v["msg"]
    f = _facts(p)
    assert f["load_source"] == "rail_sum" and f["load_a"] == 1.1
    assert _cand(p) is None


def test_quiet_rail_sum_under_its_junction_is_a_checked_fact(tmp_path):
    """Quiet twin: the same buck at 0.5 A (0.17 W, ~54 C) is judged, not
    listed, and fires nothing."""
    p = _run(_board(tmp_path, _buck()), _loads(U3=0.5))
    assert not [v for v in p["violations"]
                if v["kind"] == "thermal_stated_load"]
    f = _facts(p)
    assert f["load_source"] == "rail_sum" and f["full"] is True
    assert f["tj_c"] < 150 and _cand(p) is None


def test_a_rail_with_an_uncounted_load_is_not_judged_from_the_sum(tmp_path):
    """U4 on the rail states no current: the sum is not the rail's load,
    so a low sum can't clear it (it falls to the current limit)."""
    p = _run(_board(tmp_path, _buck(extra_loads=("U3", "U4"))),
             _loads(U3=0.2))
    f = _facts(p)
    assert f is None or f.get("load_source") != "rail_sum"
    c = _cand(p)
    assert c["reason"] == "over_at_limit" and "U4" in c["detail"]


# ---- source 2: the converter's datasheet current limit -----------------

def test_planted_current_limit_over_stays_a_candidate(tmp_path):
    """Planted fault: no stated load, and at its 1.3 A limit an LMR54406
    would reach ~222 C: the limit doesn't bound it, said as such."""
    p = _run(_board(tmp_path, _buck()))
    c = _cand(p)
    assert c["reason"] == "over_at_limit" and c["load_source"] == \
        "current_limit"
    assert c["i_limit_a"] == 1.3 and c["tj_at_limit_c"] > 150
    assert "a screen" in c["detail"]
    assert not [v for v in p["violations"]
                if v["kind"] == "thermal_stated_load"]


def test_quiet_current_limit_under_is_a_screened_fact(tmp_path):
    """Quiet twin: a TPS629203 (250 mOhm, 120 C/W SOT-583) at its 1.4 A
    limit loses 0.74 W, ~113 C, under its 125 C: judged as a screen."""
    p = _run(_board(tmp_path, _buck(value="TPS629203DRLR",
                                    lib="Package_TO_SOT_SMD:SOT-583-8")))
    f = _facts(p)
    assert f["load_source"] == "current_limit" and f["screen"] is True
    assert f["i_limit_a"] == 1.4 and f["tj_at_limit_c"] < 125
    assert _cand(p) is None


def test_no_output_rail_says_so(tmp_path):
    """A buck whose SW pin reaches no inductor: the reason says why."""
    body = _part("U2", "LMR54406DBVR", "Package_TO_SOT_SMD:SOT-23-6",
                 [("GND", "GND"), ("/sw", "SW"), ("+12V", "VIN")])
    c = _cand(_run(_board(tmp_path, body), _loads(U3=0.5)))
    assert c["reason"] == "no_load" and "no output rail" in c["detail"]


# ---- LDO: (Vin - Vout) x I from the rails and the stated load ----------

def _ldo(vin="+12V", vout="+3V3", value="AMS1117-3.3"):
    return (_part("U2", value, "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
                  [("GND", "GND"), (vout, "VOUT"), (vin, "VIN")])
            + _part("U3", "MCU", "Package_QFP:LQFP-48",
                    [(vout, "VDD"), ("GND", "VSS")], at=(10, 32)))


def test_planted_ldo_over_from_a_stated_load_fires(tmp_path):
    """Planted fault: AMS1117-3.3 from 12 V at a stated 0.3 A:
    8.7 V x 0.3 A = 2.61 W x 46 C/W = 145 C rise, past 125 C."""
    p = _run(_board(tmp_path, _ldo()), _loads(U3=0.3))
    [v] = [v for v in p["violations"] if v["kind"] == "thermal_stated_load"]
    assert v["refs"] == ["U2"] and v["power_w"] == 2.61
    f = _facts(p)
    assert f["kind"] == "ldo" and f["vin_v"] == 12 and f["vout_v"] == 3.3
    assert f["judged_by"] == "check_thermal" and _cand(p) is None


def test_quiet_ldo_under_is_a_checked_fact(tmp_path):
    """Quiet twin: the same LDO at 50 mA (0.44 W, ~45 C) is judged and
    fires nothing."""
    p = _run(_board(tmp_path, _ldo()), _loads(U3=0.05))
    assert not [v for v in p["violations"]
                if v["kind"] == "thermal_stated_load"]
    f = _facts(p)
    assert f["power_w"] == check_thermal.checklib.rnd(8.7 * 0.05)
    assert f["tj_c"] < 125 and _cand(p) is None


def test_ldo_with_no_input_volts_says_so(tmp_path):
    c = _cand(_run(_board(tmp_path, _ldo(vin="/vraw")), _loads(U3=0.3)))
    assert c["reason"] == "ldo_no_vin" and "/vraw" in c["detail"]


def test_ldo_with_no_output_volts_says_so(tmp_path):
    c = _cand(_run(_board(tmp_path, _ldo(vout="/vo", value="AMS1117")),
                   _loads(U3=0.3)))
    assert c["reason"] == "ldo_no_vout" and "/vo" in c["detail"]


def test_ldo_with_no_stated_load_says_so(tmp_path):
    c = _cand(_run(_board(tmp_path, _ldo())))
    assert c["reason"] == "ldo_no_load" and "+3V3" in c["detail"]


def test_kicad_pin_number_suffixes_still_name_the_pins(tmp_path):
    """KiCad writes VIN_3 / VOUT_2 where a symbol repeats a pin name: the
    _N suffix is not part of the name."""
    body = (_part("U2", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3",
                  [("GND", "GND_1"), ("+3V3", "VOUT_2"), ("+12V", "VIN_3")])
            + _part("U3", "MCU", "Package_QFP:LQFP-48",
                    [("+3V3", "VDD"), ("GND", "VSS")], at=(10, 32)))
    f = _facts(_run(_board(tmp_path, body), _loads(U3=0.05)))
    assert f["vin_v"] == 12 and f["vout_v"] == 3.3


def test_role_named_divider_and_caps_draw_nothing(tmp_path):
    """Rfbt1 / Cout2 (2 pads) are the feedback divider and output caps,
    not uncounted loads: the rail sum stays full."""
    body = _buck() + _part("Rfbt1", "69.8k", "Resistor_SMD:R_0402",
                           [("+3V3", "1"), ("/fb", "2")], at=(32, 10)) \
        + _part("Cout2", "10uF", "Capacitor_SMD:C_0805",
                [("+3V3", "1"), ("GND", "2")], at=(36, 10))
    f = _facts(_run(_board(tmp_path, body), _loads(U3=0.5)))
    assert f["load_source"] == "rail_sum" and f["full"] is True


def test_an_inductor_to_ground_is_no_output_rail(tmp_path):
    """An inverting stage's inductor runs from SW to ground: ground is not
    the converter's output, so nothing on it is summed as its load."""
    body = (_part("U2", "LMR54406DBVR", "Package_TO_SOT_SMD:SOT-23-6",
                  [("/vneg", "GND"), ("/sw", "SW"), ("+12V", "VIN")])
            + _part("L1", "4.7uH", "Inductor_SMD:L_1210",
                    [("/sw", "1"), ("GND", "2")], at=(26, 20)))
    c = _cand(_run(_board(tmp_path, body), _loads(U3=0.5)))
    assert c["reason"] == "no_load" and "no output rail" in c["detail"]


def test_a_buck_with_no_pin_names_says_so(tmp_path):
    """Pads with no pin names: the SW or VOUT pin can't be told, and the
    candidate says that rather than the general reason."""
    body = (_part("U2", "LMR54406DBVR", "Package_TO_SOT_SMD:SOT-23-6",
                  [("GND", ""), ("/sw", ""), ("+12V", "")])
            + _part("L1", "4.7uH", "Inductor_SMD:L_1210",
                    [("/sw", "1"), ("+3V3", "2")], at=(26, 20)))
    c = _cand(_run(_board(tmp_path, body), _loads(U3=0.5)))
    assert c["reason"] == "no_load" and "no pin names" in c["detail"]
