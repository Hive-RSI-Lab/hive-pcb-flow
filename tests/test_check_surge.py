"""check_surge + surgelib (#492). Known answers: the combination-wave
generator against IEC 61000-4-5's waveform table, the SMBJ20A's datasheet line
through its SPICE model, the current arithmetic, the level table by
declaration; a planted fault on pinned lite-main (the VBUS SMBJ swapped for a
USB ESD array)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_surge  # noqa: E402
import surgelib  # noqa: E402

LITE = ROOT / "pinned" / "lite-main" / "kicad" / "lite-main.kicad_pcb"
LITE_PARTS = ROOT / "pinned" / "lite-main" / "parts"


def _spice(decks):
    env = pytest.importorskip("env")
    dll = env.find_ngspice_dll()
    if dll is None:
        pytest.skip("no ngspice library")
    import sim_board
    return sim_board.run_engine(decks, dll, 120.0)[0]


# ------------------------------------------------------------- the generator

@pytest.mark.parametrize("kv", [0.5, 1.0, 2.0])
def test_generator_meets_the_standards_waveform(kv):
    """IEC 61000-4-5 Ed.3 Tables 2/3: 1.2/50 us +-30/20 % open circuit, 8/20
    us +-20/20 % short circuit, peaks +-10 %, 500 A per kV (2 ohm)."""
    op, sc = _spice([surgelib.load_deck(kv, 1e6), surgelib.load_deck(kv, 1e-3)])
    v = surgelib.voltage_shape(op["time"], op["o"])
    i = surgelib.current_shape(sc["time"], [x / 1e-3 for x in sc["o"]])
    assert v["peak"] == pytest.approx(kv * 1000, rel=0.10)
    assert v["t1_s"] == pytest.approx(1.2e-6, rel=0.30)
    assert v["td_s"] == pytest.approx(50e-6, rel=0.20)
    assert i["peak"] == pytest.approx(kv * 500, rel=0.10)
    assert i["t1_s"] == pytest.approx(8e-6, rel=0.20)
    assert i["td_s"] == pytest.approx(20e-6, rel=0.20)
    assert v["peak"] / i["peak"] == pytest.approx(2.0, rel=0.10)


# ----------------------------------------------------------------- the clamp

def test_smbj20a_family_matches_its_datasheet_line():
    c = surgelib.family_clamp("SMBJ20A")
    assert c["vrwm"] == 20
    assert 22.2 <= c["vbr"] <= 24.5          # Vbr min / max
    assert c["vc"] == pytest.approx(32.4)
    assert c["ipp_1000_a"] == pytest.approx(18.5, abs=0.1)
    assert c["vbr"] + c["rdyn"] * 18.5 == pytest.approx(32.4, abs=0.1)
    assert c["estimated"]


def test_smbj20a_spice_model_reproduces_vc_at_ipp():
    """Datasheet: Vc max 32.4 V at 18.5 A. The diode card, driven at 18.5 A
    in reverse, lands within the datasheet's 5 % Vbr window."""
    c = surgelib.family_clamp("SMBJ20A")
    deck = "\n".join(["* vc at ipp", "I1 0 k 18.5", "D1 0 k dtvs",
                      surgelib.diode_card(c), ".op", ".end", ""])
    plot = _spice([deck])[0]
    assert plot["k"][0] == pytest.approx(32.4, rel=0.05)


def test_closed_form_and_the_wave_agree():
    """The closed form I = (V - Vbr)/(Z + Rdyn) against the full transient,
    with the generator's 2 ohm inside it: a TVS on a 12 ohm line-earth path."""
    c = surgelib.family_clamp("SMBJ20A")
    got = surgelib.clamped(500.0, 12.0, c)
    plot = _spice([surgelib.clamp_deck(0.5, 10.0, c)])[0]
    assert max(plot["s"]) / 1e-3 == pytest.approx(got["i_a"], rel=0.10)
    assert max(plot["c"]) == pytest.approx(got["vc_v"], rel=0.10)


