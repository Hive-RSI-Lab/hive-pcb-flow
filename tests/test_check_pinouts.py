"""check_pinouts (#1027): off-board connectors against their standard pinout,
on netlists cut to the four real errata it was seeded from."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_pinouts as cp  # noqa: E402
import errata_corpus as ec  # noqa: E402


def board(nets, comps=()):
    """nets: {name: [(ref, pin, pinfunction)]} -> parse_netlist's shape."""
    refs = {r for ms in nets.values() for r, _, _ in ms} | set(comps)
    return {"components": {r: {"value": "", "footprint": "", "libsource": ""}
                           for r in refs},
            "nets": {n: [{"ref": r, "pin": p, "pinfunction": f, "pintype": ""}
                         for r, p, f in ms] for n, ms in nets.items()}}


def kinds(viols):
    return sorted((v["kind"], tuple(v["refs"])) for v in viols)


def jtag(tck="TCK", tdo="TDO", tdi="TDI", gnd10="GND", series=False):
    """sizif's J11 with the FPGA functions each header net reaches."""
    tck_net = [("J11", "1", ""), ("U6", "36", tck)]
    nets = {"/TCK": tck_net,
            "GND": [("J11", "2", ""), ("U6", "1", "GND")],
            "/TDO": [("J11", "3", ""), ("U6", "34", tdo)],
            "+3V3": [("J11", "4", ""), ("U6", "2", "VCCIO")],
            "/TMS": [("J11", "5", ""), ("U6", "33", "TMS")],
            "/TDI": [("J11", "9", ""), ("U6", "35", tdi)]}
    nets.setdefault(gnd10, []).append(("J11", "10", ""))
    if series:
        nets["/TCK"] = [("J11", "1", ""), ("R5", "1", "")]
        nets["/TCK_FPGA"] = [("R5", "2", ""), ("U6", "36", tck)]
    return board(nets)


def test_table_rows_cite_a_source():
    t = cp.load_table()
    assert {"altera-jtag-10", "audio-trs", "audio-trrs-ctia",
            "bedlift-lift-actuator"} <= set(t)
    for name, row in t.items():
        assert row["kind"] in ("jtag", "audio_jack", "stated"), name
        assert row["source"] and row["url"].startswith("https://"), name


def test_jtag_crossed_at_the_ic_fires_though_net_names_agree():
    v, judged = cp.check(jtag(tck="TDO", tdo="TDI", tdi="TCK"), {})
    assert judged == [{"ref": "J11", "standard": "altera-jtag-10"}]
    assert [x["pin"] for x in v] == ["1", "3", "9"]
    assert {x["kind"] for x in v} == {"pinout_mismatch"}


def test_jtag_right_is_clean_and_through_a_series_resistor():
    assert cp.check(jtag(), {})[0] == []
    assert cp.check(jtag(series=True), {})[0] == []
    v = cp.check(jtag(tck="TDO", tdo="TCK", series=True), {})[0]
    assert {x["pin"] for x in v} == {"1", "3"}


def test_jtag_header_off_the_altera_layout_is_not_judged():
    """Pin 10 off ground: some other 10-pin JTAG layout, not this row's."""
    v, judged = cp.check(jtag(tck="TDO", tdo="TCK", gnd10="/SPARE"), {})
    assert v == [] and judged == []


def jack(**pins):
    return board({net: [("J4", p, "")] for p, net in pins.items()})


def test_jack_common_on_the_tip_fires():
    """echoplayer r1-rev1's J4."""
    v, _ = cp.check(jack(T="/audio/hp_com", R1="/audio/hp_left",
                         R2="/audio/hp_right", S="/audio/hp_mic"), {})
    assert kinds(v) == [("pinout_mismatch", ("J4",))]
    assert "tip T" in v[0]["msg"]


def test_jack_ctia_omtp_and_trs_are_clean():
    for pins, fit in [
            (dict(T="/hp_left", R1="/hp_right", R2="GND", S="/mic"),
             "audio-trrs-ctia"),
            (dict(T="/hp_left", R1="/hp_right", R2="/mic", S="GND"),
             "audio-trrs-omtp"),
            (dict(T="/HP_L", R="/HP_R", S="GND"), "audio-trs")]:
        v, judged = cp.check(jack(**pins), {})
        assert v == [], pins
        assert fit in judged[0]["fits"]


