"""check_ldo_esr (#356): an ESR-compensated LDO's output capacitor against the
window and minimum its datasheet states, on synthetic boards and on the
planted mutant.

The figures are the table's (reference/ldo_output_cap.yaml): LM1117 SNOS412Q
p. 15 (10 uF tantalum, ESR 0.3 to 22 ohm), AMS1117 p. 4 (22 uF tantalum, no
ESR bound), TI SNVA167A p. 5 (a ceramic over 1 uF is a few milliohms)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import geom  # noqa: E402
import check_ldo_esr as ldo  # noqa: E402

ROWS, NOTE = ldo.load_table()


def _board(tmp_path_factory, name: str, body: str) -> geom.BoardGeom:
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 80 10) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path_factory.mktemp(name) / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return geom.load_board(p)


def _part(ref, x, pads, value="", lib=None, fn=True):
    lib = lib or f"t:{ref[0]}"
    val = f'    (property "Value" "{value}" (at 0 0 0))\n' if value else ""
    body = "".join(
        f'    (pad "{num}" smd rect (at {i} 0) (size 0.5 1) (layers "F.Cu") '
        f'(net "{net}")' + (f' (pinfunction "{pf}")' if pf and fn else "") + ")\n"
        for i, (num, net, pf) in enumerate(pads))
    return (f'  (footprint "{lib}" (at {x} 8)\n    (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0))\n{val}{body}  )\n')


def _ldo(part="LM1117-3.3", fn=True):
    """U1 in SOT-223: pin 1 GND/ADJ, 2 and tab 4 VOUT, 3 VIN."""
    return _part("U1", 10, [("1", "GND", "GND"), ("2", "+3V3", "VOUT"),
                            ("3", "+5V", "VIN"), ("4", "+3V3", "VOUT")],
                 part, lib="Package_TO_SOT_SMD:SOT-223-3_TabPin2", fn=fn)


def _cap(ref, x, value, lib="Capacitor_SMD:C_0805_2012Metric", a="+3V3"):
    return _part(ref, x, [("1", a, ""), ("2", "GND", "")], value, lib=lib)


def _kinds(vs):
    return [v["kind"] for v in vs]


def test_lm1117_ceramic_only_is_an_error(tmp_path_factory):
    bg = _board(tmp_path_factory, "cer", _ldo() + _cap("C1", 20, "10uF")
                + _cap("C2", 30, "100nF"))
    viols, recs, nj = ldo.judge(bg, {}, ROWS, NOTE)
    hit, = viols
    assert hit["kind"] == "ldo_esr_low" and hit["severity"] == "error"
    assert hit["net"] == "+3V3" and set(hit["refs"]) == {"U1", "C1", "C2"}
    assert hit["ceramic_uf"] == 10.1
    assert "0.3-22 ohm" in hit["msg"] and "SNVA167A" in hit["msg"]
    assert recs[0]["out_from"] == "pin function" and nj == []


def test_quiet_twin_tantalum_beside_the_ceramics(tmp_path_factory):
    bg = _board(tmp_path_factory, "tant", _ldo() + _cap("C1", 20, "10uF")
                + _cap("C2", 30, "10uF",
                       lib="Capacitor_Tantalum_SMD:CP_EIA-3528-21_Kemet-B"))
    viols, recs, nj = ldo.judge(bg, {}, ROWS, NOTE)
    assert viols == [] and nj == []
    assert recs[0]["esr_ok_by"] == ["C2"]
    c2 = next(c for c in recs[0]["caps"] if c["ref"] == "C2")
    assert c2["dielectric"] == "tantalum" and "footprint" in c2["read_from"]


def test_small_ceramic_only_is_a_warning(tmp_path_factory):
    # Under 1 uF, SNVA167A's few-milliohm statement does not back the
    # guidance window: a warning.
    bg = _board(tmp_path_factory, "small", _ldo() + _cap("C1", 20, "470nF"))
    hit, = ldo.judge(bg, {}, ROWS, NOTE)[0]
    assert hit["kind"] == "ldo_esr_low" and hit["severity"] == "warning"


def test_ams1117_ceramic_only_is_a_warning(tmp_path_factory):
    bg = _board(tmp_path_factory, "ams", _ldo("AMS1117-3.3")
                + _cap("C1", 20, "22uF"))
    hit, = ldo.judge(bg, {}, ROWS, NOTE)[0]
    assert hit["kind"] == "ldo_esr_low" and hit["severity"] == "warning"
    assert "no ESR bound" in hit["msg"] and "series resistor" not in hit["msg"]


def test_output_from_sot223_pads_without_pin_functions(tmp_path_factory):
    bg = _board(tmp_path_factory, "nofn", _ldo(fn=False) + _cap("C1", 20, "10uF"))
    viols, recs, _ = ldo.judge(bg, {}, ROWS, NOTE)
    assert _kinds(viols) == ["ldo_esr_low"]
    assert recs[0]["out_from"].startswith("pads 2/4 of SOT-223")


def test_missing_output_cap(tmp_path_factory):
    bg = _board(tmp_path_factory, "none", _ldo())
    hit, = ldo.judge(bg, {}, ROWS, NOTE)[0]
    assert hit["kind"] == "ldo_cout_missing" and hit["severity"] == "error"


def test_series_resistor_gives_esr(tmp_path_factory):
    # C1 reached through R1 (0.5 ohm): in the window, no finding. 0.1 ohm:
    # outside it, a warning.
    def board(name, ohms):
        return _board(tmp_path_factory, name, _ldo()
                      + _part("R1", 20, [("1", "+3V3", ""), ("2", "/ESR", "")], ohms)
                      + _cap("C1", 30, "10uF", a="/ESR"))
    viols, recs, _ = ldo.judge(board("r05", "0.5"), {}, ROWS, NOTE)
    assert viols == [] and recs[0]["caps"][0]["esr_from"] == "series R1"
    hit, = ldo.judge(board("r01", "0.1"), {}, ROWS, NOTE)[0]
    assert hit["kind"] == "ldo_esr_window" and hit["esr_ohm"] == 0.1


def test_declared_ldo_caps_override_the_footprint(tmp_path_factory):
    bg = _board(tmp_path_factory, "decl", _ldo() + _cap("C1", 20, "10uF"))
    assert ldo.judge(bg, {"ldo_caps": {"C1": "tantalum"}}, ROWS, NOTE)[0] == []
    hit, = ldo.judge(bg, {"ldo_caps": {"C1": {"dielectric": "ceramic",
                                               "esr_ohm": 40}}}, ROWS, NOTE)[0]
    assert hit["kind"] == "ldo_esr_window" and "constraints ldo_caps" in hit["msg"]


def test_tantalum_under_the_minimum(tmp_path_factory):
    bg = _board(tmp_path_factory, "low", _ldo() + _cap("C1", 20, "4.7uF tantalum"))
    hit, = ldo.judge(bg, {}, ROWS, NOTE)[0]
    assert hit["kind"] == "ldo_cout_low" and hit["severity"] == "warning"
    assert hit["cout_uf"] == 4.7 and hit["cout_min_uf"] == 10


def test_electrolytic_is_not_judged(tmp_path_factory):
    bg = _board(tmp_path_factory, "elec", _ldo() + _cap(
        "C1", 20, "100uF", lib="Capacitor_SMD:CP_Elec_6.3x5.4"))
    viols, recs, nj = ldo.judge(bg, {}, ROWS, NOTE)
    assert viols == [] and recs[0]["status"] == "not_judged"
    assert "ldo_caps" in nj[0]


def test_dnp_cap_and_unlisted_regulator_are_skipped(tmp_path_factory):
    dnp = _cap("C1", 20, "10uF tantalum").replace(
        '    (layer "F.Cu")\n', '    (layer "F.Cu")\n    (attr smd dnp)\n', 1)
    bg = _board(tmp_path_factory, "dnp", _ldo() + dnp)
    assert _kinds(ldo.judge(bg, {}, ROWS, NOTE)[0]) == ["ldo_cout_missing"]
    bg = _board(tmp_path_factory, "other", _ldo("TLV75533") + _cap("C1", 20, "1uF"))
    assert ldo.judge(bg, {}, ROWS, NOTE)[1] == []


def test_unverified_row_is_not_judged(tmp_path_factory):
    rows = [dict(r, verified=False) for r in ROWS]
    bg = _board(tmp_path_factory, "unver", _ldo())
    viols, recs, nj = ldo.judge(bg, {}, rows, NOTE)
    assert viols == [] and "[unverified:" in nj[0]


def test_cli_exit_codes(tmp_path_factory, tmp_path):
    bg = _board(tmp_path_factory, "cli", _ldo() + _cap("C1", 20, "10uF"))
    out = tmp_path / "r.json"
    assert ldo.main(["--pcb", str(bg.path), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["regulators"][0]["ref"] == "U1"
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"ldo_caps": {"C1": "tantalum"}}))
    assert ldo.main(["--pcb", str(bg.path), "--constraints", str(c),
                     "--out", str(out)]) == 0
    assert ldo.main(["--pcb", str(tmp_path / "none.kicad_pcb")]) == 2


def test_golden_mutant_and_quiet_twin():
    """The planted mutant (U2 an LM1117 over blinky2's all-ceramic +3V3) is
    an error; with C5 declared a tantalum it is quiet."""
    d = ROOT / "tests" / "golden"
    cons = json.loads((d / "blinky2" / "constraints.json").read_text())
    bg = geom.load_board(d / "mutants" / "ldo-esr-ceramic-only" / "blinky2.kicad_pcb")
    hit, = ldo.judge(bg, cons, ROWS, NOTE)[0]
    assert (hit["kind"], hit["severity"], hit["net"]) == ("ldo_esr_low", "error", "+3V3")
    assert ldo.judge(bg, dict(cons, ldo_caps={"C5": "tantalum"}), ROWS, NOTE)[0] == []
    # The golden itself (AMS1117) gives the warning triage.yaml records as real.
    gold = geom.load_board(d / "blinky2" / "blinky2.kicad_pcb")
    hit, = ldo.judge(gold, cons, ROWS, NOTE)[0]
    assert (hit["kind"], hit["severity"]) == ("ldo_esr_low", "warning")