def test_current_arithmetic():
    c = surgelib.clamp_model(vbr=10.0, vc=20.0, ipp_a=10.0)
    assert c["rdyn"] == pytest.approx(1.0)
    got = surgelib.clamped(1000.0, 42.0, c)
    assert got["i_a"] == pytest.approx(990.0 / 43.0)
    assert got["vc_v"] == pytest.approx(10.0 + 990.0 / 43.0)
    assert surgelib.clamped(5.0, 2.0, c)["i_a"] == 0.0        # below Vbr


def test_clamp_lookup_order():
    table = surgelib.load_clamp_table()
    assert surgelib.find_clamp(["USBLC6-2SC6"], table)["ipp_a"] == 5
    assert surgelib.find_clamp(["SMCJ24A"], table)["estimated"]
    assert surgelib.find_clamp(["BAT54"], table) is None
    over = {"vbr": 30, "vc": 40, "ipp_a": 200}
    assert surgelib.find_clamp(["SMBJ20A"], table, over)["ipp_a"] == 200


# ---------------------------------------------------------------- the levels

def _levels(port, env="residential"):
    table = surgelib.load_levels()["environments"][env]
    return check_surge.port_levels(port, table)


def test_short_dc_cable_has_no_surge_level():
    modes, eft = _levels({"kind": "dc_power", "cable_m": 2})
    assert modes == [] and eft is None


def test_long_dc_cable_floating_is_line_line_only():
    modes, eft = _levels({"kind": "dc_power", "cable_m": 40})
    assert [(m["mode"], m["kv"], m["z_ohm"]) for m in modes] == [("ll", 0.5, 0)]
    assert eft == 0.5


def test_earth_adds_the_line_earth_mode_through_10_ohm():
    modes, _ = _levels({"kind": "dc_power", "cable_m": 40, "earthed": True})
    assert ("le", 0.5, 10) in [(m["mode"], m["kv"], m["z_ohm"]) for m in modes]


def test_outdoor_signal_goes_through_40_ohm():
    modes, _ = _levels({"kind": "signal", "cable_m": 1, "outdoor": True})
    assert [(m["mode"], m["kv"], m["z_ohm"]) for m in modes] == [("le", 1.0, 40)]


def test_mains_always_applies_and_level_can_be_forced():
    modes, eft = _levels({"kind": "mains"})
    assert modes[0]["kv"] == 1.0 and eft == 1.0
    assert _levels({"kind": "mains"}, "industrial")[1] == 2.0
    modes, _ = _levels({"kind": "dc_power", "cable_m": 1, "level_kv": 0.1})
    assert modes[0]["kv"] == 0.1


# --------------------------------------------------------- a board in a string

LAYERS = ('(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))')


def _part(ref, value, x, nets, fpname=None, y=0.0):
    pads = " ".join(
        f'(pad "{i + 1}" smd rect (at {i * 2.0} 0) (size 1 1) '
        f'(layers "F.Cu") (net "{n}"))' for i, n in enumerate(nets))
    return (f'(footprint "Lib:{fpname or value}" (layer "F.Cu") (at {x} {y})\n'
            f' (property "Reference" "{ref}" (at 0 -1 0) (layer "F.SilkS"))\n'
            f' (property "Value" "{value}" (at 0 1 0) (layer "F.Fab"))\n'
            f' {pads})\n')


def _board(tmp_path, parts) -> Path:
    names = sorted({n for p in parts for n in p[3]})
    decl = " ".join(f'(net {i + 1} "{n}")' for i, n in enumerate(names))
    body = "".join(_part(*p, y=10.0 * k) for k, p in enumerate(parts))
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(f'(kicad_pcb (version 20240108) (generator "t")\n{LAYERS}\n'
                   f'(net 0 "") {decl}\n{body})\n', encoding="utf-8")
    return pcb


def _kinds(viols) -> set:
    return {v["kind"] for v in viols}


PORT = {"kind": "signal", "cable_m": 40, "nets": ["LINE*"], "level_kv": 1.0}