def test_jack_with_unnamed_nets_is_not_judged():
    v, _ = cp.check(jack(T="Net-(J4-T)", R="Net-(J4-R)", S="GND"), {})
    assert v == []


def actuator(p2, p3, vhall_driver=True):
    """bed-lift's J8 (v0.2: p2=/VHALL, p3=GND; v1: p2=GND, p3=+5V)."""
    nets = {"/HALL2": [("J8", "1", "")], "/OUT2": [("J8", "4", "")],
            "/OUT1": [("J8", "5", "")], "/HALL1": [("J8", "6", "")],
            "+3.3V": [("U4", "5", "VCC")]}
    nets.setdefault(p2, []).append(("J8", "2", ""))
    nets.setdefault(p3, []).append(("J8", "3", ""))
    nets.setdefault("GND", []).append(("U4", "3", "GND"))
    if vhall_driver:
        nets.setdefault("/VHALL", []).append(("U4", "4", "Y"))
    return board(nets)


STATED = {"pinouts": [{"ref": "J8", "standard": "bedlift-lift-actuator"}]}


def test_stated_swap_and_low_supply_fire():
    v, judged = cp.check(actuator("/VHALL", "GND"), STATED)
    assert judged[0]["stated"]
    k = [(x["kind"], x.get("pin")) for x in v]
    assert ("pinout_mismatch", "2") in k and ("pinout_mismatch", "3") in k
    sv = [x for x in v if x["kind"] == "pinout_supply_volts"]
    assert sv and sv[0]["net"] == "/VHALL" and sv[0]["volts"] == 3.3
    assert "U4" in sv[0]["msg"]


def test_stated_right_wiring_is_clean():
    assert cp.check(actuator("GND", "+5V", vhall_driver=False), STATED)[0] == []


def test_stated_needs_the_constraint_and_a_known_ref():
    assert cp.check(actuator("/VHALL", "GND"), {}) == ([], [])
    v, _ = cp.check(actuator("GND", "+5V"),
                    {"pinouts": [{"ref": "J99", "standard": "bedlift-lift-actuator"},
                                 {"ref": "J8", "standard": "no-such"}]})
    assert [(x["kind"], x["severity"]) for x in v] == [
        ("pinout_unresolved", "warning")] * 2


def test_cli_reads_a_netlist(tmp_path):
    net = ROOT / "tests" / "golden" / "mutants" / "pinout-jtag-swap" / "usbbuck4.net"
    if not net.is_file():
        pytest.skip("mutant not generated")
    out = tmp_path / "r.json"
    rc = cp.main(["--pcb", str(net.with_suffix(".kicad_pcb")),
                  "--netlist", str(net), "--out", str(out)])
    d = json.loads(out.read_text())
    assert rc != 0 and {x["kind"] for x in d["violations"]} == {"pinout_mismatch"}


def test_corpus_writes_a_catchs_constraints_per_revision(tmp_path, monkeypatch):
    seen = []

    def fake(cache, b, rev, script, constraints=None):
        seen.append((rev, constraints))
        return ([{"kind": "pinout_mismatch", "refs": ["J8"]}]
                if rev == "rev_a" else [])
    monkeypatch.setattr(ec, "audit", fake)
    c = {"script": "check_pinouts", "kind": "pinout_mismatch", "refs": ["J8"],
         "constraints": {"rev_a": {"pinouts": [{"ref": "J8"}]},
                         "rev_b": {"pinouts": [{"ref": "J10"}]}}}
    res = ec.run(tmp_path, {"id": "x", "defects": [{"id": "x-1", "catch": c}]})
    assert res["x-1"] == {"ran": True, "rev_a": True, "rev_b": False}
    assert seen == [("rev_a", {"pinouts": [{"ref": "J8"}]}),
                    ("rev_b", {"pinouts": [{"ref": "J10"}]})]


def test_constraints_lint_knows_pinouts(tmp_path):
    import constraints_lint as cl
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps(STATED))
    bad.write_text(json.dumps({"pinouts": [{"ref": 3}]}))
    assert cl.lint_file(ok)[0] == []
    assert [v["kind"] for v in cl.lint_file(bad)[0]] == ["bad_type"]