def test_no_declared_ports_judges_nothing(tmp_path):
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"])])
    viols, facts = check_surge.check(pcb, {})
    assert viols == [] and facts["ports_declared"] == 0


def test_unclamped_line_fails_but_ethernet_rests_on_magnetics(tmp_path):
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"]),
                            ("U1", "LM358", 20, ["LINE", "GND"])])
    viols, _ = check_surge.check(pcb, {"surge": {"ports": {"J1": PORT}}})
    assert _kinds(viols) == {"surge_unclamped"}
    assert viols[0]["severity"] == "error" and viols[0]["assumption"] is True
    eth = {"kind": "ethernet", "outdoor": True, "nets": ["LINE*"]}
    viols, _ = check_surge.check(pcb, {"surge": {"ports": {"J1": eth}}})
    assert _kinds(viols) == {"surge_relies_on_magnetics"}
    assert viols[0]["severity"] == "warning"


def test_undeclared_port_is_not_judged(tmp_path):
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"]),
                            ("J2", "CONN", 20, ["OTHER", "GND"]),
                            ("U1", "LM358", 40, ["OTHER", "GND"]),
                            ("U2", "LM358", 60, ["LINE", "GND"])])
    viols, facts = check_surge.check(pcb, {"surge": {"ports": {"J1": PORT}}})
    assert {v["net"] for v in viols} == {"LINE"}
    assert [l["port"] for l in facts["lines"]] == ["J1"]


def test_series_resistor_counts_toward_the_clamps_current(tmp_path):
    """1 kV through 42 ohm + 10 ohm into a SMBJ: I known from the closed
    form, the walk finds the clamp behind the resistor."""
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"]),
                            ("R1", "10R", 20, ["LINE", "LINE2"]),
                            ("D1", "SMBJ20A", 40, ["LINE2", "GND"])])
    viols, facts = check_surge.check(pcb, {"surge": {"ports": {"J1": PORT}}})
    row = facts["lines"][0]["modes"][0]
    c = surgelib.family_clamp("SMBJ20A")
    assert row["z_ohm"] == pytest.approx(52.0)
    assert row["i_a"] == pytest.approx(
        surgelib.clamped(1000.0, 52.0, c)["i_a"], abs=0.1)
    assert row["i_simple_a"] == pytest.approx(1000 / 52, abs=0.1)
    assert "surge_clamp_overstress" not in _kinds(viols)


def test_big_resistor_isolates_what_is_behind_it(tmp_path):
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"]),
                            ("D1", "SMBJ20A", 20, ["LINE", "GND"]),
                            ("R1", "100k", 40, ["LINE", "SENSE"]),
                            ("U1", "LM358", 60, ["SENSE", "GND"])])
    cons = {"surge": {"ports": {"J1": PORT}, "abs_max": {"U1": 5}}}
    viols, facts = check_surge.check(pcb, cons)
    assert "SENSE" not in facts["lines"][0]["reach"]
    assert "surge_vc_over_abs_max" not in _kinds(viols)


def test_vc_over_abs_max_then_a_secondary_clamp_cures_it(tmp_path):
    parts = [("J1", "CONN", 0, ["LINE", "GND"]),
             ("D1", "SMBJ20A", 20, ["LINE", "GND"]),
             ("R1", "10R", 40, ["LINE", "LINE2"]),
             ("U1", "LM358", 60, ["LINE2", "GND"])]
    abs_max = {"U1.1": 12}
    viols, _ = check_surge.check(
        _board(tmp_path, parts),
        {"surge": {"ports": {"J1": PORT}, "abs_max": abs_max}})
    hit = [v for v in viols if v["kind"] == "surge_vc_over_abs_max"]
    assert hit and "U1" in hit[0]["refs"]
    assert "secondary clamp" in hit[0]["msg"]
    # an array behind the 10 ohm holds the pin under 12 V
    parts.append(("D2", "SRV05-4", 80, ["LINE2", "GND"]))
    viols, facts = check_surge.check(
        _board(tmp_path, parts),
        {"surge": {"ports": {"J1": PORT}, "abs_max": abs_max}})
    assert "surge_vc_over_abs_max" not in _kinds(viols)
    row = facts["lines"][0]["modes"][0]
    assert row["secondary"] == "D2" and row["secondary_vc_v"] < 12


def test_unrated_clamp_warns_and_unrated_pin_is_listed(tmp_path):
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"]),
                            ("D1", "PESD_mystery", 20, ["LINE", "GND"]),
                            ("D2", "SMBJ20A", 30, ["LINE", "GND"]),
                            ("U1", "LM358", 40, ["LINE", "GND"])])
    viols, facts = check_surge.check(pcb, {"surge": {"ports": {"J1": PORT}}})
    assert "surge_clamp_unrated" in _kinds(viols)
    assert "U1.1" in facts["unrated_pins"]


def test_eft_judges_vc_not_the_8_20_rating(tmp_path):
    """0.5 kV through 50 ohm is ~9 A: more than a USB array's 5 A 8/20, but
    a 5/50 ns pulse isn't an 8/20 one: no overstress, Vc still judged."""
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"]),
                            ("D1", "USBLC6-2SC6", 20, ["LINE", "GND"]),
                            ("U1", "LM358", 40, ["LINE", "GND"])])
    port = {"kind": "signal", "cable_m": 5, "nets": ["LINE"]}
    viols, facts = check_surge.check(
        pcb, {"surge": {"ports": {"J1": port}, "abs_max": {"U1": 10}}})
    assert facts["lines"][0]["eft"]["level_kv"] == 0.5
    assert _kinds(viols) == {"surge_eft_over_abs_max"}
    assert viols[0]["severity"] == "warning"


# ------------------------------------------- series parts and the other polarity

def test_8_20_i2t_known_answer():
    """I2t of the 8/20 current per A^2 of peak: the analytic wave
    integrated numerically, and its shape by the standard's 10-90 % and
    50 % definitions."""
    n, dt = 400000, 1e-10
    ts = [k * dt for k in range(n)]
    i2 = sum(surgelib.wave_8_20(t) ** 2 for t in ts) * dt
    assert surgelib.I2T_PER_A2 == pytest.approx(i2, rel=0.01)
    assert surgelib.pulse_i2t(100.0) == pytest.approx(1e4 * i2, rel=0.01)
    sh = surgelib.current_shape(ts, [surgelib.wave_8_20(t) for t in ts])
    assert sh["peak"] == pytest.approx(1.0, rel=0.01)
    assert sh["t1_s"] == pytest.approx(8e-6, rel=0.05)
    assert sh["td_s"] == pytest.approx(20e-6, rel=0.10)


def test_generator_short_circuit_i2t_matches_the_closed_form():
    """The generator into a short, integrated: I2t / Ipk^2 within 20 % of
    the closed form the series check uses."""
    sc = _spice([surgelib.load_deck(1.0, 1e-3)])[0]
    t, i = sc["time"], [x / 1e-3 for x in sc["o"]]
    i2t = sum((i[k] ** 2 + i[k + 1] ** 2) / 2 * (t[k + 1] - t[k])
              for k in range(len(t) - 1))
    assert i2t / max(i) ** 2 == pytest.approx(surgelib.I2T_PER_A2, rel=0.20)


def test_resistor_pulse_table_reads_the_chart():
    assert surgelib.resistor_pulse_w("0603", 10e-6) == pytest.approx(19.9)
    assert 15.8 < surgelib.resistor_pulse_w("0603") < 19.9      # 12.2 us
    assert surgelib.resistor_pulse_w("2512") > 200
    assert surgelib.resistor_pulse_w(None) is None


SERIES = [("J1", "CONN", 0, ["LINE", "GND"]),
          ("D1", "SMBJ20A", 20, ["LINE", "GND"]),
          ("R1", "10R", 40, ["LINE", "LINE2"], "R_0603_1608Metric"),
          ("D2", "SRV05-4", 60, ["LINE2", "GND"])]


def test_planted_small_resistor_between_two_clamps_overstresses(tmp_path):
    """1 kV into the SMBJ20A holds LINE near 37 V; 10 ohm to the SRV05-4
    behind it is ~2.7 A, ~73 W for 8/20 us: over an 0603's ~18 W."""
    cons = {"surge": {"ports": {"J1": PORT}}}
    viols, facts = check_surge.check(_board(tmp_path, SERIES), cons)
    bad = [v for v in viols if v["kind"] == "surge_series_overstress"]
    assert bad and "R1" in bad[0]["refs"] and bad[0]["severity"] == "error"
    row = facts["series"][0]
    assert row["ref"] == "R1" and row["pulse_w"] < 20
    assert row["p_w"] == pytest.approx(73, rel=0.15)
    # the quiet twin: a 2512, or a pulse rating the designer gives
    twin = SERIES[:2] + [("R1", "10R", 40, ["LINE", "LINE2"],
                          "R_2512_6332Metric"), SERIES[3]]
    viols, _ = check_surge.check(_board(tmp_path, twin), cons)
    assert "surge_series_overstress" not in _kinds(viols)
    cons["surge"]["series"] = {"R1": {"pulse_w": 150}}
    viols, facts = check_surge.check(_board(tmp_path, SERIES), cons)
    assert "surge_series_overstress" not in _kinds(viols)
    assert facts["series"][0]["pulse_source"] == "constraints surge.series"


def test_fuse_i2t_opens_and_unrated_series_parts_are_listed(tmp_path):
    parts = [("J1", "CONN", 0, ["LINE", "GND"]),
             ("F1", "PTC", 20, ["LINE", "LINE2"]),
             ("D1", "SMBJ20A", 40, ["LINE2", "GND"])]
    cons = {"surge": {"ports": {"J1": PORT}}}
    viols, facts = check_surge.check(_board(tmp_path, parts), cons)
    assert facts["series_unrated"] == ["F1"]
    cons["surge"]["series"] = {"F1": {"i2t_a2s": 0.001}}
    viols, _ = check_surge.check(_board(tmp_path, parts), cons)
    hit = [v for v in viols if v["kind"] == "surge_fuse_opens"]
    assert hit and hit[0]["severity"] == "warning"       # ~23 A: ~6.4e-3 A2s
    cons["surge"]["series"] = {"F1": {"i2t_a2s": 0.1}}
    viols, _ = check_surge.check(_board(tmp_path, parts), cons)
    assert "surge_fuse_opens" not in _kinds(viols)


NEG = [("J1", "CONN", 0, ["LINE", "GND"]),
       ("D1", "SMBJ20A", 20, ["LINE", "GND"]),
       ("U1", "LM358", 40, ["LINE", "GND"])]


def test_planted_unidirectional_clamp_forward_under_abs_min(tmp_path):
    """The negative 1 kV pulse forward-biases a SMBJ20A: ~23 A at ~2 V, under
    a pin's -0.5 V; the bidirectional SMBJ20CA holds it at -Vc instead."""
    cons = {"surge": {"ports": {"J1": PORT}, "abs_max": {"U1": 40},
                      "abs_min": {"U1": -0.5}}}
    viols, facts = check_surge.check(_board(tmp_path, NEG), cons)
    bad = [v for v in viols if v["kind"] == "surge_negative_under_abs_min"]
    assert bad and bad[0]["severity"] == "error" and "U1" in bad[0]["refs"]
    assert "unidirectional" in bad[0]["msg"]
    neg = facts["lines"][0]["modes"][0]["negative"]
    assert neg["conducts"] == "forward" and -3.5 < neg["vc_v"] < -0.7
    bi = [NEG[0], ("D1", "SMBJ20CA", 20, ["LINE", "GND"]), NEG[2]]
    cons["surge"]["abs_min"] = {"U1": -40}
    viols, facts = check_surge.check(_board(tmp_path, bi), cons)
    assert "surge_negative_under_abs_min" not in _kinds(viols)
    neg = facts["lines"][0]["modes"][0]["negative"]
    pos = facts["lines"][0]["modes"][0]
    assert neg["conducts"] == "reverse"
    assert neg["vc_v"] == pytest.approx(-pos["vc_v"])


def test_unknown_direction_is_not_judged_negative(tmp_path):
    parts = [NEG[0], ("D1", "TVS", 20, ["LINE", "GND"]), NEG[2]]
    cons = {"surge": {"ports": {"J1": PORT}, "abs_min": {"U1": -0.5},
                      "clamps": {"D1": {"vbr": 23, "vc": 32, "ipp_a": 100}}}}
    viols, facts = check_surge.check(_board(tmp_path, parts), cons)
    assert "surge_negative_under_abs_min" not in _kinds(viols)
    assert facts["negative_not_judged"] == ["D1"]
    cons["surge"]["clamps"]["D1"]["vf_v"] = 1.5
    viols, _ = check_surge.check(_board(tmp_path, parts), cons)
    assert "surge_negative_under_abs_min" in _kinds(viols)


def test_bad_declaration_is_an_error(tmp_path):
    import checklib
    pcb = _board(tmp_path, [("J1", "CONN", 0, ["LINE", "GND"])])
    with pytest.raises(checklib.CheckError, match="kind"):
        check_surge.check(pcb, {"surge": {"ports": {"J1": {"kind": "usb"}}}})
    with pytest.raises(checklib.CheckError, match="no such part"):
        check_surge.check(pcb, {"surge": {"ports": {"J9": PORT}}})
    with pytest.raises(checklib.CheckError, match="environment"):
        check_surge.check(pcb, {"surge": {"environment": "moon",
                                          "ports": {"J1": PORT}}})


# ----------------------------------------------------------- the pinned board

VBUS_100V = {"surge": {"ports": {"J1": {"kind": "dc_power", "nets": ["VBUS"],
                                        "level_kv": 0.1}}}}


def _swap_clamp(tmp_path) -> Path:
    """The pinned board with D1 (VBUS, SMBJ20A) re-valued a USB ESD array."""
    pcb = tmp_path / "lite-main.kicad_pcb"
    text = LITE.read_text(encoding="utf-8")
    swapped, n = re.subn(r'"SMBJ20A"', '"USBLC6-2SC6"', text)
    assert n >= 1
    pcb.write_text(swapped, encoding="utf-8")
    return pcb


def test_lite_main_smbj_holds_100v_and_the_planted_usb_array_fails(tmp_path):
    """The same declaration on the same copper: the SMBJ20A carries the
    current, a USB ESD array in its place does not."""
    viols, facts = check_surge.check(LITE, VBUS_100V, LITE_PARTS)
    assert "surge_clamp_overstress" not in _kinds(viols)
    assert facts["lines"][0]["clamps"] == ["D1"]
    viols, facts = check_surge.check(_swap_clamp(tmp_path), VBUS_100V)
    bad = [v for v in viols if v["kind"] == "surge_clamp_overstress"]
    assert bad and "D1" in bad[0]["refs"]
    assert bad[0]["i_a"] > bad[0]["ipp_a"]


def test_lite_main_ch224a_pins_are_rated_by_pad_number():
    _viols, facts = check_surge.check(LITE, VBUS_100V, LITE_PARTS)
    pins = facts["lines"][0]["modes"][0]["pins"]
    assert {(p["ref"], p["pin"], p["abs_max_v"]) for p in pins} >= {
        ("U1", "1", 32.0), ("U1", "8", 32.0)}


def test_cli_writes_a_report(tmp_path):
    cons = tmp_path / "c.json"
    cons.write_text(json.dumps(VBUS_100V), encoding="utf-8")
    out = tmp_path / "r.json"
    rc = check_surge.main(["--pcb", str(LITE), "--constraints", str(cons),
                           "--parts", str(LITE_PARTS), "--out", str(out)])
    assert rc == 1                   # Vc 38 V over the CH224A's 32 V
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["script"] == "check_surge"
    assert {v["kind"] for v in got["violations"]} == {"surge_vc_over_abs_max"}
